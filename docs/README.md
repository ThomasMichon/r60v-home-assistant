# Rocket R60V — Documentation

Reference, architecture, and troubleshooting for the Rocket R60V Home Assistant
integration and its companion bridge daemon.

New here? Start with the [project README](../README.md) for install and entities,
then come back for depth.

## The suite

| Document | Read it when you want to… |
|----------|---------------------------|
| **[protocol.md](protocol.md)** | Understand the R60V's ASCII-hex wire protocol — framing, checksum, the full memory/address map, value encodings, and implementation guidance. The reference. |
| **[architecture.md](architecture.md)** | Understand *how* the project is built and why — the two-layer governor/consumer design, the store as the single arbiter of availability, wedge recovery, the push/front-end/MQTT faces, and the health taxonomy. |
| **[troubleshooting.md](troubleshooting.md)** | Diagnose a misbehaving setup — "all entities unavailable," off vs wedged vs link-down vs client-mode, the refused-vs-timeout test, the diagnostic entities, and recovery. |
| **[reverse-engineering.md](reverse-engineering.md)** | See where the protocol knowledge came from — decompilation of the official app, proxy sniffing, cross-validation, live-hardware confirmations, and how to reproduce or extend it. |

## Quick links by task

- **Setting it up** → [README: Requirements & Install](../README.md#requirements),
  [bridge/README](../bridge/README.md) (host, radios, NetworkManager tips).
- **"Home Assistant says everything is unavailable"** →
  [troubleshooting: master decision tree](troubleshooting.md#the-master-decision-tree).
- **"The machine is on but HA can't see it"** →
  [troubleshooting §C](troubleshooting.md#c-the-machine-is-off-or-wedged).
- **Understanding the `Connection` sensor states** →
  [architecture: health taxonomy](architecture.md#the-3-signal-health-taxonomy).
- **Adding/correcting a protocol register** →
  [reverse-engineering: extend it](reverse-engineering.md#extend-it--the-emulator-as-a-test-oracle).

## Key facts, up front

- The machine hosts its **own Wi-Fi AP** (`RocketEspresso`) and listens on a
  single, fragile TCP control socket at the **fixed** address `192.168.1.1:1774`.
- That listener tolerates **exactly one** calm client and **wedges** under
  connection churn — after which only a **physical power-cycle** recovers it. The
  whole architecture exists to honor this.
- The protocol is **plaintext ASCII-hex over raw TCP** — no encryption, no
  protobuf (a common misconception; see [reverse-engineering.md](reverse-engineering.md)).
