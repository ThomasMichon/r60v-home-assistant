"""Tests for the auto-clock-sync enable/disable option.

The integration keeps the machine's onboard clock on local time by writing the
date/time at setup and daily; each write touches the machine. This option lets
the user turn that off so the integration never writes the clock on its own.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v.const import (
    CONF_CLOCK_SYNC,
    DEFAULT_CLOCK_SYNC,
    clock_sync_enabled,
)
from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30


@pytest.fixture
async def emulator(socket_enabled):
    """Run the R60V wire emulator on an ephemeral loopback port for one test."""
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        yield emu
    finally:
        await emu.stop()


# -- pure resolver -------------------------------------------------------


def test_clock_sync_default_when_unset() -> None:
    assert clock_sync_enabled({}, {}) is DEFAULT_CLOCK_SYNC


def test_clock_sync_explicit_false_is_honoured() -> None:
    # A boolean False must NOT fall through to the default (the reason the
    # resolver checks key presence rather than truthiness).
    assert clock_sync_enabled({}, {CONF_CLOCK_SYNC: False}) is False


def test_clock_sync_options_override_data() -> None:
    assert clock_sync_enabled({CONF_CLOCK_SYNC: True}, {CONF_CLOCK_SYNC: False}) is True
    assert clock_sync_enabled({CONF_CLOCK_SYNC: False}, {CONF_CLOCK_SYNC: True}) is False


# -- setup wiring --------------------------------------------------------


async def _setup(hass: HomeAssistant, port: int, data_extra: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port, **(data_extra or {})},
        unique_id=f"127.0.0.1:{port}",
    )
    entry.add_to_hass(hass)
    async with asyncio.timeout(SETUP_TIMEOUT):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_clock_sync_runs_by_default(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """With no option set, the integration wires up the clock sync as before."""
    with patch("custom_components.rocket_r60v.async_setup_clock_sync") as setup_clock:
        setup_clock.return_value = lambda: None
        entry = await _setup(hass, emulator.bound_port)
    assert setup_clock.called
    assert entry.runtime_data.clock_unsub is not None


async def test_clock_sync_skipped_when_disabled(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """With clock_sync=False, the integration never wires the clock writer."""
    with patch("custom_components.rocket_r60v.async_setup_clock_sync") as setup_clock:
        entry = await _setup(
            hass, emulator.bound_port, {CONF_CLOCK_SYNC: False}
        )
    assert not setup_clock.called
    assert entry.runtime_data.clock_unsub is None
