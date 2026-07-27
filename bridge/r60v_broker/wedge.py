"""Wedge recovery: the bridge's own back-off when the R60V listener wedges.

The R60V's single-socket control listener can **wedge** -- it keeps greeting but
swallows every read and does NOT self-recover while a client keeps knocking. The
documented escape is to *stop touching it* for a while so its control module
resets (see ``docs/protocol.md`` and the link-resilience notes). Historically the
Home Assistant integration owned this cooldown; in the store-as-arbiter design
the **bridge** owns the machine relationship, so the recovery lives here -- the
governor stays pure I/O, and the poll loop drives this controller.

The shape mirrors the proven integration logic: tolerate transient misses, and
only once failures are *sustained* declare a wedge and enter a **cooldown** --
close the link (free the machine's single client slot) and stop polling for a
lengthening spell, recovering **gently** (one light probe) when it elapses.
"""
from __future__ import annotations

import time

#: Continuous-failure duration before we treat the machine as wedged. Each
#: governed read already retries + reconnects internally, so sustained failure
#: over this window indicates a genuine wedge, not transient flakiness. Chosen
#: cadence-independent (wall-clock) so it holds at any poll interval.
WEDGE_AFTER = 45.0

#: Lengthening cooldown backoff (seconds). Matches the observed 5-30 min R60V
#: control-module recovery window.
COOLDOWN_STEPS: tuple[float, ...] = (300.0, 600.0, 1200.0, 1800.0)


class WedgeRecovery:
    """Tracks failure streaks and drives the wedge cooldown/back-off.

    State machine (driven by the poll loop each cycle):
    - **polling** -- normal. ``record_failure`` starts/extends a streak; once
      ``wedged`` is true, the loop calls ``begin_cooldown`` and frees the link.
    - **in cooldown** (``in_cooldown``) -- don't touch the device; keep its slot
      free so the listener resets.
    - **probe due** (``awaiting_probe``) -- the cooldown elapsed; the loop does one
      gentle read: ``record_success`` on success (resume), ``begin_cooldown`` again
      on failure (extend the back-off).
    """

    def __init__(
        self,
        *,
        wedge_after: float = WEDGE_AFTER,
        cooldown_steps: tuple[float, ...] = COOLDOWN_STEPS,
        _now=time.monotonic,
    ) -> None:
        self.wedge_after = wedge_after
        self.cooldown_steps = cooldown_steps
        self._now = _now
        self._first_fail_at: float | None = None
        self._cooldown_until: float | None = None
        self._cooldown_index = 0

    # -- signals from the poll loop ---------------------------------------

    def record_success(self) -> None:
        """A good read: clear all wedge/cooldown state."""
        self._first_fail_at = None
        self._cooldown_until = None
        self._cooldown_index = 0

    def record_failure(self) -> None:
        """A failed read outside cooldown: start the streak clock if new."""
        if self._first_fail_at is None:
            self._first_fail_at = self._now()

    def begin_cooldown(self) -> float:
        """Enter (or extend) the cooldown; return the step length in seconds."""
        step = self.cooldown_steps[min(self._cooldown_index, len(self.cooldown_steps) - 1)]
        self._cooldown_until = self._now() + step
        self._cooldown_index += 1
        return step

    # -- state queried by the poll loop -----------------------------------

    @property
    def wedged(self) -> bool:
        """True when failures have been continuous for at least ``wedge_after``."""
        return (
            self._first_fail_at is not None
            and self._now() - self._first_fail_at >= self.wedge_after
        )

    @property
    def in_cooldown(self) -> bool:
        """True while a cooldown window is active (leave the machine alone)."""
        return self._cooldown_until is not None and self._now() < self._cooldown_until

    @property
    def awaiting_probe(self) -> bool:
        """True when a cooldown window was set and has now elapsed (probe due)."""
        return self._cooldown_until is not None and self._now() >= self._cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        """Seconds left in the current cooldown (0 when not cooling down)."""
        if not self.in_cooldown:
            return 0.0
        assert self._cooldown_until is not None
        return max(0.0, self._cooldown_until - self._now())
