"""Decode R60V memory into Home Assistant entities and encode commands back.

This module is the semantic layer between the raw protocol (:mod:`.protocol`,
:mod:`.client`) and Home Assistant. It defines:

- :data:`LIVE_REGISTERS` -- the individual live registers the real machine
  answers (it ignores multi-register range reads at ``0xB000``);
- :class:`StateSnapshot` -- a captured settings block plus live registers;
- :data:`ENTITIES` -- a declarative registry of every HA entity, each knowing
  how to *decode* its value from a snapshot and (if writable) how to *encode*
  and range-validate a command into a protocol write.

Multi-byte settings values are little-endian (confirmed on real hardware, e.g.
the total coffee counter at ``0x4D``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p
from .protocol import Address

#: Live/read-only registers the machine answers, with the exact read length.
#: The R60V rejects range reads here, so each is polled individually.
LIVE_REGISTERS: dict[int, int] = {
    Address.CURRENT_BREW_TEMP: 1,
    Address.CURRENT_SERVICE_TEMP: 1,
    Address.CURRENT_PRESSURE: 1,
    Address.DISPLAY: 16,
}


def le16(data: list[int], offset: int) -> int:
    """Decode a little-endian uint16 from ``data`` at ``offset``."""
    return (data[offset] & 0xFF) | ((data[offset + 1] & 0xFF) << 8)


@dataclass
class StateSnapshot:
    """A point-in-time capture of the machine's readable memory."""

    settings: list[int] = field(default_factory=lambda: [0] * p.SETTINGS_LEN)
    live: dict[int, list[int]] = field(default_factory=dict)

    def settings_byte(self, address: int) -> int:
        return self.settings[address] & 0xFF if address < len(self.settings) else 0

    def live_bytes(self, address: int) -> list[int]:
        return self.live.get(address, [])


@dataclass(frozen=True)
class Entity:
    """A declarative Home Assistant entity backed by R60V memory.

    :param key: stable unique-id suffix (also the MQTT object id).
    :param name: human-readable name.
    :param component: MQTT Discovery component (``sensor``/``switch``/...).
    :param decode: maps a :class:`StateSnapshot` to the entity's value.
    :param encode: maps an HA command payload to ``(address, [bytes])``; only
        present for writable entities. Should raise ``ValueError`` on an
        out-of-range or unrecognized payload.
    :param config: extra MQTT Discovery config fields (unit, options, ...).
    """

    key: str
    name: str
    component: str
    decode: Callable[[StateSnapshot], object]
    encode: Callable[[str], tuple[int, list[int]]] | None = None
    config: dict = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return self.encode is not None


# -- decode helpers ------------------------------------------------------


def _byte(address: int) -> Callable[[StateSnapshot], int]:
    return lambda s: s.settings_byte(address)


def _live_byte(address: int) -> Callable[[StateSnapshot], int]:
    def decode(s: StateSnapshot) -> int:
        data = s.live_bytes(address)
        return data[0] if data else 0
    return decode


def _live_decibar(address: int) -> Callable[[StateSnapshot], float]:
    """Decode a live pressure register stored in decibar (tenths of a bar).

    The R60V reports pressure as a single byte in tenths of a bar (raw 90 =
    9.0 bar). Treating the raw byte as whole bar over-reports 10x.
    """
    def decode(s: StateSnapshot) -> float:
        data = s.live_bytes(address)
        return (data[0] if data else 0) / 10.0
    return decode


def _live_text(address: int) -> Callable[[StateSnapshot], str]:
    def decode(s: StateSnapshot) -> str:
        data = s.live_bytes(address)
        return bytes(data).decode("ascii", "replace").rstrip(" \x00")
    return decode


def _enum(address: int, options: list[str]) -> Callable[[StateSnapshot], str]:
    def decode(s: StateSnapshot) -> str:
        idx = s.settings_byte(address)
        return options[idx] if 0 <= idx < len(options) else "unknown"
    return decode


def _time(hour_address: int, minute_address: int) -> Callable[[StateSnapshot], str]:
    """Decode two adjacent hour/minute bytes into an ``HH:MM`` string."""
    def decode(s: StateSnapshot) -> str:
        return f"{s.settings_byte(hour_address):02d}:{s.settings_byte(minute_address):02d}"
    return decode


