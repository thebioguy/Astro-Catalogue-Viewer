# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None
project_root = Path.cwd()

pyside6_datas, pyside6_binaries, pyside6_hidden = collect_all("PySide6")
tiff_datas, tiff_binaries, tiff_hidden = collect_all("tifffile")

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=pyside6_binaries + tiff_binaries,
    datas=[(str(project_root / "data"), "data")] + pyside6_datas + tiff_datas,
    hiddenimports=pyside6_hidden + tiff_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Astro Catalogue Viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "build_assets" / "ACV.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Astro Catalogue Viewer",
)
