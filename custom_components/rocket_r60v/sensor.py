"""Sensor platform for the Rocket R60V."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
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
    entities: list[SensorEntity] = [
        R60VSensor(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("sensor")
    ]
    entities.append(R60VConnectionSensor(coordinator, entry.unique_id))
    # Bridge diagnostic sensors only exist when the back-channel is configured.
    if coordinator.bridge_health_enabled:
        entities.extend(
            [
                R60VBridgeLinkSensor(coordinator, entry.unique_id),
                R60VBridgeSignalSensor(coordinator, entry.unique_id),
                R60VBridgeRecoveriesSensor(coordinator, entry.unique_id),
                R60VBridgeLastRecoverySensor(coordinator, entry.unique_id),
                R60VBridgeUptimeSensor(coordinator, entry.unique_id),
            ]
        )
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
    _attr_options = [
        "connected",
        "reconnecting",
        "cooldown",
        "bridge_down",
        "bridge_recovering",
    ]
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
            "bridge_down": "mdi:wifi-strength-off",
            "bridge_recovering": "mdi:wifi-sync",
        }.get(state, "mdi:wifi")


class R60VBridgeEntity(R60VEntity, SensorEntity):
    """Base for bridge-health diagnostic sensors.

    These describe the *bridge/link* between Home Assistant and the machine
    (published by the bridge's health endpoint), not the machine itself, so they
    stay **available even when the machine is not** and read from
    ``coordinator.bridge_health``.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        # Bridge diagnostics must be visible precisely when the link is down.
        return True

    @property
    def _health(self):
        return self.coordinator.bridge_health


class R60VBridgeLinkSensor(R60VBridgeEntity):
    """The bridge -> machine WiFi link state (up / down / recovering)."""

    _attr_translation_key = "bridge_link"
    _attr_device_class = "enum"
    _attr_options = ["up", "down", "recovering", "unknown"]

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "bridge_link", "Bridge Link")

    @property
    def native_value(self) -> str:
        health = self._health
        if health is None or not health.usable:
            return "unknown"
        if health.diagnostic_window:
            return "recovering"
        return "up" if health.link_up else "down"

    @property
    def icon(self) -> str:
        return {
            "up": "mdi:wifi",
            "down": "mdi:wifi-strength-off",
            "recovering": "mdi:wifi-sync",
        }.get(self.native_value, "mdi:wifi-strength-alert-outline")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        health = self._health
        if health is None:
            return {"available": False}
        return {
            "available": health.available,
            "stale": health.stale,
            "ap_visible": health.ap_visible,
            "machine_powered_off": health.machine_powered_off,
            "ip": health.ip,
            "ssid": health.ssid,
        }


class R60VBridgeSignalSensor(R60VBridgeEntity):
    """WiFi signal strength of the bridge's link to the machine AP (0-100%)."""

    _attr_translation_key = "bridge_signal"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi-strength-3"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "bridge_signal", "Bridge Signal")

    @property
    def native_value(self) -> int | None:
        health = self._health
        if health is None or not health.usable or not health.link_up:
            return None
        return health.signal


class R60VBridgeRecoveriesSensor(R60VBridgeEntity):
    """How many times the bridge watchdog has recovered a parked link."""

    _attr_translation_key = "bridge_recoveries"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:wifi-refresh"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(
            coordinator, unique_id, "bridge_recoveries", "Bridge Recoveries"
        )

    @property
    def native_value(self) -> int | None:
        health = self._health
        if health is None or not health.available:
            return None
        return health.recoveries_total


class R60VBridgeLastRecoverySensor(R60VBridgeEntity):
    """Timestamp of the bridge watchdog's most recent link recovery."""

    _attr_translation_key = "bridge_last_recovery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(
            coordinator, unique_id, "bridge_last_recovery", "Bridge Last Recovery"
        )

    @property
    def native_value(self) -> datetime | None:
        health = self._health
        if health is None:
            return None
        return health.last_recovery


class R60VBridgeUptimeSensor(R60VBridgeEntity):
    """How long the current bridge -> machine link has been up (seconds)."""

    _attr_translation_key = "bridge_uptime"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "bridge_uptime", "Bridge Link Uptime")

    @property
    def native_value(self) -> int | None:
        health = self._health
        if health is None or not health.usable or not health.link_up:
            return None
        return health.link_uptime_s

