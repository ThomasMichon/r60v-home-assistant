"""Keep the machine's real-time clock (reg 0xA000) in sync with local time.

The R60V has no network time source; its onboard clock drifts and has no concept
of daylight-saving transitions. Its built-in auto on/off timers (and the display
clock) run off that clock, so if it is wrong the machine powers on/off at the
wrong wall-clock time. We push Home Assistant's local time to the machine at
startup and once a day, which keeps it correct across DST changes automatically
(``dt_util.now()`` is timezone-aware and DST-adjusted).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .client import R60VClient, R60VConnectionError
from .protocol import Address

LOGGER = logging.getLogger(__name__)

#: Local time of day for the daily push -- a few minutes after the 02:00 DST
#: boundary so a spring-forward/fall-back is reflected the same night.
_SYNC_HOUR = 3
_SYNC_MINUTE = 7


def build_clock_payload(now: datetime) -> list[int]:
    """Build the 7-byte ``0xA000`` payload for a local ``datetime``.

    Wire order (from the official app's date/time write):
    ``[0, minute, hour, weekday, day, month, year]`` where ``weekday`` is 1..7
    (Monday..Sunday) and ``year`` is the offset from 2000.
    """
    return [
        0,
        now.minute,
        now.hour,
        now.weekday() + 1,  # Python Monday=0 -> machine Monday=1
        now.day,
        now.month,
        now.year - 2000,
    ]


async def async_push_clock(client: R60VClient) -> bool:
    """Write the current local time to the machine. Returns True on success."""
    payload = build_clock_payload(dt_util.now())
    try:
        await client.write(Address.DATE_TIME, payload)
    except R60VConnectionError as exc:
        LOGGER.warning("failed to push clock to R60V: %s", exc)
        return False
    LOGGER.debug("pushed clock to R60V: %s", payload)
    return True


def async_setup_clock_sync(hass: HomeAssistant, client: R60VClient) -> Callable[[], None]:
    """Push the clock now and daily; return an unsubscribe callback."""

    @callback
    def _daily(_now: datetime) -> None:
        hass.async_create_task(async_push_clock(client))

    unsub = async_track_time_change(
        hass, _daily, hour=_SYNC_HOUR, minute=_SYNC_MINUTE, second=0
    )
    # One sync shortly after setup -- scheduled as a task so device I/O never
    # blocks config-entry setup on the event loop.
    hass.async_create_task(async_push_clock(client))
    return unsub
