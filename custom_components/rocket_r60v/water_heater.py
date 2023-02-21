"""Support for RocketR60V switches."""
from __future__ import annotations

from rocket_r60v.machine import Machine
from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]

    async_add_entities(
        [
            RocketR60VBrewBoilerWaterHeaterEntity(data, entry),
            RocketR60VServiceBoilerWaterHeaterEntity(data, entry),
        ],
        True,
    )


class RocketR60VBrewBoilerWaterHeaterEntity(WaterHeaterEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        self._attr_current_operation = "electric"
        self._attr_operation_list = ["electric"]

        self._attr_current_temperature = self.data.current_brew_boiler_temperature
        self._attr_target_temperature = self.data.brew_boiler_temperature
        self._attr_is_away_mode_on = False
        self._attr_supported_features = (
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE
        )
        self._attr_available = True
        self._attr_name = "Brew Boiler"

        if self.data.temperature_unit == "Fahrenheit":
            self._attr_temperature_unit = "°F"
            self._attr_min_temp = 176
            self._attr_max_temp = 230
        else:
            self._attr_temperature_unit = "°C"
            self._attr_min_temp = 80
            self._attr_max_temp = 100

        self._attr_unique_id = "rocket_r60v_brew_boiler"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    def set_temperature(self, **kwargs) -> None:
        self.data.brew_boiler_temperature = kwargs.get(ATTR_TEMPERATURE)
        self.schedule_update_ha_state()

    def update(self) -> None:
        self._attr_current_temperature = self.data.current_brew_boiler_temperature
        self._attr_target_temperature = self.data.brew_boiler_temperature

        if self.data.temperature_unit == "Fahrenheit":
            self._attr_temperature_unit = "°F"
            self._attr_min_temp = 176
            self._attr_max_temp = 230
        else:
            self._attr_temperature_unit = "°C"
            self._attr_min_temp = 80
            self._attr_max_temp = 100


class RocketR60VServiceBoilerWaterHeaterEntity(WaterHeaterEntity):
    def __init__(self, data: Machine, entry: ConfigEntry) -> None:
        self.data = data[entry.entry_id]

        if self.data.service_boiler == "on":
            self._attr_current_operation = "electric"
        else:
            self._attr_current_operation = "off"

        self._attr_current_temperature = self.data.current_service_boiler_temperature
        self._attr_target_temperature = self.data.service_boiler_temperature

        self._attr_is_away_mode_on = False
        self._attr_operation_list = ["electric", "off"]
        self._attr_supported_features = (
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE
        )
        self._attr_available = True
        self._attr_name = "Service Boiler"

        if self.data.temperature_unit == "Fahrenheit":
            self._attr_temperature_unit = "°F"
            self._attr_min_temp = 230
            self._attr_max_temp = 259
        else:
            self._attr_temperature_unit = "°C"
            self._attr_min_temp = 110
            self._attr_max_temp = 126

        self._attr_unique_id = "rocket_r60v_service_boiler"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "instance")},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    def set_temperature(self, **kwargs) -> None:
        self.data.brew_boiler_temperature = kwargs.get(ATTR_TEMPERATURE)
        self.schedule_update_ha_state()

    def set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode == "electric":
            self.data.service_boiler = "on"
        else:
            self.data.service_boiler = "off"
        self.schedule_update_ha_state()

    def update(self) -> None:
        if self.data.service_boiler == "on":
            self._attr_current_operation = "electric"
        else:
            self._attr_current_operation = "off"

        self._attr_current_temperature = self.data.current_service_boiler_temperature
        self._attr_target_temperature = self.data.service_boiler_temperature

        if self.data.temperature_unit == "Fahrenheit":
            self._attr_temperature_unit = "°F"
            self._attr_min_temp = 230
            self._attr_max_temp = 259
        else:
            self._attr_temperature_unit = "°C"
            self._attr_min_temp = 110
            self._attr_max_temp = 126
