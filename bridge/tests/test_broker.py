"""End-to-end broker test: emulator <-> governor <-> publisher <-> (fake) MQTT.

Exercises the full daemon data path -- governor reads feeding the publisher
cache, publish-from-cache, and command encode/write/refresh -- against the
wire-level emulator, with a fake MQTT bridge capturing published state.
"""
from __future__ import annotations

import asyncio

from r60v_broker.protocol import Address
from r60v_broker.broker import Broker
from r60v_broker.config import Config
from r60v_broker.emulator import R60VEmulator
from r60v_broker.state import LIVE_REGISTERS
from r60v_broker import protocol as p


class FakeMqtt:
    """Records published state, climate, and availability."""

    def __init__(self, config, on_command=None):
        self.config = config
        self.on_command = on_command
        self.states: dict[str, str] = {}
        self.climate: dict[str, dict] = {}
        self.available = None

    def connect(self):  # pragma: no cover
        pass

    def disconnect(self):  # pragma: no cover
        pass

    def publish_discovery(self, *, sw_version=""):  # pragma: no cover
        pass

    def publish_availability(self, online):
        self.available = online

    def publish_state(self, key, value):
        self.states[key] = str(value)

    def publish_climate(self, key, current, target, mode="heat"):
        self.climate[key] = {"current": str(current), "target": str(target), "mode": mode}


async def _make_broker(emu):
    config = Config(machine_host="127.0.0.1", machine_port=emu.bound_port,
                    request_gap=0)
    broker = Broker(config)
    fake = FakeMqtt(config)
    broker.mqtt = fake
    broker.publisher.mqtt = fake
    await broker.governor.start()
    return broker, fake


async def _poll_once(broker):
    broker.publisher.update_settings(
        await broker.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
    )
    for address, length in LIVE_REGISTERS.items():
        broker.publisher.update_live(address, await broker.governor.read(address, length))
    broker.publisher.note_success()


def test_poll_publishes_decoded_state():
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        broker, fake = await _make_broker(emu)
        try:
            await _poll_once(broker)
            broker.publisher.publish()
            states = fake.states
            assert states["power"] == "ON"
            assert states["total_coffee_count"] == "500"
            assert states["display"] == "READY"
            # Boilers are climate thermostats now.
            assert fake.climate["brew_boiler"]["target"] == "105"
            assert 0 <= int(fake.climate["brew_boiler"]["current"]) <= 255
            assert fake.available is True  # note_success -> online
        finally:
            await broker.governor.stop()
            await emu.stop()

    asyncio.run(scenario())


def test_command_writes_and_refreshes():
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        broker, fake = await _make_broker(emu)
        try:
            await _poll_once(broker)
            await broker._apply_command("brew_boiler", "110")
            assert emu.model.settings[Address.BREW_BOILER_TEMP] == 110
            assert fake.climate["brew_boiler"]["target"] == "110"
        finally:
            await broker.governor.stop()
            await emu.stop()

    asyncio.run(scenario())


def test_power_off_command_sets_standby():
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        broker, fake = await _make_broker(emu)
        try:
            await _poll_once(broker)
            await broker._apply_command("power", "OFF")
            assert emu.model.settings[Address.STANDBY] == 1
            assert fake.states["power"] == "OFF"
        finally:
            await broker.governor.stop()
            await emu.stop()

    asyncio.run(scenario())


def test_out_of_range_command_is_rejected():
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        broker, fake = await _make_broker(emu)
        try:
            await _poll_once(broker)
            before = emu.model.settings[Address.BREW_BOILER_TEMP]
            await broker._apply_command("brew_boiler", "200")  # above 115 C
            assert emu.model.settings[Address.BREW_BOILER_TEMP] == before
        finally:
            await broker.governor.stop()
            await emu.stop()

    asyncio.run(scenario())


