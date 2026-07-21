# Rocket R60V — Home Assistant integration + bridge

Control a **Rocket R60V** espresso machine from Home Assistant: boiler
temperatures, standby, pressure profiles, auto on/off timers, water source,
counters, and live readings — as first-class HA entities.

The R60V is a lovely machine with a hostile network interface. This project pairs
a **Home Assistant custom integration** with a small **bridge daemon** that makes
that interface reliable.

> **Heads up — you need a dedicated always-on host with a spare Wi-Fi radio.**
> This is not a plug-and-play cloud integration. See
> [Requirements](#requirements) before you start.

---

## The problem

The R60V has no cloud and no normal home-network client mode. Instead it **hosts
its own Wi-Fi access point** (`RocketEspresso`) and exposes a single, fragile TCP
control socket at `192.168.1.1:1774`. That listener:

- has **no internet**, so a device that treats it as its primary network gets its
  connection torn down by the OS/Home Assistant, and
- tolerates **exactly one** well-behaved client. Connection churn or concurrent
  clients **wedge** it — it then greets but silently ignores every request, and
  only a **physical power-cycle** brings it back.

Earlier integrations that talked to the machine directly, per-setting, flooded
that socket and had to be cut back until they barely worked.

## The solution — a governor, not a passthrough

A **bridge daemon** runs on a small always-on host. It holds **one** connection to
the machine and puts a **governor** in front of it: a single worker that owns the
one upstream socket and serializes every request (commands preempt polls). It then
re-presents the machine on your LAN two ways:

- a **native-protocol front-end** at `‹bridge-ip›:1774` — identical wire protocol,
  so the Home Assistant integration (or any client) just points at the bridge, and
- optional **MQTT Discovery**, if you prefer the MQTT path.

No matter how many clients connect to the bridge, the machine only ever sees one
calm conversation — the wedge is structurally impossible. A raw TCP
passthrough / NAT would **not** work: it would let each client open its own socket
and re-create the wedge. **The bridge is the governor.**

```
  Home Assistant                bridge host (Pi)                 R60V
  ┌────────────┐   LAN   ┌───────────────────────────┐   Wi-Fi  ┌──────────┐
  │ rocket_r60v│────────▶│ front-end ─▶ governor ─────┼─────────▶│ 1774 (1  │
  │ integration│  :1774  │ (many clients) (one socket)│  AP link │  client) │
  └────────────┘         └───────────────────────────┘          └──────────┘
```

## Requirements

- A **Rocket R60V** with the Wi-Fi module (the `RocketEspresso` AP).
- A **dedicated always-on host** (a Raspberry Pi is ideal) with **both**:
  - its own LAN/internet connectivity (wired Ethernet, or a second Wi-Fi radio),
    **and**
  - a **spare Wi-Fi radio** pinned to the `RocketEspresso` AP (no gateway,
    connectivity checks off).
- Home Assistant, with [HACS](https://hacs.xyz/) for easy installation.

A single-radio host cannot hold both the machine's AP and your home network at
once — hence the dedicated bridge. Details and NetworkManager tips are in
[`bridge/README.md`](bridge/README.md).

## Install

### 1. Run the bridge

On your dedicated host:

```bash
git clone https://github.com/ThomasMichon/r60v-home-assistant.git
cd r60v-home-assistant/bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
r60v-bridge          # or install the systemd unit; see bridge/README.md
```

The bridge connects to the machine (`192.168.1.1:1774` by default) and serves the
native protocol at `‹bridge-host-ip›:1774`.

### 2. Install the Home Assistant integration

Add this repository as a **HACS custom repository** (category: Integration), install
**Rocket R60V**, and restart Home Assistant. Then **Settings → Devices & Services →
Add Integration → Rocket R60V**, and enter the **host**:

- the **bridge host's LAN IP** (recommended), or
- `192.168.1.1` if your HA host itself is the thing connected to the machine's AP.

> You must enter the address explicitly — there is no autodiscovery. Point it at
> the bridge.

## Entities

- **`climate`** — Brew Boiler and Steam Boiler, each with its live temperature
  and writable setpoint. Temperatures are reported in the machine's own display
  unit (°C or °F, per its Temperature Unit setting); a boiler reads `heat` only
  while it is actually energized, otherwise `off`.
- **`switch`** — Power (machine on / standby) and Steam Boiler (enable).
- **`select`** — Pressure Profile (A/B/C), Water Source (tank / mains),
  Temperature Unit (°C / °F) and Language. The Temperature Unit and Water Source
  icons reflect the current value.
- **`time`** — Auto-On and Auto-Off timers (native time pickers).
- **`sensor`** — Brew Pressure (bar), Display text, and Total Coffee Count.

The integration also pushes Home Assistant's local time to the machine's onboard
clock at startup and once a day, so its built-in timers stay correct across DST.

## Repository layout

| Path | What |
|------|------|
| [`custom_components/rocket_r60v/`](custom_components/rocket_r60v/) | The Home Assistant custom integration. |
| [`bridge/`](bridge/) | The bridge daemon: protocol codec, governor, front-end, MQTT, and a wire-level emulator. |
| [`docs/protocol.md`](docs/protocol.md) | Reverse-engineered R60V control-protocol reference. |

## Prior art & credits

This project stands on earlier reverse-engineering of the R60V protocol:

- **[jffry/rocket-r60v](https://github.com/jffry/rocket-r60v)** — the original
  reverse-engineering (NodeJS emulator + proxy sniffing) that mapped the protocol.
- **[confirm/Rocket-R60V](https://github.com/confirm/Rocket-R60V)** — the mature
  Python API + CLI, and the [`REVERSE_ENGINEERING.rst`](https://github.com/confirm/Rocket-R60V/blob/master/REVERSE_ENGINEERING.rst)
  write-up. The Home Assistant integration uses this library as its transport.
- **[JulianKahnert/RocketAPI](https://github.com/JulianKahnert/RocketAPI)** — an
  earlier Python toolkit.

Thank you to those authors — without their work this would not exist.

The protocol reference here was additionally cross-validated by decompiling the
official Android app; see [`docs/protocol.md`](docs/protocol.md) for provenance.

## Acknowledgment

Much of this project — the bridge/governor design, the re-architected integration,
the emulator, the tests, and this documentation — was developed with the
assistance of **GitHub Copilot**.

## License

[MIT](LICENSE).