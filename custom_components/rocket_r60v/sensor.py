"""Rocket R60V sensor platform (current brew time)."""
from __future__ import annotations

from datetime import timedelta

from rocket_r60v.machine import Machine

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

PARALLEL_UPDATES = 1

# The brew timer is only shown on the machine's display *while a shot is
# pouring* (~25-40 s). It is derived from the display, so a slow poll almost
# always misses it. Poll this one cheap sensor quickly so a shot is captured;
# every read still serializes through the bridge governor.
SCAN_INTERVAL = timedelta(seconds=5)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]

    async_add_entities(
        [RocketR60VCurrentBrewTimeSensorEntity(data, entry)],
        True,
    )


class RocketR60VCurrentBrewTimeSensorEntity(SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_available = True
        self._attr_name = "Current Brew Time"
        self._attr_unique_id = "rocket_r60v_current_brew_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )
        self.update()

    def update(self) -> None:
        # The library returns the pouring time (seconds) while a shot is live,
        # else None. Report 0 when idle so it is a clean numeric measurement
        # (a shot then reads as a spike toward ~30 s in history).
        self._attr_native_value = self.data.current_brew_time or 0
