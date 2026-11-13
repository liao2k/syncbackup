#!/bin/bash
# 雙擊執行：建立 Python 虛擬環境、安裝套件，並產生「資料夾同步備份.app」
set -e
cd "$(dirname "$0")"

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "找不到 python3，請先安裝 Python 3（例如：brew install python）"
  exit 1
fi

echo "▸ 建立虛擬環境…"
[ -d .venv ] || "$PY" -m venv .venv
echo "▸ 安裝套件（PySide6、watchdog）…"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "▸ 建立應用程式…"
.venv/bin/python build_app.py
echo ""
echo "✓ 完成！雙擊「資料夾同步備份.app」即可開啟。"