def test_broker_run_propagates_task_crash():
    """A crashing core task must make run() RAISE, not swallow the error.

    Regression (2026-07-26): a ValueError from a garbled frame unwound the task
    set and closed every server, but the exception was never surfaced -- the
    process lingered alive with no listeners and systemd never restarted it.
    run() must now propagate the crash so the process exits non-zero.
    """
    import pytest

    async def scenario():
        config = Config(
            machine_host="127.0.0.1",
            machine_port=1,
            request_gap=0,
            push_enabled=True,
            push_host="127.0.0.1",
            push_port=0,
            frontend_enabled=False,
        )
        broker = Broker(config)

        async def boom() -> None:
            raise ValueError("simulated garbled-frame crash")

        broker._poll_loop = boom  # type: ignore[assignment]

        with pytest.raises(ValueError, match="simulated"):
            await asyncio.wait_for(broker.run(), timeout=5)

    asyncio.run(scenario())


def test_broker_wedge_cooldown_lifecycle():
    """A sustained wedge makes the poll loop back off (close the link + cooldown),
    then a gentle probe recovers when the machine answers again -- all on the
    bridge, so the integration needs none of it."""
    from r60v_broker.client import R60VConnectionError
    from r60v_broker.wedge import WedgeRecovery
    from tests.test_wedge import Clock

    async def scenario():
        config = Config(machine_host="127.0.0.1", machine_port=1, request_gap=0,
                        push_enabled=False, frontend_enabled=False)
        broker = Broker(config)

        class FakeGov:
            def __init__(self):
                self.closed = 0
                self.fail = True

            async def read(self, address, length):
                if self.fail:
                    raise R60VConnectionError("wedged")
                return [0] * length

            async def close_link(self):
                self.closed += 1

        broker.governor = FakeGov()
        clk = Clock()
        broker.wedge = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0, 600.0),
                                     _now=clk)

        # Failures accrue; availability drops after the store's grace, but no
        # cooldown yet (streak younger than wedge_after).
        for _ in range(6):
            await broker._poll_once(False)
        assert broker.store.available is False
        assert broker.governor.closed == 0
        assert not broker.wedge.in_cooldown

        # Cross the wedge window -> next failing poll enters cooldown + frees link.
        clk.advance(50)
        await broker._poll_once(False)
        assert broker.wedge.in_cooldown
        assert broker.governor.closed == 1

        # Cooldown elapses -> probe still fails -> extend back-off.
        clk.advance(300)
        assert broker.wedge.awaiting_probe
        assert await broker._probe_link() is False

        # Machine recovers: a probe now succeeds -> resume, state cleared.
        broker.governor.fail = False
        assert await broker._probe_link() is True
        broker.wedge.record_success()
        broker.store.note_success()
        assert broker.store.available is True
        assert not broker.wedge.in_cooldown and not broker.wedge.awaiting_probe

    asyncio.run(scenario())


def test_wedge_before_grace_still_marks_offline():
    """Regression: when the (time-based) wedge fires before the (count-based)
    availability grace trips, entering the cooldown must still mark the store
    offline -- otherwise it freezes at last-known ``available`` for the whole
    cooldown (the "shows last known status while truly unreachable" bug).

    A high grace + a wedge window that elapses within only a couple of failing
    polls reproduces the race: without the fix, ``store.available`` stays True.
    """
    from r60v_broker.client import R60VConnectionError
    from r60v_broker.wedge import WedgeRecovery
    from tests.test_wedge import Clock

    async def scenario():
        config = Config(machine_host="127.0.0.1", machine_port=1, request_gap=0,
                        push_enabled=False, frontend_enabled=False)
        broker = Broker(config)
        # Grace deliberately higher than the number of failing polls before the
        # wedge fires, so the grace counter alone would NOT drop availability.
        broker.store.availability_grace = 6

        class DeadGov:
            def __init__(self):
                self.closed = 0

            async def read(self, address, length):
                raise R60VConnectionError("wedged (read timeout)")

            async def close_link(self):
                self.closed += 1

        broker.governor = DeadGov()
        clk = Clock()
        broker.wedge = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0,),
                                     _now=clk)

        # Two failing polls, still inside the wedge window: below grace(6), so
        # availability is (correctly) still last-known.
        await broker._poll_once(False)
        clk.advance(20)
        await broker._poll_once(False)
        assert broker.store.available is True
        assert not broker.wedge.in_cooldown

        # Cross the wedge window on the 3rd failing poll (streak=3 < grace=6):
        # the cooldown is entered AND the store is forced offline.
        clk.advance(30)  # t=50 >= wedge_after=45
        await broker._poll_once(False)
        assert broker.wedge.in_cooldown
        assert broker.store.available is False  # <-- the fix
        assert broker.governor.closed == 1

    asyncio.run(scenario())


