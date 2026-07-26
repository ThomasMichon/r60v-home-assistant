"""Tests for the DeviceState store: the single arbiter of state + availability.

The store is transport-agnostic (no MQTT/WS handle); it caches the last-known
snapshot, owns availability with hysteresis, interpolates stale temps, and emits
change events so transports can be mere projections of it.
"""
from __future__ import annotations

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker.store import DeviceState


def _settings(overrides: dict | None = None) -> list[int]:
    s = [0] * p.SETTINGS_LEN
    s[Address.BREW_BOILER_TEMP] = 105
    s[Address.STANDBY] = 0
    for addr, val in (overrides or {}).items():
        s[addr] = val
    return s


def test_change_events_fire_on_update_and_availability():
    store = DeviceState(availability_grace=2)
    events: list[str] = []
    store.subscribe(lambda: events.append("x"))

    store.update_settings(_settings())        # 1 change
    store.update_live(Address.CURRENT_BREW_TEMP, [104])  # 2
    store.note_success()                       # availability None->True: 3
    n_after_success = len(events)
    assert n_after_success == 3

    # Sub-threshold failure: no availability change -> no extra event.
    store.note_failure()
    assert len(events) == n_after_success
    # Threshold failure flips availability -> one event.
    store.note_failure()
    assert len(events) == n_after_success + 1
    assert store.available is False


def test_unsubscribe_stops_events():
    store = DeviceState()
    events: list[str] = []
    unsub = store.subscribe(lambda: events.append("x"))
    store.update_settings(_settings())
    assert len(events) == 1
    unsub()
    store.update_settings(_settings())
    assert len(events) == 1  # no further events after unsubscribe


def test_availability_is_single_source_with_grace():
    store = DeviceState(availability_grace=3)
    assert store.available is True  # None -> treated available (fresh, unproven)
    store.note_success()
    assert store.available is True
    store.note_failure()
    store.note_failure()
    assert store.available is True   # within grace
    store.note_failure()
    assert store.available is False  # third consecutive trips offline
    store.note_success()
    assert store.available is True   # first success restores


def test_raw_snapshot_carries_store_availability():
    store = DeviceState(availability_grace=1)
    store.update_settings(_settings())
    store.update_live(Address.CURRENT_BREW_TEMP, [104])
    store.note_failure()  # grace=1 -> offline immediately
    snap = store.raw_snapshot()
    assert snap["available"] is False
    assert snap["settings"][Address.BREW_BOILER_TEMP] == 105
    assert snap["live"][str(Address.CURRENT_BREW_TEMP)] == [104]


def test_interpolation_eases_toward_setpoint_when_stale_and_on():
    store = DeviceState(stale_after=0.0, interpolate_step=1.0)
    store.update_settings(_settings({Address.BREW_BOILER_TEMP: 110}))
    store.update_live(Address.CURRENT_BREW_TEMP, [100])
    store._last_live_update_at = 0.0  # force staleness
    store.interpolate_live_temps()
    assert store.snapshot.live[Address.CURRENT_BREW_TEMP] == [101]


def test_no_interpolation_when_machine_off():
    store = DeviceState(stale_after=0.0, interpolate_step=1.0)
    store.update_settings(_settings({Address.BREW_BOILER_TEMP: 110, Address.STANDBY: 1}))
    store.update_live(Address.CURRENT_BREW_TEMP, [100])
    store._last_live_update_at = 0.0
    store.interpolate_live_temps()
    assert store.snapshot.live[Address.CURRENT_BREW_TEMP] == [100]
