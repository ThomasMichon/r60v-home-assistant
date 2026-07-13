"""Tests for the persistent half-duplex R60VClient against the emulator."""
from __future__ import annotations

import asyncio

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker.client import R60VClient, _expected_response_len
from r60v_broker.emulator import R60VEmulator


def _run(coro):
    return asyncio.run(coro)


async def _with_emulator(scenario, *, warmup=True):
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    client = R60VClient(host="127.0.0.1", port=emu.bound_port, warmup=warmup,
                        request_gap=0)
    try:
        return await scenario(client, emu)
    finally:
        await client.close()
        await emu.stop()


def test_expected_response_len():
    # Read of the full settings block: envelope + data + checksum.
    assert _expected_response_len(p.build_read(0x0000, 0x73)) == 9 + 0x73 * 2 + 2
    # Single-register live read.
    assert _expected_response_len(p.build_read(0xB000, 1)) == 13
    # Write acknowledgement is always 13 chars (envelope + "OK" + checksum).
    assert _expected_response_len(p.build_write(0x4A, [1])) == 13


def test_connect_and_read_all():
    async def scenario(client, emu):
        data = await client.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
        assert len(data) == p.SETTINGS_LEN
        assert data[Address.BREW_BOILER_TEMP] == 105
        assert data[Address.STANDBY] == 0
    _run(_with_emulator(scenario))


def test_write_then_read_back():
    async def scenario(client, emu):
        ack = await client.write(Address.BREW_BOILER_TEMP, [110])
        assert ack.is_ack
        data = await client.read(Address.BREW_BOILER_TEMP, 1)
        assert data == [110]
    _run(_with_emulator(scenario))


def test_single_register_live_read():
    async def scenario(client, emu):
        data = await client.read(Address.CURRENT_BREW_TEMP, 1)
        assert len(data) == 1
        assert 0 <= data[0] <= 255
    _run(_with_emulator(scenario))


def test_request_gap_paces_requests():
    """Consecutive requests are spaced by at least request_gap seconds."""
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        client = R60VClient(host="127.0.0.1", port=emu.bound_port, request_gap=0.1)
        try:
            import time
            await client.read(Address.BREW_BOILER_TEMP, 1)  # warm-up + first read
            start = time.monotonic()
            for _ in range(3):
                await client.read(Address.BREW_BOILER_TEMP, 1)
            elapsed = time.monotonic() - start
            # 3 paced reads must take at least ~2 gaps (>= 0.2s).
            assert elapsed >= 0.2
        finally:
            await client.close()
            await emu.stop()
    _run(scenario())


def test_half_duplex_serializes_concurrent_requests():
    """Many concurrent callers must not interleave on the wire."""
    async def scenario(client, emu):
        addrs = [Address.BREW_BOILER_TEMP, Address.SERVICE_BOILER_TEMP,
                 Address.STANDBY, Address.GROUP_TEMP, Address.ACTIVE_PROFILE]
        results = await asyncio.gather(*(client.read(a, 1) for a in addrs))
        # Each reply is correctly matched to its request (no cross-talk).
        assert results[0] == [105]           # brew setpoint
        assert results[1] == [123]           # service setpoint
        assert results[2] == [0]             # standby off
    _run(_with_emulator(scenario))


def test_auto_reconnect_after_drop():
    """A dropped connection is transparently re-established on next request."""
    async def scenario(client, emu):
        await client.read(Address.BREW_BOILER_TEMP, 1)
        assert client.connected
        # Simulate a link drop by closing our side.
        await client._drop()
        assert not client.connected
        # Next request should reconnect and succeed.
        data = await client.read(Address.BREW_BOILER_TEMP, 1)
        assert data == [105]
        assert client.connected
    _run(_with_emulator(scenario))


def test_connect_failure_raises_without_hanging():
    """With backoff capped low, a request to a dead port eventually gives up."""
    async def scenario():
        # Point at a closed port; keep backoff tiny so the test is fast.
        client = R60VClient(host="127.0.0.1", port=1, backoff_initial=0.01,
                            backoff_max=0.02, connect_timeout=0.2, request_gap=0)

        async def one_request():
            return await client.read(0x00, 1)

        try:
            await asyncio.wait_for(one_request(), timeout=1.0)
        except asyncio.TimeoutError:
            # Expected: it keeps retrying with backoff and never connects.
            pass
        else:
            raise AssertionError("expected the request to keep retrying")
        finally:
            await client.close()

    _run(scenario())


def test_swallowed_request_retries_on_same_connection():
    """A swallowed request is re-issued on the SAME socket (no reconnect).

    The R60V occasionally drops a request; reconnecting on every empty reply
    wedges its listener, so the client must retry in place. This server
    swallows the first two reads (reads them, replies nothing) then answers --
    all on one connection.
    """
    async def scenario():
        conns = {"count": 0}

        async def handler(reader, writer):
            conns["count"] += 1
            writer.write(p.HELLO.encode())
            await writer.drain()
            seen = 0
            while True:
                try:
                    await reader.readexactly(11)
                except asyncio.IncompleteReadError:
                    break
                seen += 1
                if seen <= 2:  # swallow the first two attempts
                    continue
                data = bytearray(p.SETTINGS_LEN)
                data[Address.BREW_BOILER_TEMP] = 105
                writer.write(p.build_frame(p.READ, p.SETTINGS_BASE,
                                           p.SETTINGS_LEN, data).encode())
                await writer.drain()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # warmup=False so only the explicit read exercises the retry path.
        client = R60VClient(host="127.0.0.1", port=port, request_gap=0,
                            request_timeout=0.3, max_retries=3, warmup=False)
        try:
            data = await client.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
            assert data[Address.BREW_BOILER_TEMP] == 105
            # Crucially: it never reconnected -- one connection served it all.
            assert conns["count"] == 1
        finally:
            await client.close()
            server.close()

    _run(scenario())


def test_garbled_warmup_reply_does_not_crash():
    """A bad-checksum reply during warm-up must not escape connection setup.

    Regression for the reviewer's finding: a ProtocolError raised while parsing
    the warm-up reply used to propagate out of request() and kill the daemon.
    The client must swallow it and the first real request must still succeed.
    """
    async def scenario():
        state = {"count": 0}

        def _read_all_reply(corrupt: bool) -> str:
            data = bytearray(p.SETTINGS_LEN)
            data[Address.BREW_BOILER_TEMP] = 105
            reply = p.build_frame(p.READ, p.SETTINGS_BASE, p.SETTINGS_LEN, data)
            return (reply[:-2] + "ZZ") if corrupt else reply  # bad checksum

        async def handler(reader, writer):
            writer.write(p.HELLO.encode())
            await writer.drain()
            while True:
                try:
                    await reader.readexactly(11)  # every read frame is 11 chars
                except asyncio.IncompleteReadError:
                    break
                corrupt = state["count"] == 0  # only the warm-up reply is bad
                writer.write(_read_all_reply(corrupt).encode())
                await writer.drain()
                state["count"] += 1

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = R60VClient(host="127.0.0.1", port=port, request_gap=0)
        try:
            data = await client.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
            assert data[Address.BREW_BOILER_TEMP] == 105
        finally:
            await client.close()
            server.close()

    _run(scenario())
