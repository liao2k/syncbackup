"""介面共用的格式化工具。"""
from __future__ import annotations

import time
from datetime import datetime


def fmt_size(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_speed(bps: float) -> str:
    return f"{fmt_size(bps)}/s" if bps > 0 else "—"


def fmt_ago(ts: float | None) -> str:
    if not ts:
        return "尚未同步"
    d = time.time() - ts
    if d < 60:
        return "剛剛"
    if d < 3600:
        return f"{int(d // 60)} 分鐘前"
    if d < 86400:
        return f"{int(d // 3600)} 小時前"
    return datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")


def fmt_time(ts: float | None, with_date: bool = True) -> str:
    if not ts:
        return ""
    fmt = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return datetime.fromtimestamp(ts).strftime(fmt)


def fmt_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return ""
    if sec < 60:
        return f"{sec:.1f} 秒"
    if sec < 3600:
        return f"{int(sec // 60)} 分 {int(sec % 60)} 秒"
    return f"{int(sec // 3600)} 小時 {int(sec % 3600 // 60)} 分"


ACTION_COLORS = {
    "new": "#1f9d55",
    "update": "#2a72e8",
    "delete": "#d9822b",
    "rmdir": "#d9822b",
    "error": "#d64545",
    "info": "#7a7a7a",
}

STATUS_DISPLAY = {
    "idle": ("✓ 已同步", "#1f9d55"),
    "queued": ("⋯ 等待同步", "#7a7a7a"),
    "syncing": ("⟳ 同步中", "#2a72e8"),
    "waiting_drive": ("⏏ 等待外接硬碟", "#d9822b"),
    "error": ("⚠ 有錯誤", "#d64545"),
    "blocked": ("⚠ 需要確認", "#d64545"),
    "disabled": ("已停用", "#9a9a9a"),
}
