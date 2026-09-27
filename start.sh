#!/usr/bin/env bash
# Kaus local launcher
set -euo pipefail

cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3.11 || command -v python3)"

echo "启动 Kaus → http://127.0.0.1:8877/new"
echo "（Ctrl+C 停止）"
exec "$PY" server.py
