"""The Rocket R60V broker daemon.

Composes three cleanly separated pieces:

- :class:`~r60v_broker.governor.DeviceGovernor` — the **sole owner** of the
  device link. Every read/write is submitted to it and serialized through one
  throttled worker draining a priority queue (commands beat polls). Nothing else
  touches the socket, so the fragile listener sees exactly one calm conversation.
- :class:`~r60v_broker.publisher.StatePublisher` — the **MQTT-facing illusion of
  continuity**. It caches the last-known state, publishes on a steady cadence
  regardless of device hiccups, holds/interpolates through misses, and manages
  availability with a grace period.
- this daemon — the **orchestrator**: a gentle poll loop feeds the cache via the
  governor, a publish loop emits the cached view, and a command loop turns HA
  commands into range-validated, high-priority writes.

Run:  ``python -m r60v_broker.broker``  (configured via environment; see
:class:`~r60v_broker.config.Config`).
"""
from __future__ import annotations

import asyncio
import logging
import signal

from . import __version__
from . import protocol as p
from .client import R60VClient, R60VConnectionError
from .config import Config
from .governor import DeviceGovernor
from .mqtt_bridge import MqttBridge
from .publisher import StatePublisher
from .push_server import WsPushServer
from .state import CLIMATE_BY_KEY, ENTITIES_BY_KEY, LIVE_REGISTERS
from .store import DeviceState
from .tcp_frontend import R60VFrontend

LOGGER = logging.getLogger("r60v.broker")


