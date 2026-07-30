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


def test_note_wedged_forces_offline_before_grace_trips():
    """A wedge cooldown is a definitive unreachable verdict: it must force the
    store offline even when the grace counter has not yet reached its threshold.

    This is the flapping-prevention over-correction fix -- otherwise, if the
    (time-based) wedge fires before the (count-based) grace trips and the cooldown
    then stops polling, the store would freeze at the last-known ``available``
    verdict and serve stale state for the whole cooldown.
    """
    store = DeviceState(availability_grace=4)
    store.update_settings(_settings())
    store.note_success()
    assert store.available is True

    # Only a couple of failures so far -- below the grace threshold, so the
    # grace counter alone would keep the store "available" (last-known).
    store.note_failure()
    store.note_failure()
    assert store.available is True

    # Entering a wedge cooldown forces offline regardless of the grace count.
    store.note_wedged()
    assert store.available is False

    # A subsequent good read brings it back online.
    store.note_success()
    assert store.available is True


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


# -- optimistic write overlay --------------------------------------------


def test_apply_optimistic_patches_and_notifies():
    store = DeviceState()
    store.update_settings(_settings())
    events: list[str] = []
    store.subscribe(lambda: events.append("x"))
    assert store.apply_optimistic(Address.BREW_BOILER_TEMP, [110]) is True
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
    assert len(events) == 1
    # Availability is untouched -- an intent is not a reading.
    assert store.available is True


def test_apply_optimistic_survives_a_stale_poll():
    """A poll that reads pre-command state must not revert the optimistic value."""
    store = DeviceState()
    store.update_settings(_settings())  # brew=105
    store.apply_optimistic(Address.BREW_BOILER_TEMP, [110])
    # A stale authoritative read arrives carrying the OLD value.
    store.update_settings(_settings())  # brew=105 again
    # The overlay re-applies on top -> HA still sees the intended 110.
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
    assert store.raw_snapshot()["settings"][Address.BREW_BOILER_TEMP] == 110


def test_reconcile_clear_lets_authoritative_truth_show():
    store = DeviceState()
    store.update_settings(_settings())
    store.apply_optimistic(Address.BREW_BOILER_TEMP, [110])
    store.reconcile_clear({Address.BREW_BOILER_TEMP: 110})
    # After clearing, an authoritative read is no longer masked by the overlay.
    store.update_settings(_settings({Address.BREW_BOILER_TEMP: 108}))
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 108


def test_apply_optimistic_rejected_without_baseline_or_out_of_range():
    store = DeviceState()
    # No baseline snapshot yet.
    assert store.apply_optimistic(Address.BREW_BOILER_TEMP, [110]) is False
    store.update_settings(_settings())
    # Out of the settings block.
    assert store.apply_optimistic(p.SETTINGS_LEN, [1]) is False
    # Bad byte / empty data.
    assert store.apply_optimistic(Address.BREW_BOILER_TEMP, [999]) is False
    assert store.apply_optimistic(Address.BREW_BOILER_TEMP, []) is False


def test_two_optimistic_writes_keep_independent_bytes():
    store = DeviceState()
    store.update_settings(_settings())
    store.apply_optimistic(Address.BREW_BOILER_TEMP, [110])
    store.apply_optimistic(Address.STANDBY, [1])
    # A stale poll re-applies BOTH overlays.
    store.update_settings(_settings())
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
    assert store.snapshot.settings[Address.STANDBY] == 1
    # Clearing one leaves the other overlaid.
    store.reconcile_clear({Address.BREW_BOILER_TEMP: 110})
    store.update_settings(_settings())
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 105
    assert store.snapshot.settings[Address.STANDBY] == 1


def test_reconcile_clear_preserves_a_newer_same_address_write():
    """A slow command's reconcile must not yank back a newer command's intent."""
    store = DeviceState()
    store.update_settings(_settings())
    store.apply_optimistic(Address.BREW_BOILER_TEMP, [110])  # command A
    store.apply_optimistic(Address.BREW_BOILER_TEMP, [112])  # command B supersedes A
    # A completes and clears its overlay (value 110) -- but B's 112 must stand.
    store.reconcile_clear({Address.BREW_BOILER_TEMP: 110})
    store.update_settings(_settings())  # A's reconcile read carries the old 110
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 112  # latest intent kept
    # B completes -> clears its own overlay; authoritative truth now shows.
    store.reconcile_clear({Address.BREW_BOILER_TEMP: 112})
    store.update_settings(_settings({Address.BREW_BOILER_TEMP: 112}))
    assert store.snapshot.settings[Address.BREW_BOILER_TEMP] == 112
