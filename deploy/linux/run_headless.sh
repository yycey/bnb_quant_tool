#!/usr/bin/env bash
# 服务器 headless 一键启动（前台；生产请用 systemd）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PYTHON="${BNB_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
  else
    PYTHON="python"
  fi
fi

MODE="${1:-both}"  # both | watcher | autopilot | once
shift || true

export PYTHONUNBUFFERED=1
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

mkdir -p "$ROOT/data/logs" "$ROOT/data/locks"

case "$MODE" in
  once)
    exec "$PYTHON" "$ROOT/autopilot_daemon.py" --once --no-embed-watcher "$@"
    ;;
  watcher)
    exec "$PYTHON" "$ROOT/paper_watcher.py" "$@"
    ;;
  autopilot)
    exec "$PYTHON" "$ROOT/autopilot_daemon.py" --no-embed-watcher "$@"
    ;;
  both)
    echo "[run_headless] starting paper_watcher in background…"
    "$PYTHON" "$ROOT/paper_watcher.py" "$@" &
    WPID=$!
    trap 'kill $WPID 2>/dev/null || true' EXIT INT TERM
    echo "[run_headless] starting autopilot (foreground)…"
    "$PYTHON" "$ROOT/autopilot_daemon.py" --no-embed-watcher "$@"
    ;;
  *)
    echo "Usage: $0 {both|watcher|autopilot|once} [extra args]"
    exit 2
    ;;
esac
