"""Rocket R60V wire-protocol codec (framing, checksum, address map).

This is the single source of truth for the R60V control protocol, shared by
the broker (client) and the emulator (server). It is a faithful, dependency-free
implementation of the protocol reverse-engineered from the official Android app
and documented in ``docs/reference/rocket-r60v-protocol.md``.

Wire format (all ASCII, each byte written as two uppercase hex characters)::

    <command><address:4><length:4><data...><checksum:2>

- ``command``   -- ``r`` (read) or ``w`` (write)
- ``address``   -- uint16, big-endian hex text
- ``length``    -- uint16, number of *data bytes*
- ``data``      -- ``length`` bytes, each two hex chars (write only)
- ``checksum``  -- ``sum(preceding ASCII bytes) & 0xFF`` as two hex chars

A write is acknowledged with the request envelope followed by the literal
``OK`` and a checksum. The connection opens with the server greeting ``*HELLO*``.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import reduce

#: Default control endpoint the machine listens on (its own AP).
DEFAULT_HOST = "192.168.1.1"
DEFAULT_PORT = 1774

#: Greeting the machine sends immediately after a client connects.
HELLO = "*HELLO*"

#: Write-acknowledgement payload (returned in the data field of a write reply).
ACK = "OK"

READ = "r"
WRITE = "w"


class ProtocolError(Exception):
    """Raised when a frame is malformed or fails checksum validation."""


def checksum(message: str) -> str:
    """Return the 2-char uppercase hex checksum of ``message``.

    The checksum is the sum of the ASCII byte values of every character in
    ``message`` (the frame *without* its own checksum), modulo 256.
    """
    total = reduce(lambda acc, byte: acc + byte, message.encode(), 0) % 256
    return f"{total:02X}"


def encode_data(data: bytes | bytearray | list[int] | None) -> str:
    """Encode a byte sequence as an uppercase hex string (``None`` -> ``""``)."""
    if not data:
        return ""
    return "".join(f"{b & 0xFF:02X}" for b in data)


def decode_data(hex_str: str) -> list[int]:
    """Decode an even-length uppercase hex string into a list of byte values.

    A desync'd/garbled read yields non-hex characters here; surface that as a
    :class:`ProtocolError` (not a raw ``ValueError``) so callers that already
    handle protocol faults -- ``request()``'s same-socket retry, the poll loop's
    failure path -- treat it as the transient frame corruption it is, rather than
    letting an unhandled ``ValueError`` escape and tear down the daemon.
    """
    if len(hex_str) % 2:
        raise ProtocolError(f"odd-length data field: {hex_str!r}")
    try:
        return [int(hex_str[i : i + 2], 16) for i in range(0, len(hex_str), 2)]
    except ValueError as exc:
        raise ProtocolError(f"non-hex data field: {hex_str!r}") from exc


def build_envelope(command: str, address: int, length: int) -> str:
    """Build the 9-char envelope (command + address + length)."""
    if command not in (READ, WRITE):
        raise ProtocolError(f"invalid command: {command!r}")
    return f"{command}{address:04X}{length:04X}"


def build_frame(
    command: str,
    address: int,
    length: int,
    data: bytes | bytearray | list[int] | None = None,
) -> str:
    """Build a complete raw message (envelope + data + checksum)."""
    envelope = build_envelope(command, address, length)
    body = envelope + encode_data(data)
    return body + checksum(body)


def build_read(address: int, length: int) -> str:
    """Build a read request for ``length`` bytes at ``address``."""
    return build_frame(READ, address, length)


def build_write(address: int, data: bytes | bytearray | list[int]) -> str:
    """Build a write request placing ``data`` at ``address``."""
    return build_frame(WRITE, address, len(data), data)


def build_ack(command: str, address: int, length: int) -> str:
    """Build a write acknowledgement (envelope + ``OK`` + checksum)."""
    body = build_envelope(command, address, length) + ACK
    return body + checksum(body)


@dataclass(frozen=True)
class Frame:
    """A parsed protocol frame."""

    command: str
    address: int
    length: int
    data: list[int]
    checksum: str
    raw: str

    @property
    def envelope(self) -> str:
        """The 9-char envelope of this frame."""
        return self.raw[:9]

    @property
    def is_ack(self) -> bool:
        """Whether this frame is a write acknowledgement (``OK`` payload)."""
        return self.raw[9:11] == ACK


def parse_frame(raw: str, *, verify: bool = True) -> Frame:
    """Parse a raw message string into a :class:`Frame`.

    :param verify: when true, validate the trailing checksum.
    :raises ProtocolError: on malformed input or checksum mismatch.
    """
    if len(raw) < 11:
        raise ProtocolError(f"frame too short: {raw!r}")
    command = raw[0]
    if command not in (READ, WRITE):
        raise ProtocolError(f"invalid command: {command!r}")
    try:
        address = int(raw[1:5], 16)
        length = int(raw[5:9], 16)
    except ValueError as exc:
        raise ProtocolError(f"bad envelope in {raw!r}") from exc

    payload = raw[9:-2]
    supplied = raw[-2:]

    # A write acknowledgement carries the literal "OK" instead of `length` bytes.
    if payload == ACK:
        data: list[int] = []
    else:
        data = decode_data(payload)

    if verify and checksum(raw[:-2]) != supplied.upper():
        raise ProtocolError(
            f"checksum mismatch in {raw!r}: got {supplied}, want {checksum(raw[:-2])}"
        )

    return Frame(command, address, length, data, supplied, raw)


# ---------------------------------------------------------------------------
# Memory address map (see docs/reference/rocket-r60v-protocol.md section 6)
# ---------------------------------------------------------------------------

class Address:
    """R60V memory addresses. Settings block is 0x00-0x72; live/read-only
    registers live at 0xA000+/0xB000+; counters at 0x00D9+."""

    # Settings block (read/write)
    TEMPERATURE_UNIT = 0x00      # 0 = Celsius, 1 = Fahrenheit
    LANGUAGE = 0x01              # 0 En, 1 De, 2 Fr, 3 It
    BREW_BOILER_TEMP = 0x02      # setpoint
    SERVICE_BOILER_TEMP = 0x03   # steam boiler setpoint
    KP_COFFEE = 0x04
    KP_GROUP = 0x06
    KI_COFFEE = 0x0A
    KI_GROUP = 0x0C
    KD_COFFEE = 0x10
    KD_GROUP = 0x12
    PROFILE_A = 0x16             # 16-byte block
    PROFILE_B = 0x26            # 16-byte block
    PROFILE_C = 0x36            # 16-byte block
    PRE_INFUSION_ENABLE = 0x2B
    PRE_INFUSION_T_OFF = 0x2C
    PRE_INFUSION_T_ON = 0x30
    WAND_TEMP = 0x45
    WATER_FEED = 0x46           # tank / mains
    ACTIVE_PROFILE = 0x47       # selects A/B/C
    WAND_WASH_TIME = 0x48
    SERVICE_BOILER_ENABLE = 0x49
    STANDBY = 0x4A              # 0 = on, 1 = standby (toggled)
    GROUP_TEMP = 0x4C          # group setpoint
    TOTAL_COFFEE_COUNT = 0x4D
    AUTO_ON_HOUR = 0x51
    AUTO_ON_MINUTE = 0x52
    AUTO_OFF_HOUR = 0x53
    AUTO_OFF_MINUTE = 0x54

    # Counters block (read via r00D90038)
    COUNTERS_BASE = 0x00D9
    COUNTERS_LEN = 0x38

    # Live / read-only registers
    DATE_TIME = 0xA000
    CURRENT_BREW_TEMP = 0xB000
    CURRENT_SERVICE_TEMP = 0xB001
    CURRENT_PRESSURE = 0xB002
    DISPLAY = 0xB007


#: The full settings block spans addresses 0x00..0x72 (115 bytes); ReadAll.
SETTINGS_BASE = 0x0000
SETTINGS_LEN = 0x73

#: Safe write ranges (min, max) in Celsius, from the app's SettingsSingleton.
BREW_TEMP_RANGE_C = (85, 115)
SERVICE_TEMP_RANGE_C = (115, 125)
GROUP_TEMP_RANGE_C = (89, 100)
