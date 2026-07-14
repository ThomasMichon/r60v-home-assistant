"""Switch platform for the Rocket R60V."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up R60V switches from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VSwitch(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("switch")
    )


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
        await self.coordinator.client.write(address, data)
        await self.coordinator.async_request_refresh()
