"""Tests for the R60V wire-protocol codec."""
from __future__ import annotations

import pytest

from r60v_broker import protocol as p
from r60v_broker.protocol import Address


def test_checksum_known_vector():
    # "set language to English" from the reverse-engineering doc.
    assert p.checksum("w0001000100") == "59"


def test_build_write_known_vector():
    assert p.build_write(Address.LANGUAGE, [0x00]) == "w000100010059"


def test_build_read_all_settings():
    # ReadAll: read 0x0000, length 0x73 (115 bytes).
    frame = p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)
    assert frame.startswith("r00000073")
    parsed = p.parse_frame(frame)
    assert parsed.command == p.READ
    assert parsed.address == 0x0000
    assert parsed.length == 0x73


def test_build_read_counter_vector():
    assert p.build_read(Address.COUNTERS_BASE, Address.COUNTERS_LEN).startswith("r00D90038")


def test_roundtrip_write_with_data():
    raw = p.build_write(Address.BREW_BOILER_TEMP, [105])
    frame = p.parse_frame(raw)
    assert frame.command == p.WRITE
    assert frame.address == Address.BREW_BOILER_TEMP
    assert frame.length == 1
    assert frame.data == [105]


def test_parse_rejects_bad_checksum():
    with pytest.raises(p.ProtocolError):
        p.parse_frame("w000100010000")


def test_ack_roundtrip():
    ack = p.build_ack(p.WRITE, Address.LANGUAGE, 1)
    frame = p.parse_frame(ack)
    assert frame.is_ack
    assert frame.envelope == "w00010001"


def test_encode_decode_data_roundtrip():
    data = [0x00, 0x0F, 0xF4, 0x01, 0xFF]
    assert p.decode_data(p.encode_data(data)) == data


def test_decode_data_rejects_non_hex_as_protocol_error():
    # A desync'd/garbled read yields non-hex chars; it must surface as a
    # ProtocolError (which request()/the poll loop handle), never a raw
    # ValueError that would escape and tear down the daemon (regression: a
    # 'rB' byte in a garbled frame crashed the broker, 2026-07-26).
    with pytest.raises(p.ProtocolError):
        p.decode_data("rB")


def test_parse_frame_wraps_garbled_payload():
    # A frame whose envelope parses but whose payload is non-hex must raise
    # ProtocolError, not ValueError, so client.request()'s retry path handles it.
    envelope_ok = "r00010001"
    payload = "gg"  # non-hex
    raw = envelope_ok + payload + p.checksum(envelope_ok + payload)
    with pytest.raises(p.ProtocolError):
        p.parse_frame(raw)
