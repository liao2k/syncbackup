"""預覽變更：只掃描比對，不寫入任何檔案。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QProgressBar, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..config import Task
from ..engine import ACTION_LABELS, Cancelled, Hooks, SyncEngine, SyncError
from .common import ACTION_COLORS, fmt_size

MAX_ROWS = 5000


class _Emitter(QObject):
    done = Signal(object)
    failed = Signal(str)


class _ScanHooks(Hooks):
    def __init__(self, cancel: threading.Event):
        self.cancel = cancel

    def scan_tick(self):
        if self.cancel.is_set():
            raise Cancelled()


class PreviewDialog(QDialog):
    def __init__(self, parent, task: Task):
        super().__init__(parent)
        self.task = task
        self.sync_requested = False
        self.setWindowTitle(f"預覽變更：{task.name}")
        self.resize(820, 560)

        v = QVBoxLayout(self)
        self.summary = QLabel("正在掃描來源與備份位置，比對差異中…")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        v.addWidget(self.busy)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["動作", "檔案", "大小"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        v.addWidget(self.table)

        self.buttons = QDialogButtonBox()
        self.sync_btn = self.buttons.addButton("立即同步", QDialogButtonBox.AcceptRole)
        self.sync_btn.setEnabled(False)
        close = self.buttons.addButton("關閉", QDialogButtonBox.RejectRole)
        self.sync_btn.clicked.connect(self._sync)
        close.clicked.connect(self.reject)
        v.addWidget(self.buttons)

        self._cancel = threading.Event()
        self._emitter = _Emitter()
        self._emitter.done.connect(self._show_plan)
        self._emitter.failed.connect(self._show_error)
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            plan = SyncEngine(self.task, _ScanHooks(self._cancel)).plan_full()
            self._emitter.done.emit(plan)
        except Cancelled:
            pass
        except SyncError as e:
            self._emitter.failed.emit(str(e))
        except OSError as e:
            self._emitter.failed.emit(str(e))

    def _show_error(self, msg: str):
        self.busy.hide()
        self.summary.setText(f"⚠ {msg}")

    def _show_plan(self, plan):
        self.busy.hide()
        new, upd, dele = plan.count("new"), plan.count("update"), plan.count("delete")
        if not (new or upd or dele):
            self.summary.setText(f"✓ 備份已是最新狀態。來源共 {plan.src_files} 個檔案（{fmt_size(plan.src_bytes)}）。")
            return
        text = (f"來源共 {plan.src_files} 個檔案（{fmt_size(plan.src_bytes)}）。同步將會："
                f"<b style='color:{ACTION_COLORS['new']}'>新增 {new}</b>、"
                f"<b style='color:{ACTION_COLORS['update']}'>更新 {upd}</b>、"
                f"<b style='color:{ACTION_COLORS['delete']}'>刪除 {dele}</b> 個檔案，"
                f"需傳輸 {fmt_size(plan.copy_bytes)}。")
        if dele and self.task.keep_versions:
            text += "<br>被刪除與覆蓋的檔案會移到「_備份歷史版本」資料夾。"
        self.summary.setText(text)

        rows = [a for a in plan.actions if a.kind in ("new", "update", "delete")]
        if len(rows) > MAX_ROWS:
            self.summary.setText(text + f"<br>（僅列出前 {MAX_ROWS} 筆）")
            rows = rows[:MAX_ROWS]
        self.table.setRowCount(len(rows))
        for i, a in enumerate(rows):
            item = QTableWidgetItem(ACTION_LABELS[a.kind])
            item.setForeground(QColor(ACTION_COLORS.get(a.kind, "#000")))
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem(a.rel))
            size = QTableWidgetItem(fmt_size(a.size))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 2, size)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(2)
        self.sync_btn.setEnabled(True)

    def _sync(self):
        self.sync_requested = True
        self.accept()

    def done(self, r):
        self._cancel.set()
        super().done(r)
