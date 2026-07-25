"""Tests for the bridge-health back-channel: parsing, the fetch client, and the
coordinator's pause-on-bridge-outage behavior (distinct from a machine wedge)."""
from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.rocket_r60v.bridge_health import (
    BridgeHealth,
    async_fetch_bridge_health,
)
from custom_components.rocket_r60v.coordinator import R60VCoordinator, UpdateFailed
from custom_components.rocket_r60v.entities import StateSnapshot

HEALTH_URL = "http://bridge.local:8787/health"


# -- BridgeHealth parsing / semantics ------------------------------------


def _healthy(**over) -> dict:
    base = {
        "schema": 1,
        "available": True,
        "link_up": True,
        "ssid": "RocketEspresso",
        "signal": 62,
        "ip": "192.168.1.12",
        "machine_reachable": True,
        "ap_visible": True,
        "machine_powered_off": False,
        "diagnostic_window": False,
        "link_uptime_s": 120,
        "recoveries_total": 3,
        "last_recovery": "2026-07-24T20:29:00Z",
        "stale": False,
        "updated": "2026-07-24T20:35:00Z",
    }
    base.update(over)
    return base


def test_from_json_parses_fields() -> None:
    h = BridgeHealth.from_json(_healthy())
    assert h.available and h.link_up and h.schema == 1
    assert h.signal == 62 and h.recoveries_total == 3
    assert h.last_recovery == datetime(2026, 7, 24, 20, 29, tzinfo=timezone.utc)
    assert h.usable and not h.blocking


def test_blocking_when_link_down() -> None:
    assert BridgeHealth.from_json(_healthy(link_up=False)).blocking is True


def test_blocking_when_diagnostic_window() -> None:
    assert BridgeHealth.from_json(_healthy(diagnostic_window=True)).blocking is True


def test_not_blocking_when_stale() -> None:
    # A stale snapshot is not trustworthy -> do not act on it.
    h = BridgeHealth.from_json(_healthy(link_up=False, stale=True))
    assert h.usable is False
    assert h.blocking is False


def test_not_blocking_on_unknown_schema() -> None:
    h = BridgeHealth.from_json(_healthy(link_up=False, schema=99))
    assert h.blocking is False


def test_unavailable_payload_not_blocking() -> None:
    h = BridgeHealth.from_json({"available": False, "link_up": False, "stale": True})
    assert h.blocking is False


def test_bool_not_parsed_as_signal_int() -> None:
    # A stray bool must not be coerced into an int field.
    assert BridgeHealth.from_json(_healthy(signal=True)).signal is None


# -- async_fetch_bridge_health -------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload, *, raise_json=False) -> None:
        self.status = status
        self._payload = payload
        self._raise_json = raise_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self, resp=None, exc=None) -> None:
        self._resp = resp
        self._exc = exc

    def get(self, url, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


async def test_fetch_ok() -> None:
    session = _FakeSession(_FakeResp(200, _healthy()))
    h = await async_fetch_bridge_health(session, HEALTH_URL)
    assert h is not None and h.link_up


async def test_fetch_non_200_returns_none() -> None:
    session = _FakeSession(_FakeResp(503, {}))
    assert await async_fetch_bridge_health(session, HEALTH_URL) is None


async def test_fetch_transport_error_returns_none() -> None:
    session = _FakeSession(exc=aiohttp.ClientError("boom"))
    assert await async_fetch_bridge_health(session, HEALTH_URL) is None


async def test_fetch_bad_json_returns_none() -> None:
    session = _FakeSession(_FakeResp(200, None, raise_json=True))
    assert await async_fetch_bridge_health(session, HEALTH_URL) is None


async def test_fetch_non_dict_returns_none() -> None:
    session = _FakeSession(_FakeResp(200, ["not", "a", "dict"]))
    assert await async_fetch_bridge_health(session, HEALTH_URL) is None


# -- coordinator pause-on-bridge-outage ----------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = 0

    async def close(self) -> None:
        self.connected = False
        self.closed += 1


def _coord(hass: HomeAssistant, health: BridgeHealth | None) -> R60VCoordinator:
    async def fetcher(url: str):
        return health

    coord = R60VCoordinator(
        hass,
        _FakeClient(),
        bridge_health_url=HEALTH_URL,
        health_fetcher=fetcher,
    )

    async def _noop_refresh() -> None:
        return None

    coord.async_request_refresh = _noop_refresh  # type: ignore[method-assign]
    return coord


async def test_bridge_down_skips_machine_and_no_cooldown(hass: HomeAssistant) -> None:
    coord = _coord(hass, BridgeHealth.from_json(_healthy(link_up=False)))
    coord.data = StateSnapshot()
    coord.last_update_success = True
    coord.client.connected = True

    read_calls = 0

    async def _read() -> StateSnapshot:
        nonlocal read_calls
        read_calls += 1
        return StateSnapshot()

    coord._read_snapshot = _read  # type: ignore[method-assign]

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert read_calls == 0                       # machine was NOT polled
    assert coord.consecutive_failures == 0       # not counted as a machine wedge
    assert coord.in_cooldown is False
    assert coord.connection_state == "bridge_down"
    assert coord.client.closed >= 1              # slot freed


async def test_bridge_recovering_state(hass: HomeAssistant) -> None:
    coord = _coord(hass, BridgeHealth.from_json(_healthy(diagnostic_window=True)))
    coord.data = StateSnapshot()
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.connection_state == "bridge_recovering"


async def test_healthy_bridge_polls_machine(hass: HomeAssistant) -> None:
    coord = _coord(hass, BridgeHealth.from_json(_healthy()))
    snap = StateSnapshot()

    async def _read() -> StateSnapshot:
        return snap

    coord._read_snapshot = _read  # type: ignore[method-assign]
    assert await coord._async_update_data() is snap
    assert coord.connection_state == "connected"


async def test_stale_bridge_does_not_block(hass: HomeAssistant) -> None:
    # Bridge reports link down but stale -> we must still try the machine.
    coord = _coord(hass, BridgeHealth.from_json(_healthy(link_up=False, stale=True)))
    read_calls = 0

    async def _read() -> StateSnapshot:
        nonlocal read_calls
        read_calls += 1
        return StateSnapshot()

    coord._read_snapshot = _read  # type: ignore[method-assign]
    await coord._async_update_data()
    assert read_calls == 1


async def test_fetcher_exception_does_not_block(hass: HomeAssistant) -> None:
    async def boom(url: str):
        raise RuntimeError("health endpoint exploded")

    coord = R60VCoordinator(
        hass, _FakeClient(), bridge_health_url=HEALTH_URL, health_fetcher=boom
    )

    async def _noop() -> None:
        return None

    coord.async_request_refresh = _noop  # type: ignore[method-assign]

    read_calls = 0

    async def _read() -> StateSnapshot:
        nonlocal read_calls
        read_calls += 1
        return StateSnapshot()

    coord._read_snapshot = _read  # type: ignore[method-assign]
    await coord._async_update_data()
    assert read_calls == 1               # back-channel failure never blocks polling
    assert coord.bridge_health is None


async def test_disabled_bridge_polls_normally(hass: HomeAssistant) -> None:
    # No bridge_health_url -> back-channel inert; behaves as before.
    coord = R60VCoordinator(hass, _FakeClient())
    assert coord.bridge_health_enabled is False

    async def _noop() -> None:
        return None

    coord.async_request_refresh = _noop  # type: ignore[method-assign]

    snap = StateSnapshot()

    async def _read() -> StateSnapshot:
        return snap

    coord._read_snapshot = _read  # type: ignore[method-assign]
    assert await coord._async_update_data() is snap
