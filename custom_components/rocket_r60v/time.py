"""Time platform for the Rocket R60V (auto on/off timers)."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
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
    """Set up R60V time pickers from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VTime(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("time")
    )


class R60VTime(R60VEntity, TimeEntity):
    """A writable HH:MM R60V timer surfaced as a native time picker."""

    def __init__(self, coordinator, unique_id: str, desc: R60VEntityDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_icon = desc.icon

    @property
    def native_value(self) -> dt_time | None:
        """Decode the time from the coordinator's cached snapshot."""
        return self._desc.decode(self.coordinator.data)

    async def async_set_value(self, value: dt_time) -> None:
        address, data = self._desc.encode(value)
        await self.coordinator.async_write(address, data, key=self._desc.key)
