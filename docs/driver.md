# OpenGK flash drivers

`assets/kernel/` contains the AMD flash-driver assembly sources used by the
SIMK4x [kernel](kernel.md) for BSL and MTOS operations.

## Layout

| Source | Purpose |
|---|---|
| `flash_driver.inc` | Shared AMD program, sector erase, identification and status handling |
| `simk4x_driver_i4_a29fx00bx.a66` | Crossed-data-line I4 driver selection |
| `simk4x_driver_v6_a29fx00bx.a66` | Linear-data-line V6 driver selection |

The driver helpers start at 0xF600; their register-call entry stays at 0xF640.
Board-specific command values and status-bit checks preserve the existing I4/V6
driver behavior. Existing watchdog adaptation is unchanged.

The I4 erase-ready check uses CPU AD6, which maps to flash DQ7 on the crossed
data bus. This is required for ST Labs M29Fx flash chips in older SIMK ECU's.

## Build

The drivers use the shared [build process](kernel.md#build-and-check), including
the pinned assembler and RAM-layout validation. Run `python tools/build.py` or
`make build` to generate:

- `build/out/assets/simk4x_driver_i4_a29fx00bx.bin`
- `build/out/assets/simk4x_driver_v6_a29fx00bx.bin`

Listings and symbol maps are written under `build/out/listings/`, and hashes are
included in `build/out/SHA256SUMS`. Release packages include the generated drivers;
the binaries are not committed to the source tree.
