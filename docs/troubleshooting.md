# Rocket R60V — Troubleshooting

A symptom-driven guide to diagnosing a Rocket R60V + bridge + Home Assistant
setup. For how the pieces fit together, read [`architecture.md`](architecture.md)
first; for the wire protocol, [`protocol.md`](protocol.md).

Throughout, replace the placeholders with your own values:

- `‹machine-ip›` — the machine's control endpoint. In the normal case (hosting
  its own `RocketEspresso` AP) this is the **fixed, invariant** address
  `192.168.1.1` — it never changes, so the bridge's default `R60V_HOST` never
  needs touching. It differs only if you have deliberately joined the machine to
  your home network, in which case it is the fixed address it uses there.
- `‹bridge-ip›` — the bridge host's LAN address.
- `‹machine-iface›` — the bridge's Wi-Fi interface pinned to the `RocketEspresso`
  AP.
- `‹bridge-service›` — the systemd unit running the bridge, if you installed one.

> **The golden rule of R60V diagnosis: an ICMP ping proves almost nothing.**
> `192.168.1.1` is one of the most common addresses on earth. If the bridge host
> also has a home-network interface, a `ping 192.168.1.1` can be answered by *a
> completely different device* via your LAN gateway, not the espresso machine.
> Always confirm which interface the traffic actually egresses (see
> [Check your route](#check-your-route)) before trusting reachability. This false
> lead has fooled experienced operators.

### Addressing (it's static)

The R60V's own addressing is **fixed, not a dynamic lease**:

- When hosting its `RocketEspresso` AP, the machine is **always** `192.168.1.1`
  (its own gateway and DHCP server) on the `192.168.1.0/24` subnet, listening on
  TCP `1774`. This never changes, so the bridge's `R60V_HOST=192.168.1.1` default
  is permanent.
- The machine *is* a DHCP server on that subnet, so an address it hands a **client**
  (e.g. the bridge's dongle) may vary between associations — don't hard-code a
  client lease; rely on the fixed `192.168.1.1` for the machine itself.
- The only time the machine's control address is *not* `192.168.1.1` is if you
  have deliberately provisioned it onto a home network, where it takes a fixed
  address there. That address is specific to your setup; point `R60V_HOST` at it.

---

## Start here — read the diagnostic entities

The integration surfaces three deliberately separate health signals (see
[architecture §3-signal taxonomy](architecture.md#the-3-signal-health-taxonomy)).
They stay **visible even when every machine entity is unavailable**, so read them
first — they usually tell you the failure mode without touching a terminal.

| Entity | What a bad value means |
|--------|------------------------|
| **Connection** sensor | `reconnecting` → HA can't reach the **bridge**. `machine_unavailable` → bridge is fine, the **machine** is off/wedged. `bridge_down` / `bridge_recovering` → the **bridge's Wi-Fi link** to the machine is down. `cooldown` → a wedge back-off is active. |
| **Machine** connectivity | `off` → the store (the arbiter) considers the machine unreachable. |
| **Bridge link** sensors | `machine_reachable`, `ap_visible`, `machine_powered_off`, link `up`/`down`/`recovering`, signal — the bridge's own view of the Wi-Fi hop. |

Map the `Connection` value to a section below:

- `reconnecting` → [The bridge is down / unreachable](#a-the-bridge-is-down-or-unreachable)
- `bridge_down` / `bridge_recovering` → [The bridge lost its Wi-Fi link](#b-the-bridge-lost-its-wi-fi-link-to-the-machine)
- `machine_unavailable` / `cooldown` → [The machine is off or wedged](#c-the-machine-is-off-or-wedged)
- `connected` but entities wrong → [State looks stale or wrong](#d-state-looks-stale-or-wrong)

---

## The master decision tree

When **all machine entities read `unavailable`**, walk this once:

```
All machine entities unavailable
│
├─ Connection = reconnecting ─────────▶ Bridge unreachable → A
│
├─ Connection = bridge_down/recovering ▶ Bridge Wi-Fi link down → B
│      (bridge link sensor: ap_visible=false, link=down)
│
├─ Connection = machine_unavailable ──▶ Machine off OR wedged → C
│   or cooldown                          use the refused-vs-timeout test
│
└─ Connection itself unavailable ─────▶ Integration/bridge transport is fully
                                         down, or the config entry failed to load
                                         → restart HA / re-check the push URL
```

The single most useful discriminator, once you know the machine's real IP, is
**whether `:1774` is *refused* vs *times out* vs *accepts-then-swallows*** — it
separates "powered off" from "on the network but control-listener down" from
"wedged." See [The refused-vs-timeout test](#the-refused-vs-timeout-test).

---

## A. The bridge is down or unreachable

**Symptom:** `Connection` = `reconnecting`; the push stream won't stay up.

1. Is the bridge process running?
   ```bash
   systemctl status ‹bridge-service›
   # or, if run by hand, check the process / its log output
   ```
2. Are its ports listening on the bridge host?
   ```bash
   ss -ltnp | grep -E ':1774|:8788'
   ```
   `:1774` = native front-end, `:8788` = WebSocket push. If the integration is in
   push mode, `:8788` must be listening.
3. Can Home Assistant reach the bridge? From the HA host:
   ```bash
   nc -vz ‹bridge-ip› 8788    # push mode
   nc -vz ‹bridge-ip› 1774    # polling mode
   ```
4. Check the integration's configured host / push URL (Settings → Devices &
   Services → Rocket R60V → Configure): the push URL should be
   `ws://‹bridge-ip›:8788`, and the host should be the **bridge's LAN IP**, not
   `192.168.1.1` (unless HA itself is the machine-facing host).

**Fix:** start/restart the bridge; correct the configured address.

---

## B. The bridge lost its Wi-Fi link to the machine

**Symptom:** `Connection` = `bridge_down`/`bridge_recovering`; bridge link sensor
shows `ap_visible=false`, `link=down`. The bridge is up but can't hold the
`RocketEspresso` AP.

### Is the dongle associated?

On the bridge host:

```bash
iw dev ‹machine-iface› link          # "Not connected." = not associated
ip link show ‹machine-iface›         # look for NO-CARRIER / state DOWN
wpa_cli -i ‹machine-iface› status    # wpa_state=INACTIVE = no association
```

### Is the AP even on air?

```bash
sudo iw dev ‹machine-iface› scan | grep -i 'SSID:'   # is RocketEspresso listed?
```

- **`RocketEspresso` present but not associated** → the bridge's pinned Wi-Fi
  profile isn't connecting. Re-check the profile: it must have **no gateway**
  (`ipv4.never-default yes`, `ipv4.dns ""`), tolerate the missing internet
  (`ipv4.may-fail yes`), and **retry forever**. On NetworkManager, the
  autoconnect retry count must be **unlimited** (`connection.autoconnect-retries
  0`) — otherwise, the first time the machine powers off, NM gives up and
  **never** reconnects when it returns. Also disable NM connectivity checking so
  the internet-less AP isn't torn down as "no connectivity."
- **`RocketEspresso` *absent* from the scan** → the machine isn't broadcasting its
  AP. That is **not** proof the machine is off (see C — a wedged or
  client-mode machine also stops broadcasting). Go to C.

> **Watchdog caveat.** A bridge-side link watchdog that concludes *"AP not
> visible ⇒ machine powered off ⇒ no action"* is making an assumption that is
> false for a wedged machine or one that has dropped its AP into client mode. If
> your machine is demonstrably on but the watchdog reports "powered off," this is
> why — confirm with the refused-vs-timeout test below rather than trusting the
> heuristic.

---

## C. The machine is off or wedged

**Symptom:** `Connection` = `machine_unavailable` (or `cooldown`); the bridge is
healthy but the machine's data is stale/gone. This is the hardest to read,
because **three very different states look similar from a distance**:

1. **Powered off** — normal. The machine is asleep; its AP and control listener
   are both down. Entities *should* read unavailable. Nothing to fix.
2. **Wedged control module** — the machine is mechanically live (it heats and
   brews) but its Wi-Fi/control board has hung: it may still answer at the IP
   layer, yet its `:1774` control listener is dead or swallowing every request,
   and it may have **dropped its `RocketEspresso` AP**.
3. **Joined your home network as a client** — the machine's Wi-Fi module left AP
   mode and connected to your home network with a fixed address of its own, so the
   bridge's `192.168.1.1` target is stale even though the machine is reachable at
   that other address.

Disambiguate with the tests below.

### Check your route

Before anything, make sure you're testing the **real** machine and not a
same-addressed device on your LAN:

```bash
ip route get 192.168.1.1
```

If this egresses your **home** interface via your LAN gateway (e.g.
`via ‹gateway› dev ‹home-iface›`) rather than the machine-facing radio, then any
ping/probe of `192.168.1.1` is hitting **the wrong device**. Test against the
machine's *actual* current address instead (see [Find the machine](#find-the-machine)).

### The refused-vs-timeout test

Once you have the machine's real `‹machine-ip›`, probe the control port:

```bash
timeout 5 bash -c '</dev/tcp/‹machine-ip›/1774' && echo OPEN || echo "refused/timeout"
# or:  nc -vz ‹machine-ip› 1774
```

| Result | Interpretation |
|--------|----------------|
| **Connection refused** (immediate RST) | Something is at that IP, but nothing is listening on `:1774`. Either the machine is **powered off / deep standby** (AP also gone), or its control listener has died while the network stack limps on (**wedged**). |
| **Timeout / no route** | The IP is unreachable at the network layer — the AP/link is down (→ B) or the address is wrong. |
| **OPEN** | The listener is up. If entities are still unavailable, it's a **wedge that accepts but swallows** — confirm with the greeting probe. |

### The greeting probe (alive vs wedged)

A healthy listener greets with the literal string `*HELLO*` the moment you
connect. A wedged one either refuses, or accepts and then **goes silent**.

```bash
python3 - <<'PY'
import socket
try:
    s = socket.create_connection(("‹machine-ip›", 1774), timeout=4)
    s.settimeout(4)
    print("GREETING:", s.recv(64))   # expect b'*HELLO*'
    s.close()
except Exception as e:
    print("failed:", type(e).__name__, e)
PY
```

- `b'*HELLO*'` then normal reads → the machine is healthy; look upstream (bridge
  config, dongle).
- **Connection refused** → listener down (powered off, or wedged with the service
  crashed).
- **Connects, greets, then swallows every read** → the classic **wedge**.

### Read the bridge log

```bash
journalctl -u ‹bridge-service› --since '20 min ago' --no-pager | tail -40
```

- Repeated `connect failed ([Errno 111] Connection refused ... :1774)` with **no
  wedge/cooldown messages** → the listener is simply refusing (off, or crashed).
- `R60V appears wedged (... sustained failure); entering a ... cooldown` → the
  bridge has detected a wedge and backed off. The machine needs a power-cycle.

### Find the machine

If the machine is on but `RocketEspresso` isn't on air, it may have joined your
home network. Look on your router / controller for a **recently-joined wireless
client whose Wi-Fi-module vendor OUI matches the machine's** (the R60V's Wi-Fi
board is not an Espressif/ESP part — identify it once from your own working setup
and remember the OUI). Then probe `:1774` at that client's address with the tests
above.

- If `:1774` is **open** there → the machine simply moved to client mode. Point
  the bridge at the new address (`R60V_HOST=‹machine-ip›`) or re-provision the
  machine back to hosting its `RocketEspresso` AP.
- If `:1774` is **refused** there while the machine is live → it is **wedged**;
  power-cycle it.

### Recovery

| State | Action |
|-------|--------|
| Powered off | None — it's correct. Power the machine on; entities repopulate automatically. |
| Wedged | **Physically power-cycle the machine** (mains switch or its outlet). A hung control module cannot be revived over the network — the bridge's cooldown only stops it from making things worse. After it boots and re-hosts `RocketEspresso` (or its listener returns), the bridge reconnects on its own. |
| Cooldown active, machine already recovered | Press the **End Cooldown** button entity to retry immediately instead of waiting out the back-off. |
| Client-mode drift | Re-provision to AP mode, or set `R60V_HOST` to the machine's home-network IP. |

---

## D. State looks stale or wrong

**Symptom:** `Connection` = `connected`, entities available, but a value looks
wrong.

- **A boiler shows the setpoint as its "current" temperature.** On some units the
  live temperature register mirrors the setpoint; the integration prefers the
  front-panel display text when shown. This is a known hardware quirk, not a bug.
- **Temperatures look off by a unit.** Live temperatures are a single byte in
  **Celsius regardless of the display unit**; setpoints in the settings block are
  in the machine's *current* unit. See [`protocol.md` §6.2, §7](protocol.md).
- **The group setpoint reads implausibly.** The group setpoint register decode is
  a known open question (see [`protocol.md` §6.1 note](protocol.md)); writes are
  still range-clamped.
- **A brief window of last-known values after the machine goes away.** By design:
  the store serves the last-good snapshot through a short grace window to absorb
  the R60V's occasional read desync, *then* flips to unavailable. If it stays
  "available" with frozen values for many minutes, that's a bug — file it.

---

## Behavior FAQ

**Why don't entities go unavailable the instant the machine drops?**
Deliberate. The R60V drops the odd read even when perfectly healthy; blanking
every entity on one miss would flap constantly. The store waits for *sustained*
failure (a grace window; a wedge cooldown for a real hang) before reporting
Unavailable. A genuinely unreachable machine *does* end up Unavailable — it just
isn't hair-trigger.

**Why do the diagnostic entities stay visible when everything else is
unavailable?**
That's the point of them — they exist to explain *why* the machine went away, so
they must survive the machine going away. Read them first.

**The machine is on and making coffee, but HA says unavailable. Is that a bug?**
Usually not a *software* bug — it means the bridge genuinely can't reach the
machine's control interface (wedged listener, dropped AP, or client-mode drift).
Work C. The espresso machine's heating and brewing are independent of its Wi-Fi
control board; the board can wedge while the machine works fine.

**Do I ever need to power-cycle the bridge or Home Assistant?**
Rarely. The bridge and integration recover on their own once the machine is
reachable. A power-cycle is for the **machine** when its control module wedges.
