"""Support for RocketR60V switches."""
from __future__ import annotations

from rocket_r60v.machine import Machine

from homeassistant.components.text import TextEntity
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
        [
            RocketR60VPressureProfileTextEntity(data, entry, "A"),
            RocketR60VPressureProfileTextEntity(data, entry, "B"),
            RocketR60VPressureProfileTextEntity(data, entry, "C"),
            RocketR60VAutoOnTimeTextEntity(data, entry),
            RocketR60VAutoOffTimeTextEntity(data, entry)
        ],
        True,
    )


class RocketR60VPressureProfileTextEntity(TextEntity):
    def __init__(self, data: Machine, entry: ConfigEntry, key) -> None:
        self.data = data[entry.entry_id]
        self.key = key

        self._attr_available = True
        self._attr_name = f"Pressure Profile {key}"
        self._attr_unique_id = f"rocket_r60v_profile_{key.lower()}"
        self._attr_pattern = """\d+:\d+(\.\d+)? \d+:\d+(\.\d+)? \d+:\d+(\.\d+)? \d+:\d+(\.\d+)? \d+:\d+(\.\d+)?"""
        self._attr_mode = "text"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

        if self.key == "A":
            self._attr_native_value = self.data.profile_a
        elif self.key == "B":
            self._attr_native_value = self.data.profile_b
        else:
            self._attr_native_value = self.data.profile_c

    def set_value(self, value: str) -> None:
        if self.key == "A":
            self.data.profile_a = value
        elif self.key == "B":
            self.data.profile_b = value
        else:
            self.data.profile_c = value
        self.schedule_update_ha_state()

    def update(self) -> None:
        if self.key == "A":
            self._attr_native_value = self.data.profile_a
        elif self.key == "B":
            self._attr_native_value = self.data.profile_b
        else:
            self._attr_native_value = self.data.profile_c


class RocketR60VAutoOnTimeTextEntity(TextEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_available = True
        self._attr_name = "Auto-On Time"
        self._attr_unique_id = "rocket_r60v_profile_auto_on"
        self._attr_pattern = """\d\d:\d\d"""
        self._attr_mode = "text"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

        self._attr_native_value = self.data.auto_on

    def set_value(self, value: str) -> None:
        self.data.auto_on = value
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_native_value = self.data.auto_on


class RocketR60VAutoOffTimeTextEntity(TextEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_available = True
        self._attr_name = "Auto-Off Time"
        self._attr_unique_id = "rocket_r60v_profile_auto_off"
        self._attr_pattern = """\d\d:\d\d"""
        self._attr_mode = "text"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

        self._attr_native_value = self.data.auto_off

    def set_value(self, value: str) -> None:
        self.data.auto_on = value
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_native_value = self.data.auto_off
