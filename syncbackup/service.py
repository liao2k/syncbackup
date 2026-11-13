"""背景同步服務。

執行緒：
  watchdog  —— 監聽來源資料夾變動（macOS FSEvents），只記錄「哪些路徑變了」
  monitor   —— 每秒檢查：外接硬碟連線、電池 / 負載自動暫停、變動等待時間、定期完整檢查
  worker    —— 唯一的同步執行緒，一次只處理一個任務，套用 QoS / 限速 / CPU 上限
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import sysutil
from .config import APP_DISPLAY_NAME, Config, Task
from .engine import ACTION_LABELS, Cancelled, Excluder, Hooks, Plan, SyncEngine, SyncError
from .history import History
from .throttle import CpuGovernor, RateLimiter, SpeedMeter

log = logging.getLogger(__name__)

MAX_DIRTY_PATHS = 5000
DRIVE_MISSING_REMIND = 24 * 3600    # 外接硬碟未連接多久後提醒，之後每隔這段時間再提醒一次


@dataclass
class TaskState:
    status: str = "idle"        # idle / queued / syncing / waiting_drive / error / blocked / disabled
    message: str = ""
    last_sync: float = 0.0
    last_full: float = 0.0
    last_prune: float = 0.0
    file_count: int = 0
    total_bytes: int = 0
    drive_ok: bool = False
    dirty: set = field(default_factory=set)
    last_event: float = 0.0
    pending_full: bool = False
    pending_paths: set = field(default_factory=set)
    allow_mass_delete: bool = False
    errors: int = 0
    missing_notified: float = 0.0


@dataclass
class Progress:
    task_id: str | None = None
    phase: str = ""
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_file: str = ""


class _Handler(FileSystemEventHandler):
    def __init__(self, service: "SyncService", task_id: str, root: str):
        self.service = service
        self.task_id = task_id
        self.root = root

    def on_any_event(self, event):
        if event.event_type not in ("created", "deleted", "modified", "moved"):
            return
        if event.is_directory and event.event_type == "modified":
            return
        paths = [event.src_path]
        if event.event_type == "moved":
            paths.append(event.dest_path)
        self.service._mark_dirty(self.task_id, self.root, paths)


class _RunHooks(Hooks):
    def __init__(self, service: "SyncService", task: Task, cancel: threading.Event):
        self.svc = service
        self.task = task
        self.cancel = cancel

    def checkpoint(self):
        svc = self.svc
        if self.cancel.is_set() or svc._stopping.is_set():
            raise Cancelled()
        if svc.paused:
            svc.progress.phase = "已暫停"
            while svc.paused:
                if self.cancel.is_set() or svc._stopping.is_set():
                    raise Cancelled()
                time.sleep(0.5)
            svc.progress.phase = "同步中"
        svc._apply_thread_priority()
        svc.governor.limit = svc.cfg.perf.cpu_limit
        svc.governor.tick()

    def scan_tick(self):
        self.checkpoint()

    def on_bytes(self, n):
        self.svc.limiter.consume(n)
        self.svc.speed.add(n)
        self.svc.progress.bytes_done += n

    def on_plan(self, plan: Plan):
        p = self.svc.progress
        p.phase = "同步中"
        p.files_total = plan.file_actions
        p.bytes_total = plan.copy_bytes
        p.files_done = 0
        p.bytes_done = 0

    def on_action(self, kind, rel, size=0, error=None):
        p = self.svc.progress
        if kind in ("new", "update", "delete"):
            p.files_done += 1
        p.current_file = rel
        self.svc._record(self.task, kind, rel, size, error)

    def after_file(self):
        gap = self.svc.cfg.perf.file_gap_ms
        if gap > 0:
            time.sleep(gap / 1000)


class SyncService:
    def __init__(self, cfg: Config, history: History):
        self.cfg = cfg
        self.history = history
        self.lock = threading.RLock()
        self.states: dict[str, TaskState] = {}
        self.queue: deque[str] = deque()
        self.cond = threading.Condition(self.lock)

        self.user_paused = False
        self.auto_pause_reason: str | None = None
        self._busy = False

        self.limiter = RateLimiter()
        self.governor = CpuGovernor()
        self.speed = SpeedMeter()
        self.progress = Progress()
        self.current_task: str | None = None
        self._cancel = threading.Event()
        self._applied_priority: str | None = None

        self.recent: deque = deque(maxlen=2000)
        self._seq = 0
        self._excluders: dict[str, Excluder] = {}

        self._observer: Observer | None = None
        self._watched: dict[str, str] = {}    # task_id -> 監控中的來源路徑
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started_at = time.time()

    # ------------------------------------------------------------ 生命週期

    def start(self):
        self.history.prune()
        last_full = self.history.last_full_runs()
        last_sync = self.history.last_sync_times()
        with self.lock:
            for t in self.cfg.tasks:
                st = self.states.setdefault(t.id, TaskState())
                if t.id in last_full:
                    _, st.file_count, st.total_bytes = last_full[t.id]
                st.last_sync = last_sync.get(t.id) or 0.0
                # 啟動時一律做一次完整檢查，補上程式未執行期間的變動
                st.pending_full = t.enabled
                st.drive_ok = sysutil.dest_available(t.dest)
                if not t.enabled:
                    st.status = "disabled"
                else:
                    st.status = "idle" if st.drive_ok else "waiting_drive"
        self.apply_performance()
        self._observer = Observer()
        self._observer.start()
        self._reschedule_watchers()
        for target, name in ((self._monitor_loop, "monitor"), (self._worker_loop, "worker")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        with self.lock:
            for t in self.cfg.tasks:
                if t.enabled and self.states[t.id].drive_ok:
                    self._enqueue(t.id)

    def stop(self):
        self._stopping.set()
        self._cancel.set()
        with self.cond:
            self.cond.notify_all()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
        for th in self._threads:
            th.join(timeout=5)
        self.history.flush()

    # ------------------------------------------------------------ 對外操作（介面呼叫）

    @property
    def paused(self) -> bool:
        return self.user_paused or self.auto_pause_reason is not None

    def set_paused(self, paused: bool):
        self.user_paused = paused
        with self.cond:
            self.cond.notify_all()

    def apply_performance(self):
        perf = self.cfg.perf
        self.limiter.set_rate(perf.max_speed_mb * 1024 * 1024 if perf.max_speed_mb > 0 else 0)
        self.governor.limit = perf.cpu_limit
        self._busy = False
        self._evaluate_auto_pause()

    def request_full(self, task_id: str, allow_mass_delete: bool = False):
        with self.lock:
            st = self.states.get(task_id)
            if not st:
                return
            st.pending_full = True
            st.allow_mass_delete = st.allow_mass_delete or allow_mass_delete
            if st.status == "blocked":
                st.status = "queued"
            self._enqueue(task_id)

    def request_all(self):
        for t in self.cfg.tasks:
            if t.enabled:
                self.request_full(t.id)

    def reload_tasks(self, changed_ids=()):
        """設定變更後呼叫：同步任務清單、重新監控、必要時中斷正在跑的任務。"""
        with self.lock:
            ids = {t.id for t in self.cfg.tasks}
            for tid in list(self.states):
                if tid not in ids:
                    del self.states[tid]
            for t in self.cfg.tasks:
                st = self.states.setdefault(t.id, TaskState())
                st.drive_ok = sysutil.dest_available(t.dest)
                if not t.enabled:
                    st.status = "disabled"
                    st.dirty.clear()
                    st.pending_paths.clear()
                    st.pending_full = False
                elif st.status == "disabled":
                    st.status = "idle" if st.drive_ok else "waiting_drive"
                    st.pending_full = True
                elif not st.drive_ok and st.status in ("idle", "queued"):
                    st.status = "waiting_drive"
            for tid in changed_ids:
                st = self.states.get(tid)
                if st:
                    st.pending_full = True
                    st.status = "queued" if st.drive_ok else "waiting_drive"
            self._excluders.clear()
            if self.current_task and (self.current_task not in ids or self.current_task in changed_ids):
                self._cancel.set()
            for t in self.cfg.tasks:
                if t.enabled and self.states[t.id].pending_full:
                    self._enqueue(t.id)
        self._reschedule_watchers()

    def snapshot(self) -> dict:
        with self.lock:
            tasks = {
                tid: dict(status=st.status, message=st.message, last_sync=st.last_sync,
                          file_count=st.file_count, total_bytes=st.total_bytes, drive_ok=st.drive_ok,
                          pending=len(st.dirty) + len(st.pending_paths) + (1 if st.pending_full else 0),
                          watched=tid in self._watched)
                for tid, st in self.states.items()
            }
        p = self.progress
        return dict(
            paused=self.paused,
            user_paused=self.user_paused,
            auto_pause_reason=self.auto_pause_reason,
            current_task=self.current_task,
            progress=dict(phase=p.phase, files_done=p.files_done, files_total=p.files_total,
                          bytes_done=p.bytes_done, bytes_total=p.bytes_total, current_file=p.current_file),
            speed=self.speed.rate(),
            tasks=tasks,
        )

    def events_since(self, seq: int) -> list[dict]:
        with self.lock:
            return [e for e in self.recent if e["seq"] > seq]

    # ------------------------------------------------------------ 內部：監控

    def _excluder(self, task_id: str) -> Excluder | None:
        ex = self._excluders.get(task_id)
        if ex is None:
            t = self.cfg.get_task(task_id)
            if not t:
                return None
            ex = self._excluders[task_id] = Excluder(t.excludes)
        return ex

    def _mark_dirty(self, task_id: str, root: str, paths):
        ex = self._excluder(task_id)
        if ex is None:
            return
        now = time.time()
        with self.lock:
            st = self.states.get(task_id)
            if st is None:
                return
            for p in paths:
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                rel = os.path.relpath(p, root)
                if rel == ".":
                    rel = ""
                elif rel.startswith(".."):
                    continue
                if rel and ex.match_any_part(rel):
                    continue
                st.dirty.add(rel)
                st.last_event = now
            if len(st.dirty) > MAX_DIRTY_PATHS:
                st.dirty = {""}    # 變動太多，改做一次完整同步

    def _reschedule_watchers(self):
        if not self._observer:
            return
        wanted = {}
        for t in self.cfg.tasks:
            if t.enabled and t.realtime and os.path.isdir(t.source):
                wanted[t.id] = os.path.abspath(t.source)
        with self.lock:
            if wanted == self._watched:
                return
            try:
                self._observer.unschedule_all()
            except Exception:
                pass
            self._watched = {}
            for tid, src in wanted.items():
                try:
                    self._observer.schedule(_Handler(self, tid, src), src, recursive=True)
                    self._watched[tid] = src
                except Exception as e:
                    log.warning("無法監控 %s：%s", src, e)

    def _evaluate_auto_pause(self):
        perf = self.cfg.perf
        reason = None
        if perf.pause_on_battery and sysutil.on_battery():
            reason = "使用電池中"
        if perf.pause_when_busy:
            load = sysutil.load_percent()
            # 遲滯：超過門檻才暫停，降到門檻 80% 以下才恢復
            limit = perf.busy_percent * (0.8 if self._busy else 1.0)
            self._busy = load > limit
            if self._busy and reason is None:
                reason = f"系統忙碌（負載 {load:.0f}%）"
        else:
            self._busy = False
        if reason != self.auto_pause_reason:
            self.auto_pause_reason = reason
            with self.cond:
                self.cond.notify_all()

    def _monitor_loop(self):
        tick = 0
        while not self._stopping.wait(1.0):
            tick += 1
            try:
                if tick % 3 == 0:
                    self._check_drives()
                if tick % 15 == 0:
                    self._evaluate_auto_pause()
                if tick % 30 == 0:
                    self._reschedule_watchers()
                self._dispatch_dirty()
                if tick % 5 == 0:
                    self.history.flush()
            except Exception:
                log.error("monitor 錯誤：%s", traceback.format_exc())

    def _check_drives(self):
        for t in list(self.cfg.tasks):
            ok = sysutil.dest_available(t.dest)
            with self.lock:
                st = self.states.get(t.id)
                if st is None or ok == st.drive_ok:
                    continue
                st.drive_ok = ok
                if not t.enabled:
                    continue
                if ok:
                    st.pending_full = True
                    st.missing_notified = 0.0
                    st.message = ""
                    if st.status == "waiting_drive":
                        st.status = "queued"
                    self._enqueue(t.id)
                    self._record(t, "info", "外接硬碟已連接，開始檢查變更")
                else:
                    st.status = "waiting_drive"
                    st.message = ""
                    self._record(t, "info", "外接硬碟已中斷連線")
        self._check_drive_missing()

    def _check_drive_missing(self):
        """外接硬碟長時間未連接時提醒，避免在不知情下很多天都沒有備份。"""
        now = time.time()
        overdue = []
        with self.lock:
            for t in self.cfg.tasks:
                st = self.states.get(t.id)
                if st is None or not t.enabled or st.drive_ok:
                    continue
                since = st.last_sync or self._started_at
                if now - since < DRIVE_MISSING_REMIND:
                    continue
                days = int((now - since) // 86400)
                st.message = f"已 {days} 天沒有備份，請連接外接硬碟"
                if now - st.missing_notified >= DRIVE_MISSING_REMIND:
                    st.missing_notified = now
                    overdue.append((t, days))
        if not overdue:
            return
        for t, days in overdue:
            self._record(t, "info", f"外接硬碟已 {days} 天未連接，這段期間沒有備份")
        if self.cfg.general.notify_drive_missing:
            names = "、".join(t.name for t, _ in overdue)
            days = max(d for _, d in overdue)
            sysutil.notify(f"{APP_DISPLAY_NAME}：外接硬碟未連接",
                           f"「{names}」已 {days} 天沒有備份，請連接外接硬碟")

    def _dispatch_dirty(self):
        now = time.time()
        perf = self.cfg.perf
        with self.lock:
            for t in self.cfg.tasks:
                st = self.states.get(t.id)
                if st is None or not t.enabled:
                    continue
                if st.dirty and now - st.last_event >= perf.debounce_sec and st.drive_ok:
                    st.pending_paths |= st.dirty
                    st.dirty = set()
                    self._enqueue(t.id)
                if (perf.full_scan_minutes > 0 and st.drive_ok and not st.pending_full
                        and st.status not in ("blocked", "syncing")
                        and now - st.last_full > perf.full_scan_minutes * 60):
                    st.pending_full = True
                    self._enqueue(t.id)

    def _enqueue(self, task_id: str):
        with self.cond:
            st = self.states.get(task_id)
            if st and st.status in ("idle", "error") and self.current_task != task_id:
                st.status = "queued"
            if task_id not in self.queue:
                self.queue.append(task_id)
            self.cond.notify_all()

    # ------------------------------------------------------------ 內部：同步

    def _apply_thread_priority(self):
        level = self.cfg.perf.priority
        if level != self._applied_priority:
            sysutil.set_thread_priority(level)
            self._applied_priority = level

    def _next_job(self) -> str | None:
        with self.cond:
            while not self._stopping.is_set():
                if self.queue and not self.paused:
                    return self.queue.popleft()
                self.cond.wait(timeout=1.0)
        return None

    def _worker_loop(self):
        self._apply_thread_priority()
        while not self._stopping.is_set():
            tid = self._next_job()
            if tid is None:
                continue
            try:
                self._run_job(tid)
            except Exception:
                log.error("worker 錯誤：%s", traceback.format_exc())
            finally:
                self.current_task = None
                self.progress = Progress()

    def _run_job(self, tid: str):
        task = self.cfg.get_task(tid)
        with self.lock:
            st = self.states.get(tid)
            if task is None or st is None:
                return
            if not task.enabled:
                st.pending_full = False
                st.pending_paths.clear()
                return
            full = st.pending_full
            paths = set(st.pending_paths)
            allow = st.allow_mass_delete
            if not full and not paths:
                if st.status == "queued":
                    st.status = "idle"
                return
            if not sysutil.dest_available(task.dest):
                st.drive_ok = False
                st.status = "waiting_drive"
                st.pending_full = True
                st.pending_paths.clear()
                return
            st.pending_full = False
            st.pending_paths = set()
            if full:
                st.dirty = set()    # 完整掃描已涵蓋目前為止的變動
            st.allow_mass_delete = False
            st.status = "syncing"
            st.message = "正在比對檔案…"
            self._cancel = threading.Event()
            cancel = self._cancel
            self.current_task = tid

        task = copy.deepcopy(task)
        self._apply_thread_priority()
        self.progress = Progress(task_id=tid, phase="掃描中")
        hooks = _RunHooks(self, task, cancel)
        engine = SyncEngine(task, hooks)
        kind = "完整" if full else "即時"
        run_id = self.history.start_run(tid, task.name, kind)
        plan = None
        try:
            plan = engine.plan_full() if full else engine.plan_paths(paths)
            if plan.full:
                full = True
            with self.lock:
                st.message = ""
                dest_total = st.file_count
            res = engine.execute(plan, allow_mass_delete=allow, dest_total=dest_total)

            if full and task.keep_versions and time.time() - st.last_prune > 6 * 3600:
                self.progress.phase = "清理舊版本"
                engine.prune_versions()
                st.last_prune = time.time()

            now = time.time()
            with self.lock:
                if res.blocked:
                    st.status = "blocked"
                    st.message = res.blocked
                elif res.errors:
                    st.status = "error"
                    st.message = f"{res.errors} 個檔案無法備份，詳見活動紀錄"
                else:
                    st.status = "idle"
                    st.message = ""
                if not res.blocked:
                    st.last_sync = now
                    if full:
                        st.last_full = now
                        st.file_count = plan.src_files
                        st.total_bytes = plan.src_bytes
                    else:
                        st.file_count = max(0, st.file_count + res.new - res.deleted)
                if res.retry:
                    st.dirty |= res.retry
                    st.last_event = now
                if st.pending_full or st.pending_paths:
                    self._enqueue(tid)

            status = "需要確認" if res.blocked else ("有錯誤" if res.errors else "完成")
            self.history.finish_run(run_id, status, res,
                                    plan.src_files if full else None,
                                    plan.src_bytes if full else None,
                                    res.blocked or "")
            if res.blocked:
                self._record(task, "info", res.blocked)
                self._notify(task, "需要確認", res.blocked, error=True)
            elif res.errors:
                self._notify(task, "部分檔案無法備份", st.message, error=True)
            elif full and res.changed and self.cfg.general.notify_full_sync:
                self._notify(task, "完整同步完成",
                             f"新增 {res.new}、更新 {res.updated}、刪除 {res.deleted} 個檔案")

        except Cancelled:
            with self.lock:
                if tid in self.states and self.cfg.get_task(tid):
                    if full:
                        st.pending_full = True
                    else:
                        st.pending_paths |= paths
                    st.status = "queued"
                    if not self._stopping.is_set():
                        self._enqueue(tid)
            self.history.finish_run(run_id, "已中斷")

        except SyncError as e:
            with self.lock:
                if e.drive_lost:
                    st.drive_ok = False
                    st.status = "waiting_drive"
                    st.pending_full = True
                else:
                    st.status = "error"
                    st.message = str(e)
            self.history.finish_run(run_id, "失敗", message=str(e))
            self._record(task, "error", "", 0, str(e))
            if not e.drive_lost:
                self._notify(task, "同步失敗", str(e), error=True)

        except Exception as e:
            log.error("同步失敗：%s", traceback.format_exc())
            with self.lock:
                st.status = "error"
                st.message = f"未預期的錯誤：{e}"
            self.history.finish_run(run_id, "失敗", message=str(e))
            self._record(task, "error", "", 0, str(e))
        finally:
            self.history.flush()

    def _record(self, task: Task, kind: str, rel: str, size: int = 0, error: str | None = None):
        if kind == "mkdir":
            return
        ts = time.time()
        with self.lock:
            self._seq += 1
            self.recent.append(dict(seq=self._seq, ts=ts, task_id=task.id, task_name=task.name,
                                    action=kind, label=ACTION_LABELS.get(kind, kind),
                                    path=rel, size=size, error=error))
        self.history.add_event(ts, task.id, task.name, kind, rel, size, error)

    def _notify(self, task: Task, title: str, message: str, error: bool = False):
        if error and not self.cfg.general.notify_errors:
            return
        sysutil.notify(f"{APP_DISPLAY_NAME}：{task.name}", f"{title} — {message}")
