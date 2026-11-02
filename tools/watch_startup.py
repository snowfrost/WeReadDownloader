"""Launch the shell and dump every window owned by its process tree."""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

EXE = r"D:\Program Files\WeReadDownloader\WeReadDesktop\WeReadDesktop.exe"
LOG = Path(os.environ["LOCALAPPDATA"]) / "WeReadDesktop" / "logs" / "boot.log"
user32 = ctypes.windll.user32
CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def pids_named(name):
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name],
                         capture_output=True, text=True, errors="replace").stdout
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def windows_of(pids):
    rows = []

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append((pid.value, title.value, cls.value,
                         bool(user32.IsWindowVisible(hwnd)),
                         bool(user32.IsHungAppWindow(hwnd)),
                         rect.right - rect.left, rect.bottom - rect.top))
        return True

    user32.EnumWindows(CB(cb), 0)
    return rows


before = LOG.stat().st_size if LOG.exists() else 0
proc = subprocess.Popen([EXE], cwd=str(Path(EXE).parent),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("launcher pid", proc.pid, flush=True)

for wait in (6, 6, 8):
    time.sleep(wait)
    pids = set(pids_named("WeReadDesktop.exe"))
    print("--- t+%ds pids=%s launcher_alive=%s" % (wait, sorted(pids), proc.poll() is None), flush=True)
    for row in windows_of(pids):
        print("   win pid=%d title=%r class=%r visible=%s hung=%s size=%dx%d" % row, flush=True)

if LOG.exists():
    with LOG.open("rb") as handle:
        handle.seek(before)
        new = handle.read().decode("utf-8", "replace").strip().splitlines()
    print("--- new log lines:", flush=True)
    for line in new:
        print("   ", line, flush=True)

for pid in pids_named("WeReadDesktop.exe"):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
