"""Shared base entity for the Rocket R60V integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import R60VCoordinator


class R60VEntity(CoordinatorEntity[R60VCoordinator]):
    """Base class binding an entity to the coordinator and the device.

    ``__init__`` sets ONLY static attributes -- it performs no device I/O
    (that would run on the event loop). All state is read from
    ``self.coordinator.data`` (a cached snapshot fetched off-loop by the
    coordinator), never from the device directly.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: R60VCoordinator, unique_id: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            manufacturer="Rocket Espresso",
            model="R60V",
            name="Rocket R60V",
        )

    @property
    def available(self) -> bool:
        """A machine entity is available only when BOTH hold:

        - the transport is up (``last_update_success`` -- in push mode this is
          the bridge stream; in polling mode it is the machine poll), and
        - the store says the machine is reachable (``snapshot.available``).

        The store (bridge) is the single arbiter of machine reachability, so a
        machine hiccup surfaces here without the integration deciding anything
        locally. Diagnostic entities (Connection, bridge-health) override this to
        stay visible precisely when the machine is not.
        """
        if not super().available:
            return False
        data = self.coordinator.data
        if data is None:
            return False
        return getattr(data, "available", True)

