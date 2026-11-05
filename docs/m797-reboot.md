# M7.9.7 automatic reboot

Applications using the compatible internal-ROM `32 05` interface can
automatically reboot after programming using a temporary RAM extension.
TBE4I30 / ST3IS00E and GKE2I60 / QG6IS00E have passed hardware flash/reboot tests.
Both CLI and GUI use this through `m797_flash`; no extra command is required.

The ROM's original service table has no ECUReset handler. StopCommunication
acknowledges a communication shutdown but does not restart the application.
The solution installs a reset handler before entering programming, rather
than trying additional unsupported reset requests after flashing.

## Sequence

1. Read the live dispatch pointers at `E228:E22F`, resolve the handler and SID
   tables, and check the ROM handler addresses and SID mapping. Read and
   fingerprint only the table-selected 126-byte reset handler. No program bank
   or application entry-pointer read is needed. The source BIN is not used to
   identify the firmware currently executing.
2. Enter engineering session at 10400 baud. Read the scratch RAM twice;
   require identical, zero-filled contents. Upload and read back the monitor.
   Upload and pre-activation rollback share the same verified RAM writer.
3. Temporarily replace the application dispatch pointer so TesterPresent
   invokes RAM-resident serial/interrupt shutdown routines and the ROM kernel
   entry with a RAM process descriptor.
4. Initialize the normal ROM flash driver and scheduler, with a cloned ROM
   dispatch table containing the additional reset handler. Confirm ROM ID
   `32 05`, then use the existing programming session, security and flash
   routines at the selected speed.
5. After every requested region passes CRC verification, request `11 01`.
   The relocated application handler acknowledges `51` and schedules the
   ROM's deferred reset path. Switch the host to 10400 baud and confirm
   application identification before reporting a successful reboot.

Calibration-only writes must return the original program ID. Full writes may
install a different program ID; confirmation requires application software and
calibration responses and rejects the ROM `32 05` identifier.

No persistent firmware patch is written. The extension runs from external
RAM and calls ROM while flash is erased or programmed. Reset discards it.
A flash/verification error never triggers the automatic reset. If reset or
application recovery is unconfirmed, the completed flash is reported as
verified and the user is told to power-cycle the ECU.

Unrecognized code layouts or unsupported RAM parsers retain the existing programming
and manual power-cycle process. Reboot preparation failures on a matching layout abort before erase;
they do not fall through to flashing with uncertain RAM state. Recovery from
blank flash does not install this application-dependent extension.

## Traced memory layout

`flasher/m797_reboot.py` builds a 1280-byte extension at `0x382800–0x382CFF`:

| Offset | Contents |
| --- | --- |
| `0x000` | ROM initializer list with replacement dispatch initializer |
| `0x040` | Fixed, position-independent 34-byte serial-shutdown routine |
| `0x080` | Process descriptor based on ROM `0x0668` |
| `0x100` | Stock dispatch initialization followed by RAM table selection |
| `0x180` | Application shutdown and full ROM kernel entry `0x0700` |
| `0x200` | Original 15 ROM handlers plus the RAM ECUReset handler |
| `0x240` | SID index table with `0x11` mapped to the additional handler |
| `0x300` | Application handler table with temporary activation entry |
| `0x39C` | Fixed, position-independent 226-byte interrupt-shutdown routine |
| `0x480` | Relocated, fingerprinted 126-byte application reset handler |

The kernel configures DPP2 (SFR `0xFE04`) to page `0xE0`. The initializer's
near pointer is therefore `0xA800`. C166 paged data pointers use a 14-bit
offset plus page; code pointers in handler tables use linear addresses.
Confusing these representations prevents the kernel from starting.

The reset handler uses relative branches and ROM calls, so it needs no
relocation fixups. It schedules the existing deferred action through
`E1E6/E1F5`. ROM `0x2ECE` processes SID `0x11` and clears `E2C4`; the monitor
task at `0x7EBC` subsequently reaches the CPU's SRST instruction at `0x7EC8`.

## Compatibility checks

Calibration strings and whole-program hashes are not an allowlist. Application
addresses vary between builds. Production discovers the live table and verifies:

- The ROM-backed application table entries and ECUReset/TesterPresent mapping.
- The reset handler selected by the live table, 126 bytes, SHA-256
  `0aabb26f60227299626b3b98dade68c4af62c69c67d27b1b256c9bfedd8cea57`.
