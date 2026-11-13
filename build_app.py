"""建立可雙擊開啟的「資料夾同步備份.app」（使用本資料夾的 .venv 執行 app.py）。"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_NAME = "資料夾同步備份"
APP_PATH = os.path.join(HERE, f"{APP_NAME}.app")


def make_icns(dest: str):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from syncbackup.gui.icons import app_pixmap

    app = QApplication.instance() or QApplication([])  # noqa: F841
    base = app_pixmap(1024)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "icon.iconset")
        os.mkdir(iconset)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                base.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(os.path.join(iconset, name))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", dest], check=True)


def main():
    python = os.path.join(HERE, ".venv", "bin", "python")
    if not os.path.exists(python):
        sys.exit("找不到 .venv，請先執行「安裝.command」")
    if os.path.exists(APP_PATH):
        shutil.rmtree(APP_PATH)
    macos = os.path.join(APP_PATH, "Contents", "MacOS")
    resources = os.path.join(APP_PATH, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)

    launcher = os.path.join(macos, "launcher")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/bash\nexec "{python}" "{os.path.join(HERE, "app.py")}" "$@"\n')
    os.chmod(launcher, 0o755)

    make_icns(os.path.join(resources, "AppIcon.icns"))
    with open(os.path.join(APP_PATH, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "com.local.foldersyncbackup",
            "CFBundleExecutable": "launcher",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        }, f)
    print(f"已建立：{APP_PATH}")


if __name__ == "__main__":
    main()
