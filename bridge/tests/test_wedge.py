"""Tests for WedgeRecovery -- the bridge's wedge cooldown/back-off state machine."""
from __future__ import annotations

from r60v_broker.wedge import WedgeRecovery


class Clock:
    """Injectable monotonic clock for deterministic time control."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_transient_failure_never_wedges():
    clk = Clock()
    w = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0,), _now=clk)
    w.record_failure()
    clk.advance(10)
    assert not w.wedged
    w.record_success()
    clk.advance(100)
    assert not w.wedged and not w.in_cooldown


def test_sustained_failure_becomes_wedged():
    clk = Clock()
    w = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0, 600.0), _now=clk)
    w.record_failure()
    assert not w.wedged
    clk.advance(50)
    assert w.wedged
    step = w.begin_cooldown()
    assert step == 300.0
    assert w.in_cooldown and not w.awaiting_probe


def test_cooldown_elapses_to_probe_then_extends_with_backoff():
    clk = Clock()
    w = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0, 600.0), _now=clk)
    w.record_failure()
    clk.advance(50)
    assert w.begin_cooldown() == 300.0
    clk.advance(300)
    assert not w.in_cooldown and w.awaiting_probe
    # Still wedged -> next cooldown is the longer step.
    assert w.begin_cooldown() == 600.0
    assert w.in_cooldown


def test_backoff_saturates_at_last_step():
    clk = Clock()
    w = WedgeRecovery(wedge_after=1.0, cooldown_steps=(10.0, 20.0), _now=clk)
    w.record_failure()
    clk.advance(2)
    assert w.begin_cooldown() == 10.0
    assert w.begin_cooldown() == 20.0
    assert w.begin_cooldown() == 20.0  # saturates


def test_success_clears_all_state():
    clk = Clock()
    w = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0,), _now=clk)
    w.record_failure()
    clk.advance(100)
    w.begin_cooldown()
    w.record_success()
    assert not w.wedged and not w.in_cooldown and not w.awaiting_probe
    assert w.cooldown_remaining == 0.0
    # A fresh failure streak restarts from zero (index reset).
    clk.advance(5)
    w.record_failure()
    clk.advance(50)
    assert w.begin_cooldown() == 300.0


def test_cooldown_remaining_counts_down():
    clk = Clock()
    w = WedgeRecovery(wedge_after=45.0, cooldown_steps=(300.0,), _now=clk)
    w.record_failure()
    clk.advance(50)
    w.begin_cooldown()
    assert w.cooldown_remaining == 300.0
    clk.advance(120)
    assert w.cooldown_remaining == 180.0
