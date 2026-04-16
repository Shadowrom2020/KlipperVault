# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH)
windows_icon = str(project_root / "assets" / "klippervault.ico") if sys.platform == "win32" else None

nicegui_datas, nicegui_binaries, nicegui_hiddenimports = collect_all("nicegui")
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")
keyring_datas, keyring_binaries, keyring_hiddenimports = collect_all("keyring")
paramiko_datas, paramiko_binaries, paramiko_hiddenimports = collect_all("paramiko")

datas = [
    (str(project_root / "VERSION"), "."),
    (str(project_root / "assets" / "favicon.svg"), "assets"),
    (str(project_root / "src" / "locales"), "locales"),
] + nicegui_datas + webview_datas + keyring_datas + paramiko_datas

binaries = nicegui_binaries + webview_binaries + keyring_binaries + paramiko_binaries
hiddenimports = nicegui_hiddenimports + webview_hiddenimports + keyring_hiddenimports + paramiko_hiddenimports


a = Analysis(
    [str(project_root / "klipper_vault_gui.py")],
    pathex=[str(project_root), str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name="KlipperVault",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=windows_icon,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="KlipperVault.app",
        icon=None,
        bundle_identifier="com.klippervault.gui",
        info_plist={
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": "True",
        },
    )
else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="dist",
    )