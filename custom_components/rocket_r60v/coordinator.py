"""DataUpdateCoordinator that polls the R60V off the event loop."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import R60VClient, R60VConnectionError
from .const import DOMAIN
from .entities import LIVE_REGISTERS, StateSnapshot
from .protocol import SETTINGS_BASE, SETTINGS_LEN

LOGGER = logging.getLogger(__name__)

#: How often to poll the machine. Local polling; the machine is slow and its
#: single-socket listener dislikes churn, so a relaxed interval is deliberate.
UPDATE_INTERVAL = timedelta(seconds=30)


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

    async def _async_update_data(self) -> StateSnapshot:
        """Fetch a fresh snapshot. Runs off the loop (async socket I/O)."""
        try:
            settings = await self.client.read(SETTINGS_BASE, SETTINGS_LEN)
            live: dict[int, list[int]] = {}
            for address, length in LIVE_REGISTERS.items():
                live[address] = await self.client.read(address, length)
        except R60VConnectionError as exc:
            raise UpdateFailed(str(exc)) from exc
        return StateSnapshot(settings=settings, live=live)
