# Rocket R60V — Architecture

How this project is built, and *why* it is built this way. If you just want to
install it, see the [root README](../README.md); for the wire protocol see
[`protocol.md`](protocol.md); for diagnosing a misbehaving setup see
[`troubleshooting.md`](troubleshooting.md).

The whole design exists to reconcile two facts that are in direct tension:

- **The machine wants exactly one calm client.** The R60V's control listener
  (`192.168.1.1:1774`) is strictly half-duplex and **wedges** under connection
  churn or concurrent clients — after which it greets but silently swallows every
  request, and only a **physical power-cycle** recovers it (see
  [`protocol.md` §3.1, §9](protocol.md)).
- **Home Assistant wants a continuous, always-available picture** and will
  reasonably open, close, and retry connections as entities are added, reloaded,
  or restarted.

Point Home Assistant straight at the machine and those two facts collide: HA's
normal connection behavior is exactly the churn that wedges the machine. So the
project splits into **two layers** with a hard boundary between them.

```
   Home Assistant host                 bridge host                    R60V
 ┌─────────────────────┐   LAN    ┌───────────────────────┐  Wi-Fi  ┌──────────┐
 │  rocket_r60v         │◀────────▶│  governor + store     │◀───────▶│ :1774    │
 │  (custom integration)│ :1774 or │  (single owned socket)│ AP link │ (1 client│
 │                      │  :8788   │  front-end│push│MQTT   │         │  only)   │
 └─────────────────────┘          └───────────────────────┘         └──────────┘
      the consumer                    the governor                  the appliance
```

- **The bridge** owns the machine relationship: one socket, one disciplined
  conversation, and the single source of truth for machine state and reachability.
- **The integration** is a *consumer* of the bridge. It renders Home Assistant
  entities and never talks to the fragile machine socket directly.

---

## Layer 1 — the bridge (governor)

The bridge is a small always-on daemon (`bridge/r60v_broker`). It is a
**connection governor**, not a passthrough. A raw TCP proxy / NAT would let every
LAN client open its *own* socket to the machine and re-create the wedge — so the
bridge deliberately terminates every client and re-issues their requests through
one owned upstream socket.

### The governor

`DeviceGovernor` is the **sole owner** of the machine link. Every read and write
is submitted to it and serialized through a **single worker draining a priority
queue**:

- **Exactly one request is in flight at a time**, honoring the machine's
  half-duplex contract (see [`protocol.md` §3.1](protocol.md)). Callers never
  touch the socket; they submit a job and await its result.
- **Commands preempt polls.** A user command (a write) is enqueued at higher
  priority than routine polling reads, so changing a setting is never stuck
  behind a queue of temperature reads.
- **A gentle throttle** enforces a minimum spacing between operations on top of
  the client's own request pacing, keeping the conversation calm.
- **Failures are isolated.** A failed job resolves its own result with the
  error; the worker keeps running and stays the single owner.

`N` LAN clients therefore collapse into **one** calm, ordered upstream
conversation — the wedge is structurally prevented rather than merely avoided.

### The store — the single arbiter of state and availability

`DeviceState` is a transport-agnostic cache that is the **one** authority on what
the house sees. Every northbound face (front-end, MQTT, WebSocket push) is a
*projection* of the store; none computes its own view of reachability.

- **Last-known cache.** It holds the last-good register snapshot, so a single
  transient miss never blanks the picture.
- **Availability with hysteresis.** The machine is marked **offline only after
  several *consecutive* failed reads** (a grace window) and **back online on the
  first success**. This absorbs the R60V's occasional read desync (it drops the
  odd request) without flapping every entity unavailable.
- **A wedge always means offline.** When the bridge determines the machine is
  *wedged* (see below) and stops polling, it forces the store offline
  immediately — a cooldown must never leave the store frozen at a stale
  "available" verdict. (Availability is otherwise driven by the grace *count*
  while the wedge is driven by elapsed *time*; coupling the two here keeps a
  sustained outage reading Unavailable regardless of which trips first.)
- **Change events.** The store notifies subscribers on any state/availability
  change, so projections push updates instead of polling the store.

### Wedge recovery (cooldown)

The machine's listener can **wedge** — keep greeting but swallow every read — and
it does **not** self-recover while a client keeps knocking. The documented escape
is to *stop touching it* for a while so its control module resets.

`WedgeRecovery` tracks the failure streak and drives a back-off:

