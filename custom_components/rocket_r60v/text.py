"""Text platform for the Rocket R60V (editable pressure profiles)."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
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
    """Set up R60V text entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        R60VText(coordinator, entry.unique_id, desc)
        for desc in entities_for_platform("text")
    )


class R60VText(R60VEntity, TextEntity):
    """A free-text R60V setting (e.g. a pressure profile) surfaced as text.

    A pressure profile is edited as a space-separated ``seconds:bar`` list of up
    to 5 steps, e.g. ``"3:3 6:6 25:9 0:9 0:6"``. The encode step validates the
    format and each value's range before writing the 15-byte block.
    """

    def __init__(self, coordinator, unique_id: str, desc: R60VEntityDescription) -> None:
        # Static attributes only -- no device I/O here (runs on the event loop).
        super().__init__(coordinator, unique_id, desc.key, desc.name)
        self._desc = desc
        self._attr_icon = desc.icon
        if desc.max_length is not None:
            self._attr_native_max = desc.max_length

    @property
    def native_value(self) -> str | None:
        """Decode the text value from the coordinator's cached snapshot."""
        return self._desc.decode(self.coordinator.data)

    async def async_set_value(self, value: str) -> None:
        # encode() raises ValueError on a malformed/out-of-range profile, which
        # HA surfaces to the user without touching the machine.
        address, data = self._desc.encode(value)
        await self.coordinator.client.write(address, data)
        await self.coordinator.async_request_refresh()
