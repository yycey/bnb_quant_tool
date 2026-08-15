#!/usr/bin/env python3
"""Web 平仓桥接 — 走 PaperTradingEngine 完整闭环（学习反馈/信号回填）"""

import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

import yaml

from bnb_quant_tool.data_localization import init_workspace
from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.config_access import build_data_fetcher, build_trade_advisor_config
from bnb_quant_tool.paper_trading import PaperTradingEngine
from bnb_quant_tool.trade_advisor import TradeAdvisor

logging.basicConfig(level=logging.WARNING, force=True)
init_workspace(str(PROJECT_DIR))


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: close_position.py <pid> [price]"}))
        return 1

    pid = int(sys.argv[1])
    price = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 0.0

    cfg_path = PROJECT_DIR / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    pt_cfg = cfg.get("paper_trading", {}) or {}
    fetcher = build_data_fetcher(cfg)

    engine = PaperTradingEngine(db_path=pt_cfg.get("db_path"), config=cfg)
    engine.set_learner(AILearningSystem(config=cfg))
    engine.set_trade_advisor(TradeAdvisor(build_trade_advisor_config(cfg)))

    row_before = engine._get_position_row(pid)
    if not row_before:
        print(json.dumps({"ok": False, "error": "仓位不存在或已平"}))
        return 1

    # 必须用持仓自身的品种取价，禁止用配置默认 symbol（多品种错价）
    symbol = (row_before.get("symbol") or cfg.get("trading", {}).get("symbol", "BNBUSDT")).upper()
    if price <= 0:
        price = float(fetcher.resolve_current_price(symbol) or 0)
    if price <= 0:
        print(json.dumps({"ok": False, "error": f"无法获取 {symbol} 市价"}))
        return 1

    prev_realized = float(row_before.get("realized_pnl_usdt") or 0)

    ok = engine.close_manual(pid, price=price, reason="MANUAL_WEB")
    if not ok:
        print(json.dumps({"ok": False, "error": "仓位不存在或已平"}))
        return 1

    row = engine._get_position_row(pid) or {}
    pnl = round(float(row.get("realized_pnl_usdt") or 0) - prev_realized, 4)
    print(json.dumps({
        "ok": True,
        "id": pid,
        "symbol": symbol,
        "price": price,
        "pnl": pnl,
        "r_multiple": row.get("r_multiple"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
