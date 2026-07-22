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

import re
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

    def settings_block(self, address: int, length: int) -> list[int]:
        """Return ``length`` settings bytes at ``address`` (zero-padded)."""
        block = [self.settings_byte(address + i) for i in range(length)]
        return block

    def live_bytes(self, address: int) -> list[int]:
        return self.live.get(address, [])


# -- decode helpers ------------------------------------------------------


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


# -- pressure-profile codec ----------------------------------------------
# A profile block (PROFILE_A/B/C, 15 data bytes) is 5 steps of
# (time_seconds, pressure_bar) at 0.1 precision: the first 10 bytes are 5x
# uint16-LE deciseconds, the last 5 bytes are 5x uint8 decibar. The text
# surface is a space-separated "seconds:bar" list, e.g. "3:3 6:6 25:9 0:9 0:6".


def _fmt_num(value: float) -> str:
    """Render a 0.1-precision value without a trailing '.0'."""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def decode_profile(block: list[int]) -> str:
    """Decode a 15-byte profile block into a ``"s:bar s:bar ..."`` string."""
    steps: list[str] = []
    for i in range(p.PROFILE_STEPS):
        seconds = (block[i * 2] + (block[i * 2 + 1] << 8)) / 10
        bar = block[10 + i] / 10
        steps.append(f"{_fmt_num(seconds)}:{_fmt_num(bar)}")
    return " ".join(steps)


def encode_profile(text: str) -> list[int]:
    """Encode a ``"s:bar ..."`` string (1-5 steps) into a 15-byte block.

    Missing trailing steps are zero-filled. Raises ``ValueError`` on a
    malformed string or an out-of-range time/pressure.
    """
    raw = str(text).strip()
    steps = raw.split() if raw else []
    if not 0 <= len(steps) <= p.PROFILE_STEPS:
        raise ValueError(f"expected 0-{p.PROFILE_STEPS} steps, got {len(steps)}")
    t_lo_hi: list[int] = []
    pressures: list[int] = []
    tmin, tmax = p.PROFILE_TIMING_RANGE
    pmin, pmax = p.PROFILE_PRESSURE_RANGE
    for step in steps:
        parts = step.split(":")
        if len(parts) != 2:
            raise ValueError(f"step {step!r} is not 'seconds:bar'")
        try:
            seconds = round(float(parts[0]), 1)
            bar = round(float(parts[1]), 1)
        except ValueError as exc:
            raise ValueError(f"non-numeric step {step!r}") from exc
        if not tmin <= seconds <= tmax:
            raise ValueError(f"time {seconds} out of range [{tmin}, {tmax}] s")
        if not pmin <= bar <= pmax:
            raise ValueError(f"pressure {bar} out of range [{pmin}, {pmax}] bar")
        deciseconds = int(round(seconds * 10))
        t_lo_hi.extend((deciseconds & 0xFF, (deciseconds >> 8) & 0xFF))
        pressures.append(int(round(bar * 10)))
    # Zero-fill the remaining steps.
    for _ in range(p.PROFILE_STEPS - len(steps)):
        t_lo_hi.extend((0, 0))
        pressures.append(0)
    return t_lo_hi + pressures


def _decode_profile_at(address: int) -> Callable[[StateSnapshot], str]:
    return lambda s: decode_profile(s.settings_block(address, p.PROFILE_LEN))


def _encode_profile_at(address: int) -> Callable[[str], tuple[int, list[int]]]:
    return lambda text: (address, encode_profile(text))


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
    #: Per-value icon overrides for selects whose icon should reflect the chosen
    #: option (falls back to ``icon`` for options not listed).
    icon_map: dict[str, str] | None = None
    #: Rounding precision hint for numeric sensors (HA display precision).
    suggested_precision: int | None = None
    #: Optional max length for text entities.
    max_length: int | None = None

    @property
    def writable(self) -> bool:
        return self.encode is not None

    def icon_for(self, value: object) -> str | None:
        """Resolve the icon for a current value (per-value map, else base)."""
        if self.icon_map is not None and isinstance(value, str):
            return self.icon_map.get(value, self.icon)
        return self.icon


#: Per-value icons: Temperature Unit and Water Source reflect their selection.
TEMP_UNIT_ICONS = {
    "celsius": "mdi:temperature-celsius",
    "fahrenheit": "mdi:temperature-fahrenheit",
}
WATER_FEED_ICONS = {
    "tank": "mdi:cup-water",       # onboard reservoir
    "mains": "mdi:pipe-valve",     # hard-plumbed to the water line
}


