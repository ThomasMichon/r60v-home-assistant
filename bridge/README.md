# Rocket R60V Bridge

A small always-on daemon that holds **one** healthy connection to a Rocket R60V
espresso machine and safely re-presents it on your LAN — so Home Assistant (or
any client) can control the machine without the connection dropping.

It ships three things:

- **`r60v_broker.protocol`** — a dependency-free codec for the R60V's ASCII-hex
  wire protocol (framing, checksum, address map, safe ranges). See
  [`../docs/protocol.md`](../docs/protocol.md).
- **`r60v_broker.emulator`** — a faithful TCP emulator of the machine, so you can
  develop and test against it with no physical machine.
- **the bridge daemon** — a **connection governor** plus two northbound faces:
  a **native-protocol LAN front-end** and optional **MQTT Discovery**.

## Why a bridge is needed

The R60V hosts its **own** Wi-Fi access point (`RocketEspresso`) and listens on a
single, fragile TCP control socket at `192.168.1.1:1774`. That listener tolerates
**exactly one** well-behaved client and **wedges** under connection churn or
concurrent clients — once wedged it greets but swallows every request, and only a
**physical power-cycle** recovers it (see [`../docs/protocol.md`](../docs/protocol.md)
§9).

The bridge solves this with a **governor**: a single worker that owns the one
upstream socket and serializes every caller through a priority queue (commands
preempt polls). No matter how many LAN clients connect to the bridge, the machine
only ever sees one calm, ordered conversation. **This is why a raw TCP
passthrough or NAT/DNAT does not work** — those let each client open its own
socket to the machine and wedge it. The bridge *is* the governor.

## Hardware / network topology

You need a **dedicated always-on host** (a Raspberry Pi is ideal) with:

- **Its own internet/LAN connectivity** — wired Ethernet, or a second Wi-Fi
  radio — so the host stays on your network, and
- **A spare Wi-Fi radio dedicated to the machine**, pinned to the
  `RocketEspresso` AP with **no gateway** and **connectivity checks disabled**
  (the machine's AP has no internet; a connectivity manager that treats it as the
  primary network will tear the link down).

A single-radio host cannot hold both `RocketEspresso` and your home network at
once — that is the whole reason a dedicated bridge exists. On a Raspberry Pi with
NetworkManager, pin the machine link with `ipv4.never-default yes`,
`ipv4.dns ""`, and `ipv4.may-fail yes`, and disable NM connectivity checking.

## Install

Requires Python 3.9+.

```bash
cd bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # installs r60v-bridge + r60v-emulator entry points
```

## Configure

All configuration is via environment variables (all optional):

| Variable | Default | Meaning |
|----------|---------|---------|
| `R60V_HOST` | `192.168.1.1` | The machine's control endpoint (its own AP). |
| `R60V_PORT` | `1774` | Control port. |
| `R60V_FRONTEND_ENABLED` | `true` | Serve the native protocol on the LAN. |
| `R60V_FRONTEND_HOST` | `0.0.0.0` | Front-end bind address. |
| `R60V_FRONTEND_PORT` | `1774` | Front-end bind port (same as the machine, so clients need no change). |
| `MQTT_HOST` | *(empty)* | Set to enable MQTT Discovery; empty = front-end only. |
| `MQTT_PORT` | `1883` | MQTT port. |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(empty)* | MQTT credentials. |
| `R60V_LIVE_INTERVAL` | `10` | Seconds between live-value polls. |
| `R60V_SETTINGS_INTERVAL` | `60` | Seconds between full settings reads. |

The bridge connects to the machine at `R60V_HOST` and exposes it at
`R60V_FRONTEND_HOST:R60V_FRONTEND_PORT`. Point your Home Assistant integration at
the **bridge host's LAN IP** (not `192.168.1.1`).

## Run

```bash
r60v-bridge                 # from the venv; reads the env vars above
```

Or as a systemd service — see [`rocket-r60v-bridge.service`](rocket-r60v-bridge.service).

## Develop / test

```bash
pip install -e '.[dev]'
python -m pytest            # stdlib + paho-mqtt only; no physical machine needed
python -m r60v_broker.emulator --host 127.0.0.1 --port 1774 -v   # run the emulator
```

The emulator speaks the real protocol (`*HELLO*`, hex frames, checksums, the
115-byte settings block, live registers, strict half-duplex) so the bridge and
the HA integration can be exercised end to end with no machine.
