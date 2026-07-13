"""Tests for the R60V state decode/encode layer."""
from __future__ import annotations

import pytest

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker import state
from r60v_broker.state import ENTITIES_BY_KEY, StateSnapshot, le16


def _snapshot() -> StateSnapshot:
    settings = [0] * p.SETTINGS_LEN
    settings[Address.BREW_BOILER_TEMP] = 105
    settings[Address.SERVICE_BOILER_TEMP] = 124
    settings[Address.GROUP_TEMP] = 95
    settings[Address.STANDBY] = 0            # machine ON
    settings[Address.SERVICE_BOILER_ENABLE] = 1
    settings[Address.ACTIVE_PROFILE] = 2     # profile C
    settings[Address.WATER_FEED] = 1         # mains
    settings[Address.TEMPERATURE_UNIT] = 1   # fahrenheit
    settings[Address.AUTO_ON_HOUR] = 8
    # Total coffee count = 6967 (0x1B37), little-endian.
    settings[Address.TOTAL_COFFEE_COUNT] = 0x37
    settings[Address.TOTAL_COFFEE_COUNT + 1] = 0x1B
    live = {
        Address.CURRENT_BREW_TEMP: [105],
        Address.CURRENT_SERVICE_TEMP: [124],
        Address.CURRENT_PRESSURE: [0],
        Address.DISPLAY: list(b"BREW BOIL. 221*F"),
    }
    return StateSnapshot(settings=settings, live=live)


def test_le16_little_endian():
    assert le16([0x37, 0x1B], 0) == 6967


def test_decode_setpoints_and_counters():
    s = _snapshot()
    assert ENTITIES_BY_KEY["total_coffee_count"].decode(s) == 6967


def test_display_decode():
    s = _snapshot()
    assert ENTITIES_BY_KEY["display"].decode(s) == "BREW BOIL. 221*F"


def test_climate_thermostats_decode_current_and_target():
    s = _snapshot()
    brew = state.CLIMATE_BY_KEY["brew_boiler"]
    assert brew.current(s) == 105       # live register
    assert brew.target(s) == 105        # setpoint byte
    steam = state.CLIMATE_BY_KEY["steam_boiler"]
    assert steam.current(s) == 124
    assert steam.target(s) == 124


def test_climate_target_encode_range_validated():
    brew = state.CLIMATE_BY_KEY["brew_boiler"]
    assert brew.encode_target("110") == (Address.BREW_BOILER_TEMP, [110])
    with pytest.raises(ValueError):
        brew.encode_target("200")   # above 115 C
    with pytest.raises(ValueError):
        brew.encode_target("50")    # below 85 C


def test_auto_time_decode_and_encode():
    s = _snapshot()
    assert ENTITIES_BY_KEY["auto_on"].decode(s) == "08:00"
    # Encode writes hour+minute as one 2-byte write at the hour address.
    assert ENTITIES_BY_KEY["auto_on"].encode("07:30") == (Address.AUTO_ON_HOUR, [7, 30])
    with pytest.raises(ValueError):
        ENTITIES_BY_KEY["auto_on"].encode("25:00")   # bad hour
    with pytest.raises(ValueError):
        ENTITIES_BY_KEY["auto_on"].encode("7h30")    # bad format


def test_decode_switch_and_selects():
    s = _snapshot()
    assert ENTITIES_BY_KEY["power"].decode(s) == "ON"          # standby 0 -> on
    assert ENTITIES_BY_KEY["service_boiler"].decode(s) == "ON"
    assert ENTITIES_BY_KEY["active_profile"].decode(s) == "C"
    assert ENTITIES_BY_KEY["water_feed"].decode(s) == "mains"
    assert ENTITIES_BY_KEY["temperature_unit"].decode(s) == "fahrenheit"


def test_power_off_means_standby_byte_one():
    entity = ENTITIES_BY_KEY["power"]
    assert entity.encode("OFF") == (Address.STANDBY, [1])
    assert entity.encode("ON") == (Address.STANDBY, [0])


def test_encode_enum_roundtrip():
    entity = ENTITIES_BY_KEY["active_profile"]
    assert entity.encode("A") == (Address.ACTIVE_PROFILE, [0])
    assert entity.encode("C") == (Address.ACTIVE_PROFILE, [2])
    with pytest.raises(ValueError):
        entity.encode("Z")


def test_live_registers_are_single_register_reads():
    # The real machine only answers individual live reads; the map must not
    # request a multi-register range.
    assert state.LIVE_REGISTERS[Address.CURRENT_BREW_TEMP] == 1
    assert state.LIVE_REGISTERS[Address.CURRENT_SERVICE_TEMP] == 1

