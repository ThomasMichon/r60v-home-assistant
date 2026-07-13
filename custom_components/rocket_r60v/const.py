"""Constants for the Rocket R60V integration."""

DOMAIN = "rocket_r60v"

# Default control endpoint.
#
# The R60V hosts its own WiFi AP and its single-socket TCP listener wedges under
# connection churn. In the Aperture facility the machine is reached through the
# rocket-r60v-broker's governor-fronted LAN endpoint (nova-prospekt), which
# re-presents the native protocol at a stable LAN IP and serializes all callers
# onto one upstream socket -- so even this per-setting client is safe. Point this
# at the bridge, not the machine's own 192.168.1.1 AP.
DEFAULT_HOST = "192.168.0.57"
DEFAULT_PORT = 1774
