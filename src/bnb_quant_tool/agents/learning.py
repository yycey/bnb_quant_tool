"""学习 Agent — 读取知识库、因子归因、模拟盘绩效，建议保守/激进。"""

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
    action_from_stance,
    clamp,
    stance_from_score,
)

logger = logging.getLogger(__name__)


class LearningAgent(BaseAgent):
    """进化学习专家：基于历史学习成果调节风险姿态。"""

    role = AgentRole.LEARNING

    def __init__(self, config: Optional[Dict] = None):
        cfg = (config or {}).get("multi_agent", {}).get("learning", {})
        self.min_confidence = float(cfg.get("min_confidence", 0.4))

    def analyze(self, context: MarketContext) -> AgentOpinion:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0

        li = context.learning_insights or {}
        growth = li.get("growth") or {}
        dims = growth.get("capability_dimensions") or {}
        paper = li.get("paper_trading") or {}

        pred_acc = int(dims.get("prediction_accuracy") or 0)
        discipline = int(dims.get("discipline") or 0)
        knowledge_q = int(dims.get("knowledge_quality") or 0)
        feedback_n = int(growth.get("feedback_count") or li.get("total_feedbacks") or 0)

        if feedback_n < 10:
            score -= 0.15
            concerns.append(f"学习样本仅 {feedback_n} 笔，建议保守 WAIT")
            evidence.append("样本不足，能力等级仅供参考")

        wr = float(paper.get("win_rate") or 0)
        consec = int(paper.get("consecutive_losses") or 0)
        if consec >= 3:
            score -= 0.35
            concerns.append(f"连亏 {consec} 笔，建议暂停或缩仓")
        elif wr >= 0.55 and int(paper.get("closed_trades") or 0) >= 15:
            score += 0.2
            evidence.append(f"模拟盘胜率 {wr:.1%}，可适度积极")

        if pred_acc >= 60:
            score += 0.15
            evidence.append(f"预测准确维度 {pred_acc}/100")
        elif pred_acc < 35 and feedback_n >= 10:
            score -= 0.2
            concerns.append(f"预测准确维度偏低 ({pred_acc})")

        if discipline < 40:
            score -= 0.1
            concerns.append(f"交易纪律维度 {discipline}/100 偏低")
        if knowledge_q >= 50:
            evidence.append(f"知识质量维度 {knowledge_q}/100")

        attr = li.get("factor_attribution") or []
        unreliable = [
            a for a in attr
            if int(a.get("wins", 0) + a.get("losses", 0)) >= 5
            and float(a.get("win_rate") or 0) < 0.35
        ]
        if unreliable:
            score -= 0.1 * min(3, len(unreliable))
            concerns.append(
                f"{len(unreliable)} 个因子在当前局面历史表现差"
            )

        cards = li.get("capability_cards") or []
        if cards:
            validated = sum(1 for c in cards if int(c.get("times_validated") or 0) > 0)
            evidence.append(f"注入知识卡片 {len(cards)} 条（已验证 {validated}）")

        regime = (context.market_regime or {}).get("regime") or ""
        if regime in ("PANIC", "EUPHORIA"):
            score -= 0.25
            concerns.append(f"市场状态 {regime}，学习系统建议降风险")

        score = clamp(score, -1.0, 1.0)
        stance = stance_from_score(score)
        action = action_from_stance(stance)
        if score < -0.35:
            action = Action.WAIT

        conf = clamp(0.45 + abs(score) * 0.35, self.min_confidence, 0.9)
        posture = "保守" if score < -0.15 else ("积极" if score > 0.15 else "中性")

        return AgentOpinion(
            role=self.role,
            stance=stance,
            action=action,
            confidence=conf,
            score=score,
            summary=f"学习 Agent: {posture}姿态 (score={score:+.2f})",
            evidence=evidence[:5],
            concerns=concerns[:5],
            metadata={
                "posture": posture,
                "capability_level": growth.get("capability_level"),
                "feedback_count": feedback_n,
            },
        )
