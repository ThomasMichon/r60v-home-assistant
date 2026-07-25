"""The Rocket R60V integration."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .bridge_health import async_fetch_bridge_health
from .client import R60VClient
from .clock import async_setup_clock_sync
from .const import CONF_BRIDGE_HEALTH_URL, DEFAULT_HOST, DEFAULT_PORT, DOMAIN
from .coordinator import R60VCoordinator

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.TIME,
    Platform.TEXT,
    Platform.BUTTON,
    Platform.CLIMATE,
]

#: One request in flight at a time is enforced by the client's lock; keep HA
#: from parallelizing writes on top of that.
PARALLEL_UPDATES = 1


@dataclass
class R60VRuntimeData:
    """Objects shared across the integration for one config entry."""

    client: R60VClient
    coordinator: R60VCoordinator
    clock_unsub: Callable[[], None] | None = None


type R60VConfigEntry = ConfigEntry[R60VRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: R60VConfigEntry) -> bool:
    """Set up Rocket R60V from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    # Optional bridge-health back-channel (options override entry data).
    bridge_health_url = (
        entry.options.get(CONF_BRIDGE_HEALTH_URL)
        or entry.data.get(CONF_BRIDGE_HEALTH_URL)
        or None
    )
    health_fetcher = None
    if bridge_health_url:
        session = async_get_clientsession(hass)

        async def _fetch(url: str):
            return await async_fetch_bridge_health(session, url)

        health_fetcher = _fetch

    client = R60VClient(host, port)
    coordinator = R60VCoordinator(
        hass,
        client,
        bridge_health_url=bridge_health_url,
        health_fetcher=health_fetcher,
    )

    try:
        # Raises ConfigEntryNotReady on failure -- never hangs the loop.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise

    entry.runtime_data = R60VRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Keep the machine's onboard clock (and its built-in timers) on local time.
    entry.runtime_data.clock_unsub = async_setup_clock_sync(hass, client)
    # Reload when options (e.g. the bridge-health URL) change.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(
    hass: HomeAssistant, entry: R60VConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: R60VConfigEntry) -> bool:
    """Unload a config entry and close the client."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if entry.runtime_data.clock_unsub is not None:
            entry.runtime_data.clock_unsub()
        await entry.runtime_data.client.close()
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a device no longer provided (e.g. a pre-rewrite orphan)."""
    return not any(
        domain == DOMAIN and ident == config_entry.unique_id
        for (domain, ident) in device.identifiers
    )