1. **Polling** — normal. Transient misses are tolerated.
2. **Wedged** — once failures are *sustained* (a wall-clock window, so it holds at
   any poll cadence), the bridge declares a wedge, **closes the upstream socket**
   (freeing the machine's single client slot), and stops polling for a
   **cooldown** period that lengthens on each repeat (minutes up to ~half an hour,
   matching the observed recovery window).
3. **Probe** — when the cooldown elapses, the bridge recovers **gently** with a
   single light read. If the machine answers, it resumes immediately; if it is
   still wedged, the back-off extends.

Crucially, a genuine *wedge* (control module hung) still requires a **physical
power-cycle** of the machine — the cooldown just stops the bridge from hammering a
listener that cannot recover while it is being knocked on, and resumes the instant
the machine is reachable again.

### Northbound faces

The governed link is re-presented on the LAN three (optional, composable) ways:

- **Native-protocol front-end** (`:1774`, on by default). Speaks the machine's
  *own* wire protocol, so an existing client just points at the bridge with no
  protocol change. Demand-driven: it proxies each client request through the
  governor and never runs an autonomous poll loop.
- **WebSocket push** (`:8788`, opt-in). Fast-polls the machine **only while a
  subscriber is connected** and streams the raw register snapshot (plus the
  store's `available` verdict). This is what makes the Home Assistant integration
  a `local_push` integration — near-real-time, no polling from HA.
- **MQTT Discovery** (opt-in). Publishes each entity and routes HA commands, for
  setups that prefer the MQTT path. The MQTT publisher is a projection of the
  store, like the others.

### Write-intent channel (optimistic + reconciled)

Over the push channel, a write from Home Assistant is sent as a **write-intent**
rather than a synchronous machine write:

1. The bridge applies it **optimistically** to the store and broadcasts at once,
   so HA reflects the change immediately.
2. It performs the governed write with a bounded **edge retry** (absorbing a
   transient miss so it never surfaces to the user).
3. It **reconciles** on the next authoritative read — a failed write self-heals
   to the machine's real value.

An in-flight optimistic byte is re-applied on top of every authoritative read, so
a poll that started before the command cannot revert it; the overlay is cleared
per-command once that command has reconciled.

### The emulator

`r60v_broker.emulator` is a faithful TCP emulator of the machine — it speaks the
real protocol (`*HELLO*`, hex frames, checksums, the 115-byte settings block,
live registers, strict half-duplex). It lets the bridge and the integration be
developed and CI-tested end to end **with no physical machine**.

---

## Layer 2 — the Home Assistant integration

The custom integration (`custom_components/rocket_r60v`) is a modern,
async-first HA integration built around a `DataUpdateCoordinator`. It is a **thin
consumer of the bridge** and performs no device I/O in entity constructors or
property getters (all state comes from a cached snapshot fetched off the event
loop).

### Two modes

- **Push mode** (`local_push`, recommended) — configured with a WebSocket push
  URL (`ws://<bridge-ip>:8788`). The push client subscribes to the bridge stream
  and drives every update via the coordinator; the coordinator does **no** machine
  I/O and carries none of the polling-era back-off logic. Machine-entity
  availability is keyed off the **store's** `available` flag carried in each
  frame. Setup succeeds whenever the *bridge* is reachable — it no longer depends
  on the machine being on.
- **Polling mode** (`local_polling`) — no push URL configured. The coordinator
  polls the bridge's native front-end (`:1774`) on a gentle interval, tolerating
  transient desync (serving last-known-good through a short failure tolerance)
  and running its own wedge cooldown before marking entities unavailable.

### Entity availability

A **machine entity** is available only when **both** hold: the transport is up
(the bridge stream in push mode, or the poll in polling mode) **and** the store
reports the machine reachable. So a machine hiccup surfaces as unavailable
entities without the integration deciding anything locally.

**Diagnostic entities are the exception** — they override availability to stay
**visible precisely when the machine is not**, so you can always see *why* the
machine went away.

### The 3-signal health taxonomy

Three separately-owned health signals are surfaced so a diagnosis is legible, and
so **no diagnostic ever reads "connected" while the machine is unavailable**:

| Signal | Entity | Answers |
|--------|--------|---------|
| **Machine** (store verdict) | `Machine` connectivity binary sensor | Is the machine's data live, per the bridge store (the arbiter)? |
| **Bridge ↔ machine link** | bridge-health sensors (link, signal, reachable, AP visible, powered-off, diagnostic window) | Is the bridge's Wi-Fi link to the machine's AP up / recovering? |
| **Integration ↔ bridge transport** | `Connection` sensor | Is the push stream / poll to the bridge healthy? |

The `Connection` sensor is an enum with these states:

| State | Meaning |
|-------|---------|
| `connected` | Transport up and the machine is reachable. |
| `reconnecting` | Transport down (a dropped push stream, or a failed poll). |
| `machine_unavailable` | Transport up, but the store says the machine is unreachable (off or wedged). |
| `cooldown` | A wedge cooldown is active (polling mode). |
| `bridge_down` | The bridge reports its Wi-Fi link to the machine is down. |
| `bridge_recovering` | The bridge is actively recovering the link (a diagnostic window). |

Together these let you tell "my espresso machine is off/wedged" apart from "Home
Assistant lost the bridge" apart from "the bridge lost its Wi-Fi link" — the
distinctions that make [troubleshooting](troubleshooting.md) tractable.

---

## Design invariants

A few rules the whole system leans on. Breaking one re-opens a class of bug the
architecture exists to prevent.

- **The machine sees exactly one client.** Only the governor holds an upstream
  socket. Never add a second talker (a raw proxy, a manual probe left running, a
  duplicate bridge) — that is what wedges the machine.
- **The store is the single arbiter of reachability.** Projections *read* it;
  they never compute a competing availability verdict.
- **A cooldown implies Unavailable.** Entering a wedge back-off forces the store
  offline; the house is never shown stale state as if it were live.
- **Never reconnect on a swallowed reply.** Retry a swallowed request in place on
  the same socket; reconnect only on a genuine socket fault, and always tear
  connections down cleanly so no orphaned connection holds the single client slot.
- **A wedge is recovered by the machine, not the software.** The cooldown stops
  the bridge from making it worse; a hung control module is cleared by a physical
  power-cycle.