# -- encode helpers ------------------------------------------------------


def _encode_ranged(address: int, lo: int, hi: int) -> Callable[[str], tuple[int, list[int]]]:
    def encode(payload: str) -> tuple[int, list[int]]:
        value = int(round(float(payload)))
        if not lo <= value <= hi:
            raise ValueError(f"{value} out of range [{lo}, {hi}] for 0x{address:02X}")
        return address, [value]
    return encode


def _encode_time(hour_address: int) -> Callable[[str], tuple[int, list[int]]]:
    """Encode an ``HH:MM`` string as a 2-byte write [hour, minute].

    The minute byte lives immediately after the hour byte on the R60V
    (``0x51``/``0x52`` and ``0x53``/``0x54``), so a single 2-byte write at the
    hour address sets both.
    """
    def encode(payload: str) -> tuple[int, list[int]]:
        parts = payload.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"expected HH:MM, got {payload!r}")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError(f"non-numeric time {payload!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"time out of range: {payload!r}")
        return hour_address, [hour, minute]
    return encode


def _encode_bool(
    address: int, *, on_value: int, off_value: int
) -> Callable[[str], tuple[int, list[int]]]:
    def encode(payload: str) -> tuple[int, list[int]]:
        truthy = payload.strip().upper() in ("ON", "1", "TRUE")
        return address, [on_value if truthy else off_value]
    return encode


def _encode_enum(address: int, options: list[str]) -> Callable[[str], tuple[int, list[int]]]:
    def encode(payload: str) -> tuple[int, list[int]]:
        try:
            idx = options.index(payload)
        except ValueError as exc:
            raise ValueError(f"{payload!r} not in {options}") from exc
        return address, [idx]
    return encode



# -- selection option vocabularies ---------------------------------------

PROFILE_OPTIONS = ["A", "B", "C"]
WATER_FEED_OPTIONS = ["mains", "tank"]  # 0x46: 0=HardPlumbed(mains), 1=Reservoir(tank)
TEMP_UNIT_OPTIONS = ["celsius", "fahrenheit"]
LANGUAGE_OPTIONS = ["english", "german", "french", "italian"]


#: Every Home Assistant entity the broker publishes (see also CLIMATE_ENTITIES
#: for the boiler thermostats, which combine current temp + setpoint).
ENTITIES: list[Entity] = [
    # --- live sensors (read-only) ---
    Entity("current_pressure", "Brew Pressure", "sensor",
           _live_decibar(Address.CURRENT_PRESSURE),
           config={"unit_of_measurement": "bar", "device_class": "pressure",
                   "state_class": "measurement", "suggested_display_precision": 1,
                   "icon": "mdi:gauge"}),
    Entity("display", "Display", "sensor",
           _live_text(Address.DISPLAY),
           config={"icon": "mdi:card-text"}),
    Entity("total_coffee_count", "Total Coffee Count", "sensor",
           lambda s: le16(s.settings, Address.TOTAL_COFFEE_COUNT),
           config={"state_class": "total_increasing", "icon": "mdi:counter"}),

    # --- setpoints ---
    # Brew & steam boiler setpoints are `climate` thermostats (CLIMATE_ENTITIES).
    # The group setpoint (0x4C) is intentionally NOT exposed: its decode reads an
    # implausible value and its meaning is unclear -- hidden pending further
    # investigation. Decode/range kept for when it's revived.

    # --- switches ---
    # STANDBY: byte 0 = machine ON, 1 = standby. HA "Power" switch on == on.
    Entity("power", "Power", "switch",
           lambda s: "ON" if s.settings_byte(Address.STANDBY) == 0 else "OFF",
           _encode_bool(Address.STANDBY, on_value=0, off_value=1),
           config={"icon": "mdi:power"}),
    Entity("service_boiler", "Steam Boiler", "switch",
           lambda s: "ON" if s.settings_byte(Address.SERVICE_BOILER_ENABLE) else "OFF",
           _encode_bool(Address.SERVICE_BOILER_ENABLE, on_value=1, off_value=0),
           config={"icon": "mdi:kettle-steam"}),

    # --- selects ---
    Entity("active_profile", "Pressure Profile", "select",
           _enum(Address.ACTIVE_PROFILE, PROFILE_OPTIONS),
           _encode_enum(Address.ACTIVE_PROFILE, PROFILE_OPTIONS),
           config={"options": PROFILE_OPTIONS, "icon": "mdi:chart-bell-curve"}),
    Entity("water_feed", "Water Source", "select",
           _enum(Address.WATER_FEED, WATER_FEED_OPTIONS),
           _encode_enum(Address.WATER_FEED, WATER_FEED_OPTIONS),
           config={"options": WATER_FEED_OPTIONS, "icon": "mdi:water"}),
    Entity("temperature_unit", "Temperature Unit", "select",
           _enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
           _encode_enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
           config={"options": TEMP_UNIT_OPTIONS, "icon": "mdi:temperature-celsius"}),
    Entity("language", "Language", "select",
           _enum(Address.LANGUAGE, LANGUAGE_OPTIONS),
           _encode_enum(Address.LANGUAGE, LANGUAGE_OPTIONS),
           config={"options": LANGUAGE_OPTIONS, "icon": "mdi:translate"}),

    # --- auto on/off timers (writable HH:MM text) ---
    # MQTT Discovery has no `time` platform, so a single validated HH:MM text
    # field is the closest to a picker; a native time picker would need the
    # HACS integration. Each writes both hour+minute in one 2-byte write.
    Entity("auto_on", "Auto-On Time", "text",
           _time(Address.AUTO_ON_HOUR, Address.AUTO_ON_MINUTE),
           _encode_time(Address.AUTO_ON_HOUR),
           config={"pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
                   "icon": "mdi:clock-start"}),
    Entity("auto_off", "Auto-Off Time", "text",
           _time(Address.AUTO_OFF_HOUR, Address.AUTO_OFF_MINUTE),
           _encode_time(Address.AUTO_OFF_HOUR),
           config={"pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
                   "icon": "mdi:clock-end"}),
]

#: Fast lookup by key.
ENTITIES_BY_KEY: dict[str, Entity] = {e.key: e for e in ENTITIES}


@dataclass(frozen=True)
class ClimateEntity:
    """A boiler modeled as an HA ``climate`` thermostat.

    Combines a live current temperature with a writable target setpoint in one
    entity/card, instead of a separate sensor + number. The device runs a fixed
    ``heat`` mode; on/off is the machine-wide Power (standby) switch.
    """

    key: str
    name: str
    current: Callable[[StateSnapshot], object]          # current temperature
    target: Callable[[StateSnapshot], object]           # target setpoint
    encode_target: Callable[[str], tuple[int, list[int]]]
    min_temp: int
    max_temp: int
    temp_step: float = 1.0
    config: dict = field(default_factory=dict)


#: Boiler thermostats (current temp + setpoint) published as `climate` entities.
CLIMATE_ENTITIES: list[ClimateEntity] = [
    ClimateEntity(
        "brew_boiler", "Brew Boiler",
        _live_byte(Address.CURRENT_BREW_TEMP),
        _byte(Address.BREW_BOILER_TEMP),
        _encode_ranged(Address.BREW_BOILER_TEMP, *p.BREW_TEMP_RANGE_C),
        p.BREW_TEMP_RANGE_C[0], p.BREW_TEMP_RANGE_C[1], 1,
        config={"icon": "mdi:coffee-maker"},
    ),
    ClimateEntity(
        "steam_boiler", "Steam Boiler",
        _live_byte(Address.CURRENT_SERVICE_TEMP),
        _byte(Address.SERVICE_BOILER_TEMP),
        _encode_ranged(Address.SERVICE_BOILER_TEMP, *p.SERVICE_TEMP_RANGE_C),
        p.SERVICE_TEMP_RANGE_C[0], p.SERVICE_TEMP_RANGE_C[1], 1,
        config={"icon": "mdi:kettle-steam"},
    ),
]

#: Fast lookup by key.
CLIMATE_BY_KEY: dict[str, ClimateEntity] = {c.key: c for c in CLIMATE_ENTITIES}