def test_write_intent_optimistic_then_reconciles():
    """A write-intent reflects optimistically in the store, lands on the machine,
    and is reconciled by an authoritative settings read."""
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        broker, _fake = await _make_broker(emu)
        try:
            await _poll_once(broker)  # baseline snapshot in the store
            broker._intents = asyncio.Queue()
            await broker._on_ws_command(
                {"type": "command", "address": Address.BREW_BOILER_TEMP,
                 "data": [110], "key": "brew_boiler"}
            )
            # Optimistic apply is immediate (before the governed write runs).
            assert broker.store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
            # Drain the intent through the write+reconcile path.
            address, data = await broker._intents.get()
            await broker._write_and_reconcile(address, data)
            # The machine actually received it, and the store reconciled to truth.
            assert emu.model.settings[Address.BREW_BOILER_TEMP] == 110
            assert broker.store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
            assert broker.store._pending == {}
        finally:
            await broker.governor.stop()
            await emu.stop()

    asyncio.run(scenario())


def test_write_intent_absorbs_transient_write_failure():
    """A transient write failure is retried at the edge and absorbed -- the call
    never raises to the user; the store self-heals on the reconcile read."""
    from r60v_broker.client import R60VConnectionError

    async def scenario():
        config = Config(machine_host="127.0.0.1", machine_port=1, request_gap=0,
                        push_enabled=False, frontend_enabled=False)
        broker = Broker(config)
        broker.store.update_settings([0] * p.SETTINGS_LEN)

        class FlakyGov:
            def __init__(self):
                self.writes = 0

            async def write(self, address, data, **kw):
                self.writes += 1
                if self.writes == 1:
                    raise R60VConnectionError("transient")
                return None

            async def read(self, address, length, **kw):
                # Reconcile read reflects the second (successful) write.
                s = [0] * length
                s[Address.BREW_BOILER_TEMP] = 110
                return s

        broker.governor = FlakyGov()
        broker.store.apply_optimistic(Address.BREW_BOILER_TEMP, [110])
        # Must not raise, and must retry past the first transient failure.
        await broker._write_and_reconcile(Address.BREW_BOILER_TEMP, [110])
        assert broker.governor.writes == 2
        assert broker.store.snapshot.settings[Address.BREW_BOILER_TEMP] == 110
        assert broker.store.available is True

    asyncio.run(scenario())


def test_write_intent_queue_is_bounded():
    """A runaway producer cannot grow the intent queue without limit."""
    from r60v_broker.broker import INTENT_QUEUE_MAX

    async def scenario():
        config = Config(machine_host="127.0.0.1", machine_port=1, request_gap=0,
                        push_enabled=True, push_host="127.0.0.1", push_port=0,
                        frontend_enabled=False)
        broker = Broker(config)
        broker._intents = asyncio.Queue(maxsize=INTENT_QUEUE_MAX)
        broker.store.update_settings([0] * p.SETTINGS_LEN)
        broker.push = None  # skip broadcast in this unit test
        for _ in range(INTENT_QUEUE_MAX + 10):
            await broker._on_ws_command(
                {"type": "command", "address": Address.BREW_BOILER_TEMP, "data": [110]}
            )
        assert broker._intents.qsize() == INTENT_QUEUE_MAX

    asyncio.run(scenario())
