# BSL (bootstrap loader)

The C167's boot ROM can start code sent over its serial port, with the ECU held in
boot mode (the BSL pin pulled low at power-up). GKFlasher uses that to run the stock
SIMK4x kernel, and through it reads the external flash or the internal ROM and writes
the whole external flash, boot sectors included.

The boot ROM is in the CPU, so BSL doesn't depend on what's in the flash. It recovers
an ECU that doesn't boot anymore, for example after a failed
[MTOS full flash](mtos.md#full-flash-bsl-kernel-over-mtos).

It runs on the K-line interface: a KKL cable, or a J2534 adapter.

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
| `--bsl-baudrate` | 57600 (default), 38400, 19200 or 9600. The boot ROM takes whatever the first byte comes at |
| `--bsl-driver` | `auto` (default), `i4` (SIMK43 2.0) or `v6` |
| `--bsl-rom-size` | How much internal ROM `readint` reads |
| `-o` | Where `read`/`readint` save, or where `write` saves its backup |

In the GUI it's the BSL tab, with the interface selected at the top.

Nothing resets the ECU at the end. The kernel keeps running, and the next command
picks it up rather than going through the boot ROM again. To start the ECU normally,
switch it off and disconnect the BSL pin.

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
