"""Constants for the Rocket R60V integration."""

DOMAIN = "rocket_r60v"

# Default control endpoint.
#
# Out of the box the R60V hosts its own WiFi access point and listens on
# 192.168.1.1:1774. That single-socket listener is fragile: it tolerates exactly
# one well-behaved client and wedges under connection churn or concurrent
# clients. If you run the bundled bridge (see the `bridge/` directory) on a host
# with a spare Wi-Fi radio, point this at the bridge's LAN address instead --
# the bridge serializes every caller onto one upstream socket so the machine
# stays healthy. Either way you must enter the address explicitly.
DEFAULT_HOST = "192.168.1.1"
DEFAULT_PORT = 1774

# Optional bridge-health back-channel.
#
# When the machine is reached through a bridge that runs the companion link
# watchdog + health endpoint, the integration can poll that endpoint to tell a
# *bridge/link* outage apart from a *machine* wedge, and pause its own polling
# while the bridge is actively recovering the link (a "diagnostic window")
# instead of hammering a half-built relay path. Leave unset to disable -- the
# integration then behaves exactly as before.
CONF_BRIDGE_HEALTH_URL = "bridge_health_url"
DEFAULT_BRIDGE_HEALTH_PORT = 8787


def default_bridge_health_url(host: str, port: int = DEFAULT_BRIDGE_HEALTH_PORT) -> str:
    """Suggest a bridge-health URL derived from the control host.

    The health endpoint runs on the same bridge host as the control relay, so
    the control host is a sensible default; the user can override or clear it.
    """
    return f"http://{host}:{port}/health"


# Optional WebSocket push channel.
#
# When the bridge runs the WebSocket push server (``R60V_PUSH_ENABLED``), the
# integration can SUBSCRIBE to it instead of polling: the bridge fast-polls the
# machine and streams raw state, and the integration updates its entities on
# receipt (``iot_class: local_push``). This gives near-real-time state -- the
# brew timer ticks live -- while the bridge remains the single disciplined owner
# of the fragile link. Leave unset to fall back to polling (``local_polling``).
CONF_PUSH_URL = "push_url"
DEFAULT_PUSH_PORT = 8788


def default_push_url(host: str, port: int = DEFAULT_PUSH_PORT) -> str:
    """Suggest a WebSocket push URL derived from the control host.

    The push server runs on the same bridge host as the control front-end.
    """
    return f"ws://{host}:{port}"
