"""The Rocket R60V integration."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from rocket_r60v.exceptions import RocketError
from rocket_r60v.machine import Machine

from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN

# Every surface is re-enabled: the flooding that once forced this down to
# SWITCH-only is now absorbed by the broker's governor-fronted endpoint (see
# const.py), so per-setting reads across all platforms are safe.
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.TEXT,
    Platform.WATER_HEATER,
]

PARALLEL_UPDATES = 1

DEFAULT_SCAN_INTERVAL = timedelta(minutes=1)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rocket R60V from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    machine = Machine(address=host, port=port)

    try:
        # connect() opens a socket -- keep it off the event loop.
        await hass.async_add_executor_job(machine.connect)
    except (RocketError, OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to a Rocket R60V at {host}:{port}"
        ) from err

    hass.data[DOMAIN][entry.entry_id] = machine

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        machine = hass.data[DOMAIN].pop(entry.entry_id)

        del machine

    return unload_ok
