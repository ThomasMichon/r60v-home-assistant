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


def test_push_frame_unavailable_marks_coordinator_failed(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    coord.async_set_updated_data(StateSnapshot())  # seed a good state
    assert coord.last_update_success is True

    pc = _push_client(hass, coord)
    pc._handle(json.dumps({"type": "state", "available": False, "settings": [], "live": {}}))
    assert coord.last_update_success is False


def test_push_client_ignores_malformed_frames(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass)
    pc = _push_client(hass, coord)

    # None of these should raise or set data.
    pc._handle("not json at all")
    pc._handle(json.dumps({"type": "hello"}))          # wrong type
    pc._handle(json.dumps({"type": "state", "available": True}))  # no settings
    pc._handle(json.dumps({"type": "state", "available": True, "settings": "nope"}))
    assert coord.data is None
