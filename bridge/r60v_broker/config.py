"""Bridge configuration, sourced from environment variables.

Defaults suit the common case: connect to the R60V on its own access point
(``192.168.1.1:1774``) and expose the native-protocol LAN front-end so a Home
Assistant integration (or any LAN client) can reach the machine safely. MQTT
Discovery is optional and disabled unless ``MQTT_HOST`` is set. Any secret (an
MQTT password) comes from the environment -- never hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import protocol as p


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Runtime configuration for the broker daemon."""

    # R60V control endpoint (the machine's own AP).
    machine_host: str = p.DEFAULT_HOST
    machine_port: int = p.DEFAULT_PORT

    # Native-protocol LAN front-end. When enabled, the bridge re-presents the
    # R60V wire protocol at frontend_host:frontend_port, backed by the governor
    # -- so a Home Assistant integration (or any LAN device) can talk to the
    # machine directly without re-exposing its fragile single-socket listener.
    # ON by default: this is the bridge's primary purpose.
    frontend_enabled: bool = True
    frontend_host: str = "0.0.0.0"
    frontend_port: int = p.DEFAULT_PORT

    # WebSocket push server. When enabled, the bridge streams the cached live
    # state to LAN subscribers (a Home Assistant local_push integration) over
    # WebSocket -- near-real-time without polling from HA. Fed by the governor's
    # poll loop; never touches the device itself. OFF by default; opt-in.
    push_enabled: bool = False
    push_host: str = "0.0.0.0"
    push_port: int = 8788

    # MQTT broker (optional). Leave mqtt_host empty to disable MQTT Discovery
    # entirely and run front-end-only. Set it to publish HA MQTT Discovery
    # entities in addition to (or instead of) the native front-end.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_base_topic: str = "rocket-r60v"
    discovery_prefix: str = "homeassistant"

    # Polling cadence (seconds) -- deliberately gentle; the device link is
    # fragile and HA continuity comes from the cache, not from fast polling.
    settings_interval: float = 60.0   # full ReadAll settings block
    live_interval: float = 10.0       # live temps / pressure / display
    publish_interval: float = 5.0     # steady MQTT publish from cache
    request_gap: float = 0.3          # minimum spacing between wire requests
    request_timeout: float = 5.0      # per-request read timeout

    # Stable identity for HA device registry + unique ids.
    device_id: str = "rocket_r60v"
    device_name: str = "Rocket R60V"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            machine_host=_env("R60V_HOST", p.DEFAULT_HOST),
            machine_port=_env_int("R60V_PORT", p.DEFAULT_PORT),
            frontend_enabled=_env_bool("R60V_FRONTEND_ENABLED", True),
            frontend_host=_env("R60V_FRONTEND_HOST", "0.0.0.0"),
            frontend_port=_env_int("R60V_FRONTEND_PORT", p.DEFAULT_PORT),
            push_enabled=_env_bool("R60V_PUSH_ENABLED", False),
            push_host=_env("R60V_PUSH_HOST", "0.0.0.0"),
            push_port=_env_int("R60V_PUSH_PORT", 8788),
            mqtt_host=_env("MQTT_HOST", ""),
            mqtt_port=_env_int("MQTT_PORT", 1883),
            mqtt_username=_env("MQTT_USERNAME", ""),
            mqtt_password=_env("MQTT_PASSWORD", ""),
            mqtt_base_topic=_env("R60V_BASE_TOPIC", "rocket-r60v"),
            discovery_prefix=_env("MQTT_DISCOVERY_PREFIX", "homeassistant"),
            settings_interval=_env_float("R60V_SETTINGS_INTERVAL", 60.0),
            live_interval=_env_float("R60V_LIVE_INTERVAL", 10.0),
            publish_interval=_env_float("R60V_PUBLISH_INTERVAL", 5.0),
            request_gap=_env_float("R60V_REQUEST_GAP", 0.3),
            request_timeout=_env_float("R60V_REQUEST_TIMEOUT", 5.0),
            device_id=_env("R60V_DEVICE_ID", "rocket_r60v"),
            device_name=_env("R60V_DEVICE_NAME", "Rocket R60V"),
        )

    # -- topic helpers ----------------------------------------------------

    @property
    def mqtt_enabled(self) -> bool:
        """Whether MQTT Discovery is configured (a broker host was set)."""
        return bool(self.mqtt_host)

    def availability_topic(self) -> str:
        return f"{self.mqtt_base_topic}/status"

    def state_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/state"

    def command_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/set"

    def discovery_topic(self, component: str, key: str) -> str:
        return f"{self.discovery_prefix}/{component}/{self.device_id}/{key}/config"

    def unique_id(self, key: str) -> str:
        return f"{self.device_id}_{key}"

    # -- climate sub-topics (a thermostat needs several) ------------------

    def climate_current_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/current"

    def climate_target_state_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/target"

    def climate_target_command_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/target/set"

    def climate_mode_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/{key}/mode"
