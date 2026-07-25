"""Binary sensor platform for the Rocket R60V bridge-health back-channel.

Two boolean bridge diagnostics, present only when the bridge-health endpoint is
configured. Like the other bridge diagnostics, they stay **available even when
the machine is not** -- they describe the bridge/link, not the machine.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import R60VConfigEntry
from .entity import R60VEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: R60VConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up R60V bridge binary sensors (only when the back-channel is on)."""
    coordinator = entry.runtime_data.coordinator
    if not coordinator.bridge_health_enabled:
        return
    async_add_entities(
        [
            R60VDiagnosticWindowSensor(coordinator, entry.unique_id),
            R60VMachineReachableSensor(coordinator, entry.unique_id),
        ]
    )


class _R60VBridgeBinary(R60VEntity, BinarySensorEntity):
    """Base for always-available bridge boolean diagnostics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    @property
    def _health(self):
        return self.coordinator.bridge_health


class R60VDiagnosticWindowSensor(_R60VBridgeBinary):
    """On while the bridge watchdog is actively recovering a parked link.

    When on, the integration has paused machine polling so it does not hammer a
    half-built relay path during recovery.
    """

    _attr_translation_key = "bridge_diagnostic_window"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:wrench-clock"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(
            coordinator, unique_id, "bridge_diagnostic_window", "Bridge Diagnostic Window"
        )

    @property
    def is_on(self) -> bool | None:
        health = self._health
        if health is None or not health.usable:
            return None
        return health.diagnostic_window


class R60VMachineReachableSensor(_R60VBridgeBinary):
    """Whether the bridge can reach the machine (ping) on its own link."""

    _attr_translation_key = "bridge_machine_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:lan-connect"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(
            coordinator, unique_id, "bridge_machine_reachable", "Machine Reachable"
        )

    @property
    def is_on(self) -> bool | None:
        health = self._health
        if health is None or not health.usable:
            return None
        return health.machine_reachable
