"""The device state store: the single arbiter of R60V state and availability.

The R60V link is intermittent -- the machine occasionally swallows a read, and a
reconnect takes seconds. Home Assistant, however, wants a *continuous* picture.
This store decouples the two and is the **one** authority on what the house sees:

- it caches the **last-known** :class:`~r60v_broker.state.StateSnapshot`, so a
  transient miss never blanks the picture;
- it owns **availability** with hysteresis -- offline only after several
  *consecutive* failures, back online on the first success -- and this is the
  *single source of truth* for reachability (every projection reads it, none
  computes its own);
- it eases live boiler temps toward setpoint while reads are stale and the
  machine is on (a physically-plausible continuity aid);
- it emits **change events** to subscribers, so transports (MQTT, the WebSocket
  push server) are mere *projections* of the store rather than owners of state.

This is transport-agnostic by construction: it holds no MQTT/WebSocket handle.
The MQTT publisher (:mod:`.publisher`) and the WS push server subscribe to it.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .protocol import Address
from .state import StateSnapshot

LOGGER = logging.getLogger("r60v.store")

#: A change listener: called (with no args) whenever the store's state or
#: availability changes. Kept intentionally simple -- subscribers read the store.
ChangeListener = Callable[[], None]


class DeviceState:
    """Caches machine state + availability and notifies subscribers on change.

    The sole arbiter: projections (MQTT, WS push) and, ultimately, the Home
    Assistant integration read *this* -- they never derive a competing view of
    whether the machine is reachable.
    """

    def __init__(
        self,
        *,
        availability_grace: int = 4,
        stale_after: float = 12.0,
        interpolate_step: float = 1.0,
    ) -> None:
        self.availability_grace = availability_grace
        self.stale_after = stale_after
        self.interpolate_step = interpolate_step

        self.snapshot = StateSnapshot()
        self._have_data = False
        self._fail_streak = 0
        self._available: bool | None = None
        self._last_live_update_at: float = 0.0
        self._listeners: list[ChangeListener] = []
        # Optimistic write overlay: address -> byte. Holds bytes a user command
        # has intended but the machine has not yet authoritatively confirmed, so
        # HA reflects the intent at once. Re-applied on top of every
        # authoritative settings read (so a stale poll can't clobber it) and
        # cleared per-command once that command's write has been reconciled.
        self._pending: dict[int, int] = {}

    # -- subscription -----------------------------------------------------

    def subscribe(self, listener: ChangeListener) -> Callable[[], None]:
        """Register a change listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsub() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _unsub

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 -- a bad subscriber must not break the store
                LOGGER.debug("store change listener failed: %s", exc)

    # -- state ------------------------------------------------------------

    @property
    def have_data(self) -> bool:
        return self._have_data

    @property
    def available(self) -> bool:
        """The single source of truth for machine reachability.

        ``None`` (before the first read) is treated as available so a
        fresh-but-unproven device isn't shown offline prematurely.
        """
        return True if self._available is None else bool(self._available)

    # -- cache updates (called by the poll loop / governor path) ----------

    def update_settings(self, data: list[int]) -> None:
        self.snapshot.settings = data
        # Re-apply any in-flight optimistic bytes on top of the authoritative
        # read, so a poll that started before a command cannot revert its value.
        self._apply_pending()
        self._have_data = True
        self._notify()

    def update_live(self, address: int, data: list[int]) -> None:
        self.snapshot.live[address] = data
        self._last_live_update_at = time.monotonic()
        self._have_data = True
        self._notify()

    def note_success(self) -> None:
        self._fail_streak = 0
        self._set_available(True)

    def note_failure(self) -> None:
        self._fail_streak += 1
        if self._fail_streak >= self.availability_grace:
            self._set_available(False)

    def note_wedged(self) -> None:
        """Force the machine offline on a definitive *unreachable* verdict.

        Called by the bridge when it enters a **wedge cooldown**: a wedge is a
        sustained-failure determination that the machine is unreachable, after
        which the cooldown deliberately **stops polling** -- so ``note_failure``
        is not called again and the grace counter can no longer advance.

        Availability is otherwise grace-*count*-based while the wedge is
        *time*-based, so if the wedge trips before the grace threshold is
        reached (realistic when reads time out and each failed poll cycle runs
        long), the store would freeze at its last ``available`` verdict and serve
        stale "last-known" state for the entire multi-minute cooldown. Marking
        offline here decouples availability from that grace-vs-wedge timing race:
        **a cooldown always implies unavailable.** A later good read
        (``note_success``) brings the machine back online.
        """
        self._fail_streak = max(self._fail_streak, self.availability_grace)
        self._set_available(False)

    def _set_available(self, online: bool) -> None:
        if self._available is online:
            return
        self._available = online
        LOGGER.info("device marked %s", "online" if online else "offline")
        self._notify()

    # -- optimistic write overlay -----------------------------------------

    def apply_optimistic(self, address: int, data: list[int]) -> bool:
        """Optimistically reflect a user write before the machine confirms it.

        Records the intended bytes in the pending overlay and patches the cached
        settings block so HA sees the value immediately. Only settings-block
        writes are reflected (every writable entity lives at ``0x00..0x72``); a
        write outside the block, before any baseline read, or with an
        out-of-range byte is not reflected (it still goes to the machine and
        surfaces on the next authoritative read). Availability is untouched -- an
        intent is not a reading. Returns whether the overlay was applied.
        """
        if not self._have_data:
            return False
        if address < 0 or not data:
            return False
        if address + len(data) > len(self.snapshot.settings):
            return False
        if any(b < 0 or b > 0xFF for b in data):
            return False
        for offset, byte in enumerate(data):
            self._pending[address + offset] = byte
        self._apply_pending()
        self._notify()
        return True

    def reconcile_clear(self, expected: dict[int, int]) -> None:
        """Drop overlay bytes a command has finished reconciling.

        ``expected`` maps address -> the byte that command optimistically set.
        An address is cleared **only** if the overlay still holds that command's
        value; if a *newer* command has since re-set the same address, its
        overlay is left in place so the latest user intent is never reverted to
        an older one. After clearing, the following authoritative read is the
        truth for those bytes.
        """
        for address, byte in expected.items():
            if self._pending.get(address) == byte:
                self._pending.pop(address, None)

    def _apply_pending(self) -> None:
        """Overlay the pending optimistic bytes onto the cached settings block."""
        for address, byte in self._pending.items():
            if address < len(self.snapshot.settings):
                self.snapshot.settings[address] = byte

    # -- continuity: interpolate live temps toward setpoint ---------------

    def interpolate_live_temps(self) -> None:
        """Ease live boiler temps toward setpoint while stale and machine on.

        Only runs when we have data, reads are stale, and the machine is on;
        each call nudges by at most ``interpolate_step`` degrees. A real reading
        (``update_live``) resets staleness and overrides the estimate.
        """
        if not self._have_data:
            return
        if time.monotonic() - self._last_live_update_at < self.stale_after:
            return
        if self.snapshot.settings_byte(Address.STANDBY) != 0:
            return  # machine off -> temps trend to ambient, don't fabricate

        self._ease(Address.CURRENT_BREW_TEMP, Address.BREW_BOILER_TEMP)
        if self.snapshot.settings_byte(Address.SERVICE_BOILER_ENABLE):
            self._ease(Address.CURRENT_SERVICE_TEMP, Address.SERVICE_BOILER_TEMP)

    def _ease(self, live_addr: int, setpoint_addr: int) -> None:
        data = self.snapshot.live.get(live_addr)
        if not data:
            return
        current = data[0]
        target = self.snapshot.settings_byte(setpoint_addr)
        gap = target - current
        if gap == 0:
            return
        step = max(-self.interpolate_step, min(self.interpolate_step, gap))
        self.snapshot.live[live_addr] = [int(round(current + step))]

    # -- projection helpers -----------------------------------------------

    def raw_snapshot(self) -> dict:
        """Raw register snapshot for the WebSocket push channel.

        Carries the settings block and live registers **exactly as cached**, plus
        the store's single ``available`` verdict, so a subscriber (the
        ``local_push`` HA integration) reconstructs its own ``StateSnapshot`` and
        decodes it with its own entity logic -- preserving its representation
        unchanged. The transport moves; the decode does not.
        """
        return {
            "available": self.available,
            "settings": list(self.snapshot.settings),
            "live": {
                str(addr): list(data) for addr, data in self.snapshot.live.items()
            },
        }
