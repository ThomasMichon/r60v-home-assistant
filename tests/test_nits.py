"""Tests for the batch-1 post-deploy fixes (temp unit, heat mode, icons,
pressure scale, clock sync)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from homeassistant.components.climate import HVACAction
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v import entities as ent
from custom_components.rocket_r60v.clock import build_clock_payload
from custom_components.rocket_r60v.entities import StateSnapshot
from custom_components.rocket_r60v.protocol import (
    Address,
    BREW_TEMP_RANGE_C,
    BREW_TEMP_RANGE_F,
    SETTINGS_LEN,
)
from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30


# --------------------------------------------------------------------------
# Pure-logic tests (no HA harness needed for the maths)
# --------------------------------------------------------------------------


def _snapshot(settings: dict[int, int]) -> StateSnapshot:
    data = [0] * SETTINGS_LEN
    for addr, val in settings.items():
        data[addr] = val
    return StateSnapshot(settings=data)


def test_pressure_decibar_scaled() -> None:
    """0xB002 is decibar: raw 90 decodes to 9.0 bar, not 90."""
    decode = ent._live_decibar(Address.CURRENT_PRESSURE)
    snap = StateSnapshot(live={Address.CURRENT_PRESSURE: [90]})
    assert decode(snap) == 9.0
    assert ent._live_decibar(Address.CURRENT_PRESSURE)(StateSnapshot()) == 0.0


def test_select_icon_reflects_value() -> None:
    """Temperature Unit and Water Source icons follow the chosen value."""
    temp_unit = ent.ENTITIES_BY_KEY["temperature_unit"]
    assert temp_unit.icon_for("celsius") == "mdi:temperature-celsius"
    assert temp_unit.icon_for("fahrenheit") == "mdi:temperature-fahrenheit"
    water = ent.ENTITIES_BY_KEY["water_feed"]
    assert water.icon_for("tank") == "mdi:cup-water"
    assert water.icon_for("mains") == "mdi:pipe-valve"
    # A select without an icon map falls back to its base icon.
    assert ent.ENTITIES_BY_KEY["language"].icon_for("english") == "mdi:translate"


def test_climate_is_on_predicates() -> None:
    """Brew tracks standby; steam needs both machine-on and steam-enabled."""
    brew = ent.CLIMATE_BY_KEY["brew_boiler"]
    steam = ent.CLIMATE_BY_KEY["steam_boiler"]
    running = _snapshot({Address.STANDBY: 0, Address.SERVICE_BOILER_ENABLE: 1})
    standby = _snapshot({Address.STANDBY: 1, Address.SERVICE_BOILER_ENABLE: 1})
    steam_off = _snapshot({Address.STANDBY: 0, Address.SERVICE_BOILER_ENABLE: 0})
    assert brew.is_on(running) is True
    assert brew.is_on(standby) is False
    assert steam.is_on(running) is True
    assert steam.is_on(standby) is False
    assert steam.is_on(steam_off) is False


def test_encode_setpoint_honors_unit_range() -> None:
    """Setpoint validation uses the range for the active display unit."""
    brew = ent.CLIMATE_BY_KEY["brew_boiler"]
    # Celsius: 105 valid, 221 rejected.
    assert brew.encode_setpoint(105, fahrenheit=False) == (Address.BREW_BOILER_TEMP, [105])
    with pytest.raises(ValueError):
        brew.encode_setpoint(BREW_TEMP_RANGE_F[1], fahrenheit=False)
    # Fahrenheit: 221 valid, 105 rejected.
    assert brew.encode_setpoint(221, fahrenheit=True) == (Address.BREW_BOILER_TEMP, [221])
    with pytest.raises(ValueError):
        brew.encode_setpoint(BREW_TEMP_RANGE_C[0], fahrenheit=True)


def test_is_fahrenheit() -> None:
    assert ent.is_fahrenheit(_snapshot({Address.TEMPERATURE_UNIT: 1})) is True
    assert ent.is_fahrenheit(_snapshot({Address.TEMPERATURE_UNIT: 0})) is False


def test_build_clock_payload() -> None:
    """The clock payload matches the app's [0, min, hour, wday, day, mon, yy]."""
    # 2026-07-20 is a Monday (weekday()==0 -> machine 1).
    dt = datetime(2026, 7, 20, 9, 41)
    assert build_clock_payload(dt) == [0, 41, 9, 1, 20, 7, 26]
    # Sunday -> machine 7.
    assert build_clock_payload(datetime(2026, 7, 26, 0, 0))[3] == 7


# --------------------------------------------------------------------------
# Behavioral tests (through the real config-entry + emulator)
# --------------------------------------------------------------------------


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


def _uid(entry: MockConfigEntry, suffix: str) -> str:
    return f"{entry.unique_id}_{suffix}"


async def test_climate_reports_heat_only_when_on(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """Brew thermostat is 'heat' when running, 'off' on standby."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    brew_id = ent_reg.async_get_entity_id("climate", DOMAIN, _uid(entry, "brew_boiler"))
    coordinator = entry.runtime_data.coordinator
    try:
        # Default emulator STANDBY=0 -> running -> heat.
        assert hass.states.get(brew_id).state == "heat"
        assert hass.states.get(brew_id).attributes["hvac_action"] == HVACAction.HEATING

        # Put the machine on standby -> off / idle.
        emulator.model.settings[Address.STANDBY] = 1
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(brew_id).state == "off"
        assert hass.states.get(brew_id).attributes["hvac_action"] == HVACAction.OFF
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_steam_off_when_disabled(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """Steam thermostat is off when the steam boiler is disabled, even running."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    steam_id = ent_reg.async_get_entity_id("climate", DOMAIN, _uid(entry, "steam_boiler"))
    coordinator = entry.runtime_data.coordinator
    try:
        assert hass.states.get(steam_id).state == "heat"  # default enabled
        emulator.model.settings[Address.SERVICE_BOILER_ENABLE] = 0
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(steam_id).state == "off"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_select_icon_state_reflects_value(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """The Temperature Unit / Water Source select icons track the value."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    tu_id = ent_reg.async_get_entity_id("select", DOMAIN, _uid(entry, "temperature_unit"))
    wf_id = ent_reg.async_get_entity_id("select", DOMAIN, _uid(entry, "water_feed"))
    coordinator = entry.runtime_data.coordinator
    try:
        # Emulator defaults: unit=celsius (0), water=tank (0).
        assert hass.states.get(tu_id).attributes["icon"] == "mdi:temperature-celsius"
        assert hass.states.get(wf_id).attributes["icon"] == "mdi:cup-water"
        # Flip to fahrenheit + mains.
        emulator.model.settings[Address.TEMPERATURE_UNIT] = 1
        emulator.model.settings[Address.WATER_FEED] = 1
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(tu_id).attributes["icon"] == "mdi:temperature-fahrenheit"
        assert hass.states.get(wf_id).attributes["icon"] == "mdi:pipe-valve"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
