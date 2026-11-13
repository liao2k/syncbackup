"""主視窗與選單列圖示。"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSlider, QSpinBox, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .. import sysutil
from ..config import (
    APP_DIR, APP_DISPLAY_NAME, MODE_MIRROR, PRIORITY_LABELS, PROFILE_DESCRIPTIONS, PROFILE_LABELS, VERSIONS_DIRNAME,
    Config,
)
from ..engine import ACTION_LABELS
from ..history import History
from ..service import SyncService
from . import icons
from .common import (
    ACTION_COLORS, STATUS_DISPLAY, fmt_ago, fmt_duration, fmt_size, fmt_speed, fmt_time,
)
from .preview_dialog import PreviewDialog
from .task_dialog import TaskDialog


def _item(text, color: str | None = None, align=None, tooltip: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    if color:
        it.setForeground(QColor(color))
    if align is not None:
        it.setTextAlignment(align)
    if tooltip:
        it.setToolTip(tooltip)
    return it


RIGHT = Qt.AlignRight | Qt.AlignVCenter


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setAlternatingRowColors(True)
    t.setWordWrap(False)
    t.horizontalHeader().setHighlightSections(False)
    return t


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #888;")
    return lbl


# =================================================================== 備份任務分頁

class TasksTab(QWidget):
    COLS = ["啟用", "任務", "來源資料夾", "備份位置", "外接硬碟", "狀態", "上次同步", "檔案數", "大小"]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.cfg = win.cfg
        self.service = win.service
        self._row_ids: list[str] = []
        self._updating = False
        self._free_cache: dict[str, tuple[float, int | None]] = {}

        v = QVBoxLayout(self)
        self.stack = QStackedWidget()
        v.addWidget(self.stack)

        # 空白狀態
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch()
        title = QLabel("還沒有備份任務")
        f = title.font()
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        ev.addWidget(title)
        sub = QLabel("選擇電腦上的資料夾與外接硬碟上的備份位置，之後檔案一有變動就會自動備份。")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color: #888;")
        ev.addWidget(sub)
        btn = QPushButton("＋ 新增第一個備份任務")
        btn.setFixedWidth(240)
        btn.clicked.connect(win.add_task)
        h = QHBoxLayout()
        h.addStretch()
        h.addWidget(btn)
        h.addStretch()
        ev.addSpacing(12)
        ev.addLayout(h)
        ev.addStretch()
        self.stack.addWidget(empty)

        # 任務列表
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        self.table = _table(self.COLS)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        for col, w in ((0, 44), (1, 130), (4, 150), (5, 170), (6, 90), (7, 70), (8, 80)):
            self.table.setColumnWidth(col, w)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.edit())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        pv.addWidget(self.table)

        bar = QHBoxLayout()
        self.btn_edit = QPushButton("編輯…")
        self.btn_delete = QPushButton("移除")
        self.btn_sync = QPushButton("立即同步")
        self.btn_preview = QPushButton("預覽變更…")
        self.btn_src = QPushButton("開啟來源")
        self.btn_dst = QPushButton("開啟備份")
        self.btn_ver = QPushButton("歷史版本")
        self.btn_edit.clicked.connect(self.edit)
        self.btn_delete.clicked.connect(self.delete)
        self.btn_sync.clicked.connect(self.sync_now)
        self.btn_preview.clicked.connect(self.preview)
        self.btn_src.clicked.connect(lambda: self._open("src"))
        self.btn_dst.clicked.connect(lambda: self._open("dst"))
        self.btn_ver.clicked.connect(lambda: self._open("ver"))
        for b in (self.btn_edit, self.btn_delete, self.btn_sync, self.btn_preview):
            bar.addWidget(b)
        bar.addStretch()
        for b in (self.btn_src, self.btn_dst, self.btn_ver):
            bar.addWidget(b)
        pv.addLayout(bar)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet("color: #888;")
        pv.addWidget(self.detail)
        self.stack.addWidget(page)

        self.refresh()

    # ------------------------------------------------------------ 資料

    def selected_task(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.cfg.get_task(self._row_ids[rows[0].row()])

    def _free(self, dest: str):
        vol = sysutil.volume_of(dest)
        if not vol:
            return None
        cached = self._free_cache.get(vol)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        free = sysutil.free_space(vol) if os.path.ismount(vol) else None
        self._free_cache[vol] = (time.time(), free)
        return free

    def refresh(self, snap: dict | None = None):
        snap = snap or self.service.snapshot()
        tasks = list(self.cfg.tasks)
        self.stack.setCurrentIndex(0 if not tasks else 1)
        ids = [t.id for t in tasks]
        self._updating = True
        try:
            if ids != self._row_ids:
                selected = self.selected_task()
                self.table.setRowCount(len(tasks))
                self._row_ids = ids
                if selected and selected.id in ids:
                    self.table.selectRow(ids.index(selected.id))
            prog = snap["progress"]
            for row, t in enumerate(tasks):
                st = snap["tasks"].get(t.id, {})
                self._fill_row(row, t, st, snap, prog)
        finally:
            self._updating = False
        self._update_buttons()

    def _set(self, row, col, item: QTableWidgetItem):
        old = self.table.item(row, col)
        if old and old.text() == item.text() and old.foreground() == item.foreground() \
                and old.toolTip() == item.toolTip():
            return
        self.table.setItem(row, col, item)

    def _fill_row(self, row, t, st, snap, prog):
        holder = self.table.cellWidget(row, 0)
        if holder is None or holder.property("task_id") != t.id:
            holder = QWidget()
            holder.setProperty("task_id", t.id)
            box = QCheckBox()
            box.setToolTip("勾選以啟用此任務")
            box.toggled.connect(lambda on, tid=t.id: self._toggle_enabled(tid, on))
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setAlignment(Qt.AlignCenter)
            lay.addWidget(box)
            self.table.setCellWidget(row, 0, holder)
        box = holder.findChild(QCheckBox)
        if box.isChecked() != t.enabled:
            box.blockSignals(True)
            box.setChecked(t.enabled)
            box.blockSignals(False)

        mode = "鏡像" if t.mode == MODE_MIRROR else "累積"
        flags = [mode, "即時" if t.realtime else "定期"]
        self._set(row, 1, _item(t.name, tooltip=" · ".join(flags)))
        self._set(row, 2, _item(t.source, tooltip=t.source))
        self._set(row, 3, _item(t.dest, tooltip=t.dest))

        vol = sysutil.volume_of(t.dest)
        if not vol:
            drive = _item("內建硬碟", "#7a7a7a")
        elif st.get("drive_ok"):
            free = self._free(t.dest)
            drive = _item(f"● {os.path.basename(vol)}（剩 {fmt_size(free)}）", "#1f9d55")
        else:
            drive = _item(f"○ {os.path.basename(vol)} 未連接", "#9a9a9a")
        self._set(row, 4, drive)

        status = st.get("status", "idle")
        text, color = STATUS_DISPLAY.get(status, (status, None))
        tip = st.get("message") or ""
        if status == "syncing" and snap["current_task"] == t.id:
            if prog["files_total"]:
                text = f"⟳ 同步中 {prog['files_done']}/{prog['files_total']}"
            else:
                text = f"⟳ {prog['phase'] or '掃描中'}…"
            tip = prog["current_file"]
        elif snap["paused"] and status in ("idle", "queued") and t.enabled:
            if st.get("pending"):
                text, color = f"⏸ 已暫停（{st['pending']} 項待同步）", "#7a7a7a"
        elif status == "idle" and st.get("pending"):
            text, color = f"⋯ {st['pending']} 項變更待同步", "#7a7a7a"
        if status == "idle" and t.enabled and t.realtime and not st.get("watched"):
            text, color, tip = "⚠ 來源資料夾不存在", "#d64545", t.source
        self._set(row, 5, _item(text, color, tooltip=tip or None))
        self._set(row, 6, _item(fmt_ago(st.get("last_sync")), tooltip=fmt_time(st.get("last_sync")) or None))
        self._set(row, 7, _item(f"{st.get('file_count', 0):,}", align=RIGHT))
        self._set(row, 8, _item(fmt_size(st.get("total_bytes", 0)), align=RIGHT))

    def _update_buttons(self):
        t = self.selected_task()
        for b in (self.btn_edit, self.btn_delete, self.btn_sync, self.btn_preview,
                  self.btn_src, self.btn_dst, self.btn_ver):
            b.setEnabled(t is not None)
        if t is None:
            self.detail.setText("選擇一個任務以查看詳細資訊。雙擊可編輯。")
            return
        mode = "鏡像同步（來源刪除，備份也刪除）" if t.mode == MODE_MIRROR else "累積備份（不刪除備份中的檔案）"
        ver = (f"保留歷史版本 {t.version_days} 天" if t.version_days else "永久保留歷史版本") if t.keep_versions else "不保留歷史版本"
        ex = "、".join(t.excludes[:6]) + ("…" if len(t.excludes) > 6 else "")
        st = self.service.snapshot()["tasks"].get(t.id, {})
        msg = f"<br>⚠ {st['message']}" if st.get("message") else ""
        self.detail.setText(f"{mode} · {'即時監控' if t.realtime else '只做定期檢查'} · {ver}<br>排除：{ex or '無'}{msg}")

    # ------------------------------------------------------------ 動作

    def _toggle_enabled(self, task_id: str, on: bool):
        t = self.cfg.get_task(task_id)
        if not t or self._updating:
            return
        t.enabled = on
        self.cfg.save()
        self.service.reload_tasks()

    def _context_menu(self, pos):
        if self.table.itemAt(pos) is None:
            return
        m = QMenu(self)
        for text, fn in (("立即同步", self.sync_now), ("預覽變更…", self.preview), (None, None),
                         ("編輯…", self.edit), ("移除", self.delete), (None, None),
                         ("在 Finder 開啟來源", lambda: self._open("src")),
                         ("在 Finder 開啟備份", lambda: self._open("dst")),
                         ("在 Finder 開啟歷史版本", lambda: self._open("ver"))):
            if text is None:
                m.addSeparator()
            else:
                m.addAction(text, fn)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def edit(self):
        t = self.selected_task()
        if not t:
            return
        dlg = TaskDialog(self, t, self.cfg.tasks)
        if dlg.exec():
            self.cfg.upsert_task(dlg.task)
            self.service.reload_tasks(changed_ids=[dlg.task.id])
            self.refresh()

    def delete(self):
        t = self.selected_task()
        if not t:
            return
        r = QMessageBox.question(self, "移除任務",
                                 f"確定移除備份任務「{t.name}」？\n\n只會移除任務設定，外接硬碟上已備份的檔案不會被刪除。")
        if r == QMessageBox.Yes:
            self.cfg.remove_task(t.id)
            self.service.reload_tasks()
            self.refresh()

    def sync_now(self):
        t = self.selected_task()
        if not t:
            return
        st = self.service.snapshot()["tasks"].get(t.id, {})
        if st.get("status") == "blocked":
            r = QMessageBox.warning(
                self, "確認大量刪除",
                f"{st.get('message', '')}\n\n可能原因：來源資料夾中的檔案被大量刪除或搬移。\n"
                + ("被刪除的備份檔案會移到「_備份歷史版本」資料夾。\n" if t.keep_versions else "⚠ 此任務未保留歷史版本，檔案將直接刪除。\n")
                + "\n確定要繼續同步嗎？",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
            self.service.request_full(t.id, allow_mass_delete=True)
        else:
            if not t.enabled:
                QMessageBox.information(self, "任務已停用", "請先勾選「啟用」再同步。")
                return
            self.service.request_full(t.id)
        if self.service.user_paused:
            QMessageBox.information(self, "目前已暫停", "已排入同步，按「繼續」後會開始執行。")

    def preview(self):
        t = self.selected_task()
        if not t:
            return
        if not sysutil.dest_available(t.dest):
            QMessageBox.information(self, "外接硬碟未連接", "請先連接外接硬碟再預覽。")
            return
        dlg = PreviewDialog(self, t)
        if dlg.exec() and dlg.sync_requested:
            self.service.request_full(t.id)

    def _open(self, which: str):
        t = self.selected_task()
        if not t:
            return
        if which == "src":
            sysutil.reveal(t.source)
        elif which == "dst":
            sysutil.reveal(t.dest)
        else:
            vdir = os.path.join(t.dest, VERSIONS_DIRNAME)
            if os.path.isdir(vdir):
                sysutil.reveal(vdir)
            else:
                QMessageBox.information(self, "歷史版本", "目前還沒有任何歷史版本。")


# =================================================================== 活動紀錄分頁

class ActivityTab(QWidget):
    ACTION_FILTERS = [("全部動作", None), ("新增 / 更新", ["new", "update"]),
                      ("刪除", ["delete", "rmdir"]), ("錯誤", ["error"]), ("訊息", ["info"])]
    MAX_ROWS = 2000

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.last_seq = 0
        v = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.task_combo = QComboBox()
        self.action_combo = QComboBox()
        for label, _ in self.ACTION_FILTERS:
            self.action_combo.addItem(label)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋檔名…")
        self.search.setClearButtonEnabled(True)
        refresh = QPushButton("重新整理")
        refresh.clicked.connect(self.reload)
        self.task_combo.currentIndexChanged.connect(self.reload)
        self.action_combo.currentIndexChanged.connect(self.reload)
        self.search.returnPressed.connect(self.reload)
        self.search.textChanged.connect(lambda s: s or self.reload())
        bar.addWidget(QLabel("任務："))
        bar.addWidget(self.task_combo)
        bar.addWidget(QLabel("動作："))
        bar.addWidget(self.action_combo)
        bar.addWidget(self.search, 1)
        bar.addWidget(refresh)
        v.addLayout(bar)

        self.table = _table(["時間", "任務", "動作", "檔案", "大小", "說明"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        for col, w in ((0, 150), (1, 110), (2, 80), (4, 80), (5, 200)):
            self.table.setColumnWidth(col, w)
        v.addWidget(self.table)
        self.count_label = _hint("")
        v.addWidget(self.count_label)
        self.reload_tasks()

    def reload_tasks(self):
        current = self.task_combo.currentData()
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem("全部任務", None)
        for t in self.win.cfg.tasks:
            self.task_combo.addItem(t.name, t.id)
        idx = self.task_combo.findData(current)
        self.task_combo.setCurrentIndex(max(idx, 0))
        self.task_combo.blockSignals(False)

    def _filters(self):
        return (self.task_combo.currentData(),
                self.ACTION_FILTERS[self.action_combo.currentIndex()][1],
                self.search.text().strip() or None)

    def reload(self):
        task_id, actions, text = self._filters()
        self.last_seq = self.win.service._seq
        rows = self.win.history.query_events(task_id, actions, text, limit=self.MAX_ROWS)
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for i, (ts, _tid, tname, action, path, size, error) in enumerate(rows):
            self._fill(i, ts, tname, action, path, size, error)
        self.count_label.setText(f"顯示最近 {len(rows)} 筆紀錄（保留 60 天）")

    def _fill(self, row, ts, tname, action, path, size, error):
        color = ACTION_COLORS.get(action)
        self.table.setItem(row, 0, _item(fmt_time(ts)))
        self.table.setItem(row, 1, _item(tname))
        self.table.setItem(row, 2, _item(ACTION_LABELS.get(action, action), color))
        self.table.setItem(row, 3, _item(path, tooltip=path))
        self.table.setItem(row, 4, _item(fmt_size(size) if size else "", align=RIGHT))
        self.table.setItem(row, 5, _item(error or "", "#d64545" if error else None, tooltip=error))

    def poll(self):
        new = self.win.service.events_since(self.last_seq)
        if not new:
            return
        self.last_seq = new[-1]["seq"]
        task_id, actions, text = self._filters()
        new = [e for e in new
               if (not task_id or e["task_id"] == task_id)
               and (not actions or e["action"] in actions)
               and (not text or text.lower() in e["path"].lower())]
        if not new:
            return
        self.table.setUpdatesEnabled(False)
        for e in new:
            self.table.insertRow(0)
            self._fill(0, e["ts"], e["task_name"], e["action"], e["path"], e["size"], e["error"])
        while self.table.rowCount() > self.MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)
        self.table.setUpdatesEnabled(True)


# =================================================================== 同步紀錄分頁

class RunsTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        v.addWidget(_hint("每一次同步的摘要。「完整」是掃描整個資料夾比對；「即時」是只處理有變動的檔案。"))
        self.table = _table(["開始時間", "任務", "類型", "結果", "新增", "更新", "刪除", "傳輸量", "耗時", "說明"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(9, QHeaderView.Stretch)
        for col, w in ((0, 150), (1, 110), (2, 50), (3, 70), (4, 55), (5, 55), (6, 55), (7, 85), (8, 90)):
            self.table.setColumnWidth(col, w)
        v.addWidget(self.table)

    def reload(self):
        rows = self.win.history.query_runs(limit=500)
        self.table.setRowCount(len(rows))
        colors = {"完成": "#1f9d55", "有錯誤": "#d64545", "失敗": "#d64545",
                  "需要確認": "#d9822b", "已中斷": "#7a7a7a", "執行中": "#2a72e8"}
        for i, (started, finished, tname, kind, status, new, upd, dele, nbytes, errors, msg) in enumerate(rows):
            vals = [fmt_time(started), tname, kind, status, new, upd, dele, fmt_size(nbytes),
                    fmt_duration(finished - started) if finished else "", msg or (f"{errors} 個錯誤" if errors else "")]
            for c, val in enumerate(vals):
                align = RIGHT if c in (4, 5, 6, 7, 8) else None
                self.table.setItem(i, c, _item(val, colors.get(status) if c == 3 else None, align, msg or None))


# =================================================================== 效能設定分頁

class PerformanceTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.cfg = win.cfg
        self._loading = False
        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(self._save)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        v = QVBoxLayout(inner)

        box = QGroupBox("效能模式")
        bv = QVBoxLayout(box)
        self.profile_group = QButtonGroup(self)
        self.profile_buttons = {}
        for key in ("low", "balanced", "full", "custom"):
            rb = QRadioButton(PROFILE_LABELS[key])
            f = rb.font()
            f.setBold(True)
            rb.setFont(f)
            self.profile_group.addButton(rb)
            self.profile_buttons[key] = rb
            rb.toggled.connect(lambda on, k=key: on and self._on_profile(k))
            bv.addWidget(rb)
            d = _hint(PROFILE_DESCRIPTIONS[key])
            d.setStyleSheet("color: #888; margin-left: 22px; margin-bottom: 4px;")
            bv.addWidget(d)
        v.addWidget(box)

        box = QGroupBox("詳細設定")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, 200)
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(0, 2000)
        self.speed_spin.setSuffix(" MB/s")
        self.speed_spin.setSpecialValueText("不限制")
        self.speed_slider.valueChanged.connect(self.speed_spin.setValue)
        self.speed_spin.valueChanged.connect(lambda x: self.speed_slider.setValue(min(x, 200)))
        self.speed_spin.valueChanged.connect(self._changed)
        form.addRow("最大傳輸速度：", self._pair(self.speed_slider, self.speed_spin))

        self.cpu_slider = QSlider(Qt.Horizontal)
        self.cpu_slider.setRange(5, 100)
        self.cpu_spin = QSpinBox()
        self.cpu_spin.setRange(5, 100)
        self.cpu_spin.setSuffix(" %")
        self.cpu_spin.setSpecialValueText("")
        self.cpu_slider.valueChanged.connect(self.cpu_spin.setValue)
        self.cpu_spin.valueChanged.connect(self.cpu_slider.setValue)
        self.cpu_spin.valueChanged.connect(self._changed)
        form.addRow("CPU 使用上限（單核）：", self._pair(self.cpu_slider, self.cpu_spin))

        self.priority_combo = QComboBox()
        for key, label in PRIORITY_LABELS.items():
            self.priority_combo.addItem(label, key)
        self.priority_combo.currentIndexChanged.connect(self._changed)
        form.addRow("處理優先權：", self.priority_combo)

        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 2000)
        self.gap_spin.setSuffix(" 毫秒")
        self.gap_spin.valueChanged.connect(self._changed)
        form.addRow("每個檔案之間休息：", self.gap_spin)

        self.debounce_spin = QSpinBox()
        self.debounce_spin.setRange(1, 600)
        self.debounce_spin.setSuffix(" 秒")
        self.debounce_spin.valueChanged.connect(self._changed)
        form.addRow("變動後等待：", self.debounce_spin)
        form.addRow("", _hint("檔案變動後先等待一段時間，期間的連續變動合併成一次備份（例如存檔時的多次寫入）。"))

        self.full_spin = QSpinBox()
        self.full_spin.setRange(0, 10080)
        self.full_spin.setSuffix(" 分鐘")
        self.full_spin.setSpecialValueText("關閉")
        self.full_spin.valueChanged.connect(self._changed)
        form.addRow("定期完整檢查：每", self.full_spin)
        form.addRow("", _hint("即時監控之外，定期掃描整個資料夾，補上任何可能遺漏的變動。"))
        v.addWidget(box)

        box = QGroupBox("自動暫停")
        bv = QVBoxLayout(box)
        self.battery_check = QCheckBox("使用電池時暫停備份（接上電源後自動繼續）")
        self.battery_check.toggled.connect(self._changed)
        bv.addWidget(self.battery_check)
        h = QHBoxLayout()
        self.busy_check = QCheckBox("系統忙碌時暫停：平均負載超過 CPU 核心數的")
        self.busy_spin = QSpinBox()
        self.busy_spin.setRange(30, 500)
        self.busy_spin.setSuffix(" %")
        self.busy_check.toggled.connect(self._changed)
        self.busy_spin.valueChanged.connect(self._changed)
        h.addWidget(self.busy_check)
        h.addWidget(self.busy_spin)
        h.addStretch()
        bv.addLayout(h)
        self.auto_label = _hint("")
        bv.addWidget(self.auto_label)
        v.addWidget(box)
        v.addStretch()

        self.load()

    @staticmethod
    def _pair(a, b) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(a, 1)
        b.setFixedWidth(110)
        h.addWidget(b)
        return w

    def load(self):
        p = self.cfg.perf
        self._loading = True
        self.profile_buttons.get(p.profile, self.profile_buttons["custom"]).setChecked(True)
        self.speed_spin.setValue(int(p.max_speed_mb))
        self.cpu_spin.setValue(p.cpu_limit)
        self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findData(p.priority)))
        self.gap_spin.setValue(p.file_gap_ms)
        self.debounce_spin.setValue(p.debounce_sec)
        self.full_spin.setValue(p.full_scan_minutes)
        self.battery_check.setChecked(p.pause_on_battery)
        self.busy_check.setChecked(p.pause_when_busy)
        self.busy_spin.setValue(p.busy_percent)
        self.busy_spin.setEnabled(p.pause_when_busy)
        self._loading = False

    def _on_profile(self, key: str):
        if self._loading or key == "custom":
            return
        self.cfg.perf.apply_profile(key)
        self.load()
        self._save_timer.start()

    def _changed(self, *_):
        if self._loading:
            return
        p = self.cfg.perf
        p.max_speed_mb = self.speed_spin.value()
        p.cpu_limit = self.cpu_spin.value()
        p.priority = self.priority_combo.currentData()
        p.file_gap_ms = self.gap_spin.value()
        p.debounce_sec = self.debounce_spin.value()
        p.full_scan_minutes = self.full_spin.value()
        p.pause_on_battery = self.battery_check.isChecked()
        p.pause_when_busy = self.busy_check.isChecked()
        p.busy_percent = self.busy_spin.value()
        self.busy_spin.setEnabled(p.pause_when_busy)
        p.profile = p.matching_profile()
        self._loading = True
        self.profile_buttons[p.profile].setChecked(True)
        self._loading = False
        self._save_timer.start()

    def _save(self):
        self.cfg.save()
        self.win.service.apply_performance()

    def poll(self, snap):
        reason = snap["auto_pause_reason"]
        load = sysutil.load_percent()
        self.auto_label.setText(f"目前系統負載：{load:.0f}%" + (f" · 已自動暫停：{reason}" if reason else ""))


# =================================================================== 一般設定分頁

class GeneralTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        g = win.cfg.general
        v = QVBoxLayout(self)

        box = QGroupBox("啟動與背景執行")
        bv = QVBoxLayout(box)
        self.autostart = QCheckBox("登入 macOS 時自動在背景啟動")
        self.autostart.setChecked(sysutil.autostart_enabled())
        self.autostart.toggled.connect(self._autostart)
        bv.addWidget(self.autostart)
        self.close_tray = QCheckBox("關閉視窗時繼續在背景執行（從選單列圖示開啟）")
        self.close_tray.setChecked(g.close_to_tray)
        self.close_tray.toggled.connect(lambda x: self._set("close_to_tray", x))
        bv.addWidget(self.close_tray)
        self.hide_dock = QCheckBox("在背景執行時隱藏 Dock 圖示")
        self.hide_dock.setChecked(g.hide_dock_in_background)
        self.hide_dock.toggled.connect(lambda x: self._set("hide_dock_in_background", x))
        bv.addWidget(self.hide_dock)
        v.addWidget(box)

        box = QGroupBox("通知")
        bv = QVBoxLayout(box)
        self.n_err = QCheckBox("發生錯誤、需要確認時通知")
        self.n_err.setChecked(g.notify_errors)
        self.n_err.toggled.connect(lambda x: self._set("notify_errors", x))
        bv.addWidget(self.n_err)
        self.n_full = QCheckBox("完整同步有變更時通知")
        self.n_full.setChecked(g.notify_full_sync)
        self.n_full.toggled.connect(lambda x: self._set("notify_full_sync", x))
        bv.addWidget(self.n_full)
        self.n_drive = QCheckBox("外接硬碟超過 1 天未連接、沒有備份時提醒（每天一次）")
        self.n_drive.setChecked(g.notify_drive_missing)
        self.n_drive.toggled.connect(lambda x: self._set("notify_drive_missing", x))
        bv.addWidget(self.n_drive)
        v.addWidget(box)

        box = QGroupBox("資料")
        bv = QVBoxLayout(box)
        bv.addWidget(_hint(f"設定與紀錄存放於：{APP_DIR}"))
        h = QHBoxLayout()
        b1 = QPushButton("在 Finder 開啟")
        b1.clicked.connect(lambda: sysutil.reveal(str(APP_DIR)))
        b2 = QPushButton("清除活動紀錄…")
        b2.clicked.connect(self._clear)
        h.addWidget(b1)
        h.addWidget(b2)
        h.addStretch()
        bv.addLayout(h)
        v.addWidget(box)

        box = QGroupBox("注意事項")
        bv = QVBoxLayout(box)
        bv.addWidget(_hint(
            "・第一次備份「文件」、「桌面」、「下載」等資料夾時，macOS 會詢問是否允許存取，請按「允許」。"
            "若沒有出現詢問或備份失敗，請到「系統設定 › 隱私權與安全性 › 完整磁碟取用權限」加入 Python。<br>"
            "・拔除外接硬碟前請先在 Finder 退出。未連接時會自動等待，接回後自動補上這段期間的變更。<br>"
            "・捷徑（符號連結）不會被備份。"))
        v.addWidget(box)
        v.addStretch()

    def _set(self, key, value):
        setattr(self.win.cfg.general, key, value)
        self.win.cfg.save()

    def _autostart(self, on: bool):
        try:
            sysutil.set_autostart(on, self.win.script_path)
            self._set("autostart", on)
        except OSError as e:
            QMessageBox.warning(self, "無法設定", str(e))

    def _clear(self):
        if QMessageBox.question(self, "清除活動紀錄", "確定清除所有活動與同步紀錄？（不影響任何備份檔案）") == QMessageBox.Yes:
            self.win.history.clear()
            self.win.activity_tab.reload()
            self.win.runs_tab.reload()


# =================================================================== 主視窗

class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, service: SyncService, history: History, script_path: str):
        super().__init__()
        self.cfg = cfg
        self.service = service
        self.history = history
        self.script_path = script_path
        self._quitting = False
        self._tray_state = None
        self._today = (0, 0, 0.0)

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(icons.app_icon())
        self.resize(1080, 680)
        self.setMinimumSize(860, 520)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tasks_tab = TasksTab(self)
        self.activity_tab = ActivityTab(self)
        self.runs_tab = RunsTab(self)
        self.perf_tab = PerformanceTab(self)
        self.general_tab = GeneralTab(self)
        self.tabs.addTab(self.tasks_tab, "備份任務")
        self.tabs.addTab(self.activity_tab, "活動紀錄")
        self.tabs.addTab(self.runs_tab, "同步紀錄")
        self.tabs.addTab(self.perf_tab, "效能設定")
        self.tabs.addTab(self.general_tab, "一般設定")
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._build_tray()

        # 視窗顯示時才更新介面，背景時只更新選單列圖示，降低資源使用
        self.ui_timer = QTimer(self, interval=700)
        self.ui_timer.timeout.connect(self.refresh)
        self.tray_timer = QTimer(self, interval=2000)
        self.tray_timer.timeout.connect(self._refresh_tray)
        self.tray_timer.start()
        self._tick = 0

    # ------------------------------------------------------------ 介面建構

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("header")
        frame.setStyleSheet("QFrame#header { background: palette(base); border: 1px solid palette(mid);"
                            " border-radius: 10px; }")
        h = QHBoxLayout(frame)
        h.setContentsMargins(16, 12, 16, 12)

        left = QVBoxLayout()
        self.status_title = QLabel()
        f = self.status_title.font()
        f.setPointSize(f.pointSize() + 5)
        f.setBold(True)
        self.status_title.setFont(f)
        self.status_sub = QLabel()
        self.status_sub.setStyleSheet("color: #888;")
        left.addWidget(self.status_title)
        left.addWidget(self.status_sub)

        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFixedHeight(10)
        self.progress.setTextVisible(False)
        self.speed_label = QLabel()
        self.speed_label.setMinimumWidth(170)
        self.speed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        prog_row.addWidget(self.progress, 1)
        prog_row.addWidget(self.speed_label)
        self.prog_widget = QWidget()
        self.prog_widget.setLayout(prog_row)
        prog_row.setContentsMargins(0, 4, 0, 0)
        left.addWidget(self.prog_widget)
        self.current_file = QLabel()
        self.current_file.setStyleSheet("color: #888;")
        left.addWidget(self.current_file)
        h.addLayout(left, 1)

        self.pause_btn = QPushButton()
        self.pause_btn.setMinimumWidth(110)
        self.pause_btn.clicked.connect(self.toggle_pause)
        sync_btn = QPushButton("⟳ 全部立即同步")
        sync_btn.clicked.connect(self.service.request_all)
        add_btn = QPushButton("＋ 新增備份任務")
        add_btn.setDefault(True)
        add_btn.clicked.connect(self.add_task)
        for b in (self.pause_btn, sync_btn, add_btn):
            b.setMinimumHeight(30)
            h.addWidget(b, 0, Qt.AlignTop)
        return frame

    def _build_tray(self):
        self.tray = QSystemTrayIcon(icons.tray_icon("idle"), self)
        menu = QMenu()
        self.tray_status = QAction("", menu)
        self.tray_status.setEnabled(False)
        self.tray_detail = QAction("", menu)
        self.tray_detail.setEnabled(False)
        menu.addAction(self.tray_status)
        menu.addAction(self.tray_detail)
        menu.addSeparator()
        menu.addAction("開啟主視窗", self.show_and_raise)
        self.tray_pause = menu.addAction("暫停備份", self.toggle_pause)
        menu.addAction("全部立即同步", self.service.request_all)
        menu.addSeparator()
        menu.addAction(f"結束「{APP_DISPLAY_NAME}」", self.quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        self.tray.show()

    # ------------------------------------------------------------ 狀態

    def _overall(self, snap) -> tuple[str, str, str]:
        """回傳 (標題, 選單列狀態, 圖示狀態)"""
        tasks = [t for t in self.cfg.tasks if t.enabled]
        states = snap["tasks"]
        if not self.cfg.tasks:
            return "尚未設定備份任務", "尚未設定備份任務", "idle"
        if snap["user_paused"]:
            return "⏸ 備份已暫停", "已暫停", "paused"
        if snap["auto_pause_reason"]:
            return f"⏸ 自動暫停：{snap['auto_pause_reason']}", f"自動暫停（{snap['auto_pause_reason']}）", "paused"
        if snap["current_task"]:
            t = self.cfg.get_task(snap["current_task"])
            name = t.name if t else ""
            return f"⟳ 正在同步「{name}」", f"正在同步「{name}」", "syncing"
        problems = [t for t in tasks if states.get(t.id, {}).get("status") in ("error", "blocked")]
        if problems:
            return f"⚠ {len(problems)} 個任務需要注意", f"{len(problems)} 個任務需要注意", "error"
        waiting = [t for t in tasks if states.get(t.id, {}).get("status") == "waiting_drive"]
        if waiting and len(waiting) == len(tasks):
            return "⏏ 等待外接硬碟連接", "等待外接硬碟連接", "waiting"
        if not tasks:
            return "所有任務都已停用", "所有任務都已停用", "paused"
        pending = sum(states.get(t.id, {}).get("pending", 0) for t in tasks
                      if states.get(t.id, {}).get("drive_ok"))
        if pending:
            return "● 即時監控中，有變更等待同步", "即時監控中", "idle"
        return "● 即時監控中，所有資料夾都已備份", "即時監控中，全部已備份", "idle"

    def _today_totals(self):
        if time.time() - self._today[2] > 10:
            files, nbytes = self.history.today_totals()
            self._today = (files, nbytes, time.time())
        return self._today[0], self._today[1]

    def refresh(self):
        snap = self.service.snapshot()
        title, _, state = self._overall(snap)
        self.status_title.setText(title)
        color = {"idle": "#1f9d55", "syncing": "#2a72e8", "paused": "#8a8a8a",
                 "error": "#d64545", "waiting": "#d9822b"}.get(state)
        if not self.cfg.tasks:
            color = None
        self.status_title.setStyleSheet(f"color: {color};" if color else "")
        enabled = [t for t in self.cfg.tasks if t.enabled]
        files, nbytes = self._today_totals()
        perf = self.cfg.perf
        sub = (f"{len(enabled)} 個資料夾 · 今日已備份 {files:,} 個檔案（{fmt_size(nbytes)}）"
               f" · 效能：{PROFILE_LABELS.get(perf.profile, '自訂')}")
        self.status_sub.setText(sub)

        prog = snap["progress"]
        syncing = snap["current_task"] is not None
        self.prog_widget.setVisible(syncing)
        self.current_file.setVisible(syncing)
        if syncing:
            if prog["files_total"]:
                self.progress.setRange(0, prog["files_total"])
                self.progress.setValue(prog["files_done"])
            else:
                self.progress.setRange(0, 0)
            speed = snap["speed"]
            parts = []
            if prog["files_total"]:
                parts.append(f"{prog['files_done']:,}/{prog['files_total']:,} 個")
            if speed > 0:
                parts.append(fmt_speed(speed))
                remaining = prog["bytes_total"] - prog["bytes_done"]
                if remaining > 0:
                    parts.append(f"約剩 {fmt_duration(remaining / speed)}")
            self.speed_label.setText(" · ".join(parts))
            cur = prog["current_file"]
            phase = prog["phase"]
            self.current_file.setText(f"{phase}：{cur}" if cur else f"{phase}…")

        self.pause_btn.setText("▶ 繼續" if snap["user_paused"] else "⏸ 暫停")
        self.tasks_tab.refresh(snap)
        idx = self.tabs.currentIndex()
        if idx == 1:
            self.activity_tab.poll()
        elif idx == 2:
            self._tick += 1
            if self._tick % 7 == 0:
                self.runs_tab.reload()
        elif idx == 3:
            self.perf_tab.poll(snap)
        self._refresh_tray(snap)

    def _refresh_tray(self, snap=None):
        snap = snap or self.service.snapshot()
        _, text, state = self._overall(snap)
        if state != self._tray_state:
            self.tray.setIcon(icons.tray_icon(state))
            self._tray_state = state
        self.tray_status.setText(text)
        detail = ""
        if snap["current_task"] and snap["speed"] > 0:
            detail = f"速度 {fmt_speed(snap['speed'])}"
        elif self.cfg.tasks:
            last = max((s.get("last_sync") or 0 for s in snap["tasks"].values()), default=0)
            detail = f"上次備份：{fmt_ago(last)}"
        self.tray_detail.setText(detail)
        self.tray_detail.setVisible(bool(detail))
        self.tray_pause.setText("繼續備份" if snap["user_paused"] else "暫停備份")
        self.tray.setToolTip(f"{APP_DISPLAY_NAME} — {text}")

    def _on_tab(self, idx):
        if idx == 1:
            self.activity_tab.reload_tasks()
            self.activity_tab.reload()
        elif idx == 2:
            self.runs_tab.reload()

    # ------------------------------------------------------------ 動作

    def add_task(self):
        dlg = TaskDialog(self, None, self.cfg.tasks)
        if dlg.exec():
            self.cfg.upsert_task(dlg.task)
            self.service.reload_tasks(changed_ids=[dlg.task.id])
            self.tasks_tab.refresh()
            self.activity_tab.reload_tasks()

    def toggle_pause(self):
        self.service.set_paused(not self.service.user_paused)
        self.refresh() if self.isVisible() else self._refresh_tray()

    def show_and_raise(self):
        sysutil.set_dock_visible(True)
        # Qt 認為已顯示時 show() 不會動作；長時間執行後原生視窗可能已不在畫面上
        # （其他桌面空間、被隱藏），先 hide 再 show 讓 macOS 重新把視窗放到目前畫面
        if self.isVisible():
            self.hide()
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        self.hide()
        if self.cfg.general.hide_dock_in_background:
            sysutil.set_dock_visible(False)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.ui_timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.ui_timer.stop()

    def closeEvent(self, e):
        if self._quitting or not self.cfg.general.close_to_tray:
            self._quitting = True
            e.accept()
            QApplication.quit()
            return
        e.ignore()
        self.hide_to_tray()

    def quit(self):
        self._quitting = True
        self.tray.hide()
        QApplication.quit()
