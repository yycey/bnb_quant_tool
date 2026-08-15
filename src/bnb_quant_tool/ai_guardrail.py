"""
AI 决策对齐校验与幻觉拦截 (AI Guardrail)
==========================================
在 TradeAdvisor 融合层前，硬性校验 DeepSeek 输出是否与传统指标 / 风控底线冲突。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTION_LONG = "LONG"
ACTION_SHORT = "SHORT"
ACTION_WAIT = "WAIT"


class AIGuardrail:
    """AI 建议与量化风控底线的硬性对齐校验。"""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.block_on_conflict = bool(cfg.get("block_on_conflict", True))
        self.min_mtf_agreement = float(cfg.get("min_mtf_agreement", 0.55))
        self.high_vol_atr_pct = float(cfg.get("high_vol_atr_pct", 0.035))
        self.extreme_vol_atr_pct = float(cfg.get("extreme_vol_atr_pct", 0.050))
        self.inst_bearish_threshold = int(cfg.get("inst_bearish_threshold", 8))
        self.inst_bullish_threshold = int(cfg.get("inst_bullish_threshold", 8))
        self.ai_confidence_override_min = float(cfg.get("ai_confidence_override_min", 0.85))

    def validate(
        self,
        ai_analysis: Dict,
        proposed_action: str,
        indicators: Optional[Dict] = None,
        institutional: Optional[Dict] = None,
        multi_timeframe: Optional[Dict] = None,
        market_regime: Optional[Dict] = None,
        news_summary: Optional[Dict] = None,
        current_price: float = 0.0,
    ) -> Dict[str, Any]:
        """校验 AI 方向与硬性指标是否冲突。"""
        if not self.enabled or proposed_action == ACTION_WAIT:
            return self._pass_result(proposed_action)

        indicators = indicators or {}
        institutional = institutional or {}
        multi_timeframe = multi_timeframe or {}
        market_regime = market_regime or {}
        news_summary = news_summary or {}

        ai_dir = self._ai_direction(ai_analysis)
        ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)
        conflicts: List[str] = []
        severity = 0.0

        # 1. 多周期共振严重背离
        mtf_action = (multi_timeframe.get("recommended_action") or "").upper()
        mtf_score = abs(float(multi_timeframe.get("weighted_score") or 0))
        if mtf_action and mtf_action != "WAIT" and mtf_action != proposed_action:
            if mtf_score >= self.min_mtf_agreement:
                conflicts.append(
                    f"AI/投票建议 {proposed_action} 与多周期 {mtf_action} 严重背离 (得分 {mtf_score:.2f})"
                )
                severity += 0.35

        # 2. ATR 极高 + 做多
        atr = self._safe_float(indicators.get("ATR"), 0)
        if current_price > 0 and atr > 0 and proposed_action == ACTION_LONG:
            vol_pct = atr / current_price
            if vol_pct >= self.extreme_vol_atr_pct:
                conflicts.append(
                    f"波动率极高 ATR/Price={vol_pct:.2%}，禁止高置信追多"
                )
                severity += 0.45
            elif vol_pct >= self.high_vol_atr_pct and ai_conf >= 0.75:
                conflicts.append(
                    f"高波动 {vol_pct:.2%} 下 AI 仍强烈看多 (conf={ai_conf:.0%})"
                )
                severity += 0.25

        # 3. 机构策略全面看空但 AI 做多
        buy_sig = int(institutional.get("buy_signals") or 0)
        sell_sig = int(institutional.get("sell_signals") or 0)
        if proposed_action == ACTION_LONG and sell_sig >= self.inst_bearish_threshold and buy_sig <= 2:
            if ai_dir == ACTION_LONG and ai_conf < self.ai_confidence_override_min:
                conflicts.append(
                    f"13 策略 {sell_sig} 票看空 vs {buy_sig} 票看多，AI 仍建议做多"
                )
                severity += 0.30
        if proposed_action == ACTION_SHORT and buy_sig >= self.inst_bullish_threshold and sell_sig <= 2:
            if ai_dir == ACTION_SHORT and ai_conf < self.ai_confidence_override_min:
                conflicts.append(
                    f"13 策略 {buy_sig} 票看多 vs {sell_sig} 票看空，AI 仍建议做空"
                )
                severity += 0.30

        # 5. PANIC 状态 + 做多
        regime = (market_regime.get("regime") or "").upper()
        if regime == "PANIC" and proposed_action == ACTION_LONG:
            conflicts.append("市场状态 PANIC，AI 建议做多与风控冲突")
            severity += 0.40

        # 6. 新闻极端利空 + AI 做多
        news_pol = (news_summary.get("polarity") or news_summary.get("sentiment") or "").lower()
        news_conf = self._safe_float(news_summary.get("confidence"), 0)
        if news_pol in ("bearish", "negative", "利空") and news_conf >= 0.6:
            if proposed_action == ACTION_LONG and ai_conf < self.ai_confidence_override_min:
                conflicts.append("新闻极端利空但 AI 建议做多（疑似幻觉/过度解读）")
                severity += 0.35

        blocked = bool(conflicts) and self.block_on_conflict and severity >= 0.30
        final_action = ACTION_WAIT if blocked else proposed_action

        if blocked:
            logger.warning("AI Guardrail 拦截: %s", " | ".join(conflicts))

        return {
            "passed": not blocked,
            "blocked": blocked,
            "original_action": proposed_action,
            "final_action": final_action,
            "conflicts": conflicts,
            "severity": round(min(1.0, severity), 3),
            "interpretation": (
                "AI 决策与风控底线冲突，已强制 WAIT" if blocked
                else ("轻微背离已记录" if conflicts else "AI 与硬性指标对齐")
            ),
        }

    @staticmethod
    def _ai_direction(ai_analysis: Dict) -> str:
        sig = (ai_analysis.get("signal") or "").upper()
        sig_cn = ai_analysis.get("signal") or ""
        if sig_cn in ("买入",) or sig in ("BUY", "LONG"):
            return ACTION_LONG
        if sig_cn in ("卖出",) or sig in ("SELL", "SHORT"):
            return ACTION_SHORT
        return ACTION_WAIT

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        try:
            if val is None:
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _pass_result(action: str) -> Dict:
        return {
            "passed": True,
            "blocked": False,
            "original_action": action,
            "final_action": action,
            "conflicts": [],
            "severity": 0.0,
            "interpretation": "Guardrail 未启用或无需校验",
        }

    @classmethod
    def format_for_prompt(cls, result: Dict) -> str:
        if not result or result.get("passed"):
            return ""
        lines = ["\n【AI Guardrail — 幻觉拦截】"]
        for c in result.get("conflicts") or []:
            lines.append(f"- ⚠ {c}")
        lines.append(f"- 原方向 {result.get('original_action')} → 强制 {result.get('final_action')}")
        lines.append("")
        return "\n".join(lines)
