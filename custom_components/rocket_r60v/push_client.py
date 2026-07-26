"""WebSocket push client: subscribe to the bridge's live state stream.

When the bridge runs the WebSocket push server, the integration subscribes to it
instead of polling. Each frame carries the **raw** register snapshot (settings
block + live registers), which we reconstruct into a :class:`StateSnapshot` and
hand to the coordinator via ``async_set_updated_data`` -- so the entities update
in near-real-time and the integration never polls the machine itself.

All the fragile-link discipline (one socket, pacing, wedge recovery) lives in the
bridge governor; this client is deliberately simple: connect, receive, decode,
update, reconnect. If the stream drops it marks the coordinator's data stale
(entities go unavailable) and reconnects with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import R60VConnectionError
from .coordinator import R60VCoordinator
from .entities import StateSnapshot

LOGGER = logging.getLogger(__name__)

#: Reconnect backoff bounds (seconds).
_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
#: WebSocket heartbeat -- surfaces a dead stream promptly.
_HEARTBEAT = 30.0


class R60VPushClient:
    """Streams bridge state into the coordinator; reconnects on drop."""

    def __init__(
        self, hass: HomeAssistant, coordinator: R60VCoordinator, url: str
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.url = url
        self._task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        """Launch the background subscribe loop."""
        self._task = self.hass.async_create_background_task(
            self._run(), name="r60v-push-client"
        )

    async def stop(self) -> None:
        """Stop the subscribe loop."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -- internals --------------------------------------------------------

    async def _run(self) -> None:
        session = async_get_clientsession(self.hass)
        backoff = _BACKOFF_START
        while not self._closing:
            try:
                async with session.ws_connect(
                    self.url, heartbeat=_HEARTBEAT
                ) as ws:
                    LOGGER.debug("push stream connected: %s", self.url)
                    backoff = _BACKOFF_START
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                LOGGER.debug("push stream error (%s); reconnecting", exc)
            if self._closing:
                break
            # The stream dropped: entities go unavailable until we reconnect.
            self.coordinator.async_set_update_error(
                R60VConnectionError("push stream disconnected")
            )
            await asyncio.sleep(min(backoff, _BACKOFF_MAX))
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _handle(self, raw: str) -> None:
        """Decode one push frame and update the coordinator."""
        try:
            data = json.loads(raw)
        except ValueError:
            LOGGER.debug("ignoring non-JSON push frame")
            return
        if not isinstance(data, dict) or data.get("type") != "state":
            return
        if not data.get("available", True):
            # The bridge says the machine is unreachable -- surface it as such
            # instead of freezing on stale values.
            self.coordinator.async_set_update_error(
                R60VConnectionError("bridge reports machine unavailable")
            )
            return
        settings = data.get("settings")
        if not isinstance(settings, list):
            return
        live_raw = data.get("live") or {}
        live: dict[int, list[int]] = {}
        for key, value in live_raw.items():
            try:
                live[int(key)] = list(value)
            except (TypeError, ValueError):
                continue
        snapshot = StateSnapshot(settings=list(settings), live=live)
        self.coordinator.async_set_updated_data(snapshot)
