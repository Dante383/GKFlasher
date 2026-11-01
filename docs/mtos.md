# MTOS — Mini Test Operating System

MTOS is a stock Siemens diagnostic feature baked into the SIMK43 firmware. It lets
you upload a small machine-code routine into ECU RAM and have the firmware execute
it, synchronously, over the normal KWP2000 K-line diagnostic session. It is the
ECU's own "run arbitrary code" primitive — originally a factory test hook — and we
can use it to poke at memory, prototype routines, or bootstrap larger tooling.

This document describes the execution contract (so you can author your own
payloads) and how to drive it from GKFlasher.

> **Reversed from `ca663056` (SIMK43 2.0, Infineon C167).** The header layout and
> CRC are firmware constants; the 6-byte signature is firmware-version specific and
> is read live from the ECU, so the flow is portable across firmware versions on the
> same platform. Other ECUs are untested.

---

## Quick start

MTOS runs over the K-line diagnostic session, so force the K-line protocol:

```bash
# Safest first test: upload a payload that sends "MTOS" out of the K-line and
# run it. Proves upload + CRC validation + execution + I/O in one shot.
python gkflasher.py -p kline -i j2534:openport \
    --mtos --mtos-mode hello --mtos-key 0x2d381c78
```

Expected tail of the output:

```
[*] Executing, listening on K-line
[*] Received: 4d544f53 (b'MTOS')
[+] SUCCESS: payload executed and echoed b'MTOS'
```

The `--mtos-key` is the Siemens (0xFD) security-access key for your ECU. For the
reference bench ECU it is `0x2d381c78`.

### Modes

| `--mtos-mode` | What it does |
|---------------|--------------|
| `hello` (default) | Sends the ASCII bytes `MTOS` out of the K-line, then returns. Proves upload, validation, execution and UART I/O. |
| `dump`   | Streams a block of ECU memory out of the K-line. Range comes from `-s/--address-start` and `-e/--address-stop` (defaults to `0x000000`+`0x40`). Sample stays within one 16 KB page. |
| `payload`| Uploads a raw body you authored (`--mtos-payload file.bin`, code starting at `0xC014`), runs it, and listens on the K-line. |
| `loader` | Uploads the built-in K-line loader, then streams `--mtos-payload` to it as a second stage and runs that. Lets you run code larger than the MTOS RAM budget. |

> There is deliberately no "write a value to RAM and read it back" self-test mode.
> `StartRoutine(0xFA)` ends the KWP session once your code returns, and the ensuing
> re-handshake re-initialises RAM, so a marker written to RAM can't be observed
> afterwards. Prove execution by its **K-line output** instead (`hello`/`dump`).

Examples:

```bash
# Say hello over K-line
python gkflasher.py -p kline -i j2534:openport --mtos --mtos-mode hello --mtos-key 0x2d381c78

# Dump 0x80 bytes of internal ROM at 0x000000 over K-line
python gkflasher.py -p kline -i j2534:openport --mtos --mtos-mode dump \
    -s 0x000000 -e 0x000080 --mtos-key 0x2d381c78

# Run your own payload body
python gkflasher.py -p kline -i j2534:openport --mtos --mtos-mode payload \
    --mtos-payload my_body.bin --mtos-key 0x2d381c78
```

### From the GUI

The **MTOS** tab exposes the same flow:

| GUI field | CLI equivalent |
|-----------|----------------|
| **Mode** (`hello` / `dump` / `payload` / `loader`) | `--mtos-mode` |
| **Key** | `--mtos-key` |
| **Select file** / file box | `--mtos-payload` (payload and loader modes) |
| **Dump address** / **Length** | `-s` / `-e` (dump mode; default `0x000000`+`0x40`) |
| **Run MTOS** | runs the selected mode |

Output and the received K-line bytes appear in the log pane, same as the CLI.

---

## How it works (trigger sequence)

GKFlasher performs, in order:

1. `StartDiagnosticSession(DEFAULT)` — required before Siemens-level access.
2. `SecurityAccess(0xFD)` — request seed, send key (`--mtos-key`).
3. Read the 6-byte signature from flash `0x3FE8` (`ReadMemoryByAddress`).
4. Build the blob (header + your code) and `WriteMemoryByAddress` it to RAM at
   `0xC000` in 16-byte chunks.
5. `StartRoutineByLocalIdentifier(0xFA)` — the firmware validates the header, and on
   success performs a far `calls 0xC014` into your code.

The firmware is blocked until your code executes `rets`.

---

## The blob contract

Your payload is a **blob** uploaded to RAM at `0xC000`. It is a 20-byte (`0x14`)
header followed by your machine code. All multi-byte fields are little-endian.

