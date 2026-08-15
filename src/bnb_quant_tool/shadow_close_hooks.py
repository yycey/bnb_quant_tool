"""
影子策略平仓挂钩 — 纸面平仓后给活跃影子策略记账，推动验证/晋升。

独立模块，避免继续膨胀 paper_trading / strategy_mutator。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def record_paper_close_on_shadows(
    *,
    pnl: float,
    win: bool,
    config: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
    learning_db_path: Optional[str] = None,
    market_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将一笔纸面平仓记入所有 active 影子策略。

    影子策略暂无独立执行通道，用同期纸面结果做样本验证（与变异队列配套）。
    """
    tl = (config or {}).get("training_loop") or {}
    if not bool(tl.get("enabled", True)):
        return {"ok": True, "skipped": True, "reason": "training_loop_disabled"}
    if not bool(tl.get("shadow_record_on_close", True)):
        return {"ok": True, "skipped": True, "reason": "shadow_record_disabled"}

    try:
        from bnb_quant_tool.strategy_mutator import StrategyMutator
        from bnb_quant_tool.data_localization import get_localized_db_path

        root = Path(__file__).resolve().parents[2]
        cfg_path = config_path or str(root / "config.yaml")
        db = learning_db_path or str(get_localized_db_path("ai_learning"))
        mutator = StrategyMutator(config_path=cfg_path, learning_db_path=db)
        active = mutator.list_shadow_strategies(status="active") or []
        if not active:
            return {"ok": True, "recorded": 0}

        meta = dict(market_data or {})
        meta.setdefault("source", "paper_close")
        recorded = 0
        for row in active:
            sid = row.get("strategy_id")
            if not sid:
                continue
            try:
                mutator.record_shadow_trade(
                    str(sid),
                    float(pnl),
                    bool(win),
                    market_data=meta,
                )
                recorded += 1
            except Exception as e:
                logger.debug("shadow record %s: %s", sid, e)

        if recorded:
            logger.info("影子策略记账: %s 个 active ← pnl=%.4f win=%s", recorded, pnl, win)
        return {"ok": True, "recorded": recorded}
    except Exception as e:
        logger.warning("shadow close hook failed: %s", e)
        return {"ok": False, "error": str(e)}
