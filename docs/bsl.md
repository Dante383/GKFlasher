# BSL (bootstrap loader)

The C167's boot ROM can start code sent over its serial port, with the ECU held in
boot mode (the BSL pin pulled low at power-up). GKFlasher uses that to run the stock
SIMK4x kernel, and through it reads the external flash or the internal ROM and writes
the whole external flash, boot sectors included.

The boot ROM is in the CPU, so BSL doesn't depend on what's in the flash. It recovers
an ECU that doesn't boot anymore, for example after a failed
[MTOS full flash](mtos.md#full-flash-opengk-kernel-over-mtos).

It runs on the K-line interface: a KKL cable, or a J2534 adapter.

## SIMK43 5WY19 bench connections

On the C167CS-LM board, hold CPU **pin 104 (P0L.4)** low at power-up to enter
BSL. Hold **pin 15 (P8.6)** low as well to select the K-line path through the
board's logic gates. The tested setup uses a separate 1 kOhm resistor from each
pin to ECU ground. These are CPU pin numbers, not connector pin numbers.

Connect the adapter's K-line lead to the normal ECU header K-line terminal
(`J1.b36` in the 5WY19 schematic), with a common ground. Do not connect an OBD
K-line output directly to CPU pin 78: that is a logic-level input driven by the
onboard receive gate. The header connection receives correctly at 750 kbaud on
the tested 1 MiB ECU. The user also confirmed BSL startup with the BCM W-line
connected; disconnecting W-line is not required by the tested setup.

The selector pull-down is needed before the boot ROM receives the first byte;
software uploaded afterward cannot fix the initial routing. Check the schematic
for other board versions before applying these connections. A logic analyser
limited to 5 V should monitor CPU pin 78 rather than the header K-line signal.

## Usage

Open the ECU, connect the BSL pin, connect the K-line adapter, and switch the ECU on.
Then:

```bash
# CPU, flash driver and chip, reset vector and identification
python gkflasher.py --bsl -i /dev/ttyUSB0

# read the external flash (default file: bsl_read_<time>.bin)
python gkflasher.py --bsl -i /dev/ttyUSB0 --bsl-mode read -o flash.bin

# read the internal ROM (default 0x8000 bytes, file bsl_irom_<time>.bin)
python gkflasher.py --bsl -i /dev/ttyUSB0 --bsl-mode readint --bsl-rom-size 0x8000

# write the whole external flash. Backs up to -o (or bsl_backup_<time>.bin) and asks first
python gkflasher.py --bsl -i /dev/ttyUSB0 --bsl-mode write --bsl-image image.bin
```

| Option | |
|---|---|
| `-i` | The K-line interface. Defaults to `kline: interface` in `gkflasher.yml`. `j2534` or `j2534:<name>` for a J2534 adapter |
| `--bsl-mode` | `hwinfo` (default), `read`, `readint`, `write` |
| `--bsl-baudrate` | Explicit startup/kernel baudrate; omitted = automatic discovery and qualification. GUI default: Automatic |
| `--bsl-driver` | `auto` (default), `i4` (SIMK43 2.0) or `v6` |
| `--bsl-rom-size` | How much internal ROM `readint` reads |
| `-o` | Where `read`/`readint` save, or where `write` saves its backup |
| `--skip-backup` | Do not save a backup before writing. Unless forced, the flash is still read to compare sectors. Matches GUI **Skip backup** |
| `--force-write` | Rewrite every sector even if it matches. Matches GUI **Force write all sectors** |
| `--no-verify` / `--skip-verify` | Skip all read-back verification after a `write`: sectors are programmed but never read back and checked (neither per-sector nor the final whole-flash pass). The GUI has a "Skip verification (write)" checkbox |

In the GUI it's the BSL tab, with the interface selected at the top. Both GUI and
CLI default to automatic discovery. A zero-byte query identifies the kernel or
boot ROM at the known entry rate; cold startup uses 57600 before testing RAM-kernel rates up to
750000. Each trial round-trips 256 bytes and restores the entry UART before its
result is accepted. A passed rate is committed and kernel readback is checked.
No ECU reset or flash operation is used for discovery. After a successful automatic
operation, the kernel returns to 57600 for the next automatic connection, while
the actual transfer uses the qualified faster rate. Numeric selections bypass
discovery and retain their selected rate. A kernel left by a failed or manually
selected session still requires its explicit baudrate; error messages include it.
A separate attach option is not needed. The host does not blindly scan unknown
kernel rates, since malformed command bytes can alter the parser state.

The BSL tab also provides two optional write controls, both off by default:

- **Skip backup (write)**: do not save a backup file. The flash is still read
  to identify changed sectors unless Force write is also selected.
- **Force write all sectors**: rewrite every sector, including those that already
  match. With Skip backup selected, the initial full-flash read is omitted.

Verification remains controlled separately by **Skip verification (write)**.
The confirmation dialog reflects these settings. Byte-based progress displays
the average processing speed for the current phase in **B/s**, alongside the
percentage. This is processed data throughput, not the UART's configured baudrate.
Read and backup completion messages show the output filename, then a separate
`Saved <bytes> bytes in <seconds> seconds` line. Successful flashing also reports
`Flashed <bytes> bytes in <seconds> seconds` on its own line. Flashing time covers
erase, programming and enabled verification after confirmation, excluding backup
and connection setup; its byte count is the total size of the rewritten sectors.

Nothing resets the ECU at the end. The kernel keeps running, and the next command
picks it up rather than going through the boot ROM again. To start the ECU normally,
switch it off and disconnect the BSL pin.

For repeated operations while the kernel remains powered, run the same command
again at the same baudrate. For example:

```bash
python gkflasher.py --bsl --interface COM69 --bsl-mode readint
```

Automatic reconnection also works for external-flash reads and writes. It does
not change the ECU's UART speed or reset it. To select a different speed, start
a fresh boot-mode session; requesting 57600 cannot reconnect to a kernel left
at 750000. The same command starts the loader automatically after a power cycle.
The selected baudrate must be supported by the CPU clock and adapter; 750000
was qualified on the tested 24 MHz C167CS setup.

## Fast serial reads and writes

Reads use the stock kernel's block-read protocol, including its block checksums.
At 750000 baud, the existing 512-byte read blocks achieved approximately 36.5 kB/s
on the tested FTDI/C167CS setup, with repeated exact calibration readbacks.
No separate streaming-read payload is needed.

High-speed serial writes use the same flash driver and sector-writing process,
with 2 KiB programming blocks staged in XRAM at `0xE000`. The staging area is
written and read back before any erase. Slower serial sessions and J2534 retain
the existing 512-byte programming buffer. Backup, sector verification and final
whole-flash verification remain in place.

A calibration-sector rewrite on the tested C167CS/AM29F800BB achieved approximately
32 kB/s across data upload and flash-driver programming calls. The entire 1 MiB
readback matched the original OpenGK image. This excludes erase, backup and
verification time; results vary with the adapter and programmed data.

```bash
python gkflasher.py --bsl --interface COM69 --bsl-mode readint
python gkflasher.py --bsl --interface COM69 --bsl-mode read
```

The kernel stays at the selected operating baudrate. Repeat the same command
without cycling ECU power; startup and reconnection use the same handshake.

Power the ECU in boot mode before starting the command. Repeat commands at the
same baudrate while the kernel remains running; no wait/attach or bytewise
startup option is required.

## How it works

1. GKFlasher sends `0x00`. The boot ROM measures the baudrate from it and answers with
   the CPU's ID (`0xC5` C167CR-LM, `0xD5` C167CS-LM). A kernel that's already running
   answers `0xAA` instead, since `0x00` is a no-op command to it.
2. The 32-byte loader (`assets/simk4x_bootstrap.bin`) goes to `0xFA40` and answers
   `0x01`. It takes the kernel (`assets/simk4x_kernel.bin`, 394 bytes) to `0xFA60`,
   which answers `0x03`.
3. The bus is set up the way the firmware would: `SYSCON`, `BUSCON0`, and `ADDRSEL1`/
   `BUSCON1` for the flash's write strobe at `0x400000`. `readint` maps the internal
   ROM at `0x010000` instead.
4. A flash driver goes to `0xF600`. The I4 board (SIMK43 2.0) has the flash's data
   lines crossed and needs its own driver. With `--bsl-driver auto`, the V6 driver is
   tried first: it reads the chip IDs, and if they're not a known chip, the I4 driver
   reads them un-crossed. The V6 driver can't get a crossed chip further than reading
   its IDs, so trying it first is safe.
5. `write` reads and saves the whole flash, then lists the sectors that differ from the
   image and asks before erasing anything. It erases, programs and verifies only those,
   the sector with the reset vector last, and finally verifies the whole flash.

The kernel, the drivers and the read and write code are shared with the MTOS full
flash (`flasher/kernel.py`). Every exchange with the kernel is checked before the next
byte goes out, and a broken transfer is retried after getting back in sync with the
kernel.

## Chips

| IDs | Chip | |
|---|---|---|
| `01/57`, `01/AB`, `01/58` | AM29F200BB, AM29F400BB, AM29F800BB | read, write |
| `20/D4`, `20/D6` | M29F200BB, M29F400BB | read, write |
| `01/51`, `01/23`, `01/D6` | AM29F200BT, AM29F400BT, AM29F800BT | read only |
| `20/D3`, `20/D5` | M29F200BT, M29F400BT | read only |

Top boot chips are read only. The flash drivers erase by sector number from a
bottom-boot sector table, so on a top boot chip they'd erase the wrong sectors.

## If something goes wrong

A failed or interrupted `write` leaves the kernel running. Run the same command again.
It picks the kernel up and rewrites whatever still differs. Switching the ECU off is
safe too: BSL starts from scratch the next time.

If there's no answer at all, check that the ECU is in boot mode and powered, and that
the baudrate is the same as the previous command's if a kernel was left running.

## Compression

BSL reads and writes use independent CRC16-checked LZSS blocks when the RAM
codec can be qualified. The same codec is shared with MTOS. BSL uses only the
standard 2 KiB XRAM: a 1 KiB output buffer and a separate helper; packed write
input uses the kernel's existing 512-byte buffer. Less compressible write blocks
use the original raw flash driver. Erase ordering and final verification remain
unchanged. No firmware update is required for BSL compression.

The codec has been executed and checked on the C167 through MTOS; cold BSL
read/write hardware validation is still required.
