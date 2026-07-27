"""Tests for the WebSocket push client + coordinator push mode (r60v-local-push).

Exercises the frame-decode path directly (a push frame -> reconstructed
StateSnapshot -> coordinator update) without standing up a real socket server, so
the tests are fast and deterministic. The transport itself is aiohttp's
well-tested WebSocket client.
"""
from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.rocket_r60v.coordinator import R60VCoordinator
from custom_components.rocket_r60v.entities import StateSnapshot
from custom_components.rocket_r60v.protocol import SETTINGS_LEN, Address
from custom_components.rocket_r60v.push_client import R60VPushClient


class _FakeClient:
    connected = False

    async def read(self, address: int, length: int):
        return [0] * length

    async def close(self) -> None:
        self.connected = False


def _make_coordinator(hass: HomeAssistant, *, push_enabled: bool = True):
    coord = R60VCoordinator(hass, _FakeClient(), push_enabled=push_enabled)

    async def _noop_refresh() -> None:
        return None

    coord.async_request_refresh = _noop_refresh  # type: ignore[method-assign]
    return coord


def _push_client(hass, coord):
    return R60VPushClient(hass, coord, "ws://127.0.0.1:8788/")


def test_coordinator_push_mode_disables_polling(hass: HomeAssistant) -> None:
    assert _make_coordinator(hass, push_enabled=True).update_interval is None
    assert _make_coordinator(hass, push_enabled=False).update_interval is not None


def test_push_frame_reconstructs_snapshot_into_coordinator(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)

    settings = [0] * SETTINGS_LEN
    settings[Address.STANDBY] = 0  # machine on
    settings[Address.BREW_BOILER_TEMP] = 105
    frame = {
        "type": "state",
        "schema": 1,
        "available": True,
        "settings": settings,
        "live": {str(Address.CURRENT_BREW_TEMP): [93]},
    }
    pc._handle(json.dumps(frame))

    assert isinstance(coord.data, StateSnapshot)
    assert coord.data.settings[Address.BREW_BOILER_TEMP] == 105
    # live-register keys are reconstructed from their decimal-string form to ints
    assert coord.data.live[Address.CURRENT_BREW_TEMP] == [93]
    assert coord.last_update_success is True


def test_push_frame_unavailable_keeps_transport_up_but_machine_unavailable(
    hass: HomeAssistant,
) -> None:
    """An `available:false` frame keeps the bridge transport 'up'
    (last_update_success stays True) but marks the machine unavailable via the
    snapshot -- so machine entities go unavailable while diagnostics stay live."""
    coord = _make_coordinator(hass)
    coord.async_set_updated_data(StateSnapshot())  # seed a good state
    assert coord.last_update_success is True

    pc = _push_client(hass, coord)
    pc._handle(json.dumps({"type": "state", "available": False, "settings": [], "live": {}}))
    # Transport is still up (frames are flowing); the machine is not reachable.
    assert coord.last_update_success is True
    assert coord.data is not None and coord.data.available is False
    # The Connection diagnostic must NOT claim "connected" while the machine is
    # unavailable.
    assert coord.connection_state == "machine_unavailable"


def test_push_client_ignores_malformed_frames(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)

    # None of these should raise or set data.
    pc._handle("not json at all")
    pc._handle(json.dumps({"type": "hello"}))          # wrong type
    pc._handle(json.dumps({"type": "state", "available": True}))  # no settings
    pc._handle(json.dumps({"type": "state", "available": True, "settings": "nope"}))
    assert coord.data is None


# -- write-intent command channel ----------------------------------------


