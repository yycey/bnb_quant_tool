#!/usr/bin/env python3
"""Web 分析桥接 — 触发一轮无 GUI 完整 AI 分析（可选模拟开仓）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

logging.basicConfig(level=logging.WARNING)

import yaml

from bnb_quant_tool.data_localization import init_workspace
from bnb_quant_tool.headless_runner import HeadlessAnalysisRunner

init_workspace(str(PROJECT_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one headless analysis cycle")
    parser.add_argument("--open-paper", action="store_true", help="Open paper position if allowed")
    parser.add_argument("--no-open-paper", action="store_true", help="Analysis only, no paper open")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    args = parser.parse_args()

    cfg_path = PROJECT_DIR / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    open_paper = None
    if args.open_paper:
        open_paper = True
    elif args.no_open_paper:
        open_paper = False

    runner = HeadlessAnalysisRunner(str(PROJECT_DIR), config=cfg)
    result = runner.run_cycle(
        symbol=args.symbol,
        timeframe=args.timeframe,
        open_paper=open_paper,
    )

    out = {
        "ok": bool(result.get("ok")),
        "action": result.get("action"),
        "raw_action": result.get("raw_action"),
        "effective_direction": result.get("effective_direction"),
        "passed_gate": result.get("passed_gate"),
        "confidence": result.get("confidence"),
        "gate_reasons": result.get("gate_reasons") or [],
        "execution_context": result.get("execution_context"),
        "record_id": result.get("record_id"),
        "position_id": result.get("position_id"),
        "price": result.get("price"),
        "symbol": result.get("symbol"),
        "timeframe": result.get("timeframe"),
        "error": result.get("error"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
