"""macOS 系統相關功能：執行緒優先權、電源狀態、通知、外接硬碟偵測、登入自動啟動、Dock 圖示。"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 執行緒優先權

_QOS = {"background": 0x09, "utility": 0x11, "normal": 0x15}
_IOPOL = {"background": 3, "utility": 4, "normal": 0}   # THROTTLE / UTILITY / DEFAULT
_IOPOL_TYPE_DISK = 0
_IOPOL_SCOPE_THREAD = 1

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except OSError:
    _libc = None


def set_thread_priority(level: str) -> bool:
    """設定「目前執行緒」的 QoS 與磁碟 I/O 優先權，只影響背景同步執行緒，不影響介面。"""
    if _libc is None or sys.platform != "darwin":
        return False
    ok = True
    try:
        if _libc.pthread_set_qos_class_self_np(_QOS.get(level, 0x15), 0) != 0:
            ok = False
    except AttributeError:
        ok = False
    try:
        _libc.setiopolicy_np(_IOPOL_TYPE_DISK, _IOPOL_SCOPE_THREAD, _IOPOL.get(level, 0))
    except AttributeError:
        ok = False
    return ok


# ---------------------------------------------------------------- 電源與負載

def on_battery() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in out


def load_percent() -> float:
    """1 分鐘平均負載佔 CPU 核心數的百分比。"""
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1) * 100
    except OSError:
        return 0.0


# ---------------------------------------------------------------- 通知

def notify(title: str, message: str):
    script = ["-e", "on run argv",
              "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]
    try:
        subprocess.Popen(["osascript", *script, title, message],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---------------------------------------------------------------- 外接硬碟

def volume_of(path: str) -> str | None:
    """回傳路徑所在的 /Volumes/xxx 掛載點；不在外接磁碟上則回傳 None。"""
    parts = Path(os.path.abspath(path)).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return os.path.join("/", parts[1], parts[2])
    return None


def dest_available(dest: str) -> bool:
    """備份位置是否可寫入（外接硬碟必須已掛載，避免誤寫到內建硬碟的 /Volumes 殘留資料夾）。"""
    if not dest:
        return False
    vol = volume_of(dest)
    if vol and not os.path.ismount(vol):
        return False
    p = os.path.abspath(dest)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
    return os.path.isdir(p) and os.access(p, os.W_OK)


def free_space(path: str) -> int | None:
    vol = volume_of(path) or path
    try:
        return shutil.disk_usage(vol).free
    except OSError:
        return None


def external_volumes() -> list[str]:
    result = []
    try:
        for name in sorted(os.listdir("/Volumes")):
            p = os.path.join("/Volumes", name)
            if os.path.ismount(p) and os.path.realpath(p) != "/":
                result.append(p)
    except OSError:
        pass
    return result


def reveal(path: str):
    target = path
    while target and not os.path.exists(target):
        target = os.path.dirname(target)
    if target:
        subprocess.Popen(["open", target])


# ---------------------------------------------------------------- 登入自動啟動

LAUNCH_AGENT_LABEL = "com.local.foldersyncbackup"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def set_autostart(enabled: bool, script_path: str):
    if not enabled:
        try:
            LAUNCH_AGENT_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [sys.executable, os.path.abspath(script_path), "--hidden"],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    with open(LAUNCH_AGENT_PATH, "wb") as f:
        plistlib.dump(plist, f)


def autostart_enabled() -> bool:
    return LAUNCH_AGENT_PATH.exists()


# ---------------------------------------------------------------- Dock 圖示

def _objc():
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    lib.objc_getClass.restype = ctypes.c_void_p
    lib.objc_getClass.argtypes = [ctypes.c_char_p]
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    return lib


def _send(lib, obj, selector: bytes, *args, argtypes=(), restype=ctypes.c_void_p):
    addr = ctypes.cast(lib.objc_msgSend, ctypes.c_void_p).value
    fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(addr)
    return fn(obj, lib.sel_registerName(selector), *args)


def set_dock_visible(visible: bool):
    """切換 Dock 圖示（Regular = 0 / Accessory = 1）。"""
    if sys.platform != "darwin":
        return
    try:
        lib = _objc()
        app = _send(lib, lib.objc_getClass(b"NSApplication"), b"sharedApplication")
        if not app:
            return
        _send(lib, app, b"setActivationPolicy:", 0 if visible else 1,
              argtypes=(ctypes.c_long,), restype=ctypes.c_bool)
        if visible:
            _send(lib, app, b"unhide:", None, argtypes=(ctypes.c_void_p,), restype=None)
            # macOS 14 起 activateIgnoringOtherApps: 已停用，改用 activate
            if _send(lib, app, b"respondsToSelector:", lib.sel_registerName(b"activate"),
                     argtypes=(ctypes.c_void_p,), restype=ctypes.c_bool):
                _send(lib, app, b"activate", restype=None)
            else:
                _send(lib, app, b"activateIgnoringOtherApps:", True,
                      argtypes=(ctypes.c_bool,), restype=None)
    except Exception:
        pass