- Stable zeroed scratch RAM and a ROM `32 05` response after handover.

The invariant shutdown routines are included in the monitor, rather than found
by scanning the ECU's program banks. Their bytes match all seven available
firmware variants; the interrupt routine's SHA-256 is
`6917bf1edb80c3ca636877ccc1b2c01cd22033317eb9134a6e80de8a69b1843f`.
They occupy previously unused space, so the RAM allocation remains 1280 bytes.

Reboot preparation reads 156 bytes of handlers, 128 bytes of SID mapping and
126 bytes of reset code: **410 bytes of flash instead of 64–128 KiB of bank
data plus tables**. Adjacent tables share a single read operation. The 8-byte
RAM pointer read, two scratch snapshots and upload readback remain unchanged.
The patchless reader also performs small access probes. Calibration-only
flashing separately reads the 512-byte checksum table; that check is unchanged.

Offline discovery matches ADE1I52, GKE2I60, GKE2I61, LCR0N53, SAE4IS18,
TBE4I30 and the unidentified Accent image. This is evidence for their code
layouts, not hardware validation of each ECU. The current installer also
requires the native dynamic-LID RAM parser; older direct-RAM read architectures
remain on the stock programming path. Different ROM interfaces need tracing
before support can be claimed.

Research capture identities:

- Internal ROM, 32768 bytes:
  `1de3ee76eac77a34b4485dd470c402465ae1dd98a72b9494308d08ba33def2e5`
- Original full flash, 524288 bytes:
  `f26019dd80042811806abe82c176f12cd22bd94005d7d4c69bd26ef58843090f`

The production code does not require these capture files. It reconstructs
the small known ROM tables and copies the reset handler from the verified
running application. No per-calibration reboot profile or local dump file is required.

## Hardware validation

On the OpenPort-connected bench ECU, a calibration erase/write passed both
CRC verification ranges, then rebooted automatically. Application IDs
`TBE4I30` and `ST3IS00E` returned at 10400 baud. Reading all 65536 calibration
bytes back matched the pre-flash backup exactly. This validates an actual
programming cycle, beyond a reset-only probe.

The production backend also completed a full boot/program/calibration flash
at 108800 baud, rebooted, and returned the expected application IDs. A full
524288-byte readback matched the original capture exactly (SHA-256
`f26019dd80042811806abe82c176f12cd22bd94005d7d4c69bd26ef58843090f`).

Calibration flashing through the production backend at 57600 baud also
completed, rebooted and passed all 65536 readback bytes (SHA-256
`81b4f32aece9726252285dd18f9920a4413fe3e3e6b1cf76da74d9152c79b352`).
The GUI uses this same backend and reports confirmed reboot separately from
the fallback power-cycle outcome.

Hardware-free tests cover firmware rejection, occupied RAM, restoration
after upload failure, uncertain dispatch activation, pointer layouts,
application confirmation and reset ordering after verification.

On 2026-11-04, GKE2I60 / QG6IS00E passed programming-mode entry, security access
and automatic reset without erase. A production calibration rewrite at 108800
baud then passed CRC32 `BCC2B8EC` and `F3801F67`, automatically rebooted to the
application at 10400 baud, and matched all 65536 pre-flash calibration bytes.
Its complete internal ROM dump matches the TBE4I30 ROM SHA-256 above.

The targeted-read monitor was subsequently tested on GKE2I61 / QG6IS00E.
Reboot preparation read 284 bytes at `0x819E16` and 126 bytes at `0x837708`.
A production calibration rewrite at 108800 baud passed CRC32 `B1B01C7D` and
`05D83CBC`, rebooted without a power cycle, and matched all 65536 original
calibration bytes. Readback SHA-256:
`e9f298c61e27af9f9127a4139fc042b06b071f85c252910c52e5ae02a48d4d9c`.

## Other firmware

An experiment without firmware restrictions completed flash verification on
another firmware, but reset returned:

```text
[!] Flash verified; automatic reboot not confirmed: 0x11 - Service Not Supported. Power-cycle the ECU.
```

The tested firmware identifiers were not recorded. Simply removing the old
allowlist left TBE4I30 application addresses in place. The implementation now
resolves and verifies the live reset handler and supplies the invariant shutdown
code in RAM, rather than assuming another application's addresses.
