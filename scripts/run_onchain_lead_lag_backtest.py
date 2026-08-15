#!/usr/bin/env python3
"""每日凌晨跑：链上 lead-lag 最优窗口回测 → 写入配置表（次日生效）。

示例（Windows 任务计划 / cron）:
  python scripts/run_onchain_lead_lag_backtest.py --config config.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bnb_quant_tool.onchain_lead_lag_backtest import main

if __name__ == "__main__":
    raise SystemExit(main())
