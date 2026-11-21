"""Assemble OpenGK loader assets and write binaries/listings under build/out.

Like opengk-custom-firmware, this uses the pinned openc166 assembler with source
files as the authority. Packaging consumes generated binaries, never source-tree
copies. --check compares an existing build without changing it.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build' / 'out'
sys.path.insert(0, str(ROOT))

from flasher.mtos_asm import _assemble
from flasher.rsw_kline import RSW_SHA256


def build():
    assemblies = {}

    def assemble(name, directory, address, **values):
        result = _assemble(name, source_directory=directory, **values)
        image = result.image()
        if image.base != address:
            raise ValueError('{} starts at 0x{:X}, expected 0x{:X}'.format(name, image.base, address))
        assemblies[name] = result
        return image.data

    kernel = assemble('simk4x_kernel', 'assets/kernel', 0xFA60)
    if not kernel or len(kernel) % 2 or 0xFA60 + len(kernel) > 0xFC00:
        raise ValueError('UART kernel must be word-aligned and fit below the transfer buffer at 0xFC00')
    bootstrap = assemble('simk4x_bootstrap', 'assets/kernel', 0xFA40, KERNEL_SIZE=len(kernel))
    if len(bootstrap) != 32:
        raise ValueError('The C167 boot ROM requires a 32-byte bootstrap loader')
    artifacts = {'simk4x_bootstrap': bootstrap, 'simk4x_kernel': kernel}
    for variant in ('i4', 'v6'):
        name = 'simk4x_driver_{}_a29fx00bx'.format(variant)
        driver = assemble(name, 'assets/kernel', 0xF600)
        if assemblies[name].symbols['FlashEntry'].value != 0xF640 or 0xF600 + len(driver) > 0xF9F0:
            raise ValueError('{} overlaps kernel RAM or changes the driver entry point'.format(name))
        artifacts[name] = driver

    rsw = assemble('simk43_rsw_fl2', 'assets/rsw', 0x82000)
    if len(rsw) != 0x5FFF or hashlib.sha256(rsw).hexdigest() != RSW_SHA256:
        raise ValueError('RSW changed: review its header and audited K-Line relocations before rebuilding')
    artifacts['simk43_rsw_fl2'] = rsw

    can_io = assemble('can_link', 'assets/mtos', 0xE200)
    symbols = assemblies['can_link'].symbols
    startup = assemble('can_stage2', 'assets/mtos', 0xE000, CAN_INIT=symbols['CanInit'].value)
    can_kernel = assemble('simk4x_kernel_can', 'assets/kernel', 0xE000 + len(startup),
                          KERNEL_BASE=0xE000 + len(startup),
                          CAN_TX=symbols['CanTx'].value, CAN_RX=symbols['CanRx'].value)
    stage2 = startup + can_kernel
    if len(stage2) > 0x200 or len(can_io) > 0x400:
        raise ValueError('MTOS CAN kernel exceeds its RAM allocation')
    artifacts['mtos_can_stage2'] = stage2 + bytes(0x200 - len(stage2)) + can_io
    return artifacts, assemblies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='verify an existing build without rewriting files')
    action.add_argument('--clean', action='store_true', help='remove this repository\'s build/out directory')
    args = parser.parse_args()
    if args.clean:
        # Keep cleanup confined to the literal build output directory, including on Windows.
        if OUTPUT.exists():
            if OUTPUT.resolve() != ROOT.resolve() / 'build' / 'out':
                parser.error('Refusing to clean an output directory redirected outside the repository')
            shutil.rmtree(OUTPUT)
        return

    artifacts, assemblies = build()
    checksums = []
    mismatches = []
    for name, data in artifacts.items():
        relative = Path('assets') / (name + '.bin')
        path = OUTPUT / relative
        if args.check:
            matches = path.is_file() and path.read_bytes() == data
            if not matches:
                mismatches.append(relative.as_posix())
            state = 'matches' if matches else 'DIFFERS'
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            state = 'built'
        print('{}: {} bytes, {}'.format(relative.name, len(data), state))
        checksums.append('{}  {}'.format(hashlib.sha256(data).hexdigest(), relative.as_posix()))
    if mismatches:
        parser.exit(1, 'Generated assets differ or are missing; run python tools/build.py: {}\n'.format(', '.join(mismatches)))
    if not args.check:
        listing_dir = OUTPUT / 'listings'
        listing_dir.mkdir(parents=True, exist_ok=True)
        for name, result in assemblies.items():
            (listing_dir / (name + '.lst')).write_text(result.listing, encoding='utf-8', newline='\n')
            symbols = sorted((s.value, s.name) for s in result.all_symbols if s.kind in ('label', 'proc'))
            (listing_dir / (name + '.map')).write_text(
                ''.join('0x{:06X} {}\n'.format(address, symbol) for address, symbol in symbols), encoding='utf-8', newline='\n')
        (OUTPUT / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n', encoding='ascii', newline='\n')


if __name__ == '__main__':
    main()
