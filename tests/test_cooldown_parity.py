"""Drift guard: the integration's polling-mode cooldown mirror must stay pinned
to the bridge's canonical wedge schedule.

The bridge (``r60v_broker.wedge``) is the *designated single owner* of the wedge
cooldown discipline. When a bridge is present (push mode) the integration
consumes the bridge's availability verdict and runs no cooldown logic at all; the
constants asserted here matter only for the legacy **bridge-less polling mode**,
where the integration must mirror the bridge exactly. These tests fail the moment
one side of that mirror is tuned without the other -- the mechanical resolution
to the "duplicated cooldown/grace logic risks drift" follow-up.

Not everything is required to match: the bridge's ``WEDGE_AFTER`` (a wall-clock
margin, since the bridge polls continuously) and the integration's
``FAILURE_TOLERANCE`` (a poll-count margin at the coordinator's 30s interval) are
deliberately *different formulations* of "when is a wedge declared" for two very
different cadences, so they are intentionally not asserted equal. What must not
drift is the shared backoff ladder and the graded-resume probe count.
"""
from __future__ import annotations

from custom_components.rocket_r60v import coordinator as integration
from r60v_broker import wedge as bridge


def test_cooldown_backoff_schedule_matches() -> None:
    """The backoff ladder must be identical (integration uses timedeltas)."""
    integration_seconds = tuple(
        step.total_seconds() for step in integration.COOLDOWN_STEPS
    )
    assert integration_seconds == bridge.COOLDOWN_STEPS


def test_resume_probe_count_matches() -> None:
    """The graded-resume confirmation count must be identical on both sides."""
    assert integration.RESUME_AFTER_PROBES == bridge.RESUME_AFTER_PROBES
