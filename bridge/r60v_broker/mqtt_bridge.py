"""MQTT Discovery bridge: publish R60V entities to Home Assistant.

The pure :func:`build_discovery_payload` builds each entity's MQTT Discovery
config document and is unit-testable without a broker. :class:`MqttBridge`
wraps ``paho-mqtt`` for the actual wire: it publishes discovery + state,
advertises availability (with a Last-Will), and routes inbound command-topic
messages to a callback.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from .config import Config
from .state import CLIMATE_ENTITIES, ClimateEntity, Entity, ENTITIES

LOGGER = logging.getLogger("r60v.mqtt")

#: Called with (entity_key, payload) when HA sends a command. Thread-safe
#: marshalling into the asyncio loop is the caller's responsibility.
CommandHandler = Callable[[str, str], None]


def build_discovery_payload(entity: Entity, config: Config, *, sw_version: str = "") -> dict:
    """Build the MQTT Discovery config document for one entity."""
    payload: dict = {
        "name": entity.name,
        "unique_id": config.unique_id(entity.key),
        "object_id": f"{config.device_id}_{entity.key}",
        "state_topic": config.state_topic(entity.key),
        "availability_topic": config.availability_topic(),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [config.device_id],
            "name": config.device_name,
            "manufacturer": "Rocket Espresso",
            "model": "R60V",
        },
    }
    if sw_version:
        payload["device"]["sw_version"] = sw_version

    # Component-specific defaults.
    if entity.component == "switch":
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"

    if entity.writable:
        payload["command_topic"] = config.command_topic(entity.key)

    # Entity-declared config (unit, device_class, options, min/max, ...)
    # overrides/extends the defaults above.
    payload.update(entity.config)
    return payload


def build_climate_discovery_payload(climate: ClimateEntity, config: Config,
                                    *, sw_version: str = "") -> dict:
    """Build the MQTT Discovery config document for one climate thermostat."""
    payload: dict = {
        "name": climate.name,
        "unique_id": config.unique_id(climate.key),
        "object_id": f"{config.device_id}_{climate.key}",
        "availability_topic": config.availability_topic(),
        "payload_available": "online",
        "payload_not_available": "offline",
        "current_temperature_topic": config.climate_current_topic(climate.key),
        "temperature_state_topic": config.climate_target_state_topic(climate.key),
        "temperature_command_topic": config.climate_target_command_topic(climate.key),
        "mode_state_topic": config.climate_mode_topic(climate.key),
        "modes": ["heat"],
        "min_temp": climate.min_temp,
        "max_temp": climate.max_temp,
        "temp_step": climate.temp_step,
        "temperature_unit": "C",
        "device": {
            "identifiers": [config.device_id],
            "name": config.device_name,
            "manufacturer": "Rocket Espresso",
            "model": "R60V",
        },
    }
    if sw_version:
        payload["device"]["sw_version"] = sw_version
    payload.update(climate.config)
    return payload


class MqttBridge:
    """Thin ``paho-mqtt`` wrapper for the broker's northbound side."""

    def __init__(self, config: Config, on_command: CommandHandler | None = None) -> None:
        self.config = config
        self.on_command = on_command
        self._client = None

    def _make_client(self):
        import paho.mqtt.client as mqtt  # imported lazily so tests need no broker

        try:  # paho-mqtt 2.x
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"{self.config.device_id}-broker",
            )
        except (AttributeError, TypeError):  # paho-mqtt 1.x
            client = mqtt.Client(client_id=f"{self.config.device_id}-broker")

        if self.config.mqtt_username:
            client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        client.will_set(self.config.availability_topic(), "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        LOGGER.info("connecting to MQTT %s:%d", self.config.mqtt_host, self.config.mqtt_port)
        self._client = self._make_client()
        self._client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self.publish_availability(False)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001 -- best-effort shutdown
            LOGGER.debug("error during MQTT disconnect: %s", exc)

    # -- publishing -------------------------------------------------------

    def publish_discovery(self, *, sw_version: str = "") -> None:
        if self._client is None:
            return
        for entity in ENTITIES:
            topic = self.config.discovery_topic(entity.component, entity.key)
            payload = build_discovery_payload(entity, self.config, sw_version=sw_version)
            self._client.publish(topic, json.dumps(payload), retain=True)
        for climate in CLIMATE_ENTITIES:
            topic = self.config.discovery_topic("climate", climate.key)
            payload = build_climate_discovery_payload(climate, self.config, sw_version=sw_version)
            self._client.publish(topic, json.dumps(payload), retain=True)
        LOGGER.info("published discovery for %d entities + %d thermostats",
                    len(ENTITIES), len(CLIMATE_ENTITIES))

    def publish_availability(self, online: bool) -> None:
        if self._client is None:
            return
        self._client.publish(
            self.config.availability_topic(),
            "online" if online else "offline",
            retain=True,
        )

    def publish_state(self, key: str, value: object) -> None:
        if self._client is None:
            return
        self._client.publish(self.config.state_topic(key), str(value), retain=True)

    def publish_climate(self, key: str, current: object, target: object,
                        mode: str = "heat") -> None:
        if self._client is None:
            return
        self._client.publish(self.config.climate_current_topic(key), str(current), retain=True)
        self._client.publish(self.config.climate_target_state_topic(key), str(target), retain=True)
        self._client.publish(self.config.climate_mode_topic(key), mode, retain=True)

    # -- callbacks --------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        LOGGER.info("MQTT connected (rc=%s); subscribing to command topics", rc)
        for entity in ENTITIES:
            if entity.writable:
                client.subscribe(self.config.command_topic(entity.key))
        for climate in CLIMATE_ENTITIES:
            client.subscribe(self.config.climate_target_command_topic(climate.key))
        self.publish_availability(True)

    def _on_message(self, client, userdata, message) -> None:
        payload = message.payload.decode("utf-8", "replace")
        for entity in ENTITIES:
            if entity.writable and message.topic == self.config.command_topic(entity.key):
                LOGGER.info("command %s <- %r", entity.key, payload)
                if self.on_command is not None:
                    self.on_command(entity.key, payload)
                return
        for climate in CLIMATE_ENTITIES:
            if message.topic == self.config.climate_target_command_topic(climate.key):
                LOGGER.info("climate %s target <- %r", climate.key, payload)
                if self.on_command is not None:
                    self.on_command(climate.key, payload)
                return
        LOGGER.warning("command on unmapped topic %s", message.topic)

