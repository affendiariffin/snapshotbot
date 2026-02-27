# TTS_Replay.spec
# Build command: pyinstaller TTS_Replay.spec
#
# Requirements:
#   pip install pyinstaller mss Pillow pystray opencv-python-headless numpy
#
# Run from the folder containing capture.pyw:
#   pyinstaller TTS_Replay.spec
#
# Output: dist/TTS_Replay.exe  (single file, no console window)

import sys
from PyInstaller.building.build_main import Analysis, PYZ, EXE

a = Analysis(
    ['capture.pyw'],
    pathex=[],
    binaries=[],
    datas=[],           # All assets are embedded as strings in capture.pyw
    hiddenimports=[
        'mss',
        'mss.tools',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'cv2',
        'numpy',
        'tkinter',
        'tkinter.scrolledtext',
        'pkg_resources.py2_stdlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'PyQt5', 'PyQt6', 'wx'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TTS_Replay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,           # Compress if UPX is available (reduces size)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # No terminal window — runs silently in tray
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # Uncomment and add an .ico file to set a custom icon
)
