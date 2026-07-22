"""Tests for the display-text brew temperature (#1 workaround) and the
wedge-recovery cooldown state machine + diagnostic entities."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rocket_r60v import entities as ent
from custom_components.rocket_r60v.coordinator import (
    COOLDOWN_STEPS,
    FAILURE_TOLERANCE,
)
from custom_components.rocket_r60v.entities import (
    StateSnapshot,
    parse_display_brew_temp,
)
from custom_components.rocket_r60v.protocol import SETTINGS_LEN, Address
from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30


# -- display-text brew temperature (#1) ----------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BREW BOIL. 221*F", (221, True)),
        ("BREW BOIL. 105*C", (105, False)),
        ("BREW BOIL. ECO*", None),
        ("", None),
        ("SERV. BOIL. 250*F", None),
    ],
)
def test_parse_display_brew_temp(text, expected) -> None:
    assert parse_display_brew_temp(text) == expected


def test_brew_current_prefers_display() -> None:
    """The brew boiler uses the display temp when present, else the register."""
    brew = ent.CLIMATE_BY_KEY["brew_boiler"]

    def snap(unit: int, reg: int, display: str) -> StateSnapshot:
        settings = [0] * SETTINGS_LEN
        settings[Address.TEMPERATURE_UNIT] = unit
        s = StateSnapshot(settings=settings)
        s.live[Address.CURRENT_BREW_TEMP] = [reg]
        s.live[Address.DISPLAY] = list(display.encode("ascii"))
        return s

    # Machine in F, display shows the real 200F while the register is pinned at
    # the 221F setpoint -> the display (200F=93C) wins.
    s = snap(1, 221, "BREW BOIL. 200*F")
    assert brew.current_c(s) == round((200 - 32) / 1.8)  # 93
    # On standby the panel shows ECO (no number) -> fall back to the register.
    s = snap(1, 221, "BREW BOIL. ECO*")
    assert brew.current_c(s) == round((221 - 32) / 1.8)  # 105

    # The steam boiler does NOT use the display (its register reads true).
    steam = ent.CLIMATE_BY_KEY["steam_boiler"]
    assert steam.display_current is False


# -- cooldown state machine (unit) ---------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = 0

    async def close(self) -> None:
        self.connected = False
        self.closed += 1


def _make_coordinator(hass: HomeAssistant):
    from custom_components.rocket_r60v.coordinator import R60VCoordinator

    coord = R60VCoordinator(hass, _FakeClient())

    async def _noop_refresh() -> None:
        return None

    # Avoid scheduling a real debounced refresh (leaves a lingering timer in a
    # bare-coordinator unit test); the state transitions are what we assert.
    coord.async_request_refresh = _noop_refresh  # type: ignore[method-assign]
    return coord


async def test_cooldown_enters_and_overrides(hass: HomeAssistant) -> None:
    """Sustained failures after a good read trigger a cooldown; the override
    (End Cooldown) clears it."""
    from custom_components.rocket_r60v.coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    # Pretend we've loaded once (so a wedge triggers cooldown, not setup retry).
    coord.data = StateSnapshot()
    coord.last_update_success = True

    # Force _read_snapshot to always fail.
    from custom_components.rocket_r60v.client import R60VConnectionError

    async def fail() -> StateSnapshot:
        raise R60VConnectionError("wedged")

    coord._read_snapshot = fail  # type: ignore[method-assign]

    # Burn through the tolerance (cached served), then one more to enter cooldown.
    for _ in range(FAILURE_TOLERANCE):
        assert await coord._async_update_data() is coord.data  # served cached
    assert not coord.in_cooldown
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()  # tolerance exceeded -> cooldown
    assert coord.in_cooldown
    assert coord.connection_state == "cooldown"
    assert coord.cooldown_remaining > 0
    assert coord.client.closed >= 1  # slot freed

    # While cooling down, no device read is attempted.
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    # Override ends the cooldown.
    await coord.async_end_cooldown()
    assert not coord.in_cooldown
    assert coord.connection_state in ("connected", "reconnecting")


async def test_cooldown_backoff_lengthens(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    coord.data = StateSnapshot()
    coord._consecutive_failures = FAILURE_TOLERANCE + 1
    coord._enter_cooldown()
    first = coord.cooldown_remaining
    coord._cooldown_until = None  # simulate expiry
    coord._enter_cooldown()
    second = coord.cooldown_remaining
    assert second >= first
    assert first <= COOLDOWN_STEPS[0].total_seconds()


# -- diagnostic entities (behavioral) ------------------------------------


@pytest.fixture
async def emulator(socket_enabled):
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        yield emu
    finally:
        await emu.stop()


async def test_connection_sensor_and_button_stay_available(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """The Connection sensor and End Cooldown button remain available even when
    the device (and thus the machine entities) are not."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: emulator.bound_port},
        unique_id=f"127.0.0.1:{emulator.bound_port}",
    )
    entry.add_to_hass(hass)
    async with asyncio.timeout(SETUP_TIMEOUT):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    ent_reg = er.async_get(hass)
    conn_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_connection")
    btn_id = ent_reg.async_get_entity_id("button", DOMAIN, f"{entry.unique_id}_end_cooldown")
    try:
        assert hass.states.get(conn_id).state == "connected"

        # Kill the device; a machine sensor goes unavailable but the diagnostic
        # Connection sensor + button stay available.
        await emulator.stop()
        coordinator = entry.runtime_data.coordinator
        for _ in range(FAILURE_TOLERANCE + 2):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        display_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_display")
        assert hass.states.get(display_id).state == "unavailable"
        assert hass.states.get(conn_id).state == "cooldown"
        assert hass.states.get(btn_id).state != "unavailable"

        # Press the override; the cooldown clears.
        await hass.services.async_call(
            "button", "press", {"entity_id": btn_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert coordinator.in_cooldown is False
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
