"""設定檔：備份任務、效能設定與一般設定，存成 JSON。"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "FolderSyncBackup"
APP_DISPLAY_NAME = "資料夾同步備份"
# 可用環境變數 FSB_DATA_DIR 指定其他資料夾（測試用）
APP_DIR = Path(os.environ.get("FSB_DATA_DIR") or Path.home() / "Library" / "Application Support" / APP_NAME)
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.db"

# 備份位置內存放「被覆蓋 / 被刪除檔案」的資料夾名稱
VERSIONS_DIRNAME = "_備份歷史版本"

DEFAULT_EXCLUDES = [
    ".DS_Store",
    "._*",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    ".TemporaryItems",
    ".DocumentRevisions-V100",
    "*.tmp",
    "*.part",
    "*.crdownload",
    "~$*",
    ".~lock.*",
    "*.syncpart",
]

MODE_MIRROR = "mirror"          # 鏡像：備份與來源完全一致（來源刪除，備份也刪除）
MODE_ACCUMULATE = "accumulate"  # 累積：只新增與更新，永不刪除備份中的檔案

PRIORITY_LABELS = {
    "background": "背景（最低，優先使用省電核心）",
    "utility": "公用程式（中等）",
    "normal": "一般",
}

PROFILES = {
    "low": dict(max_speed_mb=10, cpu_limit=15, priority="background",
                file_gap_ms=30, debounce_sec=10, full_scan_minutes=180),
    "balanced": dict(max_speed_mb=40, cpu_limit=40, priority="utility",
                     file_gap_ms=5, debounce_sec=5, full_scan_minutes=60),
    "full": dict(max_speed_mb=0, cpu_limit=100, priority="normal",
                 file_gap_ms=0, debounce_sec=2, full_scan_minutes=30),
}
PROFILE_LABELS = {"low": "低調", "balanced": "平衡", "full": "全速", "custom": "自訂"}
PROFILE_DESCRIPTIONS = {
    "low": "限速 10 MB/s、CPU 15%、背景優先權。幾乎感覺不到在備份，適合平常工作時使用。",
    "balanced": "限速 40 MB/s、CPU 40%。速度與系統負擔兼顧。",
    "full": "不限速、不限 CPU。最快完成備份，適合不使用電腦時。",
    "custom": "自行調整下方各項數值。",
}


def _from_dict(cls, data: dict):
    """忽略未知欄位，缺少的欄位用預設值。"""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


@dataclass
class Task:
    name: str = ""
    source: str = ""
    dest: str = ""
    mode: str = MODE_MIRROR
    keep_versions: bool = True
    version_days: int = 30          # 0 = 永久保留
    excludes: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    realtime: bool = True
    enabled: bool = True
    mass_delete_guard: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class Performance:
    profile: str = "low"
    max_speed_mb: float = 10        # 0 = 不限制
    cpu_limit: int = 15             # 百分比，100 = 不限制
    priority: str = "background"
    file_gap_ms: int = 30
    debounce_sec: int = 10
    full_scan_minutes: int = 180    # 0 = 不定期檢查
    pause_on_battery: bool = True
    pause_when_busy: bool = False
    busy_percent: int = 100         # 系統負載 / CPU 核心數 超過此百分比即暫停

    def apply_profile(self, name: str):
        self.profile = name
        for k, v in PROFILES.get(name, {}).items():
            setattr(self, k, v)

    def matching_profile(self) -> str:
        for name, values in PROFILES.items():
            if all(getattr(self, k) == v for k, v in values.items()):
                return name
        return "custom"


@dataclass
class General:
    autostart: bool = False
    close_to_tray: bool = True
    hide_dock_in_background: bool = True
    notify_errors: bool = True
    notify_full_sync: bool = False
    notify_drive_missing: bool = True


class Config:
    def __init__(self, tasks=None, perf=None, general=None, path: Path = CONFIG_PATH):
        self.tasks: list[Task] = tasks or []
        self.perf: Performance = perf or Performance()
        self.general: General = general or General()
        self.path = Path(path)
        self.lock = threading.RLock()

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        path = Path(path)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return cls(path=path)
        return cls(
            tasks=[_from_dict(Task, t) for t in data.get("tasks", [])],
            perf=_from_dict(Performance, data.get("performance", {})),
            general=_from_dict(General, data.get("general", {})),
            path=path,
        )

    def save(self):
        with self.lock:
            data = {
                "tasks": [asdict(t) for t in self.tasks],
                "performance": asdict(self.perf),
                "general": asdict(self.general),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.path)

    def get_task(self, task_id: str) -> Task | None:
        with self.lock:
            return next((t for t in self.tasks if t.id == task_id), None)

    def upsert_task(self, task: Task):
        with self.lock:
            for i, t in enumerate(self.tasks):
                if t.id == task.id:
                    self.tasks[i] = task
                    break
            else:
                self.tasks.append(task)
            self.save()

    def remove_task(self, task_id: str):
        with self.lock:
            self.tasks = [t for t in self.tasks if t.id != task_id]
            self.save()
