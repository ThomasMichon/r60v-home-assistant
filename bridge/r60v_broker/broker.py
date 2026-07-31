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
from .wedge import WedgeRecovery

LOGGER = logging.getLogger("r60v.broker")

#: How many times to re-attempt a *transient* write failure at the bridge edge
#: before giving up (absorbing a miss so it never reaches the user).
WRITE_RETRIES = 2
#: Backoff between write retries (seconds).
WRITE_RETRY_BACKOFF = 0.5
#: Bound the write-intent queue so a runaway automation can't grow it without
#: limit; excess intents are dropped (and logged) rather than buffered forever.
INTENT_QUEUE_MAX = 32


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
            WsPushServer(
                self.config.push_host,
                self.config.push_port,
                on_command=self._on_ws_command,
            )
            if self.config.push_enabled
            else None
        )
        # The single arbiter of state + availability. The MQTT projection and
        # the WS push server are both views of THIS store -- neither owns state.
        self.store = DeviceState()
        # Wedge recovery lives on the bridge now (not the integration): when the
        # machine's listener wedges, the poll loop backs off and lets it reset.
        self.wedge = WedgeRecovery()
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
        # Write-intents from the WS command channel (address, data), drained by
        # a single consumer so writes serialize and reconcile in order. Bounded
        # so a runaway producer can't grow it without limit. Created in run()
        # (in-loop) so it binds to the running event loop, not a throwaway one.
        self._intents: asyncio.Queue[tuple[int, list[int]]] | None = None

    # -- command marshalling (paho thread -> asyncio loop) ----------------

    def _enqueue_command(self, key: str, payload: str) -> None:
        if self._loop is not None and self._commands is not None:
            self._loop.call_soon_threadsafe(self._commands.put_nowait, (key, payload))

    # -- run --------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._commands = asyncio.Queue()
        self._intents = asyncio.Queue(maxsize=INTENT_QUEUE_MAX)

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
            # The write-intent consumer: turns WS command frames into governed,
            # optimistic-then-reconciled writes. Only meaningful with the push
            # channel (its transport), so it rides the same enablement.
            tasks.append(
                asyncio.create_task(
                    self._write_intent_loop(), name="r60v-intents"
                )
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
                if self.wedge.in_cooldown:
                    # Wedged: leave the machine alone so its listener can reset.
                    # Free its single client slot and keep serving the cached
                    # (unavailable) state to subscribers.
                    await self.governor.close_link()
                elif self.wedge.awaiting_probe:
                    # The cooldown elapsed -> recover GENTLY with one light read,
                    # and resume full polling only after a run of consecutive good
                    # probes confirms the listener is genuinely back (a single
                    # lucky read from a still-marginal listener must not resume
                    # cadence and immediately re-wedge).
                    if await self._probe_link():
                        if self.wedge.record_probe_success():
                            LOGGER.info(
                                "R60V answered after cooldown; resuming polling"
                            )
                            self.store.note_success()
                        else:
                            LOGGER.info(
                                "R60V post-cooldown probe %d/%d succeeded; "
                                "confirming recovery before resuming",
                                self.wedge.probe_successes,
                                self.wedge.resume_after_probes,
                            )
                    else:
                        step = self.wedge.begin_cooldown()
                        # A failed post-cooldown probe re-confirms the wedge:
                        # keep (or re-assert) the store offline so a momentary
                        # earlier probe success can't leave it reading available.
                        self.store.note_wedged()
                        LOGGER.warning(
                            "R60V still wedged after cooldown; extending back-off "
                            "to %.0fs", step,
                        )
                        await self.governor.close_link()
                else:
                    await self._poll_once(resumed or tick % settings_every == 0)
                # Push consumers get a fresh broadcast tied to the (fast) poll
                # cadence -- near-real-time, read straight from the store (the
                # arbiter). MQTT publishing is driven separately by the steady
                # publish loop.
                if self.push is not None:
                    await self.push.broadcast(self.store.raw_snapshot())
                # Advance the cadence counter exactly once per active poll cycle:
                # it gates the heavier settings read (tick % settings_every). A
                # second increment here would halve the settings interval and
                # double the fragile device's exposure.
                tick += 1
            else:
                idle = True
            await asyncio.sleep(interval)

    async def _poll_once(self, read_settings: bool) -> None:
        """One normal poll cycle: read into the store, tracking wedge state."""
        ok = False
        try:
            if read_settings:
                data = await self.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
                self.store.update_settings(data)
            for address, length in LIVE_REGISTERS.items():
                self.store.update_live(
                    address, await self.governor.read(address, length)
                )
            self.store.note_success()
            self.wedge.record_success()
            ok = True
        except R60VConnectionError as exc:
            # The cache keeps HA continuous; availability drops only after the
            # grace period of consecutive failures.
            LOGGER.warning("poll cycle failed: %s", exc)
        except Exception:  # noqa: BLE001 -- a stray poll error must never kill the loop
            # Defence in depth: an unexpected error in one cycle (e.g. a
            # malformed frame that slips past the codec) must NOT unwind the
            # gather and take the push + front-end servers down with it.
            LOGGER.exception("unexpected error in poll cycle; continuing")
        if not ok:
            self.store.note_failure()
            self.wedge.record_failure()
            if self.wedge.wedged and not self.wedge.in_cooldown:
                step = self.wedge.begin_cooldown()
                # Entering a wedge cooldown is a definitive "machine unreachable"
                # verdict, and the cooldown now STOPS polling -- so force the
                # store offline here rather than relying on the grace counter,
                # which may not have tripped yet if the wedge (time-based) fired
                # before the grace threshold (count-based) was reached.
                self.store.note_wedged()
                LOGGER.warning(
                    "R60V appears wedged (>=%.0fs sustained failure); entering a "
                    "%.0fs cooldown (closing the connection so its listener can "
                    "reset)", self.wedge.wedge_after, step,
                )
                await self.governor.close_link()

    async def _probe_link(self) -> bool:
        """Gentle post-cooldown recovery probe: a single settings read.

        The lightest touch that proves the listener greets *and* answers a read.
        On success it feeds the fresh settings into the store (not wasted); on
        failure it returns False so the caller extends the back-off, leaving the
        still-wedged listener its uninterrupted rest. Never raises.
        """
        try:
            data = await self.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
        except Exception as exc:  # noqa: BLE001 -- a failed probe is expected while wedged
            LOGGER.debug("post-cooldown probe failed: %s", exc)
            return False
        self.store.update_settings(data)
        return True

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

    # -- write-intent channel (WS command frames) -------------------------

    async def _on_ws_command(self, frame: dict) -> None:
        """Handle one inbound WS ``command`` frame: reflect + enqueue.

        Validates the frame, enqueues the write-intent (admission first, so a
        broadcast hiccup can never leave optimism applied with no write queued),
        then optimistically reflects it in the store and broadcasts -- so HA sees
        the intended value at once. The governed write + reconcile happen in
        ``_write_intent_loop``.
        """
        address = frame.get("address")
        data = frame.get("data")
        if not isinstance(address, int) or not isinstance(data, list) or not data:
            LOGGER.warning("ignoring malformed command frame: %r", frame)
            return
        if any(not isinstance(b, int) or b < 0 or b > 0xFF for b in data):
            LOGGER.warning("ignoring command frame with out-of-range bytes: %r", frame)
            return
        if self._intents is None:
            return
        try:
            self._intents.put_nowait((address, list(data)))
        except asyncio.QueueFull:
            LOGGER.warning("write-intent queue full; dropping command %r", frame)
            return
        self.store.apply_optimistic(address, data)
        if self.push is not None:
            await self.push.broadcast(self.store.raw_snapshot())

    async def _write_intent_loop(self) -> None:
        """Drain write-intents, applying each as a governed, reconciled write."""
        assert self._intents is not None
        while True:
            address, data = await self._intents.get()
            try:
                await self._write_and_reconcile(address, data)
            except Exception:  # noqa: BLE001 -- one bad intent must not kill the loop
                LOGGER.exception("unexpected error applying write-intent; continuing")
            finally:
                self._intents.task_done()

    async def _write_and_reconcile(self, address: int, data: list[int]) -> None:
        """Write one intent to the machine (edge-retried) and reconcile the store.

        Waits out any wedge cooldown first (a command must not reconnect during
        the machine's required rest). A transient write failure is retried a
        bounded number of times and, if still failing, absorbed here -- never
        surfaced to the user. The command's optimistic overlay is then cleared
        and an authoritative settings read reconciles the store (a failed write
        self-heals to the machine's real value), followed by a final broadcast.
        """
        # Wait out any wedge cooldown -- a command must not reconnect during the
        # machine's required rest. The cooldown is time-based state owned by the
        # poll loop's WedgeRecovery clock (not an event we can await), so we poll
        # it, sleeping the remaining window (bounded, cancellable) each time.
        while self.wedge.in_cooldown:  # noqa: ASYNC110 -- polling poll-loop-owned wedge clock
            await asyncio.sleep(min(self.wedge.cooldown_remaining + 0.05, 5.0))

        addresses = [address + offset for offset in range(len(data))]
        wrote = False
        for attempt in range(WRITE_RETRIES + 1):
            try:
                await self.governor.write(address, data)
                wrote = True
                break
            except R60VConnectionError as exc:
                if attempt < WRITE_RETRIES:
                    LOGGER.debug(
                        "write to 0x%02X failed (%d/%d); retrying: %s",
                        address, attempt + 1, WRITE_RETRIES, exc,
                    )
                    await asyncio.sleep(WRITE_RETRY_BACKOFF)
                    continue
                LOGGER.warning(
                    "write to 0x%02X failed after %d tries; absorbing: %s",
                    address, WRITE_RETRIES + 1, exc,
                )

        # This command's optimism is spent -- clear ONLY the overlay bytes this
        # command set, and only if a newer command to the same address hasn't
        # superseded them (so the latest user intent is never yanked back to an
        # older one). The reconcile read below is now the truth for the rest.
        self.store.reconcile_clear({addr: byte for addr, byte in zip(addresses, data)})
        if not wrote:
            self.store.note_failure()
        try:
            self.store.update_settings(
                await self.governor.read(p.SETTINGS_BASE, p.SETTINGS_LEN)
            )
            self.store.note_success()
        except R60VConnectionError as exc:
            LOGGER.warning("reconcile read after write failed: %s", exc)
            self.store.note_failure()
        if self.push is not None:
            await self.push.broadcast(self.store.raw_snapshot())


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
