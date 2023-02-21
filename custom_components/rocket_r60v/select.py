"""Support for RocketR60V switches."""
from __future__ import annotations

from rocket_r60v.machine import Machine
from homeassistant.helpers.typing import StateType
from datetime import date, datetime
from decimal import Decimal

from homeassistant.components.select import SelectEntity
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
            RocketR60VPressureProfileSelectEntity(data, entry),
            RocketR60VWaterFeedSelectEntity(data, entry),
        ],
        True,
    )


class RocketR60VPressureProfileSelectEntity(SelectEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_available = True
        self._attr_name = "Pressure Profile"
        self._attr_unique_id = "rocket_r60v_pressure_profile"
        self._attr_options = ["A", "B", "C"]
        self._attr_current_option = self.data.active_profile
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    def select_option(self, option: str) -> None:
        self.data.active_profile = option
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_current_option = self.data.active_profile


class RocketR60VWaterFeedSelectEntity(SelectEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_available = True
        self._attr_name = "Water Feed"
        self._attr_unique_id = "rocket_r60v_water_feed"
        self._attr_options = ["HardPlumbed", "Reservoir"]
        self._attr_current_option = self.data.water_feed
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    def select_option(self, option: str) -> None:
        self.data.active_profile = option
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_current_option = self.data.water_feed
