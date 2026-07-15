"""DataUpdateCoordinator that polls the R60V off the event loop."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import R60VClient, R60VConnectionError
from .const import DOMAIN
from .entities import LIVE_REGISTERS, StateSnapshot
from .protocol import SETTINGS_BASE, SETTINGS_LEN, ProtocolError

LOGGER = logging.getLogger(__name__)

#: How often to poll the machine. Local polling; the machine is slow and its
#: single-socket listener dislikes churn, so a relaxed interval is deliberate.
UPDATE_INTERVAL = timedelta(seconds=30)

#: How many consecutive failed polls to tolerate before marking the device
#: unavailable. The machine's single-socket listener intermittently desyncs the
#: stream (~5% of polls), so a single failed poll must NOT blank every entity.
#: We keep serving the last-known-good snapshot until the failures are sustained
#: (> ~2.5 min at the 30s interval), which indicates a genuine outage.
FAILURE_TOLERANCE = 5


class R60VCoordinator(DataUpdateCoordinator[StateSnapshot]):
    """Polls the settings block and live registers into a StateSnapshot."""

    def __init__(self, hass: HomeAssistant, client: R60VClient) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._consecutive_failures = 0

    async def _read_snapshot(self) -> StateSnapshot:
        """Read a fresh settings block + live registers. Runs off the loop."""
        settings = await self.client.read(SETTINGS_BASE, SETTINGS_LEN)
        live: dict[int, list[int]] = {}
        for address, length in LIVE_REGISTERS.items():
            live[address] = await self.client.read(address, length)
        return StateSnapshot(settings=settings, live=live)

    async def _async_update_data(self) -> StateSnapshot:
        """Poll the machine, tolerating transient desync by serving cached data.

        On success the failure counter resets. On a connection/protocol error we
        serve the last-known-good snapshot (entities stay available) until the
        failures become sustained (``FAILURE_TOLERANCE``) or we have no data yet
        (first refresh), in which case we raise ``UpdateFailed`` -- which yields
        ``ConfigEntryNotReady`` on the very first refresh.
        """
        try:
            snapshot = await self._read_snapshot()
        except (R60VConnectionError, ProtocolError) as exc:
            self._consecutive_failures += 1
            if self.data is not None and self._consecutive_failures <= FAILURE_TOLERANCE:
                LOGGER.debug(
                    "poll failed (%d/%d consecutive); serving cached values: %s",
                    self._consecutive_failures, FAILURE_TOLERANCE, exc,
                )
                return self.data
            raise UpdateFailed(str(exc)) from exc
        self._consecutive_failures = 0
        return snapshot
