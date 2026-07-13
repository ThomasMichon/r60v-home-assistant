"""LAN-side native-protocol front-end for the R60V (governor-fronted).

Presents the machine's *own* wire protocol on a LAN address (``<lan-ip>:1774``)
so any LAN client -- notably a Home Assistant integration -- can talk to the
espresso machine directly, **without** re-exposing its fragile single-socket
listener.

The whole discipline lives in one rule: **this front-end never opens its own
upstream socket.** Every client request is submitted through the shared
:class:`~r60v_broker.governor.DeviceGovernor`, which owns the one true
connection and serializes all callers through a single throttled worker. ``N``
LAN clients therefore collapse into one calm, ordered upstream conversation --
structurally preventing the connection-churn wedge documented in the protocol
reference (``docs/protocol.md``), recovery from which needs a physical
power-cycle of the machine.

This is the reason a raw TCP passthrough or NAT/DNAT is the *wrong* answer: those
let each LAN client open its own socket to the machine (and match responses by
envelope, not by client), which re-creates the wedge the moment a second device
connects. **The bridge is the governor, not a passthrough.**

To a LAN client the front-end is indistinguishable from the real machine: it
greets with ``*HELLO*`` and answers read/write frames with the same envelopes.
It is also *more* reliable than the bare machine -- the governor absorbs the
first-request swallow and paces requests -- so clients rarely need their own
retries.
"""
from __future__ import annotations

import asyncio
import logging

from . import protocol as p
from .client import R60VConnectionError
from .governor import DeviceGovernor
from .protocol import ProtocolError

LOGGER = logging.getLogger("r60v.frontend")


async def _read_request(reader: asyncio.StreamReader) -> str:
    """Read one self-delimiting request frame from ``reader``.

    Frames are self-delimiting: after the 9-char envelope (``command`` +
    ``address`` + ``length``) a *write* carries ``length`` data bytes (two hex
    chars each) plus a 2-char checksum, while a *read* carries only the 2-char
    checksum (its ``length`` field is the read count, not a present payload).

    :raises asyncio.IncompleteReadError: when the peer closes (EOF).
    """
    envelope = await reader.readexactly(9)
    command = chr(envelope[0])
    if command == p.WRITE:
        try:
            length = int(envelope[5:9], 16)
        except ValueError:
            # A malformed length; grab just the checksum so the frame parses
            # (and is then rejected by the caller) rather than hanging.
            count = 2
        else:
            count = length * 2 + 2
    else:
        count = 2
    rest = await reader.readexactly(count)
    return envelope.decode("ascii", "replace") + rest.decode("ascii", "replace")


class R60VFrontend:
    """A TCP server that re-presents the R60V protocol, backed by the governor.

    :param governor: the shared device governor -- the *sole* owner of the
        upstream link. The front-end submits every client request through it and
        never touches the device socket directly.
    :param host: bind address (default ``0.0.0.0`` -- reachable at the bridge's
        LAN IP, e.g. ``192.168.1.50:1774``).
    :param port: bind port (default ``1774`` -- the machine's own port, so a
        client can be pointed at the bridge with no protocol change).
    :param greeting: the greeting sent on connect (defaults to ``*HELLO*``).
    """

    def __init__(
        self,
        governor: DeviceGovernor,
        host: str = "0.0.0.0",
        port: int = p.DEFAULT_PORT,
        *,
        greeting: str = p.HELLO,
    ) -> None:
        self.governor = governor
        self.host = host
        self.port = port
        self.greeting = greeting
        self._server: asyncio.AbstractServer | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Bind and start accepting LAN clients."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        sockets = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        LOGGER.info("R60V LAN front-end listening on %s", sockets)

    async def serve_forever(self) -> None:
        """Start and serve until cancelled."""
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop accepting clients and close the listener."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def bound_port(self) -> int:
        """The actual bound port (useful when constructed with port ``0``)."""
        if not self._server or not self._server.sockets:
            raise RuntimeError("front-end not started")
        return self._server.sockets[0].getsockname()[1]

    # -- per-client handling ---------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        LOGGER.info("LAN client connected: %s", peer)
        writer.write(self.greeting.encode("ascii"))
        try:
            await writer.drain()
            while True:
                raw = await _read_request(reader)
                response = await self._dispatch(raw)
                if response is None:
                    # Malformed request or upstream failure: drop the client so
                    # it reconnects (and re-greets) rather than desyncing.
                    break
                writer.write(response.encode("ascii"))
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # client closed the connection
        except (ConnectionError, OSError) as exc:
            LOGGER.info("LAN client %s dropped: %s", peer, exc)
        finally:
            LOGGER.info("LAN client disconnected: %s", peer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _dispatch(self, raw: str) -> str | None:
        """Submit one client frame through the governor; return the reply.

        Returns ``None`` to signal the connection should be dropped -- on a
        malformed/failed-checksum frame, or when the upstream link is down (the
        governor raised). Every request goes through the governor, so concurrent
        LAN clients are serialized onto the single upstream socket.
        """
        try:
            frame = p.parse_frame(raw)
        except ProtocolError as exc:
            LOGGER.warning("rejecting malformed frame %r: %s", raw, exc)
            return None

        try:
            if frame.command == p.READ:
                data = await self.governor.read(frame.address, frame.length)
                return p.build_frame(p.READ, frame.address, frame.length, data)
            ack = await self.governor.write(frame.address, frame.data)
            # Re-present a clean ack echoing the request envelope (the upstream
            # ack already validated inside the governor/client).
            _ = ack
            return p.build_ack(p.WRITE, frame.address, frame.length)
        except R60VConnectionError as exc:
            LOGGER.warning(
                "upstream link failed serving %s 0x%04X: %s",
                frame.command, frame.address, exc,
            )
            return None
