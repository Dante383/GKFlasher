# OpenGK kernel and bootstrap loader

`assets/kernel/` contains the assembly sources for the OpenGK SIMK4x command
kernel and bootstrap loader. The [flash-driver documentation](driver.md) covers
the AMD drivers in the same directory. `assets/rsw/simk43_rsw_fl2.a66`
contains the native recovery loader. All binaries are generated under `build/out`;
none is maintained as a committed binary. Release packages include those generated
assets for BSL, K-Line MTOS, CAN MTOS and RSW.

## Layout

| Source | Purpose |
|---|---|
| `protocol.inc` | Command bytes and acknowledgements shared by the bootstrap/kernel |
| `kernel.inc` | Shared command dispatcher, memory access and register-call implementation |
| `simk4x_kernel.a66` | UART kernel at its BSL address, 0xFA60 |
| `simk4x_kernel_can.a66` | CAN kernel with addresses supplied by the build's stage-2 builder |
| `simk4x_bootstrap.a66` | 32-byte boot-ROM loader at 0xFA40 |

`assets/mtos/kernel_environment.inc` provides the shared stack, register-bank and
watchdog setup for UART and CAN handoffs. `can_stage2.a66` also calls the CAN
initializer, then falls through into the assembled CAN kernel. Its transmit and
receive entry points come from the assembler's symbols in `can_link.a66`.

## Build and check

After installing GKFlasher's requirements:

```sh
make build  # runs python3 tools/build.py
make check  # builds, then compares the generated binaries with a fresh assembly
```

On Windows, `python tools/build.py` works without make. The assembler is pinned
to openc166 0.2.5, matching opengk-custom-firmware. `python tools/build.py --check`
verifies an existing build without writing; `make clean` removes only build/out.

Outputs are `build/out/assets/*.bin`, assembly listings/symbol maps under
`build/out/listings/`, and `build/out/SHA256SUMS`. Listings and maps support source
review and openc166 simulation/debugging. The release workflow builds before
packaging and keeps build outputs as artifacts. Bootstrap/kernel, driver and CAN
RAM-layout checks protect the existing entry points and reserved buffers.

The MTOS CAN kernel/initialization/helper image is also built ahead of time as
`mtos_can_stage2.bin`; Python loads it without binary patching or runtime kernel
assembly. MTOS payloads requiring ECU-specific values still assemble at runtime.
