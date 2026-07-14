"""Declarative entity registry decoding R60V memory into native HA values.

This is the semantic layer between the raw protocol (:mod:`.protocol`,
:mod:`.client`) and the Home Assistant platforms. It defines:

- :data:`LIVE_REGISTERS` -- the individual live registers the real machine
  answers (it ignores multi-register range reads at ``0xB000``);
- :class:`StateSnapshot` -- a captured settings block plus live registers;
- :data:`ENTITIES` -- a declarative registry of every non-climate entity, each
  knowing how to *decode* its native value from a snapshot and (if writable)
  how to *encode* and range-validate a command into a protocol write;
- :data:`CLIMATE_ENTITIES` -- the boiler thermostats (current temp + setpoint).

Decode functions return native Python/HA types (``bool`` for switches,
``datetime.time`` for time pickers, ``str`` option for selects, numbers for
sensors) so platforms can bind them directly to entity attributes. Multi-byte
settings values are little-endian.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time as dt_time

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
    if offset + 1 >= len(data):
        return 0
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


# -- decode helpers ------------------------------------------------------


def _byte(address: int) -> Callable[[StateSnapshot], int]:
    return lambda s: s.settings_byte(address)


def _live_byte(address: int) -> Callable[[StateSnapshot], int]:
    def decode(s: StateSnapshot) -> int:
        data = s.live_bytes(address)
        return data[0] if data else 0
    return decode


def _live_text(address: int) -> Callable[[StateSnapshot], str]:
    def decode(s: StateSnapshot) -> str:
        data = s.live_bytes(address)
        return bytes(data).decode("ascii", "replace").rstrip(" \x00")
    return decode


def _bool_on_zero(address: int) -> Callable[[StateSnapshot], bool]:
    """True when the byte equals 0 (the STANDBY convention: 0 == running)."""
    return lambda s: s.settings_byte(address) == 0


def _bool_on_nonzero(address: int) -> Callable[[StateSnapshot], bool]:
    return lambda s: s.settings_byte(address) != 0


def _enum(address: int, options: tuple[str, ...]) -> Callable[[StateSnapshot], str | None]:
    def decode(s: StateSnapshot) -> str | None:
        idx = s.settings_byte(address)
        return options[idx] if 0 <= idx < len(options) else None
    return decode


def _time(hour_address: int, minute_address: int) -> Callable[[StateSnapshot], dt_time | None]:
    """Decode two adjacent hour/minute bytes into a :class:`datetime.time`."""
    def decode(s: StateSnapshot) -> dt_time | None:
        hour = s.settings_byte(hour_address)
        minute = s.settings_byte(minute_address)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return dt_time(hour=hour, minute=minute)
        return None
    return decode


# -- encode helpers ------------------------------------------------------


def _encode_ranged(address: int, lo: int, hi: int) -> Callable[[float], tuple[int, list[int]]]:
    def encode(value: float) -> tuple[int, list[int]]:
        ivalue = int(round(float(value)))
        if not lo <= ivalue <= hi:
            raise ValueError(f"{ivalue} out of range [{lo}, {hi}] for 0x{address:02X}")
        return address, [ivalue]
    return encode


def _encode_time(hour_address: int) -> Callable[[dt_time], tuple[int, list[int]]]:
    """Encode a :class:`datetime.time` as one 2-byte write [hour, minute].

    The minute byte lives immediately after the hour byte on the R60V, so a
    single 2-byte write at the hour address sets both.
    """
    def encode(value: dt_time) -> tuple[int, list[int]]:
        return hour_address, [value.hour, value.minute]
    return encode


def _encode_bool(
    address: int, *, on_value: int, off_value: int
) -> Callable[[bool], tuple[int, list[int]]]:
    def encode(value: bool) -> tuple[int, list[int]]:
        return address, [on_value if value else off_value]
    return encode


def _encode_enum(address: int, options: tuple[str, ...]) -> Callable[[str], tuple[int, list[int]]]:
    def encode(option: str) -> tuple[int, list[int]]:
        try:
            idx = options.index(option)
        except ValueError as exc:
            raise ValueError(f"{option!r} not in {list(options)}") from exc
        return address, [idx]
    return encode


# -- selection option vocabularies ---------------------------------------

PROFILE_OPTIONS: tuple[str, ...] = ("A", "B", "C")
WATER_FEED_OPTIONS: tuple[str, ...] = ("tank", "mains")
TEMP_UNIT_OPTIONS: tuple[str, ...] = ("celsius", "fahrenheit")
LANGUAGE_OPTIONS: tuple[str, ...] = ("english", "german", "french", "italian")


@dataclass(frozen=True)
class R60VEntityDescription:
    """A declarative native HA entity backed by R60V memory.

    :param key: stable unique-id suffix.
    :param name: human-readable name.
    :param platform: HA platform (``sensor``/``switch``/``select``/``time``).
    :param decode: maps a :class:`StateSnapshot` to the entity's native value.
    :param encode: maps an HA value to ``(address, [bytes])``; only present for
        writable entities. Raises ``ValueError`` on an out-of-range payload.
    """

    key: str
    name: str
    platform: str
    decode: Callable[[StateSnapshot], object]
    encode: Callable[[object], tuple[int, list[int]]] | None = None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    options: tuple[str, ...] | None = None
    icon: str | None = None

    @property
    def writable(self) -> bool:
        return self.encode is not None


#: Every non-climate Home Assistant entity (see CLIMATE_ENTITIES for boilers).
ENTITIES: list[R60VEntityDescription] = [
    # --- sensors (read-only) ---
    R60VEntityDescription(
        "current_pressure", "Brew Pressure", "sensor",
        _live_byte(Address.CURRENT_PRESSURE),
        unit="bar", device_class="pressure", state_class="measurement",
        icon="mdi:gauge",
    ),
    R60VEntityDescription(
        "display", "Display", "sensor",
        _live_text(Address.DISPLAY),
        icon="mdi:card-text",
    ),
    R60VEntityDescription(
        "total_coffee_count", "Total Coffee Count", "sensor",
        lambda s: le16(s.settings, Address.TOTAL_COFFEE_COUNT),
        state_class="total_increasing", icon="mdi:counter",
    ),

    # --- switches ---
    # STANDBY: byte 0 = machine ON, 1 = standby. Power switch on == running.
    R60VEntityDescription(
        "power", "Power", "switch",
        _bool_on_zero(Address.STANDBY),
        _encode_bool(Address.STANDBY, on_value=0, off_value=1),
        icon="mdi:power",
    ),
    R60VEntityDescription(
        "service_boiler", "Steam Boiler", "switch",
        _bool_on_nonzero(Address.SERVICE_BOILER_ENABLE),
        _encode_bool(Address.SERVICE_BOILER_ENABLE, on_value=1, off_value=0),
        icon="mdi:kettle-steam",
    ),

    # --- selects ---
    R60VEntityDescription(
        "active_profile", "Pressure Profile", "select",
        _enum(Address.ACTIVE_PROFILE, PROFILE_OPTIONS),
        _encode_enum(Address.ACTIVE_PROFILE, PROFILE_OPTIONS),
        options=PROFILE_OPTIONS, icon="mdi:chart-bell-curve",
    ),
    R60VEntityDescription(
        "water_feed", "Water Source", "select",
        _enum(Address.WATER_FEED, WATER_FEED_OPTIONS),
        _encode_enum(Address.WATER_FEED, WATER_FEED_OPTIONS),
        options=WATER_FEED_OPTIONS, icon="mdi:water",
    ),
    R60VEntityDescription(
        "temperature_unit", "Temperature Unit", "select",
        _enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
        _encode_enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
        options=TEMP_UNIT_OPTIONS, icon="mdi:temperature-celsius",
    ),
    R60VEntityDescription(
        "language", "Language", "select",
        _enum(Address.LANGUAGE, LANGUAGE_OPTIONS),
        _encode_enum(Address.LANGUAGE, LANGUAGE_OPTIONS),
        options=LANGUAGE_OPTIONS, icon="mdi:translate",
    ),

    # --- auto on/off timers (native time pickers) ---
    # Each writes both hour+minute in one 2-byte write at the hour address.
    R60VEntityDescription(
        "auto_on", "Auto-On Time", "time",
        _time(Address.AUTO_ON_HOUR, Address.AUTO_ON_MINUTE),
        _encode_time(Address.AUTO_ON_HOUR),
        icon="mdi:clock-start",
    ),
    R60VEntityDescription(
        "auto_off", "Auto-Off Time", "time",
        _time(Address.AUTO_OFF_HOUR, Address.AUTO_OFF_MINUTE),
        _encode_time(Address.AUTO_OFF_HOUR),
        icon="mdi:clock-end",
    ),
]

#: Fast lookup by key.
ENTITIES_BY_KEY: dict[str, R60VEntityDescription] = {e.key: e for e in ENTITIES}


def entities_for_platform(platform: str) -> list[R60VEntityDescription]:
    """Return the descriptors belonging to ``platform``."""
    return [e for e in ENTITIES if e.platform == platform]


@dataclass(frozen=True)
class R60VClimateDescription:
    """A boiler modeled as an HA ``climate`` thermostat.

    Combines a live current temperature with a writable target setpoint. The
    device runs a fixed ``heat`` mode; on/off is the machine-wide Power switch.
    """

    key: str
    name: str
    current: Callable[[StateSnapshot], object]
    target: Callable[[StateSnapshot], object]
    encode_target: Callable[[float], tuple[int, list[int]]]
    min_temp: int
    max_temp: int
    temp_step: float = 1.0
    icon: str | None = None


#: Boiler thermostats (current temp + setpoint) published as `climate` entities.
#: The group setpoint (0x4C) is intentionally NOT exposed: its decode is unclear.
CLIMATE_ENTITIES: list[R60VClimateDescription] = [
    R60VClimateDescription(
        "brew_boiler", "Brew Boiler",
        _live_byte(Address.CURRENT_BREW_TEMP),
        _byte(Address.BREW_BOILER_TEMP),
        _encode_ranged(Address.BREW_BOILER_TEMP, *p.BREW_TEMP_RANGE_C),
        p.BREW_TEMP_RANGE_C[0], p.BREW_TEMP_RANGE_C[1], 1,
        icon="mdi:coffee-maker",
    ),
    R60VClimateDescription(
        "steam_boiler", "Steam Boiler",
        _live_byte(Address.CURRENT_SERVICE_TEMP),
        _byte(Address.SERVICE_BOILER_TEMP),
        _encode_ranged(Address.SERVICE_BOILER_TEMP, *p.SERVICE_TEMP_RANGE_C),
        p.SERVICE_TEMP_RANGE_C[0], p.SERVICE_TEMP_RANGE_C[1], 1,
        icon="mdi:kettle-steam",
    ),
]

#: Fast lookup by key.
CLIMATE_BY_KEY: dict[str, R60VClimateDescription] = {c.key: c for c in CLIMATE_ENTITIES}
