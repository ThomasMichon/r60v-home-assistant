"""DataUpdateCoordinator that polls the R60V off the event loop."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .bridge_health import BridgeHealth
from .client import R60VClient, R60VConnectionError
from .const import DOMAIN
from .entities import LIVE_REGISTERS, StateSnapshot
from .protocol import SETTINGS_BASE, SETTINGS_LEN, ProtocolError

LOGGER = logging.getLogger(__name__)

#: How often to poll the machine. Local polling; the machine is slow and its
#: single-socket listener dislikes churn, so a relaxed interval is deliberate.
UPDATE_INTERVAL = timedelta(seconds=30)

#: How many consecutive failed polls to tolerate before marking the device
#: unavailable. The machine's single-socket listener intermittently desyncs the
#: stream (~5% of polls), so a single failed poll must NOT blank every entity.
#: We keep serving the last-known-good snapshot until the failures are sustained
#: (> ~2.5 min at the 30s interval), which indicates a genuine outage.
FAILURE_TOLERANCE = 5

#: Backoff schedule for the "cooldown" recovery. The R60V's single-socket
#: listener can **wedge** -- it keeps greeting but swallows every read and does
#: NOT self-recover while a client keeps knocking. The documented escape is to
#: stop touching it for a while so its control module resets. When failures
#: become sustained we therefore enter a cooldown: we close the connection
#: (freeing the machine's single client slot) and stop polling for a spell,
#: lengthening the wait on each repeat. Range matches the observed 5-30 min
#: recovery window.
#:
#: **Ownership.** The bridge (``r60v_broker.wedge``) is the *designated single
#: owner* of the wedge cooldown discipline. This coordinator carries a mirror of
#: that schedule for **bridge-less polling mode** only -- when a bridge is present
#: (push mode) ``_async_update_data`` runs no cooldown logic and the integration
#: consumes the bridge's availability verdict instead. ``COOLDOWN_STEPS`` and
#: ``RESUME_AFTER_PROBES`` are pinned to the bridge's canonical values by
#: ``tests/test_cooldown_parity.py`` so the two mirrors cannot drift as one side
#: is tuned.
COOLDOWN_STEPS: tuple[timedelta, ...] = (
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=20),
    timedelta(minutes=30),
)

#: Consecutive successful post-cooldown probes required before resuming full
#: polling. Mirrors ``r60v_broker.wedge.RESUME_AFTER_PROBES``: a single lucky
#: read from a still-marginal listener must not resume cadence and re-wedge.
RESUME_AFTER_PROBES = 2

# Connection-state vocabulary (surfaced by the Connection sensor).
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_COOLDOWN = "cooldown"
# The bridge transport is up but the machine itself is unreachable (the store --
# the arbiter -- says so): distinct from a transport reconnect so the operator
# can tell "my espresso machine is off/wedged" from "HA lost the bridge".
STATE_MACHINE_UNAVAILABLE = "machine_unavailable"
# The bridge (not the machine) is the problem: the link is down, or the bridge
# is actively recovering it (a diagnostic window). Distinct from a machine wedge
# so the operator -- and the machine-wedge cooldown -- don't misattribute it.
STATE_BRIDGE_DOWN = "bridge_down"
STATE_BRIDGE_RECOVERING = "bridge_recovering"

#: Health-fetcher signature: given a URL, return a parsed snapshot or None.
HealthFetcher = Callable[[str], Awaitable[BridgeHealth | None]]


class R60VCoordinator(DataUpdateCoordinator[StateSnapshot]):
    """Polls the settings block and live registers into a StateSnapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: R60VClient,
        *,
        bridge_health_url: str | None = None,
        health_fetcher: HealthFetcher | None = None,
        push_enabled: bool = False,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            # In push mode the bridge streams state to us (via the push client
            # calling async_set_updated_data), so we do NOT poll on a timer --
            # the one-shot first refresh loads initial data, then the stream
            # drives every update. Polling mode keeps the timed interval.
            update_interval=None if push_enabled else UPDATE_INTERVAL,
        )
        self.push_enabled = push_enabled
        self.client = client
        self._consecutive_failures = 0
        self._cooldown_until: datetime | None = None
        self._cooldown_index = 0
        self._probe_successes = 0
        # Bridge-health back-channel (optional).
        self.bridge_health_url = bridge_health_url
        self._health_fetcher = health_fetcher
        self._bridge: BridgeHealth | None = None
        # Push-mode command channel (optional): set by __init__ once the push
        # client exists, so writes can be submitted as store-reconciled intents
        # instead of synchronous machine writes. Duck-typed to avoid an import
        # cycle with push_client.
        self._push_client = None

    # -- bridge-health back-channel --------------------------------------

    @property
    def bridge_health_enabled(self) -> bool:
        """True when a bridge-health endpoint is configured."""
        return bool(self.bridge_health_url) and self._health_fetcher is not None

    @property
    def bridge_health(self) -> BridgeHealth | None:
        """Last-fetched bridge snapshot (None when disabled/unknown)."""
        return self._bridge

    async def _refresh_bridge_health(self) -> None:
        """Refresh the cached bridge snapshot. Never raises."""
        if not self.bridge_health_enabled:
            return
        assert self._health_fetcher is not None and self.bridge_health_url is not None
        try:
            self._bridge = await self._health_fetcher(self.bridge_health_url)
        except Exception as exc:  # defensive: the back-channel must never break polling
            LOGGER.debug("bridge health fetch failed: %s", exc)
            self._bridge = None

    async def async_refresh_bridge_health(self) -> None:
        """Refresh the bridge-health snapshot and re-render the diagnostics.

        Used by a periodic timer in **push mode** (where ``_async_update_data``
        does not run on an interval), so the bridge/link diagnostics stay live
        instead of frozen at the value fetched at setup.
        """
        await self._refresh_bridge_health()
        self.async_update_listeners()

    def _bridge_blocking(self) -> bool:
        """True when a fresh, usable bridge signal says to pause machine polling."""
        return self._bridge is not None and self._bridge.blocking

    # -- cooldown state ---------------------------------------------------

    @property
    def in_cooldown(self) -> bool:
        """True while a wedge-recovery cooldown is active."""
        return self._cooldown_until is not None and dt_util.utcnow() < self._cooldown_until

    @property
    def cooldown_remaining(self) -> int:
        """Seconds left in the current cooldown (0 when not cooling down)."""
        if not self.in_cooldown:
            return 0
        assert self._cooldown_until is not None
        return max(0, int((self._cooldown_until - dt_util.utcnow()).total_seconds()))

    @property
    def cooldown_ends_at(self) -> datetime | None:
        return self._cooldown_until if self.in_cooldown else None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def connection_state(self) -> str:
        """A coarse connection status for the diagnostic sensor.

        In **push mode** ``last_update_success`` reflects the *bridge transport*
        (up while frames flow), and the store decides machine reachability -- so
        we must not report ``connected`` while the store says the machine is
        unavailable, or we'd reintroduce the very "connected while every entity
        is unavailable" confusion this integration exists to avoid. (Phase 5 will
        split this into an explicit transport-vs-machine taxonomy.)
        """
        if self._bridge_blocking():
            assert self._bridge is not None
            return (
                STATE_BRIDGE_RECOVERING
                if self._bridge.diagnostic_window
                else STATE_BRIDGE_DOWN
            )
        if self.in_cooldown:
            return STATE_COOLDOWN
        if not self.last_update_success:
            # Transport down (a dropped stream in push mode; a failed poll in
            # polling mode) -> we are reconnecting, and the diagnostic says so.
            return STATE_RECONNECTING
        # Transport is up. In push mode the store is the arbiter of machine
        # reachability: surface a distinct "machine unavailable" instead of
        # claiming "connected" (or mislabeling it as a transport reconnect) --
        # so no signal ever reads "connected" while the machine is unavailable.
        # (`snapshot` is the coordinator's cached data, not a device handle -- no
        # I/O here.)
        snapshot = self.data
        if self.push_enabled and snapshot is not None and not snapshot.available:
            return STATE_MACHINE_UNAVAILABLE
        return STATE_CONNECTED

    def _enter_cooldown(self) -> None:
        step = COOLDOWN_STEPS[min(self._cooldown_index, len(COOLDOWN_STEPS) - 1)]
        self._cooldown_until = dt_util.utcnow() + step
        self._cooldown_index += 1
        # A fresh cooldown discards any partial confirm progress: the machine
        # must earn a clean run of good probes again before polling resumes.
        self._probe_successes = 0
        LOGGER.warning(
            "R60V appears wedged (%d consecutive failures); entering a %s cooldown "
            "(closing the connection so its listener can reset)",
            self._consecutive_failures, step,
        )

    async def async_end_cooldown(self) -> None:
        """Operator override: end any cooldown now and retry immediately."""
        if self._cooldown_until is not None:
            LOGGER.info("cooldown override: resuming R60V polling now")
        self._clear_cooldown()
        await self.async_request_refresh()

    def _clear_cooldown(self) -> None:
        """Reset all wedge-recovery state (after a good read or an override)."""
        self._cooldown_until = None
        self._cooldown_index = 0
        self._consecutive_failures = 0
        self._probe_successes = 0

    async def _probe_healthy(self) -> bool:
        """Gentle control-path health probe used to exit a cooldown.

        Reads **only** the settings block -- the lightest touch that proves the
        machine's listener greets *and* answers a read. This is deliberately not
        the full live-register sweep (`_read_snapshot`): a lighter probe is far
        less likely to trip on a healthy-but-briefly-flaky listener, and (unlike
        an ICMP-level reachability check) a successful read is real proof the
        control path is back. Never raises; frees the connection slot on failure
        so a still-wedged listener keeps its uninterrupted rest.
        """
        try:
            await self.client.read(SETTINGS_BASE, SETTINGS_LEN)
            return True
        except (R60VConnectionError, ProtocolError) as exc:
            LOGGER.debug("post-cooldown health probe failed: %s", exc)
            if self.client.connected:
                await self.client.close()
            return False

    # -- write path -------------------------------------------------------

    def attach_push_client(self, push_client) -> None:
        """Wire the push command channel (called once the push client exists)."""
        self._push_client = push_client

    async def async_write(self, address: int, data: list[int], *, key: str | None = None) -> None:
        """Submit a write, choosing the store-arbiter path in push mode.

        In **push mode** the write is a *write-intent* sent over the bridge's
        command channel: the bridge applies it optimistically to its store,
        broadcasts it (so HA reflects it at once), performs the governed write
        with edge retry, and reconciles on the next authoritative read. We do
        **not** trigger a coordinator refresh -- the pushed snapshot is the
        confirmation, so a machine-read error can never reach this coordinator's
        management logic through the command path. If the stream is momentarily
        down, fall back to the governed front-end write (idempotent) without a
        refresh; the next poll/push reconciles.

        In **polling mode** the behaviour is unchanged: write directly, then
        refresh so the entities reflect the machine's actual new state.
        """
        if self.push_enabled and self._push_client is not None:
            try:
                await self._push_client.async_send_command(address, data, key=key)
                return
            except R60VConnectionError as exc:
                LOGGER.debug(
                    "push command channel unavailable (%s); front-end fallback", exc
                )
                await self.client.write(address, data)
                return
        await self.client.write(address, data)
        await self.async_request_refresh()

    # -- polling ----------------------------------------------------------

    async def _read_snapshot(self) -> StateSnapshot:
        """Read a fresh settings block + live registers. Runs off the loop."""
        settings = await self.client.read(SETTINGS_BASE, SETTINGS_LEN)
        live: dict[int, list[int]] = {}
        for address, length in LIVE_REGISTERS.items():
            live[address] = await self.client.read(address, length)
        return StateSnapshot(settings=settings, live=live)

    async def _async_update_data(self) -> StateSnapshot:
        """Fetch state. In push mode a thin consumer; in polling mode, the poller.

        **Push mode (bridge streams state):** the bridge owns the whole machine
        relationship and the store is the single arbiter of availability, so this
        method does **no** machine I/O and carries **none** of the polling-era
        wedge/cooldown/failure logic. It returns a seed snapshot (unavailable
        until the first real frame); the push client then drives every update via
        ``async_set_updated_data``. Setup therefore succeeds whenever the
        *bridge* is reachable -- it no longer depends on the machine being on.

        **Polling mode (no bridge push):** poll the machine, tolerating transient
        desync and wedge cooldowns. On success the failure/cooldown state resets.
        A transient failure serves the last-known-good snapshot (entities stay
        available) until failures are sustained (``FAILURE_TOLERANCE``). Beyond
        that -- once we've loaded at least once -- we treat the machine as wedged
        and enter a **cooldown**: close the connection (free its single slot) and
        stop polling for a spell so the listener can reset, instead of hammering
        it forever. When the cooldown elapses we recover **gently** (a single
        settings read via ``_probe_healthy``): resume the moment the machine
        answers again, and keep backing off while it stays wedged.
        """
        if self.push_enabled:
            # Thin push consumer -- see docstring. No machine read, no cooldown.
            # We still refresh the bridge-health back-channel once here (at the
            # first refresh) so the bridge diagnostic sensors populate at setup,
            # exactly as before; periodic refresh of those diagnostics in push
            # mode is a Phase 5 concern (the legible health taxonomy).
            await self._refresh_bridge_health()
            return self.data or StateSnapshot(available=False)

        # Bridge-health back-channel: if the bridge reports the link is down or
        # is actively recovering it (a diagnostic window), the machine is simply
        # unreachable *through no fault of its own*. Do NOT poll (it would only
        # hammer a half-built relay path) and do NOT count this toward the
        # machine-wedge cooldown -- that failure mode is the machine, not the
        # bridge. We free the client slot and surface a distinct bridge state.
        await self._refresh_bridge_health()
        if self._bridge_blocking():
            assert self._bridge is not None
            if self.client.connected:
                await self.client.close()
            reason = (
                "recovering" if self._bridge.diagnostic_window else "down"
            )
            raise UpdateFailed(f"bridge link {reason}; skipping machine poll")

        # During cooldown, do not touch the device: keep its single slot free so
        # the machine's listener gets the uninterrupted rest it needs to reset.
        if self.in_cooldown:
            if self.client.connected:
                await self.client.close()
            raise UpdateFailed(f"cooldown active ({self.cooldown_remaining}s remaining)")

        # The cooldown just elapsed. Recover GENTLY -- a single settings read,
        # not the full live-register sweep -- so a healthy-but-briefly-flaky
        # listener is not slammed straight back into a longer cooldown, and so a
        # failed recovery re-escalates on a *light* touch instead of a full poll
        # (the old behaviour, which could itself re-wedge the machine). This is
        # the health-gated auto-recovery: only after a run of consecutive good
        # probes (RESUME_AFTER_PROBES) do we trust the machine and resume full
        # polling -- a single lucky read from a still-marginal listener must not
        # resume cadence and immediately re-wedge. While it stays wedged we keep
        # backing off.
        if self._cooldown_index > 0:
            if await self._probe_healthy():
                self._probe_successes += 1
                if self._probe_successes >= RESUME_AFTER_PROBES:
                    LOGGER.info("R60V answered after cooldown; resuming polling")
                    self._clear_cooldown()
                    # fall through to a normal full read
                else:
                    # Confirming recovery: keep probing gently at the poll
                    # interval, staying out of a full read until the machine has
                    # answered a clean run of consecutive probes.
                    LOGGER.info(
                        "R60V post-cooldown probe %d/%d succeeded; confirming "
                        "recovery before resuming",
                        self._probe_successes, RESUME_AFTER_PROBES,
                    )
                    raise UpdateFailed(
                        f"confirming recovery "
                        f"({self._probe_successes}/{RESUME_AFTER_PROBES} probes)"
                    )
            else:
                await self.client.close()
                self._enter_cooldown()  # still wedged -> extend the backoff
                raise UpdateFailed("still wedged after cooldown; extending backoff")

        try:
            snapshot = await self._read_snapshot()
        except (R60VConnectionError, ProtocolError) as exc:
            self._consecutive_failures += 1
            if self.data is not None and self._consecutive_failures <= FAILURE_TOLERANCE:
                LOGGER.debug(
                    "poll failed (%d/%d consecutive); serving cached values: %s",
                    self._consecutive_failures, FAILURE_TOLERANCE, exc,
                )
                return self.data
            # Sustained failure after a prior success: assume a wedge and back
            # off (a fresh setup still surfaces ConfigEntryNotReady instead).
            if self.data is not None:
                await self.client.close()
                self._enter_cooldown()
            raise UpdateFailed(str(exc)) from exc

        self._clear_cooldown()
        return snapshot

