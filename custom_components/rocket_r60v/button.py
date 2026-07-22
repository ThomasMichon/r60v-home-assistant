"""Button platform for the Rocket R60V (cooldown override)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import R60VConfigEntry
from .entity import R60VEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: R60VConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up R60V buttons from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([R60VEndCooldownButton(coordinator, entry.unique_id)])


class R60VEndCooldownButton(R60VEntity, ButtonEntity):
    """End a wedge-recovery cooldown early and retry the connection now.

    When the machine's listener wedges, the integration backs off (a cooldown)
    so the machine can reset. Pressing this button cancels the wait and retries
    immediately. It stays available during the cooldown (that is the whole
    point), so it does not gate on coordinator success.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi-refresh"

    def __init__(self, coordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id, "end_cooldown", "End Cooldown")

    @property
    def available(self) -> bool:
        # Must be usable exactly when the device is unavailable (in cooldown).
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_end_cooldown()
