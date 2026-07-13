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