| Offset   | Size | Field |
|----------|------|-------|
| `0xC000` | 2 | CRC16 of the body (see below) |
| `0xC002` | 2 | unused on this path (free scratch for your code) |
| `0xC004` | 4 | CRC range **start**, 24-bit linear (low word = offset, high word = segment) |
| `0xC008` | 4 | CRC range **end**, inclusive, same encoding |
| `0xC00C` | 6 | Signature — must equal flash `0x3FE8..0x3FED` (read live) |
| `0xC012` | 1 | `'M'` (`0x4D`) |
| `0xC013` | 1 | `'T'` (`0x54`) |
| `0xC014` | …  | **Entry point** — your code. Entered via far `calls`; end with `rets`. |

The validator (firmware `0x003460`) checks the magic `MT`, the signature, and that
the stored CRC matches a fresh CRC over `[start..end]`. Any mismatch aborts without
jumping — so a corrupt upload can't run.

### CRC16

- Seed: `0x544D` (the `'MT'` word).
- Reflected poly `0xA001` (CRC-16/IBM style), processed per byte, LSB first:

```python
def crc16_mtos(data, crc=0x544D):
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF
```

Recommended CRC range: start `0xC014` (first code byte), end = last code byte.
The builder sets this for you.

---

## Rules for payload authors

Entered at `0xC014` via an inter-segment `calls` (segment 0). Follow these:

- **End with a single `rets`** (far return). This hands control back to the firmware.
- **Preserve `r0`** (the stack pointer). Save/restore any other GPRs you clobber.
- **`DPP0` is `0x24`** on entry (not the default) — do **not** rely on the default
  DPP mapping for absolute data access. Prefer:
  - the `0xC000–0xFFFF` window, which maps to itself via `DPP3` (covers your blob,
    XRAM, and the SFRs), or
  - `extp`/`exts` extended addressing for one-off accesses to other pages.
