#!/usr/bin/env python3
"""Web API — 学习仪表盘快照（胜率优化 + 亏损模式 + 盈利曲线）"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.WARNING, force=True)

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

import yaml

from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.data_localization import init_workspace
from bnb_quant_tool.learning_analytics import build_learning_dashboard_snapshot

init_workspace(str(PROJECT_DIR))


def load_config() -> dict:
    cfg_path = PROJECT_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    cfg = load_config()
    learner = AILearningSystem(config=cfg)
    paper_engine = None
    try:
        from bnb_quant_tool.paper_trading import PaperTradingEngine

        paper_engine = PaperTradingEngine(config=cfg)
    except Exception:
        pass

    pattern_memory = None
    try:
        from bnb_quant_tool.pattern_memory import PatternMemory

        pattern_memory = PatternMemory()
    except Exception:
        pass

    snap = build_learning_dashboard_snapshot(
        learner,
        paper_engine=paper_engine,
        pattern_memory=pattern_memory,
        config=cfg,
    )
    print(json.dumps({"ok": True, **snap}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
