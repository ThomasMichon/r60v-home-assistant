"""The MQTT projection: publish the store's view to Home Assistant over MQTT.

This is a **projection** of the :class:`~r60v_broker.store.DeviceState` store,
not an owner of state. It holds no cache or availability logic of its own: it
reads decoded entity values from the store's cached snapshot, publishes them on
a steady cadence, and -- by subscribing to the store's change events -- mirrors
the store's single ``available`` verdict to MQTT the instant it flips. The store
is the arbiter; this class only speaks MQTT.

(The class name is retained for compatibility; it is now the MQTT projection
over a :class:`DeviceState`, which it creates itself when one isn't injected.)
"""
from __future__ import annotations

import logging

from .config import Config
from .state import CLIMATE_ENTITIES, ENTITIES
from .store import DeviceState

LOGGER = logging.getLogger("r60v.publisher")


class StatePublisher:
    """Publishes the store's continuous view to MQTT; mirrors availability."""

    def __init__(self, config: Config, mqtt, *,
                 store: DeviceState | None = None,
                 availability_grace: int = 4, stale_after: float = 12.0,
                 interpolate_step: float = 1.0) -> None:
        self.config = config
        self.mqtt = mqtt
        #: The single arbiter of state + availability. Injected by the broker so
        #: MQTT and the WS push server share one store; created here when a
        #: caller (e.g. a unit test) doesn't supply one.
        self.store = store or DeviceState(
            availability_grace=availability_grace,
            stale_after=stale_after,
            interpolate_step=interpolate_step,
        )
        self._last_pub_available: bool | None = None
        # Mirror the store's availability verdict to MQTT whenever it changes.
        self.store.subscribe(self._on_store_change)

    # -- store change -> MQTT availability --------------------------------

    def _on_store_change(self) -> None:
        available = self.store.available
        if available != self._last_pub_available:
            self._last_pub_available = available
            self.mqtt.publish_availability(available)

    # -- cache updates (delegate to the store) ----------------------------

    def update_settings(self, data: list[int]) -> None:
        self.store.update_settings(data)

    def update_live(self, address: int, data: list[int]) -> None:
        self.store.update_live(address, data)

    def note_success(self) -> None:
        self.store.note_success()

    def note_failure(self) -> None:
        self.store.note_failure()

    @property
    def snapshot(self):
        return self.store.snapshot

    # -- publishing (steady cadence, from the store's cache) --------------

    def publish(self) -> None:
        """Publish every entity from the store's cached snapshot."""
        if not self.store.have_data:
            return
        self.store.interpolate_live_temps()
        snap = self.store.snapshot
        for entity in ENTITIES:
            try:
                self.mqtt.publish_state(entity.key, entity.decode(snap))
            except Exception as exc:  # noqa: BLE001 -- one bad entity must not stop the rest
                LOGGER.debug("failed to publish %s: %s", entity.key, exc)
        for climate in CLIMATE_ENTITIES:
            try:
                self.mqtt.publish_climate(
                    climate.key,
                    climate.current(snap),
                    climate.target(snap),
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("failed to publish climate %s: %s", climate.key, exc)

    def raw_snapshot(self) -> dict:
        """Raw register snapshot for the WebSocket push channel (from the store)."""
        return self.store.raw_snapshot()