#: Every non-climate Home Assistant entity (see CLIMATE_ENTITIES for boilers).
ENTITIES: list[R60VEntityDescription] = [
    # --- sensors (read-only) ---
    R60VEntityDescription(
        "current_pressure", "Brew Pressure", "sensor",
        _live_decibar(Address.CURRENT_PRESSURE),
        unit="bar", device_class="pressure", state_class="measurement",
        icon="mdi:gauge", suggested_precision=1,
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
        options=WATER_FEED_OPTIONS, icon="mdi:water", icon_map=WATER_FEED_ICONS,
    ),
    R60VEntityDescription(
        "temperature_unit", "Temperature Unit", "select",
        _enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
        _encode_enum(Address.TEMPERATURE_UNIT, TEMP_UNIT_OPTIONS),
        options=TEMP_UNIT_OPTIONS, icon="mdi:thermometer",
        icon_map=TEMP_UNIT_ICONS,
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

    # --- pressure profiles (editable "s:bar s:bar ..." text, 5 steps) ---
    R60VEntityDescription(
        "profile_a", "Pressure Profile A", "text",
        _decode_profile_at(Address.PROFILE_A),
        _encode_profile_at(Address.PROFILE_A),
        icon="mdi:chart-bell-curve", max_length=64,
    ),
    R60VEntityDescription(
        "profile_b", "Pressure Profile B", "text",
        _decode_profile_at(Address.PROFILE_B),
        _encode_profile_at(Address.PROFILE_B),
        icon="mdi:chart-bell-curve", max_length=64,
    ),
    R60VEntityDescription(
        "profile_c", "Pressure Profile C", "text",
        _decode_profile_at(Address.PROFILE_C),
        _encode_profile_at(Address.PROFILE_C),
        icon="mdi:chart-bell-curve", max_length=64,
    ),
]

#: Fast lookup by key.
ENTITIES_BY_KEY: dict[str, R60VEntityDescription] = {e.key: e for e in ENTITIES}


def entities_for_platform(platform: str) -> list[R60VEntityDescription]:
    """Return the descriptors belonging to ``platform``."""
    return [e for e in ENTITIES if e.platform == platform]


def is_fahrenheit(s: StateSnapshot) -> bool:
    """True when the machine's display unit (reg 0x00) is Fahrenheit."""
    return s.settings_byte(Address.TEMPERATURE_UNIT) == 1


def machine_to_celsius(raw: int, fahrenheit: bool) -> int:
    """Convert a raw boiler byte (in the machine's display unit) to Celsius."""
    return round((raw - 32) / 1.8) if fahrenheit else raw


def celsius_to_machine(celsius: float, fahrenheit: bool) -> int:
    """Convert a Celsius value to the machine's display unit (raw byte)."""
    return round(celsius * 1.8 + 32) if fahrenheit else round(celsius)


#: The R60V front panel prints the *actual* brew boiler temperature, e.g.
#: ``"BREW BOIL. 221*F"`` (and ``"BREW BOIL. ECO*"`` on standby, with no
#: number). This is a more trustworthy current-temperature source than the
#: ``0xB000`` live register, which on some units mirrors the setpoint.
_BREW_DISPLAY_RE = re.compile(r"BREW\s*BOIL\.?\s*(\d{1,3})\s*\*?\s*([CF])", re.IGNORECASE)


def parse_display_brew_temp(display: str) -> tuple[int, bool] | None:
    """Parse a brew-boiler temperature off the display text.

    Returns ``(value, is_fahrenheit)`` when the panel shows a numeric brew-boiler
    temperature (e.g. ``"BREW BOIL. 221*F"`` -> ``(221, True)``), else ``None``
    (e.g. on standby it reads ``"BREW BOIL. ECO*"`` with no number).
    """
    match = _BREW_DISPLAY_RE.search(display or "")
    if not match:
        return None
    return int(match.group(1)), match.group(2).upper() == "F"


@dataclass(frozen=True)
class R60VClimateDescription:
    """A boiler modeled as an HA ``climate`` thermostat.

    The machine stores/reports both the live temperature and the setpoint in its
    *current display unit* (reg ``0x00``: Celsius or Fahrenheit). To give Home
    Assistant a single, self-consistent entity, this descriptor **always
    presents Celsius**: it converts the raw byte from the machine's unit on read
    and converts the target back to the machine's unit on write. The climate
    entity therefore declares ``temperature_unit = CELSIUS`` and ranges in
    Celsius, and HA converts uniformly to whatever the dashboard prefers -- so
    the current temp, target, and min/max never disagree on units.

    The thermostat reports ``heat`` only while the boiler is actually energized
    (``is_on``); otherwise it reports ``off``. Setting the mode drives the
    underlying power bit (brew boiler = machine standby; steam boiler = its
    enable), mirroring the Power / Steam Boiler switches.
    """

    key: str
    name: str
    #: Live current-temperature register (raw byte, in the machine's unit).
    current_address: int
    #: Settings address of the writable setpoint (raw byte, in the machine's unit).
    setpoint_address: int
    #: True while the boiler is energized (drives heat vs off).
    is_on: Callable[[StateSnapshot], bool]
    #: Power bit that turns this boiler on/off.
    power_address: int
    power_on: int
    power_off: int
    #: Valid setpoint range in Celsius (min, max) -- also the entity's min/max.
    range_c: tuple[int, int]
    temp_step: float = 1.0
    icon: str | None = None
    #: When True, prefer the actual temperature parsed from the display text
    #: (the ``0xB000`` live register mirrors the setpoint on some units).
    display_current: bool = False

    def current_c(self, s: StateSnapshot) -> int:
        """Live boiler temperature in Celsius (converted from the machine unit).

        When ``display_current`` is set, the actual temperature parsed from the
        front-panel text is preferred over the live register (which can mirror
        the setpoint); it falls back to the register when the panel shows no
        number (e.g. ``ECO`` on standby).
        """
        if self.display_current:
            parsed = parse_display_brew_temp(_live_text(Address.DISPLAY)(s))
            if parsed is not None:
                value, is_f = parsed
                return machine_to_celsius(value, is_f)
        raw = _live_byte(self.current_address)(s)
        return machine_to_celsius(raw, is_fahrenheit(s))

    def target_c(self, s: StateSnapshot) -> int:
        """Setpoint in Celsius (converted from the machine unit)."""
        raw = s.settings_byte(self.setpoint_address)
        return machine_to_celsius(raw, is_fahrenheit(s))

    def encode_setpoint(self, celsius: float, s: StateSnapshot) -> tuple[int, list[int]]:
        """Validate a Celsius target and encode a write in the machine's unit."""
        lo, hi = self.range_c
        cvalue = int(round(float(celsius)))
        if not lo <= cvalue <= hi:
            raise ValueError(f"{cvalue} out of range [{lo}, {hi}] C")
        raw = celsius_to_machine(cvalue, is_fahrenheit(s))
        return self.setpoint_address, [raw]

    def encode_power(self, on: bool) -> tuple[int, list[int]]:
        return self.power_address, [self.power_on if on else self.power_off]


def _brew_is_on(s: StateSnapshot) -> bool:
    # Brew boiler is energized whenever the machine is running (not standby).
    return s.settings_byte(Address.STANDBY) == 0


def _steam_is_on(s: StateSnapshot) -> bool:
    # Steam boiler is energized only when the machine runs AND steam is enabled.
    return s.settings_byte(Address.STANDBY) == 0 and s.settings_byte(
        Address.SERVICE_BOILER_ENABLE
    ) != 0


#: Boiler thermostats (current temp + setpoint) published as `climate` entities.
#: The group setpoint (0x4C) is intentionally NOT exposed: its decode is unclear.
CLIMATE_ENTITIES: list[R60VClimateDescription] = [
    R60VClimateDescription(
        "brew_boiler", "Brew Boiler",
        Address.CURRENT_BREW_TEMP,
        Address.BREW_BOILER_TEMP,
        _brew_is_on,
        Address.STANDBY, 0, 1,   # power_on = standby off, power_off = standby on
        p.BREW_TEMP_RANGE_C,
        1, icon="mdi:coffee-maker",
        display_current=True,   # 0xB000 mirrors the setpoint; trust the panel
    ),
    R60VClimateDescription(
        "steam_boiler", "Steam Boiler",
        Address.CURRENT_SERVICE_TEMP,
        Address.SERVICE_BOILER_TEMP,
        _steam_is_on,
        Address.SERVICE_BOILER_ENABLE, 1, 0,
        p.SERVICE_TEMP_RANGE_C,
        1, icon="mdi:kettle-steam",
    ),
]

#: Fast lookup by key.
CLIMATE_BY_KEY: dict[str, R60VClimateDescription] = {c.key: c for c in CLIMATE_ENTITIES}
