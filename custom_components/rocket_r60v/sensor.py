"""Sensor platform for the Rocket R60V."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import R60VConfigEntry
from .entities import R60VEntityDescription, entities_for_platform
from .entity import R60VEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: R60VConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up R60V sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VSensor(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("sensor")
    )


class R60VSensor(R60VEntity, SensorEntity):
    """A read-only R60V value surfaced as a sensor."""

    def __init__(self, coordinator, unique_id: str, desc: R60VEntityDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        if desc.suggested_precision is not None:
            self._attr_suggested_display_precision = desc.suggested_precision

    @property
    def native_value(self):
        """Decode the value from the coordinator's cached snapshot."""
        return self._desc.decode(self.coordinator.data)