class _RecordingClient(_FakeClient):
    """A fake client that records direct writes (the fallback / polling path)."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, list[int]]] = []

    async def write(self, address: int, data) -> None:
        self.writes.append((address, list(data)))


class _FakeWs:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)


def _coordinator_with_recording_client(hass, *, push_enabled=True):
    coord = R60VCoordinator(hass, _RecordingClient(), push_enabled=push_enabled)
    calls = {"refresh": 0}

    async def _count_refresh() -> None:
        calls["refresh"] += 1

    coord.async_request_refresh = _count_refresh  # type: ignore[method-assign]
    return coord, calls


async def test_async_send_command_sends_frame(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)
    pc._ws = _FakeWs()
    await pc.async_send_command(Address.BREW_BOILER_TEMP, [110], key="brew_boiler")
    assert pc._ws.sent == [
        {"type": "command", "address": Address.BREW_BOILER_TEMP,
         "data": [110], "key": "brew_boiler"}
    ]


async def test_async_send_command_raises_when_not_connected(hass: HomeAssistant) -> None:
    from custom_components.rocket_r60v.client import R60VConnectionError

    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)
    pc._ws = None
    try:
        await pc.async_send_command(1, [1])
        raised = False
    except R60VConnectionError:
        raised = True
    assert raised


async def test_async_write_push_mode_uses_command_channel(hass: HomeAssistant) -> None:
    coord, calls = _coordinator_with_recording_client(hass, push_enabled=True)
    pc = _push_client(hass, coord)
    pc._ws = _FakeWs()
    coord.attach_push_client(pc)

    await coord.async_write(Address.BREW_BOILER_TEMP, [110], key="brew_boiler")
    # Routed over the command channel; NO direct write and NO refresh.
    assert len(pc._ws.sent) == 1
    assert coord.client.writes == []
    assert calls["refresh"] == 0


async def test_async_write_falls_back_to_front_end_when_stream_down(hass: HomeAssistant) -> None:
    coord, calls = _coordinator_with_recording_client(hass, push_enabled=True)
    pc = _push_client(hass, coord)
    pc._ws = _FakeWs(closed=True)  # stream momentarily down
    coord.attach_push_client(pc)

    await coord.async_write(Address.BREW_BOILER_TEMP, [110])
    # Fell back to the governed front-end write WITHOUT a refresh.
    assert coord.client.writes == [(Address.BREW_BOILER_TEMP, [110])]
    assert calls["refresh"] == 0


async def test_async_write_polling_mode_writes_then_refreshes(hass: HomeAssistant) -> None:
    coord, calls = _coordinator_with_recording_client(hass, push_enabled=False)
    # No push client attached -> polling path.
    await coord.async_write(Address.BREW_BOILER_TEMP, [110])
    assert coord.client.writes == [(Address.BREW_BOILER_TEMP, [110])]
    assert calls["refresh"] == 1


# -- Phase 4: thin push consumer + availability keyed off the store ----------


class _ExplodingClient(_FakeClient):
    """A client whose reads raise -- proves push mode never touches the machine."""

    async def read(self, address: int, length: int):
        raise AssertionError("push-mode coordinator must not read the machine")


async def test_push_mode_update_data_does_no_machine_io(hass: HomeAssistant) -> None:
    """In push mode `_async_update_data` returns a seed snapshot without any
    machine read (the bridge streams state); it never drives the machine."""
    coord = R60VCoordinator(hass, _ExplodingClient(), push_enabled=True)
    snap = await coord._async_update_data()
    assert isinstance(snap, StateSnapshot)
    assert snap.available is False  # unavailable until the first real frame


async def test_available_frame_marks_machine_available(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)
    settings = [0] * SETTINGS_LEN
    settings[Address.STANDBY] = 0
    pc._handle(json.dumps(
        {"type": "state", "available": True, "settings": settings, "live": {}}
    ))
    assert coord.last_update_success is True
    assert coord.data.available is True
    assert coord.connection_state == "connected"


async def test_entity_availability_keys_off_store(hass: HomeAssistant) -> None:
    """A machine entity is available only when the transport is up AND the store
    says the machine is reachable."""
    from custom_components.rocket_r60v.entity import R60VEntity

    coord = _make_coordinator(hass)
    entity = R60VEntity(coord, "uid", "power", "Power")

    # Machine reachable -> available.
    coord.async_set_updated_data(StateSnapshot(available=True))
    assert entity.available is True
    # Machine unreachable (store) but transport up -> unavailable.
    coord.async_set_updated_data(StateSnapshot(available=False))
    assert entity.available is False
    # Transport down -> unavailable regardless.
    coord.async_set_update_error(Exception("stream down"))
    assert entity.available is False


# -- Phase 5: legible 3-signal health taxonomy -------------------------------


async def test_connection_state_machine_unavailable_when_store_says_off(
    hass: HomeAssistant,
) -> None:
    """Transport up + store says machine unavailable -> a distinct, legible
    'machine_unavailable' (never 'connected', never a transport 'reconnecting')."""
    coord = _make_coordinator(hass, push_enabled=True)
    coord.async_set_updated_data(StateSnapshot(available=False))
    assert coord.connection_state == "machine_unavailable"
    coord.async_set_updated_data(StateSnapshot(available=True))
    assert coord.connection_state == "connected"
    # A dropped stream is a transport reconnect, distinct from machine-off.
    coord.async_set_update_error(Exception("stream down"))
    assert coord.connection_state == "reconnecting"


async def test_machine_binary_sensor_reflects_store(hass: HomeAssistant) -> None:
    """The store-owned Machine signal is always visible and tracks availability."""
    from custom_components.rocket_r60v.binary_sensor import (
        R60VMachineConnectivitySensor,
    )

    coord = _make_coordinator(hass, push_enabled=True)
    sensor = R60VMachineConnectivitySensor(coord, "uid")
    assert sensor.available is True  # visible precisely when the machine is not
    coord.async_set_updated_data(StateSnapshot(available=True))
    assert sensor.is_on is True
    coord.async_set_updated_data(StateSnapshot(available=False))
    assert sensor.is_on is False


async def test_async_refresh_bridge_health_updates_and_notifies(
    hass: HomeAssistant,
) -> None:
    """The push-mode periodic refresh updates the cached health and re-renders."""
    from custom_components.rocket_r60v.bridge_health import BridgeHealth

    async def fake_fetch(url: str) -> BridgeHealth:
        return BridgeHealth(available=True, schema=1, link_up=True)

    coord = R60VCoordinator(
        hass,
        _FakeClient(),
        push_enabled=True,
        bridge_health_url="http://bridge/health",
        health_fetcher=fake_fetch,
    )
    notified: list[int] = []
    coord.async_add_listener(lambda: notified.append(1))

    await coord.async_refresh_bridge_health()
    assert coord.bridge_health is not None and coord.bridge_health.link_up is True
    assert notified  # diagnostics were re-rendered
