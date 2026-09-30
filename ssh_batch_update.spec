# -*- mode: python ; coding: utf-8 -*-

import os
import re

from PyInstaller.utils.hooks import collect_submodules


raw_version = os.environ.get("APP_VERSION", "0.1.0").removeprefix("v")
version_match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw_version)
app_version = ".".join(part for part in version_match.groups(default="0")) if version_match else "0.1.0"

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("netmiko"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SSH Batch Update",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SSH Batch Update",
)

app = BUNDLE(
    collection,
    name="SSH Batch Update.app",
    bundle_identifier="com.sg4291.sshbatchupdate",
    info_plist={
        "CFBundleDisplayName": "SSH Batch Update",
        "CFBundleShortVersionString": app_version,
        "CFBundleVersion": app_version,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    },
)
