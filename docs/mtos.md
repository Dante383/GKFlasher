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
| `probe`  | Starts the BSL kernel over MTOS and does everything a full flash does before the first erase, then resets the ECU. Writes nothing to the flash. See [Full flash](#full-flash-bsl-kernel-over-mtos). |
| `read`   | Starts the BSL kernel and reads the whole 512 KB external flash to `-o` (or `mtos_read_<time>.bin`). |
| `fullflash` | Starts the BSL kernel and rewrites the whole flash, **boot sectors included**, from `--mtos-payload` (a 512 KB image). Backs up first. |

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
| **Mode** (`hello` / `dump` / `payload` / `loader` / `probe` / `read` / `fullflash`) | `--mtos-mode` |
| **Key** | `--mtos-key` |
| **Select file** / file box | `--mtos-payload` (payload, loader and fullflash modes), `-o` (read mode) |
| **Dump address** / **Length** | `-s` / `-e` (dump mode; default `0x000000`+`0x40`) |
| **Run MTOS** | runs the selected mode |

Output and the received K-line bytes appear in the log pane, same as the CLI.
`fullflash` asks for confirmation in a dialog before the kernel starts. Picking a
session back up (`--mtos-attach`) is CLI only.

---

## How it works (trigger sequence)

GKFlasher performs, in order:

1. `StartDiagnosticSession(DEFAULT)` — required before Siemens-level access and
   for the RAM upload.
2. `SecurityAccess(0xFD)` — request seed, send key (`--mtos-key`).
3. Read the 6-byte signature from flash `0x3FE8` (`ReadMemoryByAddress`).
4. Build the blob (header + your code) and `WriteMemoryByAddress` it to RAM at
   `0xC000` in 16-byte chunks.
5. `StartRoutineByLocalIdentifier(0xFA)` — the firmware validates the header, and on
   success performs a far `calls 0xC014` into your code.

The firmware is blocked until your code executes `rets`.

> **Session note (ca663057 and later):** on these calibrations `0xFA`'s gate only
> opens in the **flash-reprogramming** session, while the RAM upload still only
> works in the **default** session. If `0xFA` comes back refused
> (`7F 31 33` securityAccessDenied) in the default session, GKFlasher switches to
> the flash-reprogramming session, re-does the `0xFD` access (the grant is
> per-session), and fires the routine again — the blob is already resident in RAM
> and survives the switch, so it is not re-uploaded. Older calibrations (ca663056)
> run the routine straight from the default session and never need the switch.

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
`build_blob()` sets this for you.

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

Service it with the protected `SRVWDT` instruction (raw bytes `A7 58 A7 A7`, as used
by the firmware itself at `0x000634` etc.). Execute it every iteration of any
wait/long loop; it is cheap and harmless to do often. The receive loops in
`loader.a66` and the loop in `memory_dump.a66` already do this.

A short, straight-line payload (a handful of bytes out, then `rets`) does not need it.

### Flush the last transmitted byte

`S0TIC.7` (`TIR`) is set when `S0TBUF` is copied into the shift register — **not** when
the byte has finished leaving the wire. A payload that `rets` immediately after
writing its last byte to `S0TBUF` loses that byte, because the firmware takeover
clobbers the still-shifting byte. Before returning, send one throwaway byte and wait
for its `TIR`: that guarantees the previous (real) byte has been shifted out. The
ready-made transmit payloads (`kline_hello.a66`, `memory_dump.a66`) end this way.

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
  After the last byte, send the throwaway byte (see above). Restore `S0TIC.6` on exit.
- **Receive a byte:** clear `S0RIC.6`, then poll `S0RIC.7` until set — **servicing the
  watchdog every iteration** because the host may take a long time — read `S0RBUF`,
  clear the flag. Restore `S0RIC.6` on exit. Host RX works during takeover as-is; the
  only thing you must get right is the watchdog.

---

## Authoring payloads

Payloads are Keil A166 assembly. The ready-made ones live in `assets/mtos/` and make
a good starting point:

| Source | Used for |
|--------|----------|
| `marker.a66` | `payload_marker()`, proof of execution without I/O |
| `kline_hello.a66` | `hello` mode |
| `memory_dump.a66` | `dump` mode |
| `loader.a66` | `loader` mode, and starting the BSL kernel |
| `stage2_hello.a66` | sample second stage for `loader` mode |
| `bsl_shim.a66` | the second stage in front of the BSL kernel (full flash) |
| `driver_patch.a66` | the flash driver's watchdog patch (full flash) |

GKFlasher assembles them at runtime with [openc166](https://github.com/Dante383/openc166),
an open-source assembler compatible with Keil A166 (`pip install -r requirements.txt`
installs it). A166 can't set a symbol from its command line, so GKFlasher fills in
runtime values (the dump range, the loader's target) by replacing the value on the
symbol's `EQU` line. The sources stay plain A166 and give the same bytes with Keil.

Your own payload goes in a `SECTION CODE AT 0C014H` (`0E000H` for a loader second
stage), inside a `PROC FAR` so that `RET` assembles as `RETS`:

```asm
$MOD167
$INCLUDE (REG167.INC)

MTOS    SECTION CODE AT 0C014H  ; MTOS calls 0C014H
Main    PROC    FAR             ; with a far call, so RET becomes RETS
        BCLR    S0TIE           ; no transmit interrupt: we poll the UART
        MOVB    S0TBUF, #'H'
Tx1:    JNB     S0TIR, Tx1      ; wait until the UART has taken it
        BCLR    S0TIR
        MOVB    S0TBUF, #0      ; dummy byte: 'H' has gone out
Tx2:    JNB     S0TIR, Tx2      ; once the UART takes it
        BCLR    S0TIR
        BSET    S0TIE           ; give the UART back to the firmware
        RET
Main    ENDP
MTOS    ENDS
        END
```

Assemble it to a raw binary and run it in `payload` mode (or `loader` mode, for a
second stage). GKFlasher adds the MTOS header:

```bash
openc166 hi.a66         # writes hi.bin
python gkflasher.py -p kline -i j2534:openport --mtos --mtos-mode payload \
    --mtos-payload hi.bin --mtos-key 0x2d381c78
```

Or from Python:

```python
import openc166
from flasher.mtos_asm import build_blob, payload_memory_dump

# Option A: a ready-made payload
body = payload_memory_dump(0x000000, 0x40)

# Option B: your own source (code starts at 0xC014)
body = openc166.assemble(open('hi.a66').read(), file_name='hi.a66').image().data

# Wrap it in the MTOS header. `signature` is the 6 bytes read from the ECU @0x3FE8.
blob = build_blob(body, signature)
```

`build_blob()` computes the CRC, sets the CRC range to the body, writes the `MT`
magic, and drops in the signature. The result is what gets written to `0xC000`.

Run the module to see the reference payloads and the self-tests, which check the
ready-made payloads against the bytes verified on the ECU:

```bash
python -m flasher.mtos_asm
```

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
same way, minus the MTOS header. `assets/mtos/stage2_hello.a66` is an example:

```bash
openc166 assets/mtos/stage2_hello.a66 -o stage2_hello.bin
python gkflasher.py -p kline -i j2534:openport --mtos --mtos-mode loader \
    --mtos-payload stage2_hello.bin --mtos-key 0x2d381c78
```

Code that only uses relative jumps, like the ready-made payloads, runs at any
address, so a top-level payload works as a second stage too:

```python
from flasher.mtos_asm import payload_memory_dump
stage2 = payload_memory_dump(0x000000, 0x200)   # raw body, no header
# write stage2 to a file and pass it with --mtos-payload in loader mode
```

Because the loader's own epilogue runs after the stage returns, a second stage does
not strictly need its own throwaway byte — but ending with one (as `memory_dump.a66`
does) is the safe default. Verified end to end on the bench: a 256-byte memory
dump streamed as a second stage and executed from `0xE000` returns byte-for-byte the
same data as reading flash directly.

---

## Full flash (BSL kernel over MTOS)

`probe`, `read` and `fullflash` turn MTOS into a whole-flash tool. They rewrite all
512 KB of the external flash, **including the boot sectors**, from a running ECU over
the normal K-line, without opening the case for BSL. Use them to move an ECU to a
firmware with a different bootloader, such as `ca663056` (or lower) to `ca663057`.
KWP2000 flashing can't do that: it only writes the program and calibration areas.

> ⚠️ A full flash can brick the ECU. If it fails or loses power while the boot sectors
> are being written, the ECU won't boot until it's recovered over BSL. Read the
> [safety](#safety) notes first, and use a bench supply.

```bash
# 1. Dry run: everything up to the first erase, then the ECU is reset. Nothing is written
python gkflasher.py -p kline -i /dev/ttyUSB0 --mtos --mtos-key 0x2d381c78 \
    --desired-baudrate 0x05 --mtos-mode probe

# 2. Read the whole flash
python gkflasher.py -p kline -i /dev/ttyUSB0 --mtos --mtos-key 0x2d381c78 \
    --desired-baudrate 0x05 --mtos-mode read -o flash.bin

# 3. Full flash. Backs up to -o (or mtos_backup_<time>.bin) and asks before erasing anything
python gkflasher.py -p kline -i /dev/ttyUSB0 --mtos --mtos-key 0x2d381c78 \
    --desired-baudrate 0x05 --mtos-mode fullflash --mtos-payload image.bin
```

`--desired-baudrate 0x05` runs the session, and so the kernel, at 125000 baud. At
the default 10400 baud a full read takes about 10 minutes, and a full flash reads the
flash twice (the backup and the verify).

### How it works

1. The usual MTOS sequence runs the built-in [loader](#the-loader-protocol-second-stage).
   Its second stage is a short prologue plus the **stock SIMK4x BSL kernel**
   (`assets/simk4x_kernel.bin`, the same one [BSL](bsl.md) uses), copied to `0xE000` in XRAM.
   The prologue moves the stack and context pointer out of the firmware's RAM, then
   jumps into the kernel. The firmware never gets the CPU back.
2. The kernel greets with `0x03` and speaks the BSL protocol over the K-line, at the
   diagnostic session's baudrate.
3. GKFlasher sets `ADDRSEL1` to `0x4008`, putting the flash write strobe where the BSL
   flash driver expects it. It then uploads the stock **I4 flash driver**
   (`assets/simk4x_driver_i4_a29fx00bx.bin`) to `0xF600` and reads it back.
4. Before anything is erased, it checks that the read path is the flash the firmware
   runs from: `0x800000` (the driver's view) must match `0x080000`, and the MTOS
   signature must be at `0x803FE8`. It also reads the chip IDs through the driver,
   which proves the write path. Only AM29F400BB and M29F400BB are accepted.
5. `fullflash` reads the whole flash and saves it as a backup. It lists the sectors
   that differ from the image and asks for confirmation. It then erases, programs and
   verifies only those sectors, and finally verifies the whole flash.
6. The kernel software-resets the ECU, and GKFlasher checks that it talks KWP2000 again.

Unlike in BSL, the **watchdog is running and can't be stopped**, because `DISWDT` only
works before `EINIT`. The prologue sets the longest watchdog period (about 335 ms). The
kernel services the watchdog while it waits for a byte. The driver is patched to
service it in its erase poll (`mtos_asm.patch_driver_watchdog`); programming polls a
word at a time and doesn't need it.
The kernel doesn't service it while it sends, so every reply has to fit well inside
the watchdog period. That's why reads are split into blocks: `0x80` bytes at 10400 baud,
more at higher rates.

The kernel code (`flasher/kernel.py`) is shared with BSL. GKFlasher checks every byte
the kernel sends back before it sends the next one. With the firmware itself being
rewritten, a parameter byte the kernel takes for a command can't just be logged and
ignored.

### Safety

- **Backup first.** `fullflash` always reads and saves the whole flash before it erases
  anything. Keep that file: it's what you'd recover with.
- **Only what differs is written.** If the flash already matches the image, nothing is
  written.
- **Boot code last.** SA0 holds the reset vector and the boot code. Up to `ca663056`
  that's all of it; `ca663057` jumps from SA0 to the rest of its boot code (boot2) in
  SA3. GKFlasher reads that from the reset vectors of the flash and the image. Data
  and program sectors go first, SA4–SA10, SA1, SA2, then the image's boot2 if it has
  one, then SA0. Sectors that only the ECU's current boot code needs come after SA0:
  going back from `ca663057` to `ca663056`, the order ends SA0, SA3. That way a reset
  boots either the old or the new boot code at every point except while SA0 itself is
  written. The confirmation prompt says when a boot sector is included.
- **Every sector is verified** after it's written, with up to 3 attempts per sector. At the
  end the whole flash is read back and compared to the image. Any sector that differs
  is rewritten, up to twice.
- **Ctrl+C** stops at the next safe point, never inside a sector write. A second
  Ctrl+C stops right away, even mid-command.
- **If it fails before the flash was touched,** the ECU is reset, or you're told to
  power cycle it.
- **If it fails after the flash was touched,** the kernel is **left running**. The ECU
  is not reset into a half-written flash. Keep the ECU powered and run the command
  GKFlasher prints, which is the same command with `--mtos-attach`:

  ```bash
  python gkflasher.py -p kline -i /dev/ttyUSB0 --mtos --mtos-mode fullflash \
      --mtos-attach --mtos-payload image.bin --desired-baudrate 0x05
  ```

  This finds the kernel again (no KWP2000, no key) and runs the mode again from the
  start, including a new backup. Use the **same `--desired-baudrate`** as the session
  that left the kernel running. Attach works with `probe`, `read` and `fullflash`.

### Getting back in sync, and what can still go wrong

If a reply doesn't arrive, or doesn't match what was expected, GKFlasher resyncs:

1. It waits for the line to go quiet.
2. It sends `F0 F9 00`, then zeros one byte at a time, until the kernel answers.
3. It confirms the kernel is back in its command loop.

A kernel left waiting for the parameters of a register-call command takes those first
bytes as the call address, `0x00F9F0`. GKFlasher keeps a `rets` there (the *trap*).
This means a call cut off between its command byte and its address, for example when
the host dies, just returns. The trap is armed before the driver is used and re-armed
after every resync. Leftover programming or erasing from such a cut-off command would
show up in the final full verify.

What remains is a call whose **address arrived partly**, from corrupted bytes on the
line or the host dying in the middle of a write. The kernel may then call an arbitrary
address. That's unlikely, but if it happens, the ECU may need BSL.

### Recovery

If the ECU no longer boots, use [BSL](bsl.md) (`--bsl --bsl-mode write`) to write back the backup
`fullflash` saved, or any good full image.

### Verified on hardware

On the bench (SIMK43 2.0 `ca663056`, ST M29F400BB, FTDI K-line adapter, 125000 baud),
`probe`, `read` and a `fullflash` that rewrote SA2, SA3 and SA0 all passed. The ECU
booted from the new boot sector and answered KWP2000 and MTOS again. A fresh read
matched the image byte for byte, and the firmware's own view (`dump` mode) agrees with
the kernel's reads. A full read takes about a minute at 125000 baud.

An upgrade from `ca663056` to `ca663057`, which replaces the bootloader, has also been
done with `fullflash`.

Not verified yet:

- J2534 adapters. FTDI is the reference.
- Going back from `ca663057` to `ca663056`.

Run `probe`, then `read`, before the first `fullflash` on a new ECU or adapter. Also
compare the read with the image you're about to write. Sectors with per-ECU data, such
as the emulated EEPROM records in SA2, get the image's contents.

---

## Troubleshooting

- **Security access fails:** wrong `--mtos-key`, or you skipped the default session.
  GKFlasher does the session for you; double-check the key.
- **`StartRoutine(0xFA)` refused with `7F 31 33` (securityAccessDenied), nothing runs:**
  the routine's session gate is shut in the current session. On ca663057 and later it
  only opens in the flash-reprogramming session — GKFlasher now detects the refusal and
  retries there automatically (you'll see a "retrying after switching session" line). If
  it still fails in both sessions the key or calibration is wrong.
- **ECU resets mid-payload (output stops early, or nothing comes back):** the watchdog
  fired. Your routine waited in a loop or ran longer than a few milliseconds without
  calling `srvwdt`. Service the watchdog in every wait/long loop (see above).
- **Last byte of a transmit/dump is missing:** you returned before it finished
  shifting out. Send a throwaway byte before `rets` (the built-in payloads do).
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
