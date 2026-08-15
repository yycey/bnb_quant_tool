#!/bin/bash
# BNB量化交易工具 - Linux/macOS 启动
# 用法:
#   ./launch.sh              # GUI（需显示器 + tkinter）
#   ./launch.sh headless     # 服务器双进程（watcher + autopilot）
#   ./launch.sh once         # 单轮分析冒烟
#   ./launch.sh watcher      # 仅 watcher
#   ./launch.sh autopilot    # 仅 autopilot

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MODE="${1:-gui}"
if [[ $# -gt 0 ]]; then shift; fi

if command -v python3 &>/dev/null; then
  PYTHON_CMD="python3"
else
  PYTHON_CMD="python"
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_CMD="$ROOT/.venv/bin/python"
fi

case "$MODE" in
  gui|"")
    if [[ ! -f "gui.py" ]]; then
      echo "[错误] 未找到 gui.py"
      exit 1
    fi
    echo "启动 GUI…"
    exec "$PYTHON_CMD" gui.py "$@"
    ;;
  headless|both)
    exec bash "$ROOT/deploy/linux/run_headless.sh" both "$@"
    ;;
  watcher|autopilot|once)
    exec bash "$ROOT/deploy/linux/run_headless.sh" "$MODE" "$@"
    ;;
  *)
    echo "用法: $0 {gui|headless|once|watcher|autopilot}"
    exit 2
    ;;
esac
