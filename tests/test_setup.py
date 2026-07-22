"""Integration tests against the wire-level R60V emulator.

These stand up the bundled emulator (a real asyncio TCP server speaking the R60V
protocol) on an ephemeral loopback port, point a config entry at it, and set the
integration up through the real HA config-entry machinery.

Coverage:

- :func:`test_all_entities_load` -- every expected entity is registered on the
  right platform, is not ``STATE_UNAVAILABLE``, and one decoded value per
  platform is spot-checked. This is also the startup regression test: setup runs
  inside ``asyncio.timeout`` so a loop-blocking ``__init__`` (the original bug)
  fails loudly instead of hanging. ``pytest-homeassistant-custom-component``
  enables HA's blocking-call detector during setup as a second guard.
- :func:`test_write_round_trip` -- writing an entity reaches the emulator and the
  next coordinator refresh reflects it.
- :func:`test_unavailable_on_dead_endpoint` -- when the device disappears
  mid-run, entities go unavailable and the refresh fails fast (no loop block).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_PORT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v.client import R60VConnectionError
from custom_components.rocket_r60v.coordinator import FAILURE_TOLERANCE
from r60v_broker.emulator import R60VEmulator
from r60v_broker.protocol import Address

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30

# The 16 entities the integration exposes, as (platform, unique_id suffix).
EXPECTED_ENTITIES: list[tuple[str, str]] = [
    ("sensor", "current_pressure"),
    ("sensor", "display"),
    ("sensor", "total_coffee_count"),
    ("switch", "power"),
    ("switch", "service_boiler"),
    ("select", "active_profile"),
    ("select", "water_feed"),
    ("select", "temperature_unit"),
    ("select", "language"),
    ("time", "auto_on"),
    ("time", "auto_off"),
    ("text", "profile_a"),
    ("text", "profile_b"),
    ("text", "profile_c"),
    ("climate", "brew_boiler"),
    ("climate", "steam_boiler"),
]


@pytest.fixture
async def emulator(socket_enabled):
    """Run the R60V wire emulator on an ephemeral loopback port for one test."""
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        yield emu
    finally:
        await emu.stop()


def _uid(entry: MockConfigEntry, suffix: str) -> str:
    return f"{entry.unique_id}_{suffix}"


async def _setup_against(hass: HomeAssistant, port: int) -> MockConfigEntry:
    """Create and set up a config entry pointed at ``127.0.0.1:port``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        unique_id=f"127.0.0.1:{port}",
    )
    entry.add_to_hass(hass)
    async with asyncio.timeout(SETUP_TIMEOUT):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_all_entities_load(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """All 16 entities load on the right platform, available, decoded sanely."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)

    try:
        resolved: dict[str, str] = {}
        for platform, suffix in EXPECTED_ENTITIES:
            entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, _uid(entry, suffix))
            assert entity_id is not None, f"{platform}/{suffix} not registered"
            state = hass.states.get(entity_id)
            assert state is not None, f"{entity_id} has no state"
            assert state.state != STATE_UNAVAILABLE, f"{entity_id} is unavailable"
            resolved[suffix] = entity_id

        # Exactly the expected set was created for this entry (no more, no less).
        registered = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        assert len(registered) == len(EXPECTED_ENTITIES)

        # --- one decoded value spot-check per platform ---
        # sensor: emulator display default is "READY".
        assert hass.states.get(resolved["display"]).state == "READY"
        # switch: STANDBY default 0 -> machine running -> Power on.
        assert hass.states.get(resolved["power"]).state == "on"
        # select: LANGUAGE default 0 -> english.
        assert hass.states.get(resolved["language"]).state == "english"
        # time: emulator auto-on default is 14:00.
        assert hass.states.get(resolved["auto_on"]).state == "14:00:00"
        # climate: brew boiler setpoint default 105 C.
        brew = hass.states.get(resolved["brew_boiler"])
        assert brew.attributes[ATTR_TEMPERATURE] == 105
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_write_round_trip(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """A write reaches the emulator and the next refresh reflects it."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    coordinator = entry.runtime_data.coordinator

    try:
        # --- switch write: turn Power off -> STANDBY byte becomes 1 ---
        power_id = ent_reg.async_get_entity_id("switch", DOMAIN, _uid(entry, "power"))
        async with asyncio.timeout(SETUP_TIMEOUT):
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": power_id}, blocking=True
            )
            await hass.async_block_till_done()
        assert emulator.model.settings[Address.STANDBY] == 1

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(power_id).state == "off"

        # --- climate write: set brew boiler setpoint to 100 C ---
        brew_id = ent_reg.async_get_entity_id("climate", DOMAIN, _uid(entry, "brew_boiler"))
        async with asyncio.timeout(SETUP_TIMEOUT):
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": brew_id, ATTR_TEMPERATURE: 100},
                blocking=True,
            )
            await hass.async_block_till_done()
        assert emulator.model.settings[Address.BREW_BOILER_TEMP] == 100

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(brew_id).attributes[ATTR_TEMPERATURE] == 100
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_unavailable_on_dead_endpoint(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """A sustained outage marks entities unavailable without hanging.

    A single failed poll is tolerated (cached values are served); only after
    ``FAILURE_TOLERANCE`` consecutive failures does the device go unavailable.
    """
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    coordinator = entry.runtime_data.coordinator
    power_id = ent_reg.async_get_entity_id("switch", DOMAIN, _uid(entry, "power"))

    try:
        # Kill the device mid-run, then poll past the tolerance. Each refresh
        # must fail fast (the client reconnects then raises) without hanging.
        await emulator.stop()
        async with asyncio.timeout(SETUP_TIMEOUT):
            for _ in range(FAILURE_TOLERANCE + 1):
                await coordinator.async_refresh()
                await hass.async_block_till_done()

        assert coordinator.last_update_success is False
        assert hass.states.get(power_id).state == STATE_UNAVAILABLE
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_cached_values_served_through_transient_failure(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """An isolated failed poll keeps entities available with last-good values."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    coordinator = entry.runtime_data.coordinator
    power_id = ent_reg.async_get_entity_id("switch", DOMAIN, _uid(entry, "power"))
    display_id = ent_reg.async_get_entity_id("sensor", DOMAIN, _uid(entry, "display"))

    good_power = hass.states.get(power_id).state
    good_display = hass.states.get(display_id).state

    try:
        # Force the next polls to fail as if the stream desynced.
        with patch.object(
            coordinator, "_read_snapshot",
            side_effect=R60VConnectionError("simulated desync"),
        ):
            # Within tolerance: entities stay available with cached values.
            for _ in range(FAILURE_TOLERANCE):
                async with asyncio.timeout(SETUP_TIMEOUT):
                    await coordinator.async_refresh()
                    await hass.async_block_till_done()
                assert coordinator.last_update_success is True
                assert hass.states.get(power_id).state == good_power
                assert hass.states.get(display_id).state == good_display

            # One failure past tolerance: now the device is marked unavailable.
            async with asyncio.timeout(SETUP_TIMEOUT):
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            assert coordinator.last_update_success is False
            assert hass.states.get(power_id).state == STATE_UNAVAILABLE
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
