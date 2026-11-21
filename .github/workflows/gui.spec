# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('crcmod')
#hiddenimports += collect_submodules('pyftdi')
hiddenimports += collect_submodules('pyserial')
hiddenimports += collect_submodules('pyusb')
hiddenimports += collect_submodules('plyer')
#hiddenimports += collect_submodules('about-time')
#hiddenimports += collect_submodules('grapheme')
#hiddenimports += collect_submodules('alive_progress')
hiddenimports += collect_submodules('can')

# Only payloads assembled at runtime belong in the Windows package.
mtos_root = Path(SPECPATH).parents[1] / 'assets' / 'mtos'
mtos_sources = [(str(mtos_root / name), 'assets/mtos') for name in (
    'baud_probe.a66', 'bsl_shim.a66', 'calibration_read.a66', 'can_loader.a66',
    'driver_patch.a66', 'kline_hello.a66', 'loader.a66', 'memory_dump.a66',
    'fast_link.inc', 'kernel_environment.inc',
)]

a = Analysis(
    ['..\\..\\gui.py'],
    pathex=[],
    # openc166 (assembles the MTOS payloads) loads openc166.dll from its package dir
    binaries=collect_dynamic_libs('openc166'),
    datas=[('..\\..\\flasher', 'flasher'),
           ('..\\..\\assets\\Siemens_T_Logo.ico', 'assets'),
           ('..\\..\\assets\\checkmark.svg', 'assets'),
           ('..\\..\\assets\\lzss_codec.a66', 'assets'),
           ('..\\..\\assets\\rsw\\kline_rpc.a66', 'assets/rsw'),
           ('..\\..\\build\\out\\assets', 'assets'),
           ('..\\..\\gkflasher.yml', '.')] + mtos_sources,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    #optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['..\\..\\assets\\Siemens_T_Logo.ico'],
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui',
)
