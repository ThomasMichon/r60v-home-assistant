"""Support for RocketR60V switches."""
from __future__ import annotations

from rocket_r60v.machine import Machine

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]

    async_add_entities(
        [RocketR60VCurrentBrewTimeSensorEntity(data, entry)],
        True,
    )


class RocketR60VCurrentBrewTimeSensorEntity(SensorEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_native_value = self.data.current_brew_time
        self._attr_available = True
        self._attr_name = "Current Brew Time"
        self._attr_unique_id = "rocket_r60v_current_brew_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    def update(self) -> None:
        self._attr_native_value = self.data.current_brew_time
