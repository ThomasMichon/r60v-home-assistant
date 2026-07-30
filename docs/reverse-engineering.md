# Rocket R60V — Reverse-Engineering & Provenance

Where the protocol knowledge in [`protocol.md`](protocol.md) comes from, and how
to reproduce or extend it. This is the *methodology* companion to the
[protocol reference](protocol.md) (which is the *result*).

The R60V has no public API and no official documentation of its control
protocol. Everything here was derived by reverse-engineering, cross-validated
across three independent efforts and confirmed against a physical machine.

---

## Provenance

The protocol map was primarily derived by **decompiling the official Android
app**:

- Package `com.gicar.Rocket_R60V`, **v2.3** (`versionCode` 13,
  `sha256 81f6b08d9e6cd54cbbc40aa97bb5373e4f0063f7767b07c9b6c95dc751a59a5b`).
- Decompiled with **[jadx](https://github.com/skylot/jadx) 1.5.0**.

It was cross-validated against two prior, independent reverse-engineering
projects, and then confirmed against a live R60V:

- **[jffry/rocket-r60v](https://github.com/jffry/rocket-r60v)** (NodeJS) — the
  original protocol mapping, done by **proxy sniffing** the app↔machine traffic,
  plus an emulator.
- **[confirm/Rocket-R60V](https://github.com/confirm/Rocket-R60V)** (Python) — a
  mature API/CLI; its
  [`REVERSE_ENGINEERING.rst`](https://github.com/confirm/Rocket-R60V/blob/master/REVERSE_ENGINEERING.rst)
  is the fullest public write-up of the wire format and address map.
- **[JulianKahnert/RocketAPI](https://github.com/JulianKahnert/RocketAPI)** — an
  earlier Python toolkit.

All three agree on the framing and the core address map; where they diverged, the
decompiled app was treated as the tie-breaker and discrepancies are noted inline
in [`protocol.md`](protocol.md).

---

## Two complementary methods

### 1. Static analysis (decompilation)

Decompiling the app reveals the protocol *by construction* — the exact constants,
the framing, the timing loop, and the field names — without needing the machine
powered on. The load-bearing classes:

| Class (decompiled) | What it reveals |
|--------------------|-----------------|
| `wifi/WiFi.java` | The **I/O loop**: a background thread on a ~100 ms tick, a single `FlagWriteInCorso` write-gate (one request in flight), envelope+checksum response matching, and the `*HELLO*` handshake. This is the source of the half-duplex discipline. |
| `bluetooth/HexProtocol.java` | The **framing and batch constants**: `ReadAll()` → `r00000073`, the counter block read/reset, the `0x6ee` (1774) port, checksum construction. (The `bluetooth` package name is legacy; the Wi-Fi path uses the same codec.) |
| `singleton/SettingsSingleton.java` | The **settings address map** — the `*_ADDRESS` constants (Italian field names) for the `0x00`–`0x72` block. |
| `singleton/TimerSingleton.java` | The **auto-on/off timer semantics**: hours/minutes as single bytes at `0x51`–`0x54`, the `100` (`SHUTDOWN_VALUE`) sentinel for "disabled", and `0x55` as a single-byte closing-day index (not a mask). |
| conversion helpers | `F = round(C*1.8+32)` / `C = round((F-32)/1.8)`, and the manual nibble→hex checksum using `+'0'` / `+'7'` offsets. |

Two pieces of **folklore were disproved** this way: the app contains **no
`javax.crypto`, no TLS, no protocol buffers** (zero `Cipher`/`AES`/`SSLSocket`/
protobuf references) — the protocol is plaintext ASCII-hex over raw TCP — and
there is **no separate timer-enable register** (the earlier-speculated 4-byte
`ENAB_PROG` at `0x55` does not exist).

### 2. Dynamic analysis (proxy sniffing)

The complementary method (jffry's original approach): stand a TCP proxy between
the app and the machine and record the actual bytes. Because the protocol is
plaintext, captured frames are directly readable, which:

- confirms the static reading against **real traffic**, and
- reveals **runtime behavior** static analysis can't show — e.g. the idle
  keepalive cadence, and the machine's quirks (see below).

### 3. Live-hardware validation

Several facts only surface against a physical machine and are confirmed in
[`protocol.md` §6.2](protocol.md):

- Live temperatures (`0xB000`/`0xB001`) are a **single byte in Celsius regardless
  of the display unit**.
- The display register `0xB007` decodes to ASCII (e.g. `"BREW BOIL. 221*F"`).
- Live registers must be read **individually** — a *range* read across the
  `0xB000` region returns nothing.
- The machine **swallows the first request after `*HELLO*`** — a throwaway warm-up
  read fixes it.
- The group setpoint `0x4C` reads an implausible value on at least one unit — its
  decode is an **open question**, flagged in the reference.

---

## Reproduce it yourself

To re-derive or verify the protocol map from the app:

```bash
# 1. Obtain the Rocket R60V Android app APK (com.gicar.Rocket_R60V).
# 2. Decompile it:
jadx -d out com.gicar.Rocket_R60V.apk

# 3. The load-bearing sources:
#    out/sources/com/gicar/.../wifi/WiFi.java              (I/O loop, handshake)
#    out/sources/.../bluetooth/HexProtocol.java            (framing, constants)
#    out/sources/.../singleton/SettingsSingleton.java      (address map)
#    out/sources/.../singleton/TimerSingleton.java         (timer semantics)

# 4. Sanity-grep for the debunked folklore:
grep -rniE 'cipher|aes|sslsocket|protobuf' out/sources   # → expect nothing
grep -rniE '0x6ee|ReadAll|FlagWriteInCorso|HELLO' out/sources
```

To capture live traffic (dynamic method): join the `RocketEspresso` AP, point a
TCP proxy at `192.168.1.1:1774`, aim the app at the proxy, and log the frames.
They're plaintext ASCII-hex — readable as-is.

---

## Extend it — the emulator as a test oracle

This repository ships a **wire-level emulator**
([`bridge/r60v_broker/emulator.py`](../bridge/r60v_broker/emulator.py)) that
speaks the real protocol (`*HELLO*`, hex frames, checksums, the 115-byte settings
block, live registers, strict half-duplex). It is the practical way to extend the
protocol work without a physical machine:

```bash
cd bridge && pip install -e '.[dev]'
python -m r60v_broker.emulator --host 127.0.0.1 --port 1774 -v
```

To add or correct a register:

1. Confirm the address/encoding from the decompiled app and/or a live capture.
2. Add it to the codec/address map ([`protocol.py`](../bridge/r60v_broker/protocol.py))
   and teach the emulator to serve it.
3. Add a test that exercises it against the emulator, and — if you have hardware —
   validate on the real machine and record the confirmation (or the anomaly) in
   [`protocol.md`](protocol.md).

Keep the reference **honest about confidence**: mark unconfirmed or anomalous
fields as such rather than presenting them as settled.

---

## See also

- [`protocol.md`](protocol.md) — the wire-protocol reference (the result).
- [`architecture.md`](architecture.md) — how the bridge and integration use it.
- The three prior-art projects linked above — credited as prior art, **not**
  runtime dependencies (this project ships its own transport and codec).
