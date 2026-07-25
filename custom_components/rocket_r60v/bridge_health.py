"""Bridge-health back-channel client.

Reads the companion bridge's health endpoint (served by the link watchdog on the
bridge host) so the integration can distinguish a *bridge/link* outage from a
*machine* wedge, and back off while the bridge is actively recovering the link.

The endpoint returns a small, documented JSON object. This module parses it
defensively into :class:`BridgeHealth`; any transport or parse failure yields
``None`` (treated as "unknown" -- the integration then polls the machine as
usual rather than assuming the bridge is down).

All I/O is non-blocking aiohttp against Home Assistant's shared client session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

LOGGER = logging.getLogger(__name__)

#: Bridge-health JSON schema versions this client understands.
SUPPORTED_SCHEMAS = frozenset({1})

_FETCH_TIMEOUT = 4.0


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_bool(value: object) -> bool:
    return bool(value)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is a subclass of int -- exclude it
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


@dataclass(frozen=True)
class BridgeHealth:
    """Parsed snapshot of the bridge's link health."""

    available: bool
    schema: int | None = None
    link_up: bool = False
    ssid: str | None = None
    signal: int | None = None
    ip: str | None = None
    machine_reachable: bool = False
    ap_visible: bool = False
    machine_powered_off: bool = False
    diagnostic_window: bool = False
    link_uptime_s: int | None = None
    recoveries_total: int | None = None
    last_recovery: datetime | None = None
    stale: bool = False
    updated: datetime | None = None

    @classmethod
    def from_json(cls, data: dict) -> "BridgeHealth":
        """Build a snapshot from the endpoint's JSON object (defensive)."""
        return cls(
            available=_as_bool(data.get("available", True)),
            schema=_as_int(data.get("schema")),
            link_up=_as_bool(data.get("link_up")),
            ssid=data.get("ssid") if isinstance(data.get("ssid"), str) else None,
            signal=_as_int(data.get("signal")),
            ip=data.get("ip") if isinstance(data.get("ip"), str) else None,
            machine_reachable=_as_bool(data.get("machine_reachable")),
            ap_visible=_as_bool(data.get("ap_visible")),
            machine_powered_off=_as_bool(data.get("machine_powered_off")),
            diagnostic_window=_as_bool(data.get("diagnostic_window")),
            link_uptime_s=_as_int(data.get("link_uptime_s")),
            recoveries_total=_as_int(data.get("recoveries_total")),
            last_recovery=_parse_iso(data.get("last_recovery")),
            stale=_as_bool(data.get("stale")),
            updated=_parse_iso(data.get("updated")),
        )

    @property
    def usable(self) -> bool:
        """True when this snapshot is trustworthy enough to act on.

        A malformed/unavailable payload, an unknown schema, or a stale one is
        NOT acted upon: we would rather poll the machine and find out than block
        on doubtful bridge data.
        """
        return (
            self.available
            and self.schema in SUPPORTED_SCHEMAS
            and not self.stale
        )

    @property
    def blocking(self) -> bool:
        """True when the integration should pause machine polling.

        Only a *fresh, usable* signal that the link is down or under active
        recovery blocks polling.
        """
        return self.usable and (self.diagnostic_window or not self.link_up)


async def async_fetch_bridge_health(
    session: aiohttp.ClientSession, url: str, timeout_s: float = _FETCH_TIMEOUT
) -> BridgeHealth | None:
    """Fetch and parse the bridge-health endpoint. Returns None on any failure."""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            if resp.status != 200:
                LOGGER.debug("bridge health %s -> HTTP %s", url, resp.status)
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        LOGGER.debug("bridge health %s unreachable/invalid: %s", url, exc)
        return None
    if not isinstance(data, dict):
        return None
    return BridgeHealth.from_json(data)
