# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('crcmod')
hiddenimports += collect_submodules('pyftdi')
hiddenimports += collect_submodules('scapy')
hiddenimports += collect_submodules('pyserial')
hiddenimports += collect_submodules('pyusb')
hiddenimports += collect_submodules('about-time')
hiddenimports += collect_submodules('grapheme')
hiddenimports += collect_submodules('alive_progress')

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('flasher', '.\flasher'),('assets', '.\assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    icon=['assets\Siemens_T_Logo.png'],
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
