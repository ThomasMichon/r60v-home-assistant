"""Tests for the DeviceGovernor (single-owner, priority-queued device access)."""
from __future__ import annotations

import asyncio

from r60v_broker import protocol as p
from r60v_broker.protocol import Address
from r60v_broker.governor import DeviceGovernor, PRIORITY_COMMAND, PRIORITY_POLL
from r60v_broker.emulator import R60VEmulator
from r60v_broker.client import R60VClient


def _run(coro):
    return asyncio.run(coro)


def test_read_and_write_via_governor():
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        client = R60VClient(host="127.0.0.1", port=emu.bound_port, request_gap=0)
        gov = DeviceGovernor(client)
        await gov.start()
        try:
            data = await gov.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
            assert data[Address.BREW_BOILER_TEMP] == 105
            ack = await gov.write(Address.BREW_BOILER_TEMP, [111])
            assert ack.is_ack
            assert (await gov.read(Address.BREW_BOILER_TEMP, 1)) == [111]
        finally:
            await gov.stop()
            await emu.stop()
    _run(scenario())


def test_error_propagates_to_caller():
    """A client failure surfaces on the awaiting caller, and the worker lives."""
    class BoomClient:
        async def read(self, address, length):
            raise RuntimeError("boom")
        async def write(self, address, data):
            return "ok"
        async def close(self):
            pass

    async def scenario():
        gov = DeviceGovernor(BoomClient())
        await gov.start()
        try:
            try:
                await gov.read(0x00, 1)
            except RuntimeError as exc:
                assert str(exc) == "boom"
            else:
                raise AssertionError("expected the read error to propagate")
            # Worker survived: a subsequent write still works.
            assert await gov.write(0x00, [1]) == "ok"
        finally:
            await gov.stop()
    _run(scenario())


def test_commands_preempt_polls():
    """A high-priority write jumps ahead of already-queued low-priority reads."""
    class RecordingClient:
        def __init__(self):
            self.order = []
        async def read(self, address, length):
            self.order.append(("read", address))
            return [0]
        async def write(self, address, data):
            self.order.append(("write", address))
            return "ok"
        async def close(self):
            pass

    async def scenario():
        client = RecordingClient()
        gov = DeviceGovernor(client)
        # Enqueue two polls then a command BEFORE starting the worker, so all
        # three sit in the priority queue together.
        tasks = [
            asyncio.create_task(gov.read(0x01, 1, priority=PRIORITY_POLL)),
            asyncio.create_task(gov.read(0x02, 1, priority=PRIORITY_POLL)),
            asyncio.create_task(gov.write(0x4A, [1], priority=PRIORITY_COMMAND)),
        ]
        await asyncio.sleep(0.02)  # let all three enqueue
        await gov.start()
        try:
            await asyncio.gather(*tasks)
            # The command (write) must have been serviced first.
            assert client.order[0] == ("write", 0x4A)
        finally:
            await gov.stop()
    _run(scenario())
