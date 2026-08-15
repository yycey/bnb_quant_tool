"""交易 Agent — 模拟盘执行与反馈记录。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bnb_quant_tool.ai_trading_context import (
    should_open_from_advice,
    needs_relaxed_open,
    get_effective_follow_direction,
)
from bnb_quant_tool.config_access import is_margin_insufficient

from .base import Action, MarketContext, RiskVerdict, TradingResult

logger = logging.getLogger(__name__)


class TradingAgent:
    """交易执行员 — 在模拟盘中执行已通过风控的指令。"""

    def __init__(self, paper_engine=None, config: Optional[Dict] = None):
        self.paper_engine = paper_engine
        self.config = config or {}
        cfg = self.config.get("multi_agent", {}).get("trading", {})
        self.auto_execute = bool(cfg.get("auto_execute", True))
        self.start_watcher = bool(cfg.get("start_watcher_on_open", True))
        self.watcher_interval = int(cfg.get("watcher_interval", 15))

    def execute(
        self,
        advice: Dict[str, Any],
        verdict: RiskVerdict,
        context: MarketContext,
        learning_record_id: Optional[int] = None,
        equity_usdt: Optional[float] = None,
    ) -> TradingResult:
        """执行模拟盘开仓（仅当风控批准且门控通过）。"""
        if not self.auto_execute:
            return TradingResult(
                executed=False,
                position_id=None,
                message="交易 Agent 自动执行已关闭",
            )

        if verdict.vetoed or not verdict.approved:
            return TradingResult(
                executed=False,
                position_id=None,
                message=f"风控否决，不执行: {verdict.veto_reason}",
                feedback={"vetoed": True},
            )

        if not self.paper_engine:
            return TradingResult(
                executed=False,
                position_id=None,
                message="模拟盘引擎未初始化",
            )

        effective = get_effective_follow_direction(advice)
        if effective not in ("LONG", "SHORT"):
            return TradingResult(
                executed=False,
                position_id=None,
                message=f"无有效交易方向 ({advice.get('action')}/{advice.get('raw_action')})",
            )

        if not should_open_from_advice(advice, self.config):
            reasons = advice.get("gate_reasons") or ["未通过门控"]
            return TradingResult(
                executed=False,
                position_id=None,
                message=f"门控拦截: {'; '.join(reasons[:2])}",
                feedback={"gate_blocked": True},
            )

        equity = equity_usdt or float(
            (self.config.get("trading") or {}).get("account_balance", 5000.0)
        )
        open_positions = self.paper_engine.get_open_positions()
        margin_required = float(
            ((advice.get("position") or {}).get("margin_required")) or 0
        )
        if is_margin_insufficient(
            equity,
            open_positions,
            margin_required,
            total_realized_pnl=self.paper_engine.get_total_realized_pnl(),
        ):
            state = self.paper_engine.get_margin_state(equity)
            return TradingResult(
                executed=False,
                position_id=None,
                message=(
                    f"保证金不足 (需 {margin_required:.2f}，"
                    f"可用 {state['available_margin']:.2f} USDT)"
                ),
            )

        relaxed = needs_relaxed_open(advice, self.config)

        try:
            pid = self.paper_engine.open_from_advice(
                advice,
                equity_usdt=equity,
                learning_record_id=learning_record_id,
                relaxed=relaxed,
            )
        except Exception as e:
            logger.error(f"交易 Agent 开仓失败: {e}")
            return TradingResult(
                executed=False,
                position_id=None,
                message=f"开仓异常: {e}",
            )

        if not pid:
            return TradingResult(
                executed=False,
                position_id=None,
                message="开仓返回 None（参数不足或方向无效）",
            )

        if self.start_watcher and not self.paper_engine.is_watching():
            try:
                self.paper_engine.start_watcher(interval=self.watcher_interval)
            except Exception as e:
                logger.debug(f"启动 watcher 失败: {e}")

        feedback = {
            "action": effective,
            "entry": (advice.get("prices") or {}).get("entry_mid"),
            "confidence": advice.get("confidence"),
            "equity_usdt": equity,
            "symbol": context.symbol,
            "timeframe": context.timeframe,
        }

        return TradingResult(
            executed=True,
            position_id=pid,
            message=f"模拟盘开仓 #{pid} {effective} @ {context.current_price}",
            feedback=feedback,
        )

    def get_execution_summary(self, result: TradingResult) -> str:
        if result.executed:
            fb = result.feedback
            return (
                f"✅ 交易Agent: 已开仓 #{result.position_id} "
                f"{fb.get('action')} 置信={float(fb.get('confidence', 0)):.0%}"
            )
        return f"⏸️ 交易Agent: {result.message}"
