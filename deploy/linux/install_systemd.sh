#!/usr/bin/env bash
# 安装 systemd 双服务（需 root）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
APP_USER="${APP_USER:-bnb}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/bnb_quant_tool}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 运行: sudo $0"
  exit 1
fi

if [[ ! -d "$INSTALL_ROOT" ]]; then
  echo "INSTALL_ROOT=$INSTALL_ROOT 不存在。"
  echo "请先: sudo mkdir -p $INSTALL_ROOT && sudo rsync -a --exclude .venv $ROOT/ $INSTALL_ROOT/"
  exit 1
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$INSTALL_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$INSTALL_ROOT"

sed "s|/opt/bnb_quant_tool|$INSTALL_ROOT|g; s|User=bnb|User=$APP_USER|g; s|Group=bnb|Group=$APP_USER|g" \
  "$ROOT/deploy/linux/bnb-watcher.service" > "$UNIT_DIR/bnb-watcher.service"
sed "s|/opt/bnb_quant_tool|$INSTALL_ROOT|g; s|User=bnb|User=$APP_USER|g; s|Group=bnb|Group=$APP_USER|g" \
  "$ROOT/deploy/linux/bnb-autopilot.service" > "$UNIT_DIR/bnb-autopilot.service"

systemctl daemon-reload
systemctl enable bnb-watcher.service bnb-autopilot.service
systemctl restart bnb-watcher.service
systemctl restart bnb-autopilot.service
systemctl --no-pager --full status bnb-watcher.service bnb-autopilot.service || true

echo ""
echo "已安装。常用命令:"
echo "  journalctl -u bnb-autopilot -f"
echo "  journalctl -u bnb-watcher -f"
echo "  tail -f $INSTALL_ROOT/data/logs/autopilot.log"
