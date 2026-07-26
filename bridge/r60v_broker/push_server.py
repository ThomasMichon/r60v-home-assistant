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

import websockets
from websockets.asyncio.server import serve

LOGGER = logging.getLogger("r60v.push")

#: Push payload schema version (bump on any breaking field change).
SCHEMA = 1

#: Max seconds to spend sending to one client before dropping it, so a slow or
#: stalled subscriber cannot hold up the broadcast (and thus the poll cycle).
_SEND_TIMEOUT = 1.0


class WsPushServer:
    """Broadcasts decoded R60V state snapshots to WebSocket subscribers."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8788) -> None:
        self.host = host
        self.port = port
        self._clients: set = set()
        self._server = None
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
        """Store and send a decoded state snapshot to every connected client."""
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
            # We don't expect inbound frames; just hold the connection open.
            async for _ in ws:
                pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            LOGGER.info(
                "push client disconnected: %s (total %d)", peer, len(self._clients)
            )

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
