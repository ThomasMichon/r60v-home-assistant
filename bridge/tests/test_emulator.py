"""Integration tests for the R60V emulator over a real TCP socket."""
from __future__ import annotations

import asyncio

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker.emulator import R60VEmulator


async def _read_exact(reader: asyncio.StreamReader, n: int) -> str:
    return (await reader.readexactly(n)).decode("ascii")


async def _session(coro):
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", emu.bound_port)
        # The machine greets first.
        hello = await _read_exact(reader, len(p.HELLO))
        assert hello == p.HELLO
        result = await coro(reader, writer)
        writer.close()
        return result
    finally:
        await emu.stop()


def _run(coro):
    return asyncio.run(_session(coro))


def test_hello_and_read_all_settings():
    async def scenario(reader, writer):
        writer.write(p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN).encode())
        await writer.drain()
        resp = await _read_exact(reader, 9 + p.SETTINGS_LEN * 2 + 2)
        frame = p.parse_frame(resp)
        assert frame.address == p.SETTINGS_BASE
        assert frame.length == p.SETTINGS_LEN
        # Defaults from the machine model.
        assert frame.data[Address.BREW_BOILER_TEMP] == 105
        assert frame.data[Address.SERVICE_BOILER_TEMP] == 123
        assert frame.data[Address.STANDBY] == 0  # machine on
        assert frame.data[Address.AUTO_ON_HOUR] == 14
        return frame

    _run(scenario)


def test_write_then_read_back():
    async def scenario(reader, writer):
        # Write a new brew boiler setpoint.
        writer.write(p.build_write(Address.BREW_BOILER_TEMP, [110]).encode())
        await writer.drain()
        ack = await _read_exact(reader, 9 + len(p.ACK) + 2)
        assert p.parse_frame(ack).is_ack

        # Read it back.
        writer.write(p.build_read(Address.BREW_BOILER_TEMP, 1).encode())
        await writer.drain()
        resp = await _read_exact(reader, 9 + 1 * 2 + 2)
        frame = p.parse_frame(resp)
        assert frame.data == [110]

    _run(scenario)


def test_live_register_read():
    async def scenario(reader, writer):
        writer.write(p.build_read(Address.CURRENT_BREW_TEMP, 1).encode())
        await writer.drain()
        resp = await _read_exact(reader, 9 + 1 * 2 + 2)
        frame = p.parse_frame(resp)
        assert 0 <= frame.data[0] <= 255  # a plausible temperature byte

    _run(scenario)
