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
lengthening spell, recovering **gently** (a light probe) when it elapses. A
single good probe does NOT resume full polling: a still-marginal listener can
answer one lucky read and re-wedge the moment cadence resumes, so recovery is
*graded* -- a run of ``RESUME_AFTER_PROBES`` consecutive good probes confirms the
machine is genuinely back before normal polling resumes.

**Ownership.** This module is the *designated single owner* of the wedge
cooldown discipline. The Home Assistant integration
(``custom_components/rocket_r60v/coordinator.py``) carries a mirror of this
schedule for its **bridge-less polling mode** only -- when a bridge is present
(push mode) the integration consumes this bridge's availability verdict and runs
none of this logic. The mirrored constants (``COOLDOWN_STEPS``,
``RESUME_AFTER_PROBES``) are pinned against drift by ``tests/test_cooldown_parity.py``.
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

#: Consecutive successful post-cooldown probes required before resuming full
#: polling. Guards against a single lucky read from a still-marginal listener
#: resuming full cadence and immediately re-wedging (churning the cooldown).
RESUME_AFTER_PROBES = 2


class WedgeRecovery:
    """Tracks failure streaks and drives the wedge cooldown/back-off.

    State machine (driven by the poll loop each cycle):
    - **polling** -- normal. ``record_failure`` starts/extends a streak; once
      ``wedged`` is true, the loop calls ``begin_cooldown`` and frees the link.
    - **in cooldown** (``in_cooldown``) -- don't touch the device; keep its slot
      free so the listener resets.
    - **probe due** (``awaiting_probe``) -- the cooldown elapsed; the loop does one
      gentle read: ``record_probe_success`` on success (resume only after a run of
      ``resume_after_probes`` consecutive good probes), ``begin_cooldown`` again on
      failure (extend the back-off, discarding any confirm progress).
    """

    def __init__(
        self,
        *,
        wedge_after: float = WEDGE_AFTER,
        cooldown_steps: tuple[float, ...] = COOLDOWN_STEPS,
        resume_after_probes: int = RESUME_AFTER_PROBES,
        _now=time.monotonic,
    ) -> None:
        self.wedge_after = wedge_after
        self.cooldown_steps = cooldown_steps
        self.resume_after_probes = max(1, resume_after_probes)
        self._now = _now
        self._first_fail_at: float | None = None
        self._cooldown_until: float | None = None
        self._cooldown_index = 0
        self._probe_successes = 0

    # -- signals from the poll loop ---------------------------------------

    def record_success(self) -> None:
        """A good read: clear all wedge/cooldown state."""
        self._first_fail_at = None
        self._cooldown_until = None
        self._cooldown_index = 0
        self._probe_successes = 0

    def record_failure(self) -> None:
        """A failed read outside cooldown: start the streak clock if new."""
        if self._first_fail_at is None:
            self._first_fail_at = self._now()

    def record_probe_success(self) -> bool:
        """Count a successful post-cooldown probe; report whether fully resumed.

        Graded recovery: a single good probe is NOT enough to resume full
        polling (a still-marginal listener can answer one lucky read and re-wedge
        the instant cadence returns). Only once ``resume_after_probes``
        consecutive probes have succeeded do we clear all wedge state and resume.
        Until then we stay in the *probe-due* state (``awaiting_probe`` remains
        true) so the loop keeps probing gently at its normal interval. Returns
        ``True`` when full polling has resumed, ``False`` while still confirming.
        """
        self._probe_successes += 1
        if self._probe_successes >= self.resume_after_probes:
            self.record_success()
            return True
        # Keep the window elapsed so the loop probes again next cycle without
        # falling through to a full poll before recovery is confirmed.
        self._cooldown_until = self._now()
        return False

    def begin_cooldown(self) -> float:
        """Enter (or extend) the cooldown; return the step length in seconds."""
        step = self.cooldown_steps[min(self._cooldown_index, len(self.cooldown_steps) - 1)]
        self._cooldown_until = self._now() + step
        self._cooldown_index += 1
        # A fresh cooldown discards any partial confirm progress: the machine
        # must earn a clean run of good probes again before polling resumes.
        self._probe_successes = 0
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

    @property
    def probe_successes(self) -> int:
        """Consecutive good post-cooldown probes so far (for logging/diagnostics)."""
        return self._probe_successes