class Broker:
    """Wires the device governor to the MQTT state publisher."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        client = R60VClient(
            self.config.machine_host,
            self.config.machine_port,
            request_gap=self.config.request_gap,
            request_timeout=self.config.request_timeout,
        )
        self.governor = DeviceGovernor(client)
        self.mqtt = MqttBridge(self.config, on_command=self._enqueue_command)
        # Optional WebSocket push server: streams the cached state to LAN
        # subscribers (a local_push HA integration). Fed by the poll loop; never
        # touches the device.
        self.push: WsPushServer | None = (
            WsPushServer(self.config.push_host, self.config.push_port)
            if self.config.push_enabled
            else None
        )
        # The single arbiter of state + availability. The MQTT projection and
        # the WS push server are both views of THIS store -- neither owns state.
        self.store = DeviceState()
        # The publisher writes decoded state to the MQTT sink (used only when
        # MQTT is enabled). The WS push server is fed separately, via
        # raw_snapshot(), so enabling both never double-publishes to MQTT.
        self.publisher = StatePublisher(self.config, self.mqtt, store=self.store)
        # Optional native-protocol LAN front-end, also fronted by
        # the governor so it never opens its own upstream socket.
        self.frontend: R60VFrontend | None = (
            R60VFrontend(
                self.governor,
                host=self.config.frontend_host,
                port=self.config.frontend_port,
            )
            if self.config.frontend_enabled
            else None
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._commands: asyncio.Queue[tuple[str, str]] | None = None

    # -- command marshalling (paho thread -> asyncio loop) ----------------

    def _enqueue_command(self, key: str, payload: str) -> None:
        if self._loop is not None and self._commands is not None:
            self._loop.call_soon_threadsafe(self._commands.put_nowait, (key, payload))

    # -- run --------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._commands = asyncio.Queue()

        if self.config.mqtt_enabled:
            self.mqtt.connect()
            self.mqtt.publish_discovery(sw_version=__version__)
        else:
            LOGGER.info("MQTT disabled (no MQTT_HOST)")
        await self.governor.start()

        tasks: list[asyncio.Task] = []
        # The poll loop feeds the publisher cache; the publish loop emits the
        # cached view to MQTT; the command loop routes MQTT commands into writes.
        # A "consumer" is anything that wants that cached state -- MQTT or the WS
        # push server. With NO consumer (front-end-only mode), running the poll
        # loop would only add autonomous churn to the fragile device link, so it
        # stays off and the single LAN client drives the governor on demand.
        has_consumer = self.config.mqtt_enabled or self.push is not None
        if has_consumer:
            tasks.append(asyncio.create_task(self._poll_loop(), name="r60v-poll"))
        if self.config.mqtt_enabled:
            tasks.append(
                asyncio.create_task(self._command_loop(), name="r60v-commands")
            )
            tasks.append(
                asyncio.create_task(self._publish_loop(), name="r60v-publish")
            )
        if self.push is not None:
            tasks.append(
                asyncio.create_task(self.push.serve_forever(), name="r60v-push")
            )
        if self.frontend is not None:
            tasks.append(
                asyncio.create_task(
                    self.frontend.serve_forever(), name="r60v-frontend"
                )
            )
        if not tasks:
            LOGGER.error(
                "no consumer enabled -- nothing to do. Set MQTT_HOST, "
                "R60V_PUSH_ENABLED=true, or R60V_FRONTEND_ENABLED=true."
            )
            await self.governor.stop()
            return
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            LOGGER.info("broker cancelled; shutting down")
        except Exception:
            # A core task raised. Log the cause (previously this exception was
            # swallowed by an un-awaited runner task, leaving the process alive
            # but with every server torn down -- see main._run) and re-raise so
            # the process exits non-zero and systemd restarts it.
            LOGGER.exception("broker task crashed; shutting down for restart")
            raise
        finally:
            for task in tasks:
                task.cancel()
            if self.frontend is not None:
                await self.frontend.stop()
            if self.push is not None:
                await self.push.stop()
            await self.governor.stop()
            if self.config.mqtt_enabled:
                self.mqtt.disconnect()

    # -- polling (feeds the cache via the governor) -----------------------

    async def _poll_loop(self) -> None:
        interval = max(0.2, self.config.live_interval)
        settings_every = max(1, round(self.config.settings_interval / interval))
        tick = 0
        idle = True
        while True:
            # Only touch the fragile device when a consumer actually wants data:
            # MQTT always wants continuity, but a push-only bridge polls ONLY
            # while a subscriber is connected -- no client, no reason to poke the
            # machine. Resuming from idle forces a fresh settings read.
            wants_data = self.config.mqtt_enabled or (
                self.push is not None and self.push.client_count > 0
            )
            if wants_data:
                resumed, idle = idle, False
                try:
                    if resumed or tick % settings_every == 0:
                        data = await self.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
                        self.store.update_settings(data)
                    for address, length in LIVE_REGISTERS.items():
                        self.store.update_live(
                            address, await self.governor.read(address, length)
                        )
                    self.store.note_success()
                except R60VConnectionError as exc:
                    # The cache keeps HA continuous; availability drops only after
                    # the grace period of consecutive failures.
                    LOGGER.warning("poll cycle failed: %s", exc)
                    self.store.note_failure()
                except Exception:  # noqa: BLE001 -- a stray poll error must never kill the loop
                    # Defence in depth: an unexpected error in one cycle (e.g. a
                    # malformed frame that slips past the codec) must NOT unwind
                    # the gather and take the push + front-end servers down with
                    # it. Log it, count a failure, and keep polling.
                    LOGGER.exception("unexpected error in poll cycle; continuing")
                    self.store.note_failure()
                # Push consumers get a fresh broadcast tied to the (fast) poll
                # cadence -- near-real-time, read straight from the store (the
                # arbiter). MQTT publishing is driven separately by the steady
                # publish loop.
                if self.push is not None:
                    await self.push.broadcast(self.store.raw_snapshot())
                tick += 1
            else:
                idle = True
            await asyncio.sleep(interval)
            tick += 1

    # -- publishing (steady cadence, from cache) --------------------------

    async def _publish_loop(self) -> None:
        interval = max(0.5, self.config.publish_interval)
        while True:
            self.publisher.publish()
            await asyncio.sleep(interval)

    # -- commands ---------------------------------------------------------

    async def _command_loop(self) -> None:
        assert self._commands is not None
        while True:
            key, payload = await self._commands.get()
            await self._apply_command(key, payload)

    async def _apply_command(self, key: str, payload: str) -> None:
        # Resolve the write: a plain writable entity, or a climate thermostat's
        # target setpoint (keyed by the climate entity's key).
        if key in ENTITIES_BY_KEY and ENTITIES_BY_KEY[key].writable:
            encode = ENTITIES_BY_KEY[key].encode
        elif key in CLIMATE_BY_KEY:
            encode = CLIMATE_BY_KEY[key].encode_target
        else:
            LOGGER.warning("ignoring command for unknown/read-only entity %r", key)
            return
        try:
            address, data = encode(payload)  # type: ignore[misc]
        except ValueError as exc:
            LOGGER.warning("rejected command %s=%r: %s", key, payload, exc)
            return
        try:
            # High priority: jump ahead of routine polls.
            await self.governor.write(address, data)
            # Re-read settings so HA reflects the machine's actual state.
            self.store.update_settings(
                await self.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
            )
            self.store.note_success()
            self.publisher.publish()
        except R60VConnectionError as exc:
            LOGGER.warning("failed to apply command %s: %s", key, exc)
            self.store.note_failure()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = Broker()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # e.g. on platforms without signal support
        runner = asyncio.create_task(broker.run())
        stopper = asyncio.create_task(stop.wait())
        # Wait for EITHER a shutdown signal OR the broker exiting on its own.
        # The second case is the one that bit us (2026-07-26): if any core task
        # raised, the broker's task set unwound and every server closed, but this
        # function only ever awaited `stop.wait()`, so the runner's exception was
        # never retrieved -- the process lingered ALIVE with no listeners and
        # systemd, seeing a live PID, never restarted it. Now we also watch the
        # runner: if it finishes unprompted we re-raise its failure so the
        # process exits non-zero and `Restart=on-failure` kicks in.
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if runner.done():
            runner.result()  # re-raises the crash (or returns on a clean exit)
            return
        # A signal arrived: cancel the broker and drain it cleanly.
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
