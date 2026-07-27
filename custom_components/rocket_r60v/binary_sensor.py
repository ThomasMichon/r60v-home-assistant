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
    """Set up R60V binary sensors.

    The store-owned **Machine** connectivity signal is present whenever the
    bridge push channel is in use (the store is the arbiter of machine
    reachability). The bridge back-channel booleans are present only when the
    bridge-health endpoint is configured.
    """
    coordinator = entry.runtime_data.coordinator
    entities: list = []
    if coordinator.push_enabled:
        entities.append(R60VMachineConnectivitySensor(coordinator, entry.unique_id))
    if coordinator.bridge_health_enabled:
        entities.append(R60VDiagnosticWindowSensor(coordinator, entry.unique_id))
        entities.append(R60VMachineReachableSensor(coordinator, entry.unique_id))
    if entities:
        async_add_entities(entities)


class R60VMachineConnectivitySensor(R60VEntity, BinarySensorEntity):
    """Whether the machine's data is live, per the bridge **store** (the arbiter).

    This is the integration's primary machine-liveness signal in push mode: ON
    when the store reports the machine reachable, OFF when it does not (off or
    wedged). It is a *distinct layer* from the bridge-health ping
    (:class:`R60VMachineReachableSensor`) -- during a wedge the bridge can still
    ping the machine while the store reads it as unavailable. Stays visible
    precisely when the machine is not, so the operator can see *why* the machine
    entities went unavailable.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "machine_connectivity"
    _attr_icon = "mdi:coffee-maker"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "machine_connectivity", "Machine")

    @property
    def available(self) -> bool:
        # Always visible -- it exists precisely to explain machine-off states.
        return True

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return bool(getattr(data, "available", True))


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
