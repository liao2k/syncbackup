"""新增 / 編輯備份任務的對話框。"""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from .. import sysutil
from ..config import DEFAULT_EXCLUDES, MODE_ACCUMULATE, MODE_MIRROR, Task
from .common import fmt_size


def _is_inside(child: str, parent: str) -> bool:
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


class TaskDialog(QDialog):
    def __init__(self, parent, task: Task | None, other_tasks: list[Task]):
        super().__init__(parent)
        self.is_new = task is None
        self.task = copy.deepcopy(task) if task else Task()
        self.other_tasks = other_tasks
        self.setWindowTitle("新增備份任務" if self.is_new else f"編輯備份任務：{self.task.name}")
        self.setMinimumWidth(680)
        self._build()
        self._load()

    # ------------------------------------------------------------ 介面

    def _build(self):
        root = QVBoxLayout(self)

        # 基本設定
        box = QGroupBox("基本設定")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：工作文件")
        form.addRow("任務名稱：", self.name_edit)

        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("要備份的資料夾")
        src_btn = QPushButton("選擇…")
        src_btn.clicked.connect(self._pick_source)
        form.addRow("來源資料夾：", self._row(self.src_edit, src_btn))

        self.drive_combo = QComboBox()
        self.drive_combo.addItem("快速選擇外接硬碟…", None)
        for vol in sysutil.external_volumes():
            free = sysutil.free_space(vol)
            self.drive_combo.addItem(f"{os.path.basename(vol)}（剩餘 {fmt_size(free)}）", vol)
        self.drive_combo.activated.connect(self._pick_drive)
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("外接硬碟上的備份資料夾")
        self.dest_edit.textChanged.connect(self._update_dest_hint)
        dest_btn = QPushButton("選擇…")
        dest_btn.clicked.connect(self._pick_dest)
        form.addRow("備份位置：", self._row(self.dest_edit, dest_btn))
        form.addRow("", self.drive_combo)
        self.dest_hint = QLabel()
        self.dest_hint.setWordWrap(True)
        self.dest_hint.setStyleSheet("color: #888;")
        form.addRow("", self.dest_hint)
        root.addWidget(box)

        # 同步方式
        box = QGroupBox("同步方式")
        v = QVBoxLayout(box)
        self.mode_group = QButtonGroup(self)
        self.mirror_radio = QRadioButton("鏡像同步：備份與來源完全一致")
        self.acc_radio = QRadioButton("累積備份：只新增、更新，永不刪除備份中的檔案")
        self.mode_group.addButton(self.mirror_radio)
        self.mode_group.addButton(self.acc_radio)
        v.addWidget(self.mirror_radio)
        v.addWidget(self._hint("來源刪除的檔案，備份中也會刪除（可搭配「保留歷史版本」避免誤刪）。"))
        v.addWidget(self.acc_radio)
        v.addWidget(self._hint("適合「只想留存、不想丟失任何檔案」的資料夾，例如照片。"))

        self.realtime_check = QCheckBox("即時監控：檔案一有變動就自動備份")
        v.addWidget(self.realtime_check)
        v.addWidget(self._hint("關閉後只會依「效能設定」中的定期完整檢查時間備份。"))
        root.addWidget(box)

        # 安全
        box = QGroupBox("安全保護")
        v = QVBoxLayout(box)
        h = QHBoxLayout()
        self.versions_check = QCheckBox("保留歷史版本：被覆蓋或刪除的檔案移到「_備份歷史版本」資料夾，保留")
        self.days_spin = QSpinBox()
        self.days_spin.setRange(0, 3650)
        self.days_spin.setSuffix(" 天")
        self.days_spin.setSpecialValueText("永久")
        self.versions_check.toggled.connect(self.days_spin.setEnabled)
        h.addWidget(self.versions_check)
        h.addWidget(self.days_spin)
        h.addStretch()
        v.addLayout(h)
        self.guard_check = QCheckBox("大量刪除保護：一次要刪除超過 30% 的備份檔案時，先暫停並詢問")
        v.addWidget(self.guard_check)
        v.addWidget(self._hint("例如來源資料夾被誤刪、改名或硬碟未掛載時，避免把備份一起清空。"))
        root.addWidget(box)

        # 排除
        box = QGroupBox("排除規則（每行一條，不分大小寫）")
        v = QVBoxLayout(box)
        v.addWidget(self._hint("「*.tmp」比對檔名；「node_modules」排除整個資料夾；含「/」時比對相對路徑，例如「專案/build」。"))
        self.excludes_edit = QPlainTextEdit()
        self.excludes_edit.setMinimumHeight(110)
        v.addWidget(self.excludes_edit)
        h = QHBoxLayout()
        h.addStretch()
        reset = QPushButton("恢復預設規則")
        reset.clicked.connect(lambda: self.excludes_edit.setPlainText("\n".join(DEFAULT_EXCLUDES)))
        h.addWidget(reset)
        v.addLayout(h)
        root.addWidget(box)

        self.enabled_check = QCheckBox("啟用此任務")
        root.addWidget(self.enabled_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("儲存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _row(*widgets) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x)
        return w

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #888; margin-left: 22px;")
        return lbl

    def _load(self):
        t = self.task
        self.name_edit.setText(t.name)
        self.src_edit.setText(t.source)
        self.dest_edit.setText(t.dest)
        (self.acc_radio if t.mode == MODE_ACCUMULATE else self.mirror_radio).setChecked(True)
        self.realtime_check.setChecked(t.realtime)
        self.versions_check.setChecked(t.keep_versions)
        self.days_spin.setValue(t.version_days)
        self.days_spin.setEnabled(t.keep_versions)
        self.guard_check.setChecked(t.mass_delete_guard)
        self.excludes_edit.setPlainText("\n".join(t.excludes))
        self.enabled_check.setChecked(t.enabled)
        self._update_dest_hint()

    # ------------------------------------------------------------ 動作

    def _pick_source(self):
        path = QFileDialog.getExistingDirectory(self, "選擇要備份的資料夾", self.src_edit.text() or os.path.expanduser("~"))
        if path:
            self.src_edit.setText(path)
            if not self.name_edit.text().strip():
                self.name_edit.setText(os.path.basename(path.rstrip("/")) or path)

    def _pick_dest(self):
        start = self.dest_edit.text() or "/Volumes"
        path = QFileDialog.getExistingDirectory(self, "選擇備份位置", start)
        if path:
            self.dest_edit.setText(path)

    def _pick_drive(self, index: int):
        vol = self.drive_combo.itemData(index)
        if not vol:
            return
        name = self.name_edit.text().strip() or os.path.basename(self.src_edit.text().rstrip("/")) or "備份"
        self.dest_edit.setText(os.path.join(vol, "資料夾備份", name))
        self.drive_combo.setCurrentIndex(0)

    def _update_dest_hint(self):
        dest = self.dest_edit.text().strip()
        if not dest:
            self.dest_hint.setText("")
            return
        vol = sysutil.volume_of(dest)
        if vol:
            if os.path.ismount(vol):
                self.dest_hint.setText(f"✓ 外接硬碟「{os.path.basename(vol)}」已連接，剩餘 {fmt_size(sysutil.free_space(vol))}")
            else:
                self.dest_hint.setText(f"外接硬碟「{os.path.basename(vol)}」目前未連接，連接後會自動開始備份")
        else:
            self.dest_hint.setText("⚠ 此位置不在外接硬碟上（不在 /Volumes 底下）")

    def _accept(self):
        name = self.name_edit.text().strip()
        src = os.path.abspath(os.path.expanduser(self.src_edit.text().strip())) if self.src_edit.text().strip() else ""
        dest = os.path.abspath(os.path.expanduser(self.dest_edit.text().strip())) if self.dest_edit.text().strip() else ""

        def fail(msg):
            QMessageBox.warning(self, "無法儲存", msg)

        if not name:
            return fail("請輸入任務名稱。")
        if not src or not os.path.isdir(src):
            return fail("來源資料夾不存在，請重新選擇。")
        if not dest:
            return fail("請選擇備份位置。")
        if _is_inside(dest, src):
            return fail("備份位置不能在來源資料夾裡面，否則會無限循環備份。")
        if _is_inside(src, dest):
            return fail("來源資料夾不能在備份位置裡面。")
        for t in self.other_tasks:
            if t.id == self.task.id:
                continue
            if _is_inside(dest, t.dest) or _is_inside(t.dest, dest):
                return fail(f"備份位置與任務「{t.name}」重疊。\n每個任務需要使用各自獨立的備份資料夾，否則鏡像同步時會互相刪除檔案。")
        if not sysutil.volume_of(dest):
            r = QMessageBox.question(self, "確認備份位置",
                                     "備份位置不在外接硬碟上。\n\n確定要備份到這台電腦的內建硬碟嗎？")
            if r != QMessageBox.Yes:
                return
        if self.mirror_radio.isChecked() and os.path.isdir(dest):
            try:
                existing = [n for n in os.listdir(dest) if not n.startswith(".")]
            except OSError:
                existing = []
            src_names = set(os.listdir(src))
            foreign = [n for n in existing if n not in src_names and n != "_備份歷史版本"]
            if foreign and self.is_new:
                r = QMessageBox.question(
                    self, "備份位置已有其他檔案",
                    f"備份位置中有 {len(foreign)} 個來源資料夾沒有的項目（例如「{foreign[0]}」）。\n\n"
                    "鏡像同步會把它們移除（若有保留歷史版本，會移到「_備份歷史版本」）。\n確定使用這個位置嗎？")
                if r != QMessageBox.Yes:
                    return

        t = self.task
        t.name = name
        t.source = src
        t.dest = dest
        t.mode = MODE_ACCUMULATE if self.acc_radio.isChecked() else MODE_MIRROR
        t.realtime = self.realtime_check.isChecked()
        t.keep_versions = self.versions_check.isChecked()
        t.version_days = self.days_spin.value()
        t.mass_delete_guard = self.guard_check.isChecked()
        t.excludes = [l.strip() for l in self.excludes_edit.toPlainText().splitlines() if l.strip()]
        t.enabled = self.enabled_check.isChecked()
        self.accept()