- **SFRs are directly addressable** regardless of DPP.
- While your code runs it has **exclusive use of the ASC0 UART** (= the K-line),
  because the firmware is blocked waiting for your `rets`. Global interrupts are
  already disabled on entry (the firmware's StartRoutine pre-hook clears `IEN`), so
  no ISR will steal your received bytes.

### ⚠️ Service the watchdog

The C167 has a hardware watchdog that resets the chip unless it is serviced
periodically. Normally the firmware main loop does this — but **while your payload
runs the firmware is blocked, so nothing services it.** Any loop that waits for I/O
(e.g. receiving bytes) or simply runs for more than a few milliseconds (e.g. a long
dump) **must** service the watchdog itself, or the ECU silently resets mid-payload.

Service it with the protected `SRVWDT` instruction — in this assembler, `Asm.srvwdt()`
(raw bytes `A7 58 A7 A7`, as used by the firmware itself at `0x000634` etc.). Call it
every iteration of any wait/long loop; it is cheap and harmless to call often. The
built-in `kline_recv_reg` and `payload_memory_dump` already do this for you.

A short, straight-line payload (a handful of bytes out, then `rets`) does not need it.

### Flush the last transmitted byte

`S0TIC.7` (`TIR`) is set when `S0TBUF` is copied into the shift register — **not** when
the byte has finished leaving the wire. A payload that `rets` immediately after its
last `kline_send` loses that last byte, because the firmware takeover clobbers the
still-shifting byte. Before returning, send one throwaway byte and wait for its `TIR`:
that guarantees the previous (real) byte has been shifted out. `Asm.kline_flush()`
does exactly this; the ready-made transmit payloads already call it.

### Memory you can use

| Region | Range | Notes |
|--------|-------|-------|
| MTOS blob / RAM | `0xC000–` | where your blob lives; `0xC002` header word is free scratch |
| XRAM | `0xE000–0xEEFF` | r/w/x — good scratch / second-stage area |
| SFRs | `0xFE00–0xFFFF` | peripherals, directly addressable |

### K-line I/O (ASC0)

The UART is already configured for the current diagnostic baud. Just poll it:

| Register | Addr | Use |
|----------|------|-----|
| `S0TBUF` | `0xFEB0` | transmit buffer |
| `S0RBUF` | `0xFEB2` | receive buffer |
| `S0TIC`  | `0xFF6C` | TX interrupt control; bit 7 = `TIR` (buffer-empty flag) |
| `S0RIC`  | `0xFF6E` | RX interrupt control; bit 7 = `RIR` (receive flag) |

- **Transmit a byte:** clear `S0TIC.6` first so the firmware's TX interrupt-enable
  doesn't fight you. Write the byte to `S0TBUF`, poll `S0TIC.7` until set, clear it.
  After the last byte, `kline_flush()` (see above). Restore `S0TIC.6` on exit.
- **Receive a byte:** clear `S0RIC.6`, then poll `S0RIC.7` until set — **servicing the
  watchdog every iteration** because the host may take a long time — read `S0RBUF`,
  clear the flag. Restore `S0RIC.6` on exit. Host RX works during takeover as-is; the
  only thing you must get right is the watchdog.

---

## Authoring payloads with the in-repo builder

`flasher/mtos_asm.py` is a tiny, dependency-free C167 assembler + blob builder. It
hand-encodes just the opcodes these payloads need (every encoding is annotated with
the firmware address it was verified against) and fills in the header + CRC for you.

```python
from flasher.mtos_asm import Asm, build_blob, payload_memory_dump

# Option A: use a ready-made payload generator
body = payload_memory_dump(0x000000, 0x40)

# Option B: hand-assemble your own body (code starts at 0xC014)
a = Asm()
a.bclr(0xFF6C, 6)            # own the TX path (clear S0TIC.6)
a.kline_send_imm(ord('H'))
a.kline_send_imm(ord('i'))
a.kline_flush()             # make sure the last byte fully leaves the wire
a.bset(0xFF6C, 6)           # restore
a.rets()
body = a.assemble()

# Wrap it in the MTOS header. `signature` is the 6 bytes read from the ECU @0x3FE8.
blob = build_blob(body, signature)
```

`build_blob()` computes the CRC, sets the CRC range to the body, writes the `MT`
magic, and drops in the signature. The result is what gets written to `0xC000`.

Run the module directly to see the reference payloads and self-tests:

```bash
python flasher/mtos_asm.py
```

> The assembler only knows the handful of instructions used here. If you need an
> opcode it doesn't have, add it (with a firmware citation) or emit raw bytes with
> `Asm.db(...)`.

---

## The loader protocol (second stage)

MTOS RAM is small. For larger routines, `--mtos-mode loader` uploads a ~100-byte
loader as the MTOS payload, which then pulls a **second stage** over the K-line:

1. The loader masks the ASC0 TX/RX interrupts so it owns the UART, and drops any
   byte left pending from the takeover.
2. The host (GKFlasher) sends a **u16 little-endian length**, then that many bytes.
3. The loader copies the bytes to `0xE000` (XRAM) and far-`calls` them. Every receive
   services the watchdog, so the loader can wait indefinitely between bytes.
4. The second stage runs, ends with `rets`, and control returns to the loader,
   which restores interrupts and `rets` back to the firmware.

So the second stage follows the **same convention** as a top-level payload (code
that ends with `rets`), it just lives at `0xE000` instead of `0xC014`. Build it the
same way, minus the MTOS header:

```python
from flasher.mtos_asm import payload_memory_dump
stage2 = payload_memory_dump(0x000000, 0x200)   # raw body, no header
# write stage2 to a file and pass it with --mtos-payload in loader mode
```

Because the loader's own epilogue runs after the stage returns, a second stage does
not strictly need its own `kline_flush()` — but using the ready-made generators (which
flush anyway) is the safe default. Verified end to end on the bench: a 256-byte memory
dump streamed as a second stage and executed from `0xE000` returns byte-for-byte the
same data as reading flash directly.

---

## Troubleshooting

- **Security access fails:** wrong `--mtos-key`, or you skipped the default session.
  GKFlasher does the session for you; double-check the key.
- **ECU resets mid-payload (output stops early, or nothing comes back):** the watchdog
  fired. Your routine waited in a loop or ran longer than a few milliseconds without
  calling `srvwdt`. Service the watchdog in every wait/long loop (see above).
- **Last byte of a transmit/dump is missing:** you returned before it finished
  shifting out. Add `kline_flush()` before `rets` (the built-in payloads do).
- **Loader receives nothing / hangs:** confirm you send the u16 length first, then the
  body; the loader waits (watchdog-serviced) for exactly that many bytes.
- **Routine ran but output looks wrong:** the header may have failed validation (bad
  CRC, wrong signature, missing `MT`). Rebuild with `build_blob()` and the live
  signature. A validation failure aborts without jumping, so you'd see no output at all.
- **Garbage / nothing on K-line:** make sure you cleared `S0TIC.6`/`S0RIC.6` and are
  polling the right flag before touching the buffer.

---

## Reference (ca663056)

- Validator: `0x003460` (called from the StartRoutine `0xFA` path at `0x0030ee`);
  executes far `calls 0x00C014` on success.
- CRC routine: `0x000E32`.
- Signature source: flash `0x3FE8` (6 bytes).
- K-line = ASC0: `S0TBUF 0xFEB0`, `S0RBUF 0xFEB2`, `S0TIC 0xFF6C`, `S0RIC 0xFF6E`.
- Watchdog: `SRVWDT` = `A7 58 A7 A7`, serviced by the firmware at `0x000634`,
  `0x0006C2`, `0x0008E2`, `0x000A58`; `WDTCON` at `0xFFAE`.
- The StartRoutine pre-hook re-inits peripherals and clears `IEN` (global interrupts
  off) before your code runs; it does **not** touch any ASC0 register, so the K-line
  stays usable as configured.
