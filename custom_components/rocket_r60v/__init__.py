"""The Rocket R60V integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .client import R60VClient
from .const import DEFAULT_HOST, DEFAULT_PORT
from .coordinator import R60VCoordinator

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.TIME,
    Platform.CLIMATE,
]

#: One request in flight at a time is enforced by the client's lock; keep HA
#: from parallelizing writes on top of that.
PARALLEL_UPDATES = 1


@dataclass
class R60VRuntimeData:
    """Objects shared across the integration for one config entry."""

    client: R60VClient
    coordinator: R60VCoordinator


type R60VConfigEntry = ConfigEntry[R60VRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: R60VConfigEntry) -> bool:
    """Set up Rocket R60V from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = R60VClient(host, port)
    coordinator = R60VCoordinator(hass, client)

    try:
        # Raises ConfigEntryNotReady on failure -- never hangs the loop.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise

    entry.runtime_data = R60VRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: R60VConfigEntry) -> bool:
    """Unload a config entry and close the client."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.close()
    return unload_ok
