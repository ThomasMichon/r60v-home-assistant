"""Climate platform for the Rocket R60V boilers."""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import R60VConfigEntry
from .entities import CLIMATE_ENTITIES, R60VClimateDescription, is_fahrenheit
from .entity import R60VEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: R60VConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up R60V boiler thermostats from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VClimate(coordinator, entry.unique_id, desc) for desc in CLIMATE_ENTITIES
    )


class R60VClimate(R60VEntity, ClimateEntity):
    """A boiler (current temp + setpoint) modeled as a thermostat.

    Temperatures are reported in the machine's *current display unit* (reg
    ``0x00``: Celsius or Fahrenheit) rather than hardcoded Celsius, and the
    valid setpoint range follows that unit. The thermostat reports ``heat``
    only while the boiler is actually energized; otherwise ``off``.
    """

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator, unique_id: str, desc: R60VClimateDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_icon = desc.icon
        self._attr_target_temperature_step = desc.temp_step

    # -- unit-aware attributes (follow reg 0x00) --------------------------

    @property
    def _fahrenheit(self) -> bool:
        return is_fahrenheit(self.coordinator.data)

    @property
    def temperature_unit(self) -> str:
        return (
            UnitOfTemperature.FAHRENHEIT
            if self._fahrenheit
            else UnitOfTemperature.CELSIUS
        )

    @property
    def min_temp(self) -> float:
        return self._desc.range_for(self._fahrenheit)[0]

    @property
    def max_temp(self) -> float:
        return self._desc.range_for(self._fahrenheit)[1]

    # -- state ------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Decode the live boiler temperature from the cached snapshot."""
        return self._desc.current(self.coordinator.data)

    @property
    def target_temperature(self) -> float | None:
        """Decode the boiler setpoint from the cached snapshot."""
        return self._desc.target(self.coordinator.data)

    @property
    def hvac_mode(self) -> HVACMode:
        """HEAT while the boiler is energized, OFF otherwise."""
        return HVACMode.HEAT if self._desc.is_on(self.coordinator.data) else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """Report the boiler as heating only when it is actually on."""
        return (
            HVACAction.HEATING
            if self._desc.is_on(self.coordinator.data)
            else HVACAction.OFF
        )

    # -- commands ---------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        address, data = self._desc.encode_setpoint(temperature, self._fahrenheit)
        await self.coordinator.client.write(address, data)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Drive the boiler's power bit (brew = machine standby; steam = enable)."""
        address, data = self._desc.encode_power(hvac_mode != HVACMode.OFF)
        await self.coordinator.client.write(address, data)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
