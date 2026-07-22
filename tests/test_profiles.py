"""Tests for the editable pressure-profile feature (#6)."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v.entities import decode_profile, encode_profile
from custom_components.rocket_r60v.protocol import PROFILE_LEN, Address
from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30


# -- pure codec ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["3:3 6:6 25:9 0:9 0:6", "5:9", "", "10.5:8.5 20:9"],
)
def test_profile_round_trip(text: str) -> None:
    block = encode_profile(text)
    assert len(block) == PROFILE_LEN
    # A full 5-step decode; a partial input is zero-filled, so re-encode of the
    # decoded (full) form is stable.
    decoded = decode_profile(block)
    assert encode_profile(decoded) == block


def test_profile_decode_known_block() -> None:
    # step0 = 3.0s@3.0bar, step1 = 25.0s@9.0bar; rest zero.
    block = [30, 0, 250, 0, 0, 0, 0, 0, 0, 0, 30, 90, 0, 0, 0]
    assert decode_profile(block) == "3:3 25:9 0:0 0:0 0:0"


def test_profile_encode_zero_fills() -> None:
    assert encode_profile("5:9") == [50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0, 0, 0, 0]


@pytest.mark.parametrize("bad", ["70:5", "5:12", "abc", "5:5:5", "1:1 2:2 3:3 4:4 5:5 6:6"])
def test_profile_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        encode_profile(bad)


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


async def test_profile_text_write_round_trip(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """Setting the profile text writes the 15-byte block and reads it back."""
    entry = await _setup_against(hass, emulator.bound_port)
    ent_reg = er.async_get(hass)
    coordinator = entry.runtime_data.coordinator
    text_id = ent_reg.async_get_entity_id("text", DOMAIN, f"{entry.unique_id}_profile_a")
    assert text_id is not None
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            await hass.services.async_call(
                "text", "set_value",
                {"entity_id": text_id, "value": "3:3 25:9"},
                blocking=True,
            )
            await hass.async_block_till_done()
        # The emulator stored the encoded block at PROFILE_A.
        block = list(emulator.model.settings[Address.PROFILE_A:Address.PROFILE_A + PROFILE_LEN])
        assert block == encode_profile("3:3 25:9")

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(text_id).state == "3:3 25:9 0:0 0:0 0:0"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
