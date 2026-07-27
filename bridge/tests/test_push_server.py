"""WebSocket push server tests (bridge local-push).

The push channel carries the RAW register snapshot (settings block + live
registers) so a subscriber reconstructs its own StateSnapshot and decodes with
its own entity logic. A subscriber receives an initial snapshot on connect and
live broadcasts thereafter; the broker runs push-only (no MQTT) against the
emulator, streaming real raw state only while a subscriber is present.
"""
from __future__ import annotations

import asyncio
import json

from r60v_broker.broker import Broker
from r60v_broker.config import Config
from r60v_broker.emulator import R60VEmulator
from r60v_broker.protocol import SETTINGS_LEN, Address
from r60v_broker.push_server import WsPushServer
from websockets.asyncio.client import connect


def test_push_server_sends_snapshot_then_updates():
    async def scenario():
        srv = WsPushServer(host="127.0.0.1", port=0)
        await srv.start()
        try:
            await srv.broadcast({"available": True, "settings": [1, 2, 3], "live": {"45056": [42]}})
            port = srv.bound_port
            async with connect(f"ws://127.0.0.1:{port}") as c:
                snap = json.loads(await asyncio.wait_for(c.recv(), 5))
                assert snap["type"] == "state"
                assert snap["schema"] == 1
                assert snap["available"] is True
                assert snap["settings"] == [1, 2, 3]
                assert snap["live"]["45056"] == [42]

                await srv.broadcast({"available": True, "settings": [9], "live": {}})
                upd = json.loads(await asyncio.wait_for(c.recv(), 5))
                assert upd["settings"] == [9]
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_push_server_broadcast_with_no_clients_is_noop():
    async def scenario():
        srv = WsPushServer(host="127.0.0.1", port=0)
        await srv.start()
        try:
            await srv.broadcast({"available": True, "settings": [0], "live": {}})
            assert srv.client_count == 0
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_broker_push_only_streams_raw_state_and_polls_only_with_a_subscriber():
    """Broker push-only (no MQTT, no front-end): the poll loop stays idle until a
    WebSocket subscriber connects, then streams real raw emulator state."""
    async def scenario():
        emu = R60VEmulator(host="127.0.0.1", port=0)
        await emu.start()
        config = Config(
            machine_host="127.0.0.1",
            machine_port=emu.bound_port,
            request_gap=0,
            mqtt_host="",  # MQTT off -> mqtt_enabled False
            push_enabled=True,
            push_host="127.0.0.1",
            push_port=0,
            live_interval=0.2,
            settings_interval=0.2,
        )
        broker = Broker(config)
        run_task = asyncio.create_task(broker.run())
        try:
            port = None
            for _ in range(300):
                if broker.push is not None:
                    try:
                        port = broker.push.bound_port
                        break
                    except RuntimeError:
                        pass
                await asyncio.sleep(0.01)
            assert port, "push server never bound"

            running = {t.get_name() for t in asyncio.all_tasks()}
            assert "r60v-poll" in running
            assert "r60v-push" in running
            assert "r60v-publish" not in running
            assert "r60v-commands" not in running

            async with connect(f"ws://127.0.0.1:{port}") as c:
                got = None
                for _ in range(40):
                    msg = json.loads(await asyncio.wait_for(c.recv(), 5))
                    if msg.get("settings"):
                        got = msg
                        break
                assert got is not None, "never received a populated raw snapshot"
                assert len(got["settings"]) == SETTINGS_LEN
                # emulator default: machine on (not standby)
                assert got["settings"][Address.STANDBY] == 0
                assert got["live"], "expected some live registers"
        finally:
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            await emu.stop()

    asyncio.run(scenario())


def test_push_server_dispatches_inbound_command_frames():
    """An inbound `command` frame is parsed and handed to on_command; a
    malformed inbound frame is ignored and never breaks the read loop."""
    async def scenario():
        seen: list = []

        async def on_command(frame):
            seen.append(frame)

        srv = WsPushServer(host="127.0.0.1", port=0, on_command=on_command)
        await srv.start()
        try:
            port = srv.bound_port
            async with connect(f"ws://127.0.0.1:{port}") as c:
                await asyncio.wait_for(c.recv(), 5)  # initial snapshot
                await c.send(json.dumps(
                    {"type": "command", "address": 2, "data": [110], "key": "brew_boiler"}
                ))
                await c.send("not json at all")            # ignored
                await c.send(json.dumps({"type": "hello"}))  # wrong type, ignored
                await c.send(json.dumps(
                    {"type": "command", "address": 5, "data": [1]}
                ))
                for _ in range(100):
                    if len(seen) >= 2:
                        break
                    await asyncio.sleep(0.01)
            assert len(seen) == 2
            assert seen[0]["address"] == 2 and seen[0]["data"] == [110]
            assert seen[1]["address"] == 5
        finally:
            await srv.stop()

    asyncio.run(scenario())
