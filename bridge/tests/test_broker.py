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
