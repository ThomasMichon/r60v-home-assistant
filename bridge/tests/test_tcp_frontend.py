"""Integration tests for the governor-fronted LAN front-end.

The whole point of the front-end is that ``N`` LAN clients collapse onto **one**
upstream socket owned by the :class:`~r60v_broker.governor.DeviceGovernor`.
These tests wire the real stack end to end -- emulator (upstream R60V) ->
:class:`~r60v_broker.client.R60VClient` -> ``DeviceGovernor`` -> front-end -- and
drive it over real sockets, proving that concurrent LAN clients read/write
correctly while the machine only ever sees the single governed connection.
"""
from __future__ import annotations

import asyncio

from r60v_broker import protocol as p
from r60v_broker.client import R60VClient, _expected_response_len
from r60v_broker.emulator import R60VEmulator
from r60v_broker.governor import DeviceGovernor
from r60v_broker.protocol import Address
from r60v_broker.tcp_frontend import R60VFrontend


def _run(coro):
    return asyncio.run(coro)


class _Stack:
    """Emulator + client + governor + front-end, wired and torn down together."""

    def __init__(self) -> None:
        self.emu = R60VEmulator(host="127.0.0.1", port=0)
        self.gov: DeviceGovernor | None = None
        self.frontend: R60VFrontend | None = None

    async def __aenter__(self) -> "_Stack":
        await self.emu.start()
        client = R60VClient(host="127.0.0.1", port=self.emu.bound_port, request_gap=0)
        self.gov = DeviceGovernor(client)
        await self.gov.start()
        self.frontend = R60VFrontend(self.gov, host="127.0.0.1", port=0)
        await self.frontend.start()
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.frontend is not None and self.gov is not None
        await self.frontend.stop()
        await self.gov.stop()
        await self.emu.stop()

    @property
    def port(self) -> int:
        assert self.frontend is not None
        return self.frontend.bound_port


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a LAN client connection and consume the greeting."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    greeting = await reader.readexactly(len(p.HELLO))
    assert greeting.decode() == p.HELLO
    return reader, writer


async def _exchange(reader, writer, frame: str) -> p.Frame:
    """Send one raw request frame and read+parse its fixed-length reply."""
    writer.write(frame.encode("ascii"))
    await writer.drain()
    raw = await reader.readexactly(_expected_response_len(frame))
    return p.parse_frame(raw.decode("ascii"))


def test_frontend_greets_and_serves_read():
    async def scenario():
        async with _Stack() as stack:
            reader, writer = await _connect(stack.port)
            try:
                reply = await _exchange(
                    reader, writer, p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)
                )
                assert reply.command == p.READ
                assert reply.data[Address.BREW_BOILER_TEMP] == 105
            finally:
                writer.close()
    _run(scenario())


def test_frontend_write_then_readback():
    async def scenario():
        async with _Stack() as stack:
            reader, writer = await _connect(stack.port)
            try:
                ack = await _exchange(
                    reader, writer, p.build_write(Address.BREW_BOILER_TEMP, [111])
                )
                assert ack.is_ack
                back = await _exchange(
                    reader, writer, p.build_read(Address.BREW_BOILER_TEMP, 1)
                )
                assert back.data == [111]
            finally:
                writer.close()
    _run(scenario())


def test_concurrent_clients_multiplex_onto_one_upstream():
    """Two LAN clients hammer the front-end at once and both get correct
    replies -- the governor serializes them onto the single upstream socket, so
    the fragile listener never sees more than one conversation."""
    async def client_reads(port: int, iterations: int) -> int:
        reader, writer = await _connect(port)
        ok = 0
        try:
            for _ in range(iterations):
                reply = await _exchange(
                    reader, writer, p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)
                )
                if reply.data[Address.SERVICE_BOILER_TEMP] == 123:
                    ok += 1
        finally:
            writer.close()
        return ok

    async def scenario():
        async with _Stack() as stack:
            # The emulator serves one client connection at a time; if the
            # front-end leaked a socket per LAN client this would deadlock or
            # desync. It doesn't, because every request goes via the governor.
            results = await asyncio.gather(
                client_reads(stack.port, 5),
                client_reads(stack.port, 5),
                client_reads(stack.port, 5),
            )
            assert results == [5, 5, 5]
    _run(scenario())


def test_malformed_frame_drops_connection():
    """A bad-checksum frame makes the front-end drop the client cleanly."""
    async def scenario():
        async with _Stack() as stack:
            reader, writer = await _connect(stack.port)
            try:
                # Well-formed envelope + data, deliberately wrong checksum.
                bad = p.build_read(p.SETTINGS_BASE, p.SETTINGS_LEN)[:-2] + "ZZ"
                writer.write(bad.encode("ascii"))
                await writer.drain()
                # The server closes the connection -> EOF on our side.
                trailing = await reader.read()
                assert trailing == b""
            finally:
                writer.close()
    _run(scenario())
