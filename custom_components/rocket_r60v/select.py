"""Select platform for the Rocket R60V."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    """Set up R60V selects from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VSelect(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("select")
    )


class R60VSelect(R60VEntity, SelectEntity):
    """An enumerated R60V setting surfaced as a select."""

    def __init__(self, coordinator, unique_id: str, desc: R60VEntityDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_options = list(desc.options or [])

    @property
    def icon(self) -> str | None:
        """Reflect the selected value where a per-value icon map is defined."""
        return self._desc.icon_for(self.current_option)

    @property
    def current_option(self) -> str | None:
        """Decode the selected option from the coordinator's cached snapshot."""
        return self._desc.decode(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        address, data = self._desc.encode(option)
        await self.coordinator.client.write(address, data)
        await self.coordinator.async_request_refresh()
