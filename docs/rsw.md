# RSW recovery flashing

CAN retains the native CCP bootstrap and the bundled RSW loader. K-Line runs the
same RSW command processor and flash routines from a relocated RAM copy, through
CRC-protected UART RPC. It does not substitute the MTOS flash programming driver.

## K-Line usage

```bash
python gkflasher.py --protocol kline --interface COM69 --rsw-cal firmware.bin --rsw-key 0x2d381c78 --desired-baudrate 0x06
```

The example key must be replaced with your ECU's Siemens key.
`--rsw-key` is required for the K-Line RAM bootstrap and unused on CAN.
The key is entered in the RSW tab for GUI operations. The existing Desired
Baudrate selector controls the UART RPC rate: automatic discovery by default,
selector `0x06` explicitly selects 750000, or use `--session-baudrate` with a selected rate. Diagnostic
initialisation still starts at 10400 before the RAM handoff.
Automatic mode starts the shared RAM kernel conservatively, qualifies its UART
with a recoverable 256-byte round trip, and selects the fastest passing candidate.
This is independent of the firmware's KWP speed and uses no firmware lookup table.

K-Line supports `--rsw-boot1`, `--rsw-boot2`, `--rsw-asw`, `--rsw-cal` and
`--rsw-full`; the GUI BSW button selects both boot regions. Input is a complete
512 KiB or 1 MiB BIN matching the selected ECU. The native FL2 loader is limited
to the SIMK43 I4 layout (Boot2 at 0x88000, calibration at 0x90000 and program at
0xA0000) and requires enabled C167CS extended XRAM. Hardware identification
checks the actual chip size before erasing. Final checksum tables and seed
pointers must match the native FL2 contract; incompatible layouts are rejected
before erase, without using firmware-version identifiers.

K-Line virginization is deliberately rejected: address 0x84000 from the legacy
CAN operation is not an erase region in the bundled native RSW table. It must
be mapped and qualified before a serial implementation can claim to erase it.

## Implementation

- `flasher/rsw.py` keeps the existing CCP operations and shares region selection,
  preparation and programming between CAN and K-Line. Partial final packets use
  their actual length; a zero-length PROGRAM flushes the RSW staging buffer.
  Both transports leave trailing erased bytes unwritten, keeping the last native
  128-byte staging block. K-Line's final readback still covers the complete region.
- CAN uploads the native loader through its header-declared end plus the status
  byte, omitting only unused trailing `0xFF` padding. The complete generated asset
  remains unchanged. Both transports report when native RSW is running; CAN
  reports its bitrate in Kbps, and K-Line reports its UART baudrate.
- `flasher/rsw_kline.py` qualifies a SHA256-pinned copy of the bundled RSW asset,
  applies instruction/table relocations audited in IDA, and loads its 4269-byte
  executable/table section into 0xC400-0xD4AC. The native parser, erase, program
  and checksum implementations remain unchanged.
- K-Line programming groups up to 96 native six-byte commands into a RAM batch
  at 0xD500-0xD7FF, qualified by write/readback before erase. The bridge checks
  the entire batch CRC and command types before programming, then gives each
  command the same setup, voltage and response checks as an individual RPC.
  Failed programming batches are not replayed. CAN keeps individual CCP calls.
- `assets/rsw/kline_rpc.a66` runs at 0xE400. It replaces only CAN setup/transmit
  and the native interrupt loop with kernel RPC. CRC16/Modbus validates commands
  before native dispatch and responses before host acceptance; the CCP counter
  is checked. Write acknowledgements are never replayed automatically.
- MTOS authenticates and starts the existing UART kernel. The stock driver is
  used only for hardware identification/read-mapping checks. All erase and write
  operations then run through native RSW routines.
- The RAM image and bridge are read back before execution. Bytes outside selected
  regions in any shared erase sector are retained and restored. On an 8 Mbit
  chip this preserves the 0x80000-0x87FFF prefix when rewriting Boot2.
- Native RSW checksum validation and exact selected-payload/preserved-byte
  readback precede software reset and a diagnostic reconnect. A host-detected write failure
  leaves the kernel running; the host does not reset into incomplete firmware.
  The original RSW retains its own watchdog and flash-fault reset behaviour.

The original RSW supply-voltage sampling and threshold are retained. The bridge
qualifies the ADC result before accepting commands and checks it before writable
commands; an unqualified or low supply aborts before native dispatch.

## Recovery and qualification

After a failed write, keep ECU power on and recover with MTOS `--mtos-attach`
at the same UART rate and a complete valid BIN. The bridge returns to the
normal kernel command loop after each transaction, so kernel recovery remains
available. For a 750k session, restore a complete valid image with:

```bash
python gkflasher.py -p kline -i COM69 --mtos --mtos-attach --mtos-mode fullflash --mtos-payload valid.bin --desired-baudrate 0x06
```

Use the explicit rate in the recovery message for an interrupted RSW kernel.
Automatic attach checks the 57600 entry rate of successful automatic BSL sessions. Use BSL if a power
loss has removed the running RAM session.

The initial bench qualification uses calibration operations on the 8 Mbit
OpenGK ECU and COM69 FTDI. Boot/program/full and 4 Mbit hardware writes are not
part of that calibration-only qualification. The UART RPC path retains native
six-byte RSW programming blocks, so it is slower than MTOS bulk programming.
