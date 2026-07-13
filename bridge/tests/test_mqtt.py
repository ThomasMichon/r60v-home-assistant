"""Tests for MQTT Discovery payload construction (no broker required)."""
from __future__ import annotations

from r60v_broker.config import Config
from r60v_broker.mqtt_bridge import build_discovery_payload, build_climate_discovery_payload
from r60v_broker.state import ENTITIES_BY_KEY, CLIMATE_BY_KEY


def _config() -> Config:
    return Config(mqtt_base_topic="rocket-r60v", discovery_prefix="homeassistant",
                  device_id="rocket_r60v", device_name="Rocket R60V")


def test_sensor_payload_is_read_only():
    payload = build_discovery_payload(ENTITIES_BY_KEY["display"], _config())
    assert payload["unique_id"] == "rocket_r60v_display"
    assert payload["state_topic"] == "rocket-r60v/display/state"
    assert "command_topic" not in payload  # read-only


def test_switch_payload_has_command_and_payloads():
    payload = build_discovery_payload(ENTITIES_BY_KEY["power"], _config())
    assert payload["command_topic"] == "rocket-r60v/power/set"
    assert payload["payload_on"] == "ON"
    assert payload["payload_off"] == "OFF"


def test_select_payload_carries_options():
    payload = build_discovery_payload(ENTITIES_BY_KEY["active_profile"], _config())
    assert payload["options"] == ["A", "B", "C"]
    assert payload["command_topic"] == "rocket-r60v/active_profile/set"


def test_group_setpoint_is_hidden():
    # Intentionally not exposed pending further investigation.
    assert "group_setpoint" not in ENTITIES_BY_KEY


def test_text_time_payload_has_pattern_and_command():
    payload = build_discovery_payload(ENTITIES_BY_KEY["auto_on"], _config())
    assert payload["command_topic"] == "rocket-r60v/auto_on/set"
    assert payload["pattern"] == r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def test_climate_payload_has_thermostat_topics():
    payload = build_climate_discovery_payload(CLIMATE_BY_KEY["brew_boiler"], _config())
    assert payload["current_temperature_topic"] == "rocket-r60v/brew_boiler/current"
    assert payload["temperature_command_topic"] == "rocket-r60v/brew_boiler/target/set"
    assert payload["temperature_state_topic"] == "rocket-r60v/brew_boiler/target"
    assert payload["min_temp"] == 85
    assert payload["max_temp"] == 115
    assert payload["modes"] == ["heat"]
    assert payload["temperature_unit"] == "C"


def test_device_block_and_availability():
    payload = build_discovery_payload(ENTITIES_BY_KEY["display"], _config(), sw_version="0.1.0")
    assert payload["device"]["identifiers"] == ["rocket_r60v"]
    assert payload["device"]["manufacturer"] == "Rocket Espresso"
    assert payload["device"]["sw_version"] == "0.1.0"
    assert payload["availability_topic"] == "rocket-r60v/status"

