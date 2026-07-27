"""WebSocket push server: streams live R60V state to LAN subscribers.

Part of the bridge's local-push support. The governor's
poll loop feeds the :class:`~r60v_broker.publisher.StatePublisher` cache; the
broker decodes a snapshot and hands it to :meth:`WsPushServer.broadcast`, which
streams it to every connected client -- once on connect, then on every poll
cycle. A Home Assistant ``local_push`` integration subscribes and updates its
entities on receipt, so it never polls the machine.

The server **never touches the device**: it only forwards decoded snapshots. All
device I/O stays behind the governor, the single disciplined owner of the machine
link -- so a subscriber can never wedge the machine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets
from websockets.asyncio.server import serve

LOGGER = logging.getLogger("r60v.push")

#: Push payload schema version (bump on any breaking field change).
SCHEMA = 1

#: Max seconds to spend sending to one client before dropping it, so a slow or
#: stalled subscriber cannot hold up the broadcast (and thus the poll cycle).
_SEND_TIMEOUT = 1.0

#: An inbound write-intent handler: receives one decoded ``command`` frame.
CommandHandler = Callable[[dict], Awaitable[None]]


class WsPushServer:
    """Broadcasts decoded R60V state snapshots to WebSocket subscribers.

    The channel is bidirectional: subscribers receive ``state`` snapshots and may
    send ``command`` frames (write-intents) back over the same socket, which are
    handed to an optional async ``on_command`` handler. All device I/O still
    happens behind the governor -- this server only forwards frames.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8788,
        *,
        on_command: CommandHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_command = on_command
        self._clients: set = set()
        self._server = None
        # Serialize broadcasts so the concurrent poll + command broadcasters
        # never interleave sends, and each send carries the freshest snapshot.
        self._broadcast_lock = asyncio.Lock()
        # The most recent wrapped snapshot, sent to a client the moment it
        # connects (so a new subscriber sees state immediately, not on next poll).
        self._latest: dict = self._wrap({"available": False})

    @staticmethod
    def _wrap(state: dict) -> dict:
        """Wrap a state dict with the envelope fields (type/schema/ts)."""
        return {"type": "state", "schema": SCHEMA, "ts": round(time.time(), 3), **state}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- broadcast --------------------------------------------------------

    async def broadcast(self, state: dict) -> None:
        """Store and send a decoded state snapshot to every connected client.

        Serialized under a lock and always sends the *freshest* stored snapshot,
        so the concurrent poll and command broadcasters cannot deliver an older
        snapshot after a newer one.
        """
        async with self._broadcast_lock:
            self._latest = self._wrap(state)
            if not self._clients:
                return
            msg = json.dumps(self._latest)
            dead: list = []
            await asyncio.gather(
                *(self._safe_send(client, msg, dead) for client in list(self._clients))
            )
            for client in dead:
                self._clients.discard(client)

    async def _safe_send(self, client, msg: str, dead: list) -> None:
        try:
            await asyncio.wait_for(client.send(msg), timeout=_SEND_TIMEOUT)
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError):
            dead.append(client)

    # -- lifecycle --------------------------------------------------------

    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        peer = getattr(ws, "remote_address", None)
        LOGGER.info("push client connected: %s (total %d)", peer, len(self._clients))
        try:
            await ws.send(json.dumps(self._latest))
            # Inbound frames are write-intents (command frames); anything else is
            # ignored. A malformed frame must never break the read loop.
            async for raw in ws:
                await self._handle_inbound(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            LOGGER.info(
                "push client disconnected: %s (total %d)", peer, len(self._clients)
            )

    async def _handle_inbound(self, raw) -> None:
        """Decode one inbound frame and, if it is a command, dispatch it."""
        if self.on_command is None:
            return
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            LOGGER.debug("ignoring non-JSON inbound push frame")
            return
        if not isinstance(frame, dict) or frame.get("type") != "command":
            return
        try:
            await self.on_command(frame)
        except Exception as exc:  # noqa: BLE001 -- a bad command must not drop the client
            LOGGER.warning("command handler failed for %r: %s", frame, exc)

    async def start(self) -> None:
        self._server = await serve(self._handler, self.host, self.port)
        LOGGER.info("R60V push server listening on ws://%s:%d", self.host, self.port)

    @property
    def bound_port(self) -> int:
        """The actual bound port (useful when constructed with port 0)."""
        if not self._server or not self._server.sockets:
            raise RuntimeError("push server not started")
        return self._server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()  # run until cancelled
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
