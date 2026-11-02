# -*- mode: python ; coding: utf-8 -*-
"""WeReadDownloader WebView shell: onedir windowed exe.

The renderer (weread/webui) is data inside the bundle; Python owns the worker
service, downloads, queue, filesystem and QR login. The heavy export engine
stays in the sibling WeReadWorker bundle, exactly as with the Tk build.
"""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(SPECPATH)
SRC = os.path.join(ROOT, "weread")
WEBUI = os.path.join(SRC, "webui")
ICON = os.path.join(ROOT, "assets", "weread-app.ico")
if not os.path.isfile(ICON):
    raise SystemExit("icon not found: " + ICON)

datas = [(WEBUI, os.path.join("weread", "webui"))]
binaries = []
hiddenimports = [
    "weread_login", "weread_core", "version",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader", "pythonnet",
]
for package in ("webview", "qrcode", "requests", "clr_loader", "pythonnet"):
    tmp = collect_all(package)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

a = Analysis(
    [os.path.join(SRC, "web_app.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "lupa", "lupa.luajit21", "lxml", "lxml.etree", "lxml.html",
        "weread_desktop", "weread_worker", "customtkinter", "tkinter",
        "_tkinter", "PIL.ImageTk", "numpy", "scipy", "pandas", "psutil",
        "cryptography", "nacl", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "IPython", "pytest", "setuptools", "pip", "sqlite3",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeReadDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=ICON,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WeReadDesktop",
)
