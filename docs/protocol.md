# Rocket R60V — Control Protocol Reference

Reverse-engineered reference for the **Rocket R60V** espresso machine's WiFi
command-and-control protocol.

> **Provenance.** Derived by decompiling the official Android app
> `com.gicar.Rocket_R60V` **v2.3** (`versionCode` 13, `sha256
> 81f6b08d9e6cd54cbbc40aa97bb5373e4f0063f7767b07c9b6c95dc751a59a5b`) with
> **jadx 1.5.0**, cross-validated against two independent
> reverse-engineering efforts: [`confirm/Rocket-R60V`](https://github.com/confirm/Rocket-R60V)
> (Python) and [`jffry/rocket-r60v`](https://github.com/jffry/rocket-r60v)
> (NodeJS). All three agree; discrepancies are noted inline.

---

## 1. Key facts (TL;DR)

- **No encryption. No protobuf.** Despite folklore, the app contains no
  `javax.crypto`, no TLS, and no protocol buffers. It is **plaintext ASCII-hex
  over raw TCP**. (Verified: zero `Cipher`/`AES`/`SSLSocket`/`protobuf`
  references in the decompiled sources.)
- **Endpoint:** the machine hosts its own WiFi AP and listens on
  **`192.168.1.1:1774`**.
- **One connection, strictly half-duplex.** The app holds a single long-lived
  socket and never has more than one request in flight. Flooding the socket with
  concurrent/pipelined requests is the root cause of the "unstable connection"
  seen with naive clients.
- **Batch reads exist.** The entire 115-byte settings block can be read in a
  single request (`r00000073`); you do **not** need one round-trip per setting.

## 2. Physical & network layer

The R60V does **not** join your WiFi. It **broadcasts its own** access point:

| Property | Value |
|----------|-------|
| SSID | `RocketEspresso` |
| Passphrase | `RocketR60V` (WPA2, fixed, publicly known) |
| Machine IP | `192.168.1.1` (also the DHCP server / gateway) |
| Client subnet | `192.168.1.0/24` (DHCP lease) |
| Control port | TCP **1774** (`0x6EE` — hardcoded in the app as `0x6ee`) |

The AP has **no uplink to the internet** and typically weak signal. A client
that treats it as its *primary* network will be dropped by connectivity-managers
(NetworkManager / Home Assistant) that expect internet, and a single radio
cannot hold both `RocketEspresso` and the home network. **The supported topology
is a dedicated bridge**: one interface pinned to `RocketEspresso` (no gateway,
`never-default`, connectivity-check disabled) and a *separate* interface (a
second Wi-Fi radio or wired Ethernet) for the home LAN. A small always-on host
(e.g. a Raspberry Pi with a USB-WiFi dongle) running the bundled bridge is the
reference setup.

## 3. Connection protocol

1. **Open** a TCP socket to `192.168.1.1:1774`.
2. The machine immediately sends a greeting; the confirm client expects the
   literal string **`*HELLO*`** before proceeding. Treat a missing/invalid
   greeting as a failed connect.
3. Exchange **request → response** messages, one at a time (§4).
4. Keep the socket **open** for the session; reconnect with backoff on drop.

### 3.1 The half-duplex discipline (critical)

The app's own I/O loop (`wifi/WiFi.java`) reveals the timing contract every
robust client must honor:

- A background thread loops with `SystemClock.sleep(100)` — a **~100 ms tick**.
- Each tick, if bytes are available it drains and dispatches them; otherwise it
  may send **at most one** queued request.
- A single `FlagWriteInCorso` state variable gates writes so **only one request
  is outstanding at any time**. The app never pipelines.
- Responses are matched by verifying the response **envelope** (command +
  address + length) equals the request's, plus a checksum check.

**Implication for our broker:** serialize all requests through a queue with a
single in-flight slot; send, await the matching response (or timeout+retry),
then release the next. Do **not** open a socket per request, and do **not** fan
out concurrent reads. This is the "cleaner protocol management" the prior
libraries lacked.

### 3.2 Idle keepalive / polling cadence

While idle, the app cycles a counter and issues periodic reads:

- Every cycle: **`HexProtocol.ReadAll()`** → `r00000073` (full settings block).
- Periodically: a hardcoded live read of the **`0xB000`** region (current brew
  boiler temperature) — the app builds `r B000 …` by hand each ~10 ticks.

A steady, gentle poll (batch settings + a couple of live registers every ~1 s)
keeps state fresh without stressing the socket.

## 4. Message frame format

All messages are **ASCII text**: each byte is written as **two uppercase hex
characters**. A raw message is:

```
<command><address><length><data...><checksum>
```

| Field | Chars | Type | Description |
|-------|-------|------|-------------|
| command | 1 | `r` or `w` | `r` (0x72) read, `w` (0x77) write |
| address | 4 | uint16, hex | memory address (big-endian hex text) |
| length | 4 | uint16, hex | number of **data bytes** |
| data | `length`×2 | uint8[] hex | payload (write only; empty for reads) |
| checksum | 2 | uint8, hex | `sum(all preceding ASCII bytes) & 0xFF` |

- The first 9 characters (`command`+`address`+`length`) are the **envelope**.
- The **checksum** is the sum of the ASCII byte values of every preceding
  character, modulo 256, emitted as 2 uppercase hex chars.

### 4.1 Worked example

Set language to English — write 1 byte `0x00` at address `0x0001`:

```
w 0001 0001 00 59
└┬┘ └─┬┘ └─┬┘ ┬ └┬┘
 │    │    │  │  └ checksum 0x59
 │    │    │  └─── data byte 0x00
 │    │    └────── length = 1
 │    └─────────── address = 0x0001
 └──────────────── command = write
```
Raw message: `w000100010059`.

### 4.2 Checksum algorithm

```python
def checksum(message: str) -> str:
    return f"{sum(message.encode()) & 0xFF:02X}"
```
(The app implements the same sum with manual nibble→hex conversion using the
`+'0'` / `+'7'` offsets for `0-9` / `A-F`.)

### 4.3 Response validation

A response echoes the request **envelope** and appends data + checksum. Validate
by (a) comparing the first 9 chars to the request envelope and (b) recomputing
the checksum. A write ack returns `OK` in the data field. An unsupported address
returns an **"invalid response envelope"** (many high counter addresses do this
— see §6.3).

## 5. Batch & special operations

Constants and builders from `bluetooth/HexProtocol.java` (used for the WiFi
path too — the `bluetooth` package name is legacy):

| Operation | Raw message | Meaning |
|-----------|-------------|---------|
| **Read all settings** | `r00000073` | read `0x0000`, length `0x73` (115 bytes) — the entire settings block in one round-trip |
| **Read counters** | `r00D90038` | read `0x00D9`, length `0x38` (56 bytes) — counter block |
| **Reset counters** | `w00DE0024` + 72×`0` + cksum | zero the counter region at `0x00DE` |
| **Set credit** | `w010A0007` + `<enable><value…>` | commercial credit/payment feature at `0x010A` |

`WriteAll()` exists in the app but is a no-op stub. The `ReadAll` batch is the
recommended way to refresh state.

## 6. Memory address map

Addresses are `uint16`. The **settings block** (`0x00`–`0x72`) is what `ReadAll`
returns; **live/read-only registers** live at `0xA000`+/`0xB000`+; **counters**
at `0x00D9`+. Field names are the app's Italian identifiers
(`singleton/SettingsSingleton.java`), cross-referenced with confirm's map.

### 6.1 Settings block (read/write, `r`/`w`)

| Addr | Field (app) | Meaning | Notes / valid range |
|------|-------------|---------|---------------------|
| `0x00` | `UM_TEMP_ADDRESS` | Temperature unit | 0 = °C, 1 = °F |
| `0x01` | `LINGUA_ADDRESS` | Language | 0=English,1=German,2=French,3=Italian |
| `0x02` | `TEMP_SET_CAF_ADDRESS` | **Brew boiler** setpoint | °C 85–115 / °F 185–239 |
| `0x03` | `TEMPERATURA_VAPORE_ADDRESS` | **Service (steam) boiler** setpoint | °C 115–125 / °F 239–257 |
| `0x04` | `KP_CAFFE_ADDRESS` | Coffee PID — Kp | 0–500 (uint16, 2 bytes) |
| `0x06` | `KP_GRUPPO_ADDRESS` | Group PID — Kp | 0–500 |
| `0x0A` | `KI_CAFFE_ADDRESS` | Coffee PID — Ki | 0–900 |
| `0x0C` | `KI_GRUPPO_ADDRESS` | Group PID — Ki | 0–900 |
| `0x10` | `KD_CAFFE_ADDRESS` | Coffee PID — Kd | 0–500 |
| `0x12` | `KD_GRUPPO_ADDRESS` | Group PID — Kd | 0–500 |
| `0x16` | *(profile A)* | **Pressure profile A** | 16-byte block (points) |
| `0x26` | *(profile B)* | **Pressure profile B** | 16-byte block |
| `0x36` | *(profile C)* | **Pressure profile C** | 16-byte block |
| `0x2B` | `ENAB_PRE_INF_ADDRESS` | Enable pre-infusion | bool |
| `0x2C` | `T_OFF_PRE_INF_ADDRESS` | Pre-infusion off-time | 4 bytes |
| `0x30` | `T_ON_PRE_INF_ADDRESS` | Pre-infusion on-time | 4 bytes |
| `0x45` | `TEMP_SET_LANCIA_ADDRESS` | Wand/lance temp | |
| `0x46` | `INGRESSO_ACQUA` | **Water feed** source | `0` = mains (HardPlumbed), `1` = tank (Reservoir) |
| `0x47` | `TIPO_TASTIERA_ADDRESS` | **Active pressure profile** | selects A/B/C |
| `0x48` | `T_LAV_LANCIA_ADDRESS` | Wand wash time | 0–255 |
| `0x49` | `ENAB_CALDVAP_ADDRESS` | **Service boiler enable** | bool |
| `0x4A` | `STATO_MACCHINA_ADDRESS` | **Standby / machine state** | 0=on, 1=standby (toggled) |
| `0x4B` | `COUNT_PARZ_ADDRESS` | Partial coffee counter | |
| `0x4C` | `TEMP_SET_GRUPPO_ADDRESS` | **Group** setpoint | °C 89–100 / °F 192–212 |
| `0x4D` | `COUNT_TOT_ADDRESS` | **Total coffee count** | |
| `0x51` | `ORA_AUTO_ON_ADDRESS` | **Auto-on hour** | 0–23, or `100` = disabled |
| `0x52` | `MIN_AUTO_ON_ADDRESS` | **Auto-on minute** | 0–59, or `100` = disabled |
| `0x53` | `ORA_AUTO_OFF_ADDRESS` | **Auto-off hour** | 0–23, or `100` = disabled |
| `0x54` | `MIN_AUTO_OFF_ADDRESS` | **Auto-off minute** | 0–59, or `100` = disabled |
| `0x55` | `DAY_OFF_ADDRESS` | **Weekly rest day** | 1 byte: 0=none, 1=Mon … 7=Sun |
| `0x59` | `CICLI_MANUT_ADDRESS` | Maintenance cycle count | 4 bytes |

> **Built-in timer enable/disable.** There is **no separate enable register**
> (an earlier note speculated a 4-byte `ENAB_PROG` at `0x55`; the decompiled app
> disproves it). A timer is *disabled* by writing the sentinel `100`
> (`SHUTDOWN_VALUE`) to **both** its hour and minute byte, and *enabled* by
> writing a valid clock time. In the official app, selecting "no automatic
> start/stop" sets `OraAuto*` = `MinAuto*` = 100; `TimerSingleton` reads/writes
> `0x51`–`0x55` as single bytes each. `0x55` (`DAY_OFF`) is a single-byte
> closing-day index, not a mask.

### 6.2 Live / read-only registers (`r` only)

| Addr | Meaning | Notes |
|------|---------|-------|
| `0xA000` | Date & time (clock) | read/write |
| `0xB000` | **Current brew boiler temperature** | live; the app's keepalive polls this |
| `0xB001` | **Current service boiler temperature** | live |
| `0xB002` | **Current pressure** | live |
| `0xB007` | **Display content** | mirrors the machine's on-screen text |

> **Real-hardware confirmations (validated against a live R60V).**
> Validated against the physical machine:
> - `0xB000` → `0x69` = **105** and `0xB001` → `0x7C` = **124**: live temps are a
>   **single byte in Celsius**, *regardless* of the display unit (`0x00`).
> - `0xB007` (16 bytes) decodes to ASCII, e.g. `"BREW BOIL. 221*F"` (221 °F =
>   105 °C), confirming the machine was set to **Fahrenheit** for display.
> - Live registers must be read **individually** — a *range* read spanning
>   `0xB000`+ (e.g. `rB0000010`) returns **nothing**; `rB0000001` works.
> - The machine frequently **swallows the very first request after the
>   `*HELLO*` greeting**; a throwaway warm-up read (or an empty-reply retry)
>   fixes it. Subsequent requests on the same socket are reliable.
> - `0xA000` (date/time) and the `0x00D9` counters block returned **empty** on
>   this unit; treat them as **unconfirmed** (not yet wired into the broker).
> - **Anomaly — group setpoint `0x4C` reads `27`**, implausible for a group
>   temperature (~93 °C expected). The `0x4C` address or its encoding is
>   suspect; broker write-validation still clamps to 89–100 °C, but the read
>   decode needs investigation before the group thermostat is trusted.

### 6.3 Counters (`0x00D9`+)

Read as a block via `r00D90038`. Individual per-key/per-group/per-tea dose and
shot counters (`DOSES_COUNT_K*_GR*`, `COUNT_K*_GR*`, `COUNT_TEA*`,
`LITRI_FILTRO`, `COUNT_LAVAGGIO`, `COUNT_POMPA`, …) live from `~0x5D` upward.
confirm found many of the **individual** high addresses return "invalid response
envelope" when read one-by-one — prefer the **block read**.

## 7. Value encoding notes

- **Single-byte** values (unit, language, enables, hours/minutes, temperatures
  in the settings block) are one data byte.
- **Multi-byte** values (PID terms, timers, counters) are little-endian byte
  sequences; e.g. PID `Kp` is a 2-byte value at `0x04`.
- **Temperature setpoints** in the settings block are stored in the machine's
  current unit; honor `0x00` (unit) when interpreting/validating. Conversion
  helpers in the app: `F = round(C*1.8+32)`, `C = round((F-32)/1.8)`.
- **Pressure profiles** are 16-byte blocks (`0x16`/`0x26`/`0x36`, spaced 0x10
  apart) encoding the profile's pressure points over time.
- **Standby** (`0x4A`) is written as a toggle: read current state, write the
  opposite.

## 8. Home Assistant entity mapping

Target entity model for the Home Assistant integration:

| HA platform | Entities |
|-------------|----------|
| **climate** | Brew boiler (setpoint `0x02`, current `0xB000`), Service boiler (setpoint `0x03`, current `0xB001`), Group (setpoint `0x4C`) |
| **sensor** | Current pressure (`0xB002`), current brew time, total coffee count (`0x4D`), dose/shot counters (`0x00D9` block), display content (`0xB007`) |
| **switch** | Standby (`0x4A`), Service boiler enable (`0x49`), pre-infusion enable (`0x2B`) |
| **select** | Active profile A/B/C (`0x47`), Water feed (`0x46`), Temperature unit (`0x00`), Language (`0x01`) |
| **number** | Pressure-profile points (`0x16`/`0x26`/`0x36`), PID terms (`0x04`–`0x12`), wand wash time (`0x48`) |
| **time** | Auto-on (`0x51`/`0x52`), Auto-off (`0x53`/`0x54`), clock (`0xA000`) |

## 9. Implementation guidance (broker)

1. **One socket, one thread of control.** Persistent connection with `*HELLO*`
   handshake and exponential-backoff reconnect.
2. **Single in-flight request queue.** Never pipeline; match responses by
   envelope; timeout → bounded **same-socket** retry (the app retries 3×).
   Pace requests (~100 ms gap); back-to-back requests get dropped.
6. **Do not reconnect on a swallowed reply.** Confirmed on real hardware: the
   listener tolerates exactly **one** stable, paced connection, but **connection
   churn wedges it** — after enough rapid reconnects (or an **orphaned/duplicate
   connection holding the single client slot**) it will greet (`*HELLO*`) yet
   swallow *every* read, and it does **not** self-recover on an idle timeout.
   Recovery then requires a **physical power-cycle** of the machine (resets its
   WiFi/control module). So: retry a swallowed request **in place** on the same
   socket, reconnect only on a genuine socket fault, and always tear connections
   down cleanly (never leave an orphaned probe/process holding the slot).
3. **Poll with the batch read.** Refresh settings with `r00000073`, and live
   values with **individual** `0xB000`-region reads (the machine rejects range
   reads there), on a gentle cadence. Absorb the first-request-after-greeting
   swallow with a warm-up read.
4. **Validate writes** against the ranges in §6 before sending — the machine
   controls real boilers; reject out-of-band setpoints client-side.
5. **Publish northbound over MQTT Discovery** so HA entities appear
   automatically and never touch the fragile TCP link directly.

## 10. References

- [`confirm/Rocket-R60V`](https://github.com/confirm/Rocket-R60V) — Python API +
  CLI; its [`REVERSE_ENGINEERING.rst`](https://github.com/confirm/Rocket-R60V/blob/master/REVERSE_ENGINEERING.rst)
  is the fullest public write-up.
- [`jffry/rocket-r60v`](https://github.com/jffry/rocket-r60v) — NodeJS; original
  emulator + proxy sniffing methodology.
- [`JulianKahnert/RocketAPI`](https://github.com/JulianKahnert/RocketAPI) —
  older `R60V.py` toolkit.
