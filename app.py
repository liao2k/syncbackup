"""資料夾同步備份：程式進入點。

    python app.py            開啟主視窗
    python app.py --hidden   只在選單列背景執行（登入自動啟動時使用）
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from syncbackup import sysutil  # noqa: E402
from syncbackup.config import APP_DIR, APP_DISPLAY_NAME, HISTORY_PATH, Config  # noqa: E402
from syncbackup.gui import icons  # noqa: E402
from syncbackup.gui.main_window import MainWindow  # noqa: E402
from syncbackup.history import History  # noqa: E402
from syncbackup.service import SyncService  # noqa: E402

SERVER_NAME = "FolderSyncBackup-single-instance"


def main() -> int:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=APP_DIR / "app.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

    hidden = "--hidden" in sys.argv
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setWindowIcon(icons.app_icon())
    app.setQuitOnLastWindowClosed(False)

    # 只允許執行一份：已在執行時，通知它顯示視窗後結束
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(500):
        if not hidden:
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(500)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    cfg = Config.load()
    history = History(HISTORY_PATH)
    service = SyncService(cfg, history)
    win = MainWindow(cfg, service, history, os.path.abspath(__file__))

    def on_connection():
        conn = server.nextPendingConnection()
        if conn:
            conn.readyRead.connect(lambda: conn.readAll() and win.show_and_raise())
    server.newConnection.connect(on_connection)

    service.start()
    if hidden and cfg.tasks:
        win.hide_to_tray()
    else:
        win.show_and_raise()

    app.aboutToQuit.connect(service.stop)
    rc = app.exec()
    sysutil.set_dock_visible(True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
