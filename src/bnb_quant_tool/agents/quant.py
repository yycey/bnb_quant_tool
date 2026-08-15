"""量化 Agent — 技术指标、多周期共振、机构策略、AI 分析。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import (
    Action,
    AgentOpinion,
    AgentRole,
    BaseAgent,
    MarketContext,
    Stance,
    clamp,
    stance_from_score,
)

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    """量化交易员：盯盘、技术指标、寻找买卖点。"""

    role = AgentRole.QUANT

    def __init__(self, config: Optional[Dict] = None):
        cfg = (config or {}).get("multi_agent", {}).get("quant", {})
        self.ai_weight = float(cfg.get("ai_weight", 0.35))
        self.institutional_weight = float(cfg.get("institutional_weight", 0.25))
        self.mtf_weight = float(cfg.get("mtf_weight", 0.25))
        self.technical_weight = float(cfg.get("technical_weight", 0.15))
        self.min_confidence = float(cfg.get("min_confidence", 0.50))

    def analyze(self, context: MarketContext) -> AgentOpinion:
        evidence: List[str] = []
        concerns: List[str] = []
        components: List[tuple] = []

        ai_score, ai_ev, ai_con = self._score_ai(context)
        if ai_ev:
            components.append((ai_score, self.ai_weight))
            evidence.extend(ai_ev)
            concerns.extend(ai_con)

        inst_score, inst_ev, inst_con = self._score_institutional(context)
        if inst_ev:
            components.append((inst_score, self.institutional_weight))
            evidence.extend(inst_ev)
            concerns.extend(inst_con)

        mtf_score, mtf_ev, mtf_con = self._score_mtf(context)
        if mtf_ev:
            components.append((mtf_score, self.mtf_weight))
            evidence.extend(mtf_ev)
            concerns.extend(mtf_con)

        tech_score, tech_ev, tech_con = self._score_technical(context)
        if tech_ev:
            components.append((tech_score, self.technical_weight))
            evidence.extend(tech_ev)
            concerns.extend(tech_con)

        advice = context.trade_advice or {}
        raw_action = (advice.get("raw_action") or advice.get("action") or "WAIT").upper()
        advice_conf = float(advice.get("confidence") or 0.5)

        if not components:
            action = Action.WAIT
            return AgentOpinion(
                role=self.role,
                stance=Stance.NEUTRAL,
                action=action,
                confidence=0.3,
                score=0.0,
                summary="量化信号不足，建议观望",
                evidence=["缺少 AI/机构/多周期/技术指标数据"],
            )

        total_weight = sum(w for _, w in components)
        score = clamp(sum(s * w for s, w in components) / total_weight)
        stance = stance_from_score(score)

        if raw_action in ("LONG", "SHORT"):
            action = Action(raw_action)
        else:
            action = Action.WAIT if abs(score) < 0.12 else (
                Action.LONG if score > 0 else Action.SHORT
            )

        avg_abs = sum(abs(s) for s, _ in components) / len(components)
        confidence = clamp(
            advice_conf * 0.4 + avg_abs * 0.4 + (0.2 if action != Action.WAIT else 0.0),
            0.0, 1.0,
        )

        if action != Action.WAIT and confidence < self.min_confidence:
            concerns.append(f"量化置信度 {confidence:.0%} 低于门槛 {self.min_confidence:.0%}")

        regime = (context.market_regime or {}).get("regime", "")
        summary = (
            f"量化综合 {len(components)} 维信号 → {action.value} "
            f"(score={score:+.2f}, 置信={confidence:.0%})"
        )
        if regime:
            evidence.append(f"市场状态: {regime}")

        return AgentOpinion(
            role=self.role,
            stance=stance,
            action=action,
            confidence=confidence,
            score=score,
            summary=summary,
            evidence=evidence[:10],
            concerns=concerns[:5],
            metadata={
                "raw_action": raw_action,
                "advice_confidence": advice_conf,
                "rr_ratio": advice.get("risk_reward_ratio"),
            },
        )

    def _score_ai(self, ctx: MarketContext) -> tuple:
        ai = ctx.ai_analysis or {}
        if not ai:
            return 0.0, [], []

        signal = (ai.get("signal") or "").upper()
        conf = float(ai.get("confidence") or 0.5)

        score = 0.0
        if signal in ("BUY", "买入", "LONG"):
            score = conf
        elif signal in ("SELL", "卖出", "SHORT"):
            score = -conf
        elif signal in ("HOLD", "持有", "WAIT"):
            score = 0.0

        evidence = [f"DeepSeek AI: {ai.get('signal', '?')} (置信 {conf:.0%})"]
        reasoning = ai.get("reasoning") or ai.get("analysis")
        if reasoning:
            evidence.append(str(reasoning)[:120])

        concerns = []
        if conf < 0.55:
            concerns.append(f"AI 置信度偏低 ({conf:.0%})")

        return score, evidence, concerns

    def _score_institutional(self, ctx: MarketContext) -> tuple:
        inst = ctx.institutional or {}
        if not inst:
            return 0.0, [], []

        buy = int(inst.get("buy_signals") or 0)
        sell = int(inst.get("sell_signals") or 0)
        hold = int(inst.get("hold_signals") or 0)
        total = buy + sell + hold or 1

        score = clamp((buy - sell) / total)
        consensus = inst.get("consensus_signal") or inst.get("signal") or "HOLD"

        evidence = [
            f"13机构策略: BUY={buy} SELL={sell} HOLD={hold}",
            f"共识: {consensus}",
        ]
        if inst.get("weighted_voting"):
            evidence.append("已启用学习权重加权投票")

        concerns = []
        if buy > 0 and sell > 0 and abs(buy - sell) <= 1:
            concerns.append("机构策略多空分歧严重")

        return score, evidence, concerns

    def _score_mtf(self, ctx: MarketContext) -> tuple:
        mtf = ctx.multi_timeframe or {}
        if not mtf:
            return 0.0, [], []

        confluence = (mtf.get("confluence") or "").lower()
        rec = (mtf.get("recommended_action") or "WAIT").upper()
        strength = float(mtf.get("strength") or mtf.get("confluence_score") or 0.5)

        score = 0.0
        if rec == "LONG" or confluence in ("bullish", "long", "buy"):
            score = strength
        elif rec == "SHORT" or confluence in ("bearish", "short", "sell"):
            score = -strength

        evidence = [
            f"多周期共振: {mtf.get('confluence', '?')} → {rec}",
        ]
        tf_signals = mtf.get("timeframe_signals") or mtf.get("signals")
        if isinstance(tf_signals, dict):
            for tf, sig in list(tf_signals.items())[:4]:
                evidence.append(f"  {tf}: {sig}")

        concerns = []
        if "mixed" in confluence or "neutral" in confluence:
            concerns.append("多周期未形成一致共振")

        return score, evidence, concerns

    def _score_technical(self, ctx: MarketContext) -> tuple:
        ind = ctx.indicators or {}
        if not ind:
            return 0.0, [], []

        score_parts: List[float] = []
        evidence: List[str] = []
        concerns: List[str] = []

        rsi = ind.get("RSI")
        if rsi is not None:
            rsi = float(rsi)
            if rsi < 30:
                score_parts.append(0.6)
                evidence.append(f"RSI 超卖 ({rsi:.1f})")
            elif rsi > 70:
                score_parts.append(-0.6)
                evidence.append(f"RSI 超买 ({rsi:.1f})")
            else:
                score_parts.append((50 - rsi) / 100)
                evidence.append(f"RSI={rsi:.1f}")

        macd = ind.get("MACD")
        macd_sig = ind.get("MACD_Signal") or ind.get("MACD_signal")
        if macd is not None and macd_sig is not None:
            diff = float(macd) - float(macd_sig)
            score_parts.append(clamp(diff / max(abs(float(macd)), 1e-6)))
            evidence.append(f"MACD {'金叉' if diff > 0 else '死叉'}")

        bb_pos = ind.get("BB_Position") or ind.get("bb_position")
        if bb_pos is not None:
            bp = float(bb_pos)
            if bp < 0.1:
                score_parts.append(0.4)
                evidence.append("价格触及布林下轨")
            elif bp > 0.9:
                score_parts.append(-0.4)
                evidence.append("价格触及布林上轨")

        if not score_parts:
            return 0.0, [], []

        score = clamp(sum(score_parts) / len(score_parts))
        atr = ind.get("ATR")
        if atr and ctx.current_price:
            vol_pct = float(atr) / float(ctx.current_price)
            if vol_pct > 0.04:
                concerns.append(f"波动率偏高 (ATR/Price={vol_pct:.1%})")

        return score, evidence, concerns
