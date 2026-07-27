"""Switch platform for the Rocket R60V."""
from __future__ import annotations

from datetime import time as dt_time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import R60VConfigEntry
from .entities import (
    R60VEntityDescription,
    R60VTimerSwitchDescription,
    TIMER_SWITCHES,
    encode_timer,
    entities_for_platform,
    timer_time,
)
from .entity import R60VEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: R60VConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up R60V switches from a config entry."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = [
        R60VSwitch(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("switch")
    ]
    entities.extend(
        R60VTimerSwitch(coordinator, entry.unique_id, desc)
        for desc in TIMER_SWITCHES
    )
    async_add_entities(entities)


class R60VSwitch(R60VEntity, SwitchEntity):
    """A boolean R60V setting surfaced as a switch."""

    def __init__(self, coordinator, unique_id: str, desc: R60VEntityDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_icon = desc.icon

    @property
    def is_on(self) -> bool:
        """Decode on/off from the coordinator's cached snapshot."""
        return bool(self._desc.decode(self.coordinator.data))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)

    async def _write(self, value: bool) -> None:
        address, data = self._desc.encode(value)
        await self.coordinator.async_write(address, data, key=self._desc.key)


class R60VTimerSwitch(R60VEntity, SwitchEntity, RestoreEntity):
    """Enable/disable one of the machine's built-in auto on/off timers.

    Turning the switch **off** disables the built-in timer (writes the sentinel
    ``100`` to its hour+minute byte) so a Home Assistant schedule can own the
    machine's power instead of the machine's own clock. Turning it **on**
    re-enables it at its remembered time -- the last time seen on the machine,
    a value restored across restarts, or a sensible default the very first time
    -- which the paired Auto-On / Auto-Off ``time`` picker can then refine.

    The R60V exposes no separate enable bit, so the switch derives its state
    from whether the timer's hour byte holds a valid clock hour (enabled) or the
    disabled sentinel.
    """

    def __init__(
        self, coordinator, unique_id: str, desc: R60VTimerSwitchDescription
    ) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_icon = desc.icon
        self._configured: dt_time = desc.default

    def _snapshot_time(self) -> dt_time | None:
        """The timer's currently-set time from the cached snapshot (or None)."""
        return timer_time(
            self.coordinator.data, self._desc.hour_address, self._desc.minute_address
        )

    @property
    def is_on(self) -> bool:
        return self._snapshot_time() is not None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # Surface the time the timer will (re)enable to -- useful while it's off.
        return {"configured_time": self._configured.strftime("%H:%M")}

    @callback
    def _handle_coordinator_update(self) -> None:
        # Remember the machine's own time whenever the timer is enabled, so a
        # later turn-on restores it rather than a stale default.
        current = self._snapshot_time()
        if current is not None:
            self._configured = current
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Prefer a live-set time; otherwise restore the last remembered one.
        current = self._snapshot_time()
        if current is not None:
            self._configured = current
        elif (last := await self.async_get_last_state()) is not None:
            stored = last.attributes.get("configured_time")
            if isinstance(stored, str):
                try:
                    hour, minute = (int(part) for part in stored.split(":"))
                    self._configured = dt_time(hour=hour, minute=minute)
                except (ValueError, TypeError):
                    pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        address, data = encode_timer(self._desc.hour_address, self._configured)
        await self.coordinator.async_write(address, data, key=self._desc.key)

    async def async_turn_off(self, **kwargs: Any) -> None:
        address, data = encode_timer(self._desc.hour_address, None)
        await self.coordinator.async_write(address, data, key=self._desc.key)
