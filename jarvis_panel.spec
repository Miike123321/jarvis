# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["cyber_panel.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtNetwork",
        "telethon",
        "google.auth.transport.requests",
        "google_auth_oauthlib.flow",
        "googleapiclient.discovery",
        "psutil",
        "dotenv",
        "requests",
        "bs4",
        "lxml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="JarvisPanel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
