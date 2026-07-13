"""Tests for the StatePublisher (continuity, availability grace, interpolation)."""
from __future__ import annotations

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker.config import Config
from r60v_broker.publisher import StatePublisher


class FakeMqtt:
    def __init__(self):
        self.states: dict[str, str] = {}
        self.climate: dict[str, dict] = {}
        self.available = None

    def publish_availability(self, online):
        self.available = online

    def publish_state(self, key, value):
        self.states[key] = str(value)

    def publish_climate(self, key, current, target, mode="heat"):
        self.climate[key] = {"current": str(current), "target": str(target), "mode": mode}


def _settings(overrides: dict | None = None) -> list[int]:
    s = [0] * p.SETTINGS_LEN
    s[Address.BREW_BOILER_TEMP] = 105
    s[Address.STANDBY] = 0
    for addr, val in (overrides or {}).items():
        s[addr] = val
    return s


def test_continuity_holds_last_value_without_new_reads():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake)
    pub.update_settings(_settings())
    pub.update_live(Address.CURRENT_BREW_TEMP, [104])
    pub.publish()
    assert fake.climate["brew_boiler"]["current"] == "104"
    fake.climate.clear()
    # No new device reads -- publishing again still emits the cached view.
    pub.publish()
    assert fake.climate["brew_boiler"]["current"] == "104"
    assert fake.climate["brew_boiler"]["target"] == "105"


def test_nothing_published_before_first_data():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake)
    pub.publish()
    assert fake.states == {}
    assert fake.climate == {}


def test_availability_grace_then_offline_then_online():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake, availability_grace=3)
    pub.update_settings(_settings())
    pub.note_success()
    assert fake.available is True
    # Two failures are within grace -> still online.
    pub.note_failure()
    pub.note_failure()
    assert fake.available is True
    # Third consecutive failure trips offline.
    pub.note_failure()
    assert fake.available is False
    # First success brings it right back.
    pub.note_success()
    assert fake.available is True


def test_interpolation_eases_live_temp_toward_setpoint_when_stale():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake, stale_after=0.0, interpolate_step=1.0)
    pub.update_settings(_settings({Address.BREW_BOILER_TEMP: 110}))
    pub.update_live(Address.CURRENT_BREW_TEMP, [100])
    pub._last_live_update_at = 0.0  # force staleness
    pub.publish()
    # 100 -> eased one step toward the 110 setpoint.
    assert fake.climate["brew_boiler"]["current"] == "101"


def test_no_interpolation_when_machine_off():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake, stale_after=0.0, interpolate_step=1.0)
    pub.update_settings(_settings({Address.BREW_BOILER_TEMP: 110, Address.STANDBY: 1}))
    pub.update_live(Address.CURRENT_BREW_TEMP, [100])
    pub._last_live_update_at = 0.0
    pub.publish()
    # Machine off -> hold, do not fabricate movement.
    assert fake.climate["brew_boiler"]["current"] == "100"


def test_fresh_read_is_not_interpolated():
    fake = FakeMqtt()
    pub = StatePublisher(Config(), fake, stale_after=100.0, interpolate_step=1.0)
    pub.update_settings(_settings({Address.BREW_BOILER_TEMP: 110}))
    pub.update_live(Address.CURRENT_BREW_TEMP, [100])  # just updated -> fresh
    pub.publish()
    assert fake.climate["brew_boiler"]["current"] == "100"
