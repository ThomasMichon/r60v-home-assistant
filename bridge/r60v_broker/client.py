"""Persistent, strictly half-duplex client for the Rocket R60V.

The R60V exposes a single fragile TCP listener (``192.168.1.1:1774``) that
misbehaves when flooded with concurrent or pipelined requests. The whole point
of the broker is to hold **one** persistent connection and mirror the machine's
own discipline exactly:

- exactly **one request in flight at a time** (an :class:`asyncio.Lock`);
- read the ``*HELLO*`` greeting once per connection;
- tolerate the machine **swallowing the first request** after the greeting
  (observed on real hardware) by issuing a throwaway warm-up read and by
  retrying an empty first exchange once;
- read **fixed-length** responses computed from the request envelope (the
  protocol is self-delimiting: a read reply is ``9 + length*2 + 2`` chars, a
  write ack is ``13`` chars) -- so no separator scanning is needed;
- **auto-reconnect** with backoff when the socket drops.

Real-hardware notes baked in here (see the effort journal, 2026-07-12):
the machine answers **single-register** live reads (``rB0000001``) but ignores
multi-register range reads at ``0xB000``; multi-byte settings values are
little-endian.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import protocol as p
from .protocol import Frame, ProtocolError

LOGGER = logging.getLogger("r60v.client")


class R60VConnectionError(Exception):
    """Raised when the client cannot complete an exchange with the machine."""


def _expected_response_len(request: str) -> int:
    """Compute the fixed byte length of the reply to ``request``.

    A read reply echoes the envelope plus ``length`` data bytes (two hex chars
    each) plus a 2-char checksum. A write is acknowledged with the envelope,
    the literal ``OK`` and a checksum (13 chars total).
    """
    command = request[0]
    length = int(request[5:9], 16)
    if command == p.READ:
        return 9 + length * 2 + 2
    return 9 + len(p.ACK) + 2


class R60VClient:
    """A persistent half-duplex connection to one R60V control listener."""

    def __init__(
        self,
        host: str = p.DEFAULT_HOST,
        port: int = p.DEFAULT_PORT,
        *,
        connect_timeout: float = 8.0,
        request_timeout: float = 4.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        warmup: bool = True,
        request_gap: float = 0.2,
        max_retries: int = 3,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.warmup = warmup
        # Minimum spacing between requests. The R60V's single-socket listener
        # drops requests fired back-to-back; a small gap (~the app's ~100ms
        # cadence) keeps the half-duplex conversation healthy.
        self.request_gap = request_gap
        # How many times to re-issue a *swallowed* request on the SAME socket
        # before giving up. The machine occasionally drops a request; the app
        # itself retries rather than reconnecting. Reconnecting on every empty
        # reply is actively harmful -- the churn wedges the fragile listener --
        # so we retry in place and only reconnect on a genuine socket fault.
        self.max_retries = max_retries

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_request_at: float = 0.0
        # Half-duplex guarantee: at most one request in flight at any time.
        self._lock = asyncio.Lock()

    # -- connection lifecycle --------------------------------------------

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the socket, consume the greeting, and warm up the listener."""
        await self._drop()
        LOGGER.info("connecting to %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        await self._read_greeting()
        if self.warmup:
            await self._warm_up()
        LOGGER.info("connected to %s:%d", self.host, self.port)

    async def _read_greeting(self) -> None:
        assert self._reader is not None
        try:
            data = await asyncio.wait_for(
                self._reader.readexactly(len(p.HELLO)), timeout=self.request_timeout
            )
        except (TimeoutError, asyncio.IncompleteReadError) as exc:
            raise R60VConnectionError(f"no greeting from {self.host}") from exc
        greeting = data.decode("ascii", "replace")
        if greeting != p.HELLO:
            LOGGER.warning("unexpected greeting: %r (wanted %r)", greeting, p.HELLO)

    async def _warm_up(self) -> None:
        """Absorb the first-request-after-greeting swallow seen on real units.

        The machine frequently drops the very first command issued after the
        greeting. We send a throwaway ReadAll and ignore any timeout so the
        first *real* request lands cleanly.
        """
        frame = p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)
        try:
            await self._exchange(frame)
        except (R60VConnectionError, ProtocolError):
            # Best-effort: a swallowed or garbled warm-up reply is fine; the
            # first real request self-heals (or reconnects) if the stream is
            # misaligned. Never let it escape connection setup.
            LOGGER.debug("warm-up read returned nothing/garbled (expected on some units)")

    async def close(self) -> None:
        await self._drop()

    async def _drop(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _ensure_connected(self) -> None:
        """Connect with exponential backoff until the socket is up."""
        if self.connected:
            return
        backoff = self.backoff_initial
        while True:
            try:
                await self.connect()
                return
            except (OSError, asyncio.TimeoutError, R60VConnectionError, ProtocolError) as exc:
                LOGGER.warning(
                    "connect failed (%s); retrying in %.1fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)

    # -- request/response ------------------------------------------------

    async def request(self, frame: str) -> Frame:
        """Send one framed request and return the parsed reply.

        Serialized behind the half-duplex lock. A *swallowed* request is
        retried in place on the same connection (see :meth:`_exchange`); only a
        genuine socket fault or a persistent no-reply drops and reconnects
        (once) -- reconnecting is deliberately rare because the churn wedges the
        machine's listener.
        """
        async with self._lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                await self._ensure_connected()
                try:
                    return await self._exchange(frame)
                except (
                    OSError,
                    asyncio.TimeoutError,
                    asyncio.IncompleteReadError,
                    R60VConnectionError,
                    ProtocolError,
                ) as exc:
                    last_exc = exc
                    LOGGER.warning(
                        "exchange failed (connection attempt %d): %s",
                        attempt + 1, exc,
                    )
                    await self._drop()
            raise R60VConnectionError(
                f"request {frame!r} failed: {last_exc}"
            ) from last_exc

    async def _pace(self) -> None:
        """Sleep just enough to honor the minimum inter-request gap."""
        if self.request_gap <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_gap:
            await asyncio.sleep(self.request_gap - elapsed)

    async def _exchange(self, frame: str) -> Frame:
        """Write one frame and read its fixed-length reply (no lock handling).

        Retries a *swallowed* request (clean timeout, no bytes received) on the
        **same socket** up to ``max_retries`` times, pacing between tries. A
        short read or EOF means the socket is actually broken and is surfaced as
        :class:`R60VConnectionError` so the caller reconnects.
        """
        assert self._reader is not None and self._writer is not None
        want = _expected_response_len(frame)
        for attempt in range(self.max_retries + 1):
            await self._pace()
            self._last_request_at = time.monotonic()
            self._writer.write(frame.encode("ascii"))
            await self._writer.drain()
            try:
                raw = await asyncio.wait_for(
                    self._reader.readexactly(want), timeout=self.request_timeout
                )
            except asyncio.IncompleteReadError as exc:
                # Bytes arrived then the peer closed: the socket is broken.
                raise R60VConnectionError(
                    f"short read/EOF for {frame!r}: {exc.partial!r}"
                ) from exc
            except (TimeoutError, asyncio.TimeoutError) as exc:
                # No bytes at all: the machine swallowed the request. Re-issue
                # it on the same connection rather than reconnecting.
                if attempt < self.max_retries:
                    LOGGER.debug(
                        "swallowed %r; same-socket retry %d/%d",
                        frame, attempt + 1, self.max_retries,
                    )
                    continue
                raise R60VConnectionError(
                    f"no reply for {frame!r} after {self.max_retries + 1} tries"
                ) from exc
            return p.parse_frame(raw.decode("ascii", "replace"))
        raise R60VConnectionError(f"no reply for {frame!r}")

    # -- typed helpers ---------------------------------------------------

    async def read(self, address: int, length: int) -> list[int]:
        """Read ``length`` data bytes from ``address``."""
        frame = await self.request(p.build_read(address, length))
        return frame.data

    async def write(self, address: int, data: list[int] | bytes | bytearray) -> Frame:
        """Write ``data`` to ``address`` and return the acknowledgement frame."""
        return await self.request(p.build_write(address, data))
