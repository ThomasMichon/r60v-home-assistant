"""Support for RocketR60V switches."""
from __future__ import annotations

from rocket_r60v.machine import Machine
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]

    async_add_entities([RocketR60VSwitchEntity(data, entry)], True)


class RocketR60VSwitchEntity(SwitchEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_is_on = self.data.standby == "on"
        self._attr_available = True
        self._attr_name = "Standby"
        self._attr_unique_id = "rocket_r60v_standby"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )
        self._attr_device_class = SwitchDeviceClass.SWITCH

    def turn_on(self, **kwargs: Any) -> None:
        self.data.standby = "on"
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self.data.standby = "off"
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_is_on = self.data.standby == "on"
