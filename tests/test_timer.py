"""Tests for the built-in auto on/off timer enable/disable switches (#4).

The R60V has no separate enable bit: a timer is disabled by writing the sentinel
100 to both its hour and minute byte, and enabled by writing a valid HH:MM. The
``Auto-On Timer`` / ``Auto-Off Timer`` switches drive that, remembering the last
time so a re-enable is round-trippable.
"""
from __future__ import annotations

import asyncio
from datetime import time as dt_time

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v.entities import (
    TIMER_SWITCHES,
    StateSnapshot,
    encode_timer,
    timer_time,
)
from custom_components.rocket_r60v.protocol import (
    SETTINGS_LEN,
    TIMER_DISABLED,
    Address,
)
from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30


def _snapshot(settings: dict[int, int]) -> StateSnapshot:
    data = [0] * SETTINGS_LEN
    for addr, val in settings.items():
        data[addr] = val
    return StateSnapshot(settings=data)


# -- pure logic ----------------------------------------------------------


def test_timer_time_decodes_valid_time() -> None:
    snap = _snapshot({Address.AUTO_ON_HOUR: 6, Address.AUTO_ON_MINUTE: 30})
    assert timer_time(snap, Address.AUTO_ON_HOUR, Address.AUTO_ON_MINUTE) == dt_time(6, 30)


def test_timer_time_none_when_disabled() -> None:
    """The disabled sentinel (100) in the hour byte decodes to None."""
    snap = _snapshot(
        {Address.AUTO_ON_HOUR: TIMER_DISABLED, Address.AUTO_ON_MINUTE: TIMER_DISABLED}
    )
    assert timer_time(snap, Address.AUTO_ON_HOUR, Address.AUTO_ON_MINUTE) is None


def test_timer_time_none_on_out_of_range() -> None:
    snap = _snapshot({Address.AUTO_OFF_HOUR: 24, Address.AUTO_OFF_MINUTE: 0})
    assert timer_time(snap, Address.AUTO_OFF_HOUR, Address.AUTO_OFF_MINUTE) is None


def test_encode_timer_enable() -> None:
    assert encode_timer(Address.AUTO_ON_HOUR, dt_time(7, 15)) == (
        Address.AUTO_ON_HOUR,
        [7, 15],
    )


def test_encode_timer_disable() -> None:
    assert encode_timer(Address.AUTO_OFF_HOUR, None) == (
        Address.AUTO_OFF_HOUR,
        [TIMER_DISABLED, TIMER_DISABLED],
    )


def test_timer_switch_descriptions() -> None:
    """The two switches target the correct hour/minute register pairs."""
    by_key = {d.key: d for d in TIMER_SWITCHES}
    on = by_key["auto_on_enabled"]
    off = by_key["auto_off_enabled"]
    assert (on.hour_address, on.minute_address) == (
        Address.AUTO_ON_HOUR,
        Address.AUTO_ON_MINUTE,
    )
    assert (off.hour_address, off.minute_address) == (
        Address.AUTO_OFF_HOUR,
        Address.AUTO_OFF_MINUTE,
    )


# -- behavioral (through the real config entry + emulator) ---------------


@pytest.fixture
async def emulator(socket_enabled):
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        yield emu
    finally:
        await emu.stop()


async def _setup_against(hass: HomeAssistant, port: int) -> MockConfigEntry:
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


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    async with asyncio.timeout(SETUP_TIMEOUT):
        await hass.services.async_call(
            "switch", service, {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()


async def test_timer_switch_disable_then_reenable_round_trip(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """Off writes the 100 sentinel; on restores the remembered time."""
    # Start with the built-in auto-on timer enabled at 06:30.
    emulator.model.settings[Address.AUTO_ON_HOUR] = 6
    emulator.model.settings[Address.AUTO_ON_MINUTE] = 30

    entry = await _setup_against(hass, emulator.bound_port)
    coordinator = entry.runtime_data.coordinator
    ent_reg = er.async_get(hass)
    switch_id = ent_reg.async_get_entity_id(
        "switch", DOMAIN, f"{entry.unique_id}_auto_on_enabled"
    )
    assert switch_id is not None

    try:
        assert hass.states.get(switch_id).state == "on"

        # Disable -> 100 sentinel in both bytes; state off.
        await _call(hass, "turn_off", switch_id)
        assert emulator.model.settings[Address.AUTO_ON_HOUR] == TIMER_DISABLED
        assert emulator.model.settings[Address.AUTO_ON_MINUTE] == TIMER_DISABLED
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(switch_id).state == "off"

        # Re-enable -> restores the remembered 06:30, not a default.
        await _call(hass, "turn_on", switch_id)
        assert emulator.model.settings[Address.AUTO_ON_HOUR] == 6
        assert emulator.model.settings[Address.AUTO_ON_MINUTE] == 30
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(switch_id).state == "on"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_timer_switch_enable_uses_default_when_unseen(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """A timer that has only ever been disabled turns on to its default time."""
    # Auto-off starts disabled and is never observed enabled.
    emulator.model.settings[Address.AUTO_OFF_HOUR] = TIMER_DISABLED
    emulator.model.settings[Address.AUTO_OFF_MINUTE] = TIMER_DISABLED

    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    switch_id = ent_reg.async_get_entity_id(
        "switch", DOMAIN, f"{entry.unique_id}_auto_off_enabled"
    )
    assert switch_id is not None

    try:
        assert hass.states.get(switch_id).state == "off"
        await _call(hass, "turn_on", switch_id)
        # The auto-off default is 22:00.
        assert emulator.model.settings[Address.AUTO_OFF_HOUR] == 22
        assert emulator.model.settings[Address.AUTO_OFF_MINUTE] == 0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
