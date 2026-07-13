"""The state publisher: the MQTT-facing illusion of continuity.

The device link is intermittent — the R60V occasionally swallows a read, and a
reconnect takes seconds. Home Assistant, however, wants a *continuous* picture.
This component decouples the two:

- it caches the **last-known** :class:`~r60v_broker.state.StateSnapshot` and
  publishes every entity from that cache on a steady cadence, **regardless** of
  whether the most recent device read succeeded — so entities never flap to
  ``unknown`` on a transient miss;
- it manages **availability** with a grace period: the device is only marked
  offline after several *consecutive* failed poll cycles, and back online on the
  first success;
- it **interpolates** the live boiler temperatures toward their setpoints while
  reads are stale and the machine is on — a physically-plausible estimate (a
  regulating boiler trends to its setpoint) that keeps the graph alive instead
  of flat-lining, until a real reading corrects it.
"""
from __future__ import annotations

import logging
import time

from .config import Config
from .protocol import Address
from .state import CLIMATE_ENTITIES, ENTITIES, StateSnapshot

LOGGER = logging.getLogger("r60v.publisher")


class StatePublisher:
    """Caches machine state and publishes a continuous view to MQTT."""

    def __init__(self, config: Config, mqtt, *,
                 availability_grace: int = 4, stale_after: float = 12.0,
                 interpolate_step: float = 1.0) -> None:
        self.config = config
        self.mqtt = mqtt
        self.availability_grace = availability_grace
        self.stale_after = stale_after
        self.interpolate_step = interpolate_step

        self.snapshot = StateSnapshot()
        self._have_data = False
        self._fail_streak = 0
        self._available: bool | None = None
        self._last_live_update_at: float = 0.0

    # -- cache updates (called by the poll loop) --------------------------

    def update_settings(self, data: list[int]) -> None:
        self.snapshot.settings = data
        self._have_data = True

    def update_live(self, address: int, data: list[int]) -> None:
        self.snapshot.live[address] = data
        self._last_live_update_at = time.monotonic()
        self._have_data = True

    def note_success(self) -> None:
        self._fail_streak = 0
        self._set_available(True)

    def note_failure(self) -> None:
        self._fail_streak += 1
        if self._fail_streak >= self.availability_grace:
            self._set_available(False)

    def _set_available(self, online: bool) -> None:
        if self._available is online:
            return
        self._available = online
        self.mqtt.publish_availability(online)
        LOGGER.info("device marked %s", "online" if online else "offline")

    # -- interpolation ----------------------------------------------------

    def _interpolate_live_temps(self) -> None:
        """Ease live boiler temps toward setpoint while stale and machine on.

        Only runs when we have data, reads are stale, and the machine is on;
        each call nudges by at most ``interpolate_step`` degrees. A real reading
        (``update_live``) resets staleness and overrides the estimate.
        """
        if not self._have_data:
            return
        if time.monotonic() - self._last_live_update_at < self.stale_after:
            return
        if self.snapshot.settings_byte(Address.STANDBY) != 0:
            return  # machine off -> temps trend to ambient, don't fabricate

        self._ease(Address.CURRENT_BREW_TEMP, Address.BREW_BOILER_TEMP)
        if self.snapshot.settings_byte(Address.SERVICE_BOILER_ENABLE):
            self._ease(Address.CURRENT_SERVICE_TEMP, Address.SERVICE_BOILER_TEMP)

    def _ease(self, live_addr: int, setpoint_addr: int) -> None:
        data = self.snapshot.live.get(live_addr)
        if not data:
            return
        current = data[0]
        target = self.snapshot.settings_byte(setpoint_addr)
        gap = target - current
        if gap == 0:
            return
        step = max(-self.interpolate_step, min(self.interpolate_step, gap))
        self.snapshot.live[live_addr] = [int(round(current + step))]

    # -- publishing -------------------------------------------------------

    def publish(self) -> None:
        """Publish every entity from the cached snapshot."""
        if not self._have_data:
            return
        self._interpolate_live_temps()
        for entity in ENTITIES:
            try:
                self.mqtt.publish_state(entity.key, entity.decode(self.snapshot))
            except Exception as exc:  # noqa: BLE001 -- one bad entity must not stop the rest
                LOGGER.debug("failed to publish %s: %s", entity.key, exc)
        for climate in CLIMATE_ENTITIES:
            try:
                self.mqtt.publish_climate(
                    climate.key,
                    climate.current(self.snapshot),
                    climate.target(self.snapshot),
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("failed to publish climate %s: %s", climate.key, exc)
