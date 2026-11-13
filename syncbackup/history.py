"""活動紀錄（SQLite）：每次同步的摘要與每個檔案的動作。"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    task_id TEXT, task_name TEXT, kind TEXT,
    started REAL, finished REAL, status TEXT,
    new INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
    src_files INTEGER, src_bytes INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL, task_id TEXT, task_name TEXT, action TEXT, path TEXT, size INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, started);
"""


class History:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._pending: list[tuple] = []

    # ------------------------------------------------------------ 寫入

    def start_run(self, task_id, task_name, kind) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO runs(task_id, task_name, kind, started, status) VALUES (?,?,?,?,?)",
                (task_id, task_name, kind, time.time(), "執行中"))
            self.conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id, status, res=None, src_files=None, src_bytes=None, message=""):
        with self.lock:
            self.conn.execute(
                "UPDATE runs SET finished=?, status=?, new=?, updated=?, deleted=?, bytes=?, errors=?,"
                " src_files=?, src_bytes=?, message=? WHERE id=?",
                (time.time(), status,
                 res.new if res else 0, res.updated if res else 0, res.deleted if res else 0,
                 res.bytes if res else 0, res.errors if res else 0,
                 src_files, src_bytes, message, run_id))
            self.conn.commit()

    def add_event(self, ts, task_id, task_name, action, path, size=0, error=None):
        with self.lock:
            self._pending.append((ts, task_id, task_name, action, path, size, error))
            if len(self._pending) >= 500:
                self._flush_locked()

    def flush(self):
        with self.lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._pending:
            return
        self.conn.executemany(
            "INSERT INTO events(ts, task_id, task_name, action, path, size, error) VALUES (?,?,?,?,?,?,?)",
            self._pending)
        self.conn.commit()
        self._pending.clear()

    def prune(self, days: int = 60):
        cutoff = time.time() - days * 86400
        with self.lock:
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM runs WHERE started < ?", (cutoff,))
            self.conn.execute("UPDATE runs SET status='已中斷' WHERE status='執行中'")
            self.conn.commit()

    def clear(self):
        with self.lock:
            self._pending.clear()
            self.conn.execute("DELETE FROM events")
            self.conn.execute("DELETE FROM runs")
            self.conn.commit()

    # ------------------------------------------------------------ 查詢

    def query_events(self, task_id=None, actions=None, text=None, limit=1000):
        sql = "SELECT ts, task_id, task_name, action, path, size, error FROM events WHERE 1=1"
        args: list = []
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if actions:
            sql += f" AND action IN ({','.join('?' * len(actions))})"
            args.extend(actions)
        if text:
            sql += " AND path LIKE ?"
            args.append(f"%{text}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            self._flush_locked()
            return self.conn.execute(sql, args).fetchall()

    def query_runs(self, task_id=None, limit=500):
        sql = ("SELECT started, finished, task_name, kind, status, new, updated, deleted, bytes, errors, message"
               " FROM runs")
        args: list = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def last_full_runs(self) -> dict:
        """每個任務最近一次成功完整同步：{task_id: (finished, src_files, src_bytes)}"""
        sql = ("SELECT task_id, MAX(finished), src_files, src_bytes FROM runs"
               " WHERE kind='完整' AND src_files IS NOT NULL GROUP BY task_id")
        with self.lock:
            return {r[0]: (r[1], r[2], r[3]) for r in self.conn.execute(sql)}

    def last_sync_times(self) -> dict:
        sql = "SELECT task_id, MAX(finished) FROM runs WHERE status IN ('完成','有錯誤') GROUP BY task_id"
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute(sql)}

    def today_totals(self) -> tuple[int, int]:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        sql = "SELECT COALESCE(SUM(new+updated),0), COALESCE(SUM(bytes),0) FROM runs WHERE started >= ?"
        with self.lock:
            row = self.conn.execute(sql, (midnight,)).fetchone()
            return int(row[0]), int(row[1])
