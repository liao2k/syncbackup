"""同步引擎：掃描來源與備份、計算差異、執行複製 / 更新 / 刪除，並保留歷史版本。

流程分成兩步：
  plan_*()  只讀取，產生 Plan（可用於「預覽變更」）
  execute() 依 Plan 實際寫入備份位置
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fnmatch
import itertools
import os
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from .config import MODE_MIRROR, VERSIONS_DIRNAME, Task

CHUNK_SIZE = 1024 * 1024
MTIME_TOLERANCE = 2.0          # FAT/exFAT 時間精度較低，2 秒內視為相同
MASS_DELETE_MIN = 50
MASS_DELETE_RATIO = 0.3

ACTION_LABELS = {
    "new": "新增",
    "update": "更新",
    "delete": "刪除",
    "rmdir": "刪除資料夾",
    "mkdir": "建立資料夾",
    "error": "錯誤",
    "info": "訊息",
}
_PHASE = {"delete": 0, "rmdir": 1, "mkdir": 2, "new": 3, "update": 3}

_tmp_counter = itertools.count()


class Cancelled(Exception):
    """同步被中斷（暫停後停止、任務被修改或程式結束）。"""


class SyncError(Exception):
    def __init__(self, message: str, drive_lost: bool = False):
        super().__init__(message)
        self.drive_lost = drive_lost


class Hooks:
    """引擎回呼介面；背景服務用它來實作暫停、限速、進度與紀錄。"""

    def checkpoint(self): ...
    def scan_tick(self): ...
    def on_bytes(self, n: int): ...
    def on_plan(self, plan: "Plan"): ...
    def on_action(self, kind: str, rel: str, size: int = 0, error: str | None = None): ...
    def after_file(self): ...


@dataclass
class Entry:
    path: str          # 實際在磁碟上的相對路徑
    size: int = 0
    mtime: float = 0.0


@dataclass
class Action:
    kind: str
    rel: str
    size: int = 0


@dataclass
class Plan:
    actions: list = field(default_factory=list)
    src_files: int = 0
    src_bytes: int = 0
    dest_files: int = 0
    full: bool = False

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)

    @property
    def copy_bytes(self) -> int:
        return sum(a.size for a in self.actions if a.kind in ("new", "update"))

    @property
    def file_actions(self) -> int:
        return sum(1 for a in self.actions if a.kind in ("new", "update", "delete"))


@dataclass
class RunResult:
    new: int = 0
    updated: int = 0
    deleted: int = 0
    bytes: int = 0
    errors: int = 0
    retry: set = field(default_factory=set)
    blocked: str | None = None

    @property
    def changed(self) -> int:
        return self.new + self.updated + self.deleted


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def describe_error(e: OSError) -> str:
    known = {
        errno.EACCES: "沒有權限",
        errno.EPERM: "沒有權限",
        errno.ENOSPC: "磁碟空間不足",
        errno.ENAMETOOLONG: "檔名太長",
        errno.EINVAL: "檔名含有外接硬碟不支援的字元",
        errno.EBUSY: "檔案使用中",
        errno.EIO: "讀寫錯誤（硬碟可能有問題）",
    }
    return known.get(e.errno, e.strerror or str(e))


class Excluder:
    """排除規則：含「/」的規則比對相對路徑，否則比對檔名；不分大小寫。"""

    def __init__(self, patterns):
        self.name_patterns = []
        self.path_patterns = []
        for p in patterns or []:
            p = p.strip()
            if not p or p.startswith("#"):
                continue
            if "/" in p:
                self.path_patterns.append(p.strip("/").lower())
            else:
                self.name_patterns.append(p.lower())

    def match(self, rel: str, name: str) -> bool:
        name = name.lower()
        if any(fnmatch.fnmatchcase(name, p) for p in self.name_patterns):
            return True
        if self.path_patterns:
            rel = rel.lower()
            return any(fnmatch.fnmatchcase(rel, p) for p in self.path_patterns)
        return False

    def match_any_part(self, rel: str) -> bool:
        parts = rel.split("/")
        return any(self.match("/".join(parts[: i + 1]), parts[i]) for i in range(len(parts)))


# ---------------------------------------------------------------- 複製延伸屬性（Finder 標籤等）

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _copyfile = _libc.copyfile
    _copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    _copyfile.restype = ctypes.c_int
except (OSError, AttributeError, TypeError):
    _copyfile = None
_COPYFILE_XATTR = 1 << 2


def _copy_xattrs(src: str, dst: str):
    if _copyfile is not None:
        try:
            _copyfile(os.fsencode(src), os.fsencode(dst), None, _COPYFILE_XATTR)
        except Exception:
            pass


def _needs_update(src: Entry, dst: Entry) -> bool:
    return src.size != dst.size or abs(src.mtime - dst.mtime) > MTIME_TOLERANCE


class SyncEngine:
    def __init__(self, task: Task, hooks: Hooks | None = None):
        self.task = task
        self.src_root = os.path.abspath(task.source)
        self.dst_root = os.path.abspath(task.dest)
        self.ex = Excluder(task.excludes)
        self.hooks = hooks or Hooks()
        self.stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.mirror = task.mode == MODE_MIRROR

    # ------------------------------------------------------------ 路徑

    def _src(self, rel: str) -> str:
        return os.path.join(self.src_root, rel) if rel else self.src_root

    def _dst(self, rel: str) -> str:
        return os.path.join(self.dst_root, rel) if rel else self.dst_root

    # ------------------------------------------------------------ 掃描

    def _scan(self, root: str, base_rel: str, is_dest: bool):
        files: dict[str, Entry] = {}
        dirs: dict[str, Entry] = {}
        failed: list[str] = []
        stack = [base_rel]
        n = 0
        while stack:
            rel_dir = stack.pop()
            abs_dir = os.path.join(root, rel_dir) if rel_dir else root
            try:
                it = os.scandir(abs_dir)
            except FileNotFoundError:
                continue
            except OSError as e:
                failed.append(rel_dir)
                if not is_dest:
                    self.hooks.on_action("error", rel_dir or ".", 0, f"無法讀取資料夾：{describe_error(e)}")
                continue
            with it:
                for entry in it:
                    n += 1
                    if n % 200 == 0:
                        self.hooks.scan_tick()
                    name = entry.name
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    if is_dest:
                        if not rel_dir and name == VERSIONS_DIRNAME:
                            continue
                        if name.endswith(".syncpart"):   # 上次中斷留下的暫存檔
                            try:
                                os.remove(entry.path)
                            except OSError:
                                pass
                            continue
                    if self.ex.match(rel, name):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            dirs[nfc(rel)] = Entry(rel)
                            stack.append(rel)
                        elif entry.is_file():
                            st = entry.stat()
                            files[nfc(rel)] = Entry(rel, st.st_size, st.st_mtime)
                    except OSError:
                        continue
        return files, dirs, failed

    # ------------------------------------------------------------ 規劃

    def plan_full(self) -> Plan:
        if not os.path.isdir(self.src_root):
            raise SyncError(f"找不到來源資料夾：{self.src_root}")
        plan = self._plan_subtree("")
        plan.full = True
        return plan

    def plan_paths(self, rels) -> Plan:
        """只處理有變動的路徑（即時監控用）。"""
        rels = {r.strip("/") for r in rels}
        if "" in rels:
            return self.plan_full()
        # 若上層資料夾也在清單中，子項目由上層一併處理
        collapsed = []
        for r in sorted(rels):
            if collapsed and r.startswith(collapsed[-1] + "/"):
                continue
            collapsed.append(r)

        plan = Plan()
        for rel in collapsed:
            if self.ex.match_any_part(rel) or rel.split("/")[0] == VERSIONS_DIRNAME:
                continue
            src = self._src(rel)
            if os.path.isfile(src) and not os.path.islink(src):
                self._plan_file(rel, plan)
            else:
                sub = self._plan_subtree(rel)
                plan.actions.extend(sub.actions)
                plan.src_files += sub.src_files
                plan.src_bytes += sub.src_bytes
                plan.dest_files += sub.dest_files
        return plan

    def _plan_file(self, rel: str, plan: Plan):
        try:
            st = os.stat(self._src(rel))
        except OSError:
            return
        src = Entry(rel, st.st_size, st.st_mtime)
        plan.src_files += 1
        plan.src_bytes += src.size
        dst_path = self._dst(rel)
        if os.path.isfile(dst_path):
            d = os.stat(dst_path)
            plan.dest_files += 1
            if _needs_update(src, Entry(rel, d.st_size, d.st_mtime)):
                plan.actions.append(Action("update", rel, src.size))
        else:
            plan.actions.append(Action("new", rel, src.size))

    def _plan_subtree(self, rel_root: str) -> Plan:
        src_abs = self._src(rel_root)
        dst_abs = self._dst(rel_root)
        src_is_dir = os.path.isdir(src_abs) and not os.path.islink(src_abs)

        src_files, src_dirs, src_failed = ({}, {}, [])
        if src_is_dir:
            src_files, src_dirs, src_failed = self._scan(self.src_root, rel_root, False)
        dst_files, dst_dirs = {}, {}
        if os.path.isdir(dst_abs) and not os.path.islink(dst_abs):
            dst_files, dst_dirs, _ = self._scan(self.dst_root, rel_root, True)
        elif rel_root and os.path.isfile(dst_abs):
            st = os.stat(dst_abs)
            dst_files[nfc(rel_root)] = Entry(rel_root, st.st_size, st.st_mtime)

        actions: list[Action] = []
        if rel_root and src_is_dir and not os.path.isdir(dst_abs):
            actions.append(Action("mkdir", rel_root))
        for key, e in sorted(src_dirs.items()):
            if key not in dst_dirs:
                actions.append(Action("mkdir", e.path))
        for key, e in sorted(src_files.items()):
            d = dst_files.get(key)
            if d is None:
                actions.append(Action("new", e.path, e.size))
            elif _needs_update(e, d):
                actions.append(Action("update", e.path, e.size))

        if self.mirror:
            # 讀取失敗的來源資料夾，不刪除其備份（避免權限問題造成誤刪）
            failed = {nfc(f) for f in src_failed}
            prefixes = tuple(f + "/" for f in failed if f)
            if "" in failed:
                prefixes = ("",)

            def protected(key: str) -> bool:
                return key in failed or (bool(prefixes) and key.startswith(prefixes))

            for key, d in sorted(dst_files.items()):
                if key not in src_files and not protected(key):
                    actions.append(Action("delete", d.path, d.size))
            for key, d in sorted(dst_dirs.items(), key=lambda kv: kv[0].count("/"), reverse=True):
                if key not in src_dirs and not protected(key):
                    actions.append(Action("rmdir", d.path))
            if rel_root and not os.path.lexists(src_abs) and os.path.isdir(dst_abs):
                actions.append(Action("rmdir", rel_root))

        return Plan(actions=actions,
                    src_files=len(src_files),
                    src_bytes=sum(e.size for e in src_files.values()),
                    dest_files=len(dst_files))

    # ------------------------------------------------------------ 執行

    def execute(self, plan: Plan, allow_mass_delete: bool = False, dest_total: int = 0) -> RunResult:
        res = RunResult()
        deletes = plan.count("delete")
        if self.mirror and self.task.mass_delete_guard and not allow_mass_delete:
            total = max(dest_total, plan.dest_files)
            if deletes >= MASS_DELETE_MIN and deletes > total * MASS_DELETE_RATIO:
                res.blocked = f"這次同步將刪除 {deletes} 個備份檔案（共 {total} 個），已暫停等待確認"
                return res

        self.hooks.on_plan(plan)
        if not plan.actions:
            return res
        os.makedirs(self.dst_root, exist_ok=True)

        for a in sorted(plan.actions, key=lambda a: _PHASE[a.kind]):
            self.hooks.checkpoint()
            try:
                if a.kind == "delete":
                    if self._remove_file(a.rel):
                        res.deleted += 1
                        self.hooks.on_action("delete", a.rel, a.size)
                elif a.kind == "rmdir":
                    if self._remove_dir(a.rel):
                        self.hooks.on_action("rmdir", a.rel)
                elif a.kind == "mkdir":
                    self._ensure_dir(a.rel)
                else:
                    copied = self._copy(a.rel)
                    if copied is None:
                        res.retry.add(a.rel)
                    elif copied:
                        if a.kind == "new":
                            res.new += 1
                        else:
                            res.updated += 1
                        res.bytes += a.size
                        self.hooks.on_action(a.kind, a.rel, a.size)
                        self.hooks.after_file()
            except Cancelled:
                raise
            except OSError as e:
                if not os.path.isdir(self.dst_root):
                    raise SyncError("外接硬碟已中斷連線", drive_lost=True) from e
                if e.errno == errno.ENOSPC:
                    raise SyncError("外接硬碟空間不足，同步已停止") from e
                res.errors += 1
                self.hooks.on_action("error", a.rel, a.size, describe_error(e))
        return res

    # ------------------------------------------------------------ 檔案操作

    def _archive(self, rel: str):
        """把備份中即將被覆蓋 / 刪除的檔案移到歷史版本資料夾；未啟用則直接刪除。"""
        dst = self._dst(rel)
        if not self.task.keep_versions:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
            return
        target = os.path.join(self.dst_root, VERSIONS_DIRNAME, self.stamp, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        base, n = target, 1
        while os.path.lexists(target):
            target = f"{base}.{n}"
            n += 1
        os.replace(dst, target)

    def _ensure_dir(self, rel: str):
        dst = self._dst(rel)
        if os.path.isdir(dst):
            return
        try:
            os.makedirs(dst, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            # 路徑上某一層在備份中是「檔案」，但來源中已變成資料夾
            parts = rel.split("/")
            for i in range(1, len(parts) + 1):
                sub = "/".join(parts[:i])
                p = self._dst(sub)
                if os.path.lexists(p) and not os.path.isdir(p):
                    self._archive(sub)
                if not os.path.isdir(p):
                    os.mkdir(p)

    def _copy(self, rel: str):
        """複製單一檔案。回傳 True 成功、False 不需複製、None 表示檔案正在變動需稍後重試。"""
        src = self._src(rel)
        dst = self._dst(rel)
        try:
            st1 = os.stat(src)
        except FileNotFoundError:
            return False
        parent_rel = os.path.dirname(rel)
        if parent_rel:
            self._ensure_dir(parent_rel)
        else:
            os.makedirs(self.dst_root, exist_ok=True)
        if os.path.isdir(dst) and not os.path.islink(dst):
            self._archive(rel)

        tmp = os.path.join(os.path.dirname(dst), f".sync-{os.getpid()}-{next(_tmp_counter)}.syncpart")
        try:
            with open(src, "rb") as fs, open(tmp, "wb") as fd:
                while True:
                    self.hooks.checkpoint()
                    buf = fs.read(CHUNK_SIZE)
                    if not buf:
                        break
                    fd.write(buf)
                    self.hooks.on_bytes(len(buf))
            st2 = os.stat(src)
            if st2.st_size != st1.st_size or st2.st_mtime != st1.st_mtime:
                os.remove(tmp)
                return None
            _copy_xattrs(src, tmp)
            os.utime(tmp, (st1.st_atime, st1.st_mtime))
            if os.path.lexists(dst):
                self._archive(rel)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True

    def _remove_file(self, rel: str) -> bool:
        dst = self._dst(rel)
        if not os.path.lexists(dst) or os.path.isdir(dst):
            return False
        self._archive(rel)
        return True

    def _remove_dir(self, rel: str) -> bool:
        dst = self._dst(rel)
        if not os.path.isdir(dst) or os.path.islink(dst):
            return False
        try:
            os.rmdir(dst)
            return True
        except OSError:
            pass
        # 只剩被排除的系統檔（如 .DS_Store）時一併清除
        try:
            leftovers = os.listdir(dst)
        except OSError:
            return False
        if all(self.ex.match(f"{rel}/{n}", n) and os.path.isfile(os.path.join(dst, n)) for n in leftovers):
            for n in leftovers:
                os.remove(os.path.join(dst, n))
            os.rmdir(dst)
            return True
        return False

    # ------------------------------------------------------------ 歷史版本

    def prune_versions(self) -> int:
        days = self.task.version_days
        vdir = os.path.join(self.dst_root, VERSIONS_DIRNAME)
        if days <= 0 or not os.path.isdir(vdir):
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for name in os.listdir(vdir):
            try:
                ts = datetime.strptime(name, "%Y-%m-%d_%H%M%S").timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                self.hooks.checkpoint()
                shutil.rmtree(os.path.join(vdir, name), ignore_errors=True)
                removed += 1
        return removed
