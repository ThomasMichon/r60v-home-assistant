"""Sensor platform for the Rocket R60V."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
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
    entities: list[SensorEntity] = [
        R60VSensor(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("sensor")
    ]
    entities.append(R60VConnectionSensor(coordinator, entry.unique_id))
    async_add_entities(entities)


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


class R60VConnectionSensor(R60VEntity, SensorEntity):
    """Diagnostic sensor exposing the link/cooldown status.

    Unlike the machine-value sensors, this stays **available even when the
    device is not** -- it reports whether the integration is connected,
    reconnecting, or in a wedge-recovery cooldown, and (as attributes) how long
    the cooldown has left. Pair it with the 'End Cooldown' button to resume
    immediately.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connection"
    _attr_options = ["connected", "reconnecting", "cooldown"]
    _attr_device_class = "enum"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "connection", "Connection")
        self._attr_icon = "mdi:wifi"

    @property
    def available(self) -> bool:
        # Diagnostic status must be visible precisely when the device is not.
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.connection_state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "cooldown_remaining_s": self.coordinator.cooldown_remaining,
            "cooldown_ends_at": self.coordinator.cooldown_ends_at,
            "consecutive_failures": self.coordinator.consecutive_failures,
        }

    @property
    def icon(self) -> str:
        state = self.coordinator.connection_state
        return {
            "connected": "mdi:wifi",
            "reconnecting": "mdi:wifi-sync",
            "cooldown": "mdi:wifi-off",
        }.get(state, "mdi:wifi")

