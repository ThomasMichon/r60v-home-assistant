"""The Rocket R60V integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.core import HomeAssistant
from datetime import timedelta

from .const import DOMAIN

from rocket_r60v.machine import Machine

from .FakeMachine import FakeMachine

# TODO List the platforms that you want to support.
# For your initial PR, limit it to 1 platform.
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.TEXT,
    Platform.WATER_HEATER,
]

DEFAULT_SCAN_INTERVAL = timedelta(minutes=1)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rocket R60V from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    machine = Machine()

    try:
        machine.connect()
    except TimeoutError:
        raise ConfigEntryNotReady("Cannot connect to a Rocket R60V")

    hass.data[DOMAIN][entry.entry_id] = machine

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        machine = hass.data[DOMAIN].pop(entry.entry_id)

        del machine

    return unload_ok
