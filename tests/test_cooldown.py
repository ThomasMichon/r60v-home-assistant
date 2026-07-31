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

    def snap(unit: int, reg_c: int, display: str) -> StateSnapshot:
        settings = [0] * SETTINGS_LEN
        settings[Address.TEMPERATURE_UNIT] = unit
        s = StateSnapshot(settings=settings)
        s.live[Address.CURRENT_BREW_TEMP] = [reg_c]   # register is Celsius
        s.live[Address.DISPLAY] = list(display.encode("ascii"))
        return s

    # Display shows the real 200F while the register is pinned at the 105C
    # setpoint -> the display (200F -> 93C) wins.
    s = snap(1, 105, "BREW BOIL. 200*F")
    assert brew.current_c(s) == round((200 - 32) / 1.8)  # 93
    # On standby the panel shows ECO (no number) -> fall back to the register,
    # which is already Celsius (no conversion).
    s = snap(1, 105, "BREW BOIL. ECO*")
    assert brew.current_c(s) == 105

    # The steam boiler does NOT use the display (its register reads true).
    steam = ent.CLIMATE_BY_KEY["steam_boiler"]
    assert steam.display_current is False


# -- cooldown state machine (unit) ---------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = 0
        # Toggle: when True, gentle probe / reads succeed; when False the
        # machine is "wedged" and every read raises.
        self.read_ok = False

    async def read(self, address: int, length: int):
        from custom_components.rocket_r60v.client import R60VConnectionError

        if not self.read_ok:
            raise R60VConnectionError("wedged")
        return [0] * length

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


async def test_connection_state_reflects_failed_update(hass: HomeAssistant) -> None:
    """A failed update must NOT read as 'connected' -- even in push mode.

    Regression (2026-07-26): in push mode a dropped stream marks the data stale
    via async_set_update_error WITHOUT incrementing _consecutive_failures, so the
    old fallback reported 'connected' while every machine entity was unavailable.
    connection_state must key off last_update_success in both modes.
    """
    coord = _make_coordinator(hass)

    # Healthy: a successful update reads as connected.
    coord.last_update_success = True
    assert coord.connection_state == "connected"

    # Push-mode drop: update failed but the polling failure counter is untouched.
    coord.last_update_success = False
    coord._consecutive_failures = 0
    assert coord.connection_state == "reconnecting"

    # Polling-mode failures read as reconnecting too.
    coord._consecutive_failures = 3
    assert coord.connection_state == "reconnecting"


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


async def test_cooldown_recovers_gently_after_expiry(hass: HomeAssistant) -> None:
    """When the cooldown elapses, recovery is GRADED: a single good probe does
    not resume (it only confirms) -- only a run of RESUME_AFTER_PROBES consecutive
    good probes resumes full polling, so a lone lucky read from a still-marginal
    listener cannot resume cadence and immediately re-wedge. No operator override
    needed."""
    from custom_components.rocket_r60v.coordinator import (
        RESUME_AFTER_PROBES,
        UpdateFailed,
    )

    coord = _make_coordinator(hass)
    coord.data = StateSnapshot()
    coord._consecutive_failures = FAILURE_TOLERANCE + 1
    coord._enter_cooldown()
    assert coord.in_cooldown
    coord._cooldown_until = None  # simulate the cooldown window elapsing

    good = StateSnapshot()

    async def read_snapshot_ok() -> StateSnapshot:
        return good

    coord._read_snapshot = read_snapshot_ok  # type: ignore[method-assign]
    coord.client.read_ok = True  # the gentle probe now succeeds (machine back)

    # Each probe before the last confirms but does NOT resume (no full read yet).
    for i in range(1, RESUME_AFTER_PROBES):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        assert coord._probe_successes == i
        assert coord._cooldown_index > 0  # not yet cleared

    # The confirming run completes -> a normal full read, all state cleared.
    result = await coord._async_update_data()
    assert result is good
    assert not coord.in_cooldown
    assert coord._cooldown_index == 0
    assert coord._consecutive_failures == 0
    assert coord._probe_successes == 0
    assert coord.connection_state != "cooldown"


async def test_confirming_probe_failure_resets_and_extends(hass: HomeAssistant) -> None:
    """A good probe that starts a confirm run, followed by a failed probe, must
    discard the partial run and extend the backoff -- never a premature resume."""
    from custom_components.rocket_r60v.coordinator import (
        RESUME_AFTER_PROBES,
        UpdateFailed,
    )

    if RESUME_AFTER_PROBES < 2:
        pytest.skip("graded resume requires >=2 probes to exercise the reset")

    coord = _make_coordinator(hass)
    coord.data = StateSnapshot()
    coord._consecutive_failures = FAILURE_TOLERANCE + 1
    coord._enter_cooldown()
    idx_after_first = coord._cooldown_index
    coord._cooldown_until = None  # elapse

    good = StateSnapshot()

    async def read_snapshot_ok() -> StateSnapshot:
        return good

    coord._read_snapshot = read_snapshot_ok  # type: ignore[method-assign]

    # One good probe -> confirming (not resumed).
    coord.client.read_ok = True
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._probe_successes == 1

    # Then a failed probe -> re-cool, confirm progress wiped, backoff escalated.
    coord._cooldown_until = None  # elapse again
    coord.client.read_ok = False
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._probe_successes == 0
    assert coord._cooldown_index == idx_after_first + 1
    assert coord.in_cooldown


async def test_cooldown_extends_gently_when_still_wedged(hass: HomeAssistant) -> None:
    """If still wedged when the cooldown elapses, the gentle probe fails and the
    backoff extends by one step -- WITHOUT a full poll (which could re-wedge)."""
    from custom_components.rocket_r60v.coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    coord.data = StateSnapshot()
    coord._consecutive_failures = FAILURE_TOLERANCE + 1
    coord._enter_cooldown()
    idx_after_first = coord._cooldown_index
    coord._cooldown_until = None  # elapse

    snapshot_calls = {"n": 0}

    async def read_snapshot_should_not_run() -> StateSnapshot:
        snapshot_calls["n"] += 1
        return StateSnapshot()

    coord._read_snapshot = read_snapshot_should_not_run  # type: ignore[method-assign]
    coord.client.read_ok = False  # gentle probe fails -> still wedged

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.in_cooldown  # re-cooling
    assert coord._cooldown_index == idx_after_first + 1  # escalated one step
    assert snapshot_calls["n"] == 0  # no full poll attempted on a wedged probe
    assert coord.client.closed >= 1


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
