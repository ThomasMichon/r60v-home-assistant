"""Persistent, strictly half-duplex async client for the Rocket R60V.

The R60V exposes a single fragile TCP listener that misbehaves when flooded
with concurrent or pipelined requests. This client holds **one** persistent
connection and mirrors the machine's discipline:

- exactly **one request in flight at a time** (an :class:`asyncio.Lock`);
- read the ``*HELLO*`` greeting once per connection;
- tolerate the machine swallowing the first request after the greeting by
  issuing a throwaway warm-up read, and by retrying a swallowed request in
  place on the same socket;
- read **fixed-length** responses computed from the request envelope (a read
  reply is ``9 + length*2 + 2`` chars, a write ack is ``13`` chars);
- **reconnect once** on a genuine socket fault, then surface the error.

Unlike a long-running daemon, this client never retries a *connect* forever:
Home Assistant must fail fast so the config entry can report
``ConfigEntryNotReady`` instead of hanging the event loop. All I/O is
non-blocking asyncio stream I/O -- safe to await directly from the loop.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import protocol as p
from .protocol import Frame, ProtocolError

LOGGER = logging.getLogger(__name__)


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
        warmup: bool = True,
        request_gap: float = 0.15,
        max_retries: int = 3,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.warmup = warmup
        # Minimum spacing between requests: the single-socket listener drops
        # requests fired back-to-back, so a small gap keeps it healthy.
        self.request_gap = request_gap
        # Re-issue a *swallowed* request this many times on the SAME socket
        # before giving up (the machine occasionally drops a request).
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
        LOGGER.debug("connecting to %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        await self._read_greeting()
        if self.warmup:
            await self._warm_up()
        LOGGER.debug("connected to %s:%d", self.host, self.port)

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
        """Absorb the first-request-after-greeting swallow seen on real units."""
        frame = p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)
        try:
            await self._exchange(frame)
        except (R60VConnectionError, ProtocolError):
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

    # -- request/response ------------------------------------------------

    async def request(self, frame: str) -> Frame:
        """Send one framed request and return the parsed reply.

        Serialized behind the half-duplex lock. Connects if needed and, on a
        genuine socket fault, reconnects **once** before giving up. A connect
        that never succeeds raises promptly rather than retrying forever, so the
        coordinator can surface ``ConfigEntryNotReady`` instead of hanging.
        """
        async with self._lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                if not self.connected:
                    try:
                        await self.connect()
                    except (
                        OSError,
                        asyncio.TimeoutError,
                        R60VConnectionError,
                        ProtocolError,
                    ) as exc:
                        last_exc = exc
                        LOGGER.debug("connect attempt %d failed: %s", attempt + 1, exc)
                        await self._drop()
                        continue
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
                    LOGGER.debug("exchange attempt %d failed: %s", attempt + 1, exc)
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
        **same socket** up to ``max_retries`` times. A short read or EOF means
        the socket is broken and is surfaced as :class:`R60VConnectionError` so
        the caller reconnects.
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
                raise R60VConnectionError(
                    f"short read/EOF for {frame!r}: {exc.partial!r}"
                ) from exc
            except (TimeoutError, asyncio.TimeoutError) as exc:
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
