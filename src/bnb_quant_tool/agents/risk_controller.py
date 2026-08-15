"""风控 Agent — 挑刺、评估风险、一票否决权。"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from bnb_quant_tool.config_access import get_confidence_threshold

from .base import (
    Action,
    AgentOpinion,
    DebateRound,
    MarketContext,
    RiskVerdict,
    Stance,
    clamp,
)

logger = logging.getLogger(__name__)


class RiskControllerAgent:
    """风控专家 — 拥有对研究员和量化建议的一票否决权。"""

    def __init__(
        self,
        pattern_memory=None,
        config: Optional[Dict] = None,
    ):
        self.pattern_memory = pattern_memory
        self.config = config or {}
        cfg = self.config.get("multi_agent", {}).get("risk", {})
        self.conflict_veto = bool(cfg.get("veto_on_agent_conflict", True))
        self.min_agreement_score = float(cfg.get("min_agreement_score", 0.25))
        self.max_risk_score = float(cfg.get("max_combined_risk_score", 0.70))
        self.require_both_agree = bool(cfg.get("require_both_agree", False))

    def review(
        self,
        researcher: AgentOpinion,
        quant: AgentOpinion,
        context: MarketContext,
        debate_rounds: Optional[List[DebateRound]] = None,
    ) -> RiskVerdict:
        objections: List[str] = []
        requirements: List[str] = []
        debate_rounds = debate_rounds or []

        proposed = quant.action
        if proposed == Action.WAIT:
            proposed_str = (context.trade_advice or {}).get("raw_action") or "WAIT"
            if proposed_str in ("LONG", "SHORT"):
                proposed = Action(proposed_str)

        # follow_ai：提案必须以 Advisor/AI 方向为准，议会仅 advisory
        ai_cfg = self.config.get("ai_trading") or {}
        if bool(ai_cfg.get("follow_ai_direction", False)):
            adv = context.trade_advice or {}
            keep = str(adv.get("action") or "").upper()
            if keep in ("LONG", "SHORT"):
                proposed = Action(keep)

        # ---- 1. Agent 分歧检查 ----
        # follow_ai：方向已锁 AI，专家分歧只降置信，不硬否决（否则与 follow_ai 矛盾）
        follow_ai = bool(ai_cfg.get("follow_ai_direction", False))
        agreement = self._agent_agreement(researcher, quant)
        soft_conflict_penalty = 0.0
        if follow_ai:
            if self.require_both_agree and researcher.action != quant.action and proposed != Action.WAIT:
                soft_conflict_penalty -= 0.08
            if agreement < self.min_agreement_score and proposed != Action.WAIT:
                soft_conflict_penalty -= 0.05
            if conflicts_count := len([d for d in debate_rounds if d.conflict]):
                if self.conflict_veto and conflicts_count and proposed != Action.WAIT:
                    soft_conflict_penalty -= 0.05
        else:
            if self.require_both_agree and researcher.action != quant.action:
                if proposed != Action.WAIT:
                    objections.append(
                        f"研究员({researcher.action.value})与量化({quant.action.value})方向不一致"
                    )

            conflicts = [d for d in debate_rounds if d.conflict]
            if conflicts and self.conflict_veto:
                objections.append(
                    f"存在 {len(conflicts)} 项专家辩论冲突: {conflicts[0].topic}"
                )

            if agreement < self.min_agreement_score and proposed != Action.WAIT:
                objections.append(
                    f"专家共识度不足 ({agreement:.0%} < {self.min_agreement_score:.0%})"
                )

        # ---- 2. 模式记忆（post 已拦则跳过，避免双重否决） ----
        pattern = context.pattern_insight or {}
        advice_early = context.trade_advice or {}
        if advice_early.get("pattern_blocked") or advice_early.get("win_rate_blocked"):
            pass  # 已由 learning_gates / pattern_memory_gate 处理
        elif pattern.get("matched", 0) > 0:
            wr = float(pattern.get("win_rate") or 0)
            matched = int(pattern.get("matched") or 0)
            ai_cfg = self.config.get("ai_trading") or {}
            if ai_cfg.get("risk_recheck_pattern", False):
                min_wr = float(ai_cfg.get("pattern_memory_block_wr", 0.35))
                min_samples = int(ai_cfg.get("pattern_memory_min_samples", 5))
                if matched >= min_samples and wr < min_wr:
                    objections.append(
                        f"模式记忆: 相似局面 {matched} 次胜率仅 {wr:.1%}"
                    )

        # ---- 4. 门控原因 ----
        advice = context.trade_advice or {}
        if not advice.get("passed_gate", True):
            for reason in advice.get("gate_reasons") or []:
                objections.append(f"门控: {reason}")

        # ---- 4b. BNB 风控哨兵 ----
        bnb = context.bnb_factors or {}
        rs = bnb.get("risk_sentry") or advice.get("risk_sentry") or {}
        if rs.get("block_long") and proposed == Action.LONG:
            fr = rs.get("funding_extreme") or {}
            if fr.get("block_long"):
                objections.append(f"BNB资金费率极值: {fr.get('interpretation', '')[:80]}")
            else:
                objections.append(rs.get("interpretation", "BNB风控哨兵拦截做多")[:80])

        # ---- 5. 风险回报比（优先净 RR；与 post net_rr_gate 对齐） ----
        net_detail = advice.get("net_rr") or {}
        net_ratio = net_detail.get("ratio") if isinstance(net_detail, dict) else None
        min_net = float(ai_cfg.get("min_net_rr", 1.5) or 1.5)
        if net_ratio is not None and proposed != Action.WAIT:
            if float(net_ratio) < min_net:
                objections.append(f"净RR过低 ({float(net_ratio):.2f} < {min_net})")
        elif not bool(ai_cfg.get("net_rr_gate_enabled", True)):
            rr = advice.get("risk_reward_ratio")
            min_rr = float(
                (self.config.get("risk_management") or {}).get(
                    "min_risk_reward_ratio", 1.5
                )
            )
            if rr is not None and float(rr) < min_rr and proposed != Action.WAIT:
                objections.append(f"RR 过低 ({float(rr):.2f} < {min_rr})")

        # ---- 6. 置信度门槛（硬门控已拦则跳过，避免 0.38 vs 0.70 双源重复否决） ----
        min_conf = get_confidence_threshold(self.config)
        conf = float(advice.get("confidence") or quant.confidence or 0)
        if (
            conf < min_conf
            and proposed != Action.WAIT
            and not advice.get("confidence_hard_probe")
            and not advice.get("confidence_hard_blocked")
        ):
            objections.append(f"置信度 {conf:.0%} 低于门槛 {min_conf:.0%}")

        # ---- 7. 保证金占用 ----
        equity = float(
            (self.config.get("trading") or {}).get("account_balance", 5000.0)
        )
        margin_required = float(
            ((advice.get("position") or {}).get("margin_required")) or 0
        )
        open_positions = advice.get("open_positions_list") or []
        total_realized = float(advice.get("total_realized_pnl") or 0)
        from bnb_quant_tool.config_access import get_margin_state, is_margin_insufficient
        margin_state = get_margin_state(
            equity, open_positions, total_realized_pnl=total_realized
        )
        if margin_required > 0 and is_margin_insufficient(
            equity, open_positions, margin_required, total_realized_pnl=total_realized
        ):
            objections.append(
                f"保证金不足 (需 {margin_required:.2f}，可用 {margin_state['available_margin']:.2f})"
            )
        elif margin_required <= 0 and margin_state["available_margin"] <= 0.01 and proposed != Action.WAIT:
            objections.append(
                f"保证金已用尽 (可用 {margin_state['available_margin']:.2f} USDT)"
            )

        # ---- 8. 综合风险分 ----
        risk_score = self._compute_risk_score(researcher, quant, context, objections)
        if risk_score > self.max_risk_score and proposed != Action.WAIT:
            objections.append(f"综合风险分过高 ({risk_score:.0%})")

        # ---- 9. 研究员/量化 concerns 汇总 ----
        for c in researcher.concerns[:3]:
            requirements.append(f"[研究员] {c}")
        for c in quant.concerns[:3]:
            requirements.append(f"[量化] {c}")

        # ---- 裁决 ----
        vetoed = len(objections) > 0 and proposed != Action.WAIT
        if vetoed:
            return RiskVerdict(
                approved=False,
                vetoed=True,
                veto_reason=objections[0],
                action=Action.WAIT,
                confidence_adjustment=-0.15,
                objections=objections,
                requirements=requirements,
            )

        conf_adj = 0.05 if agreement > 0.6 else 0.0
        if researcher.stance == quant.stance and researcher.stance != Stance.NEUTRAL:
            conf_adj += 0.05

        return RiskVerdict(
            approved=True,
            vetoed=False,
            veto_reason="",
            action=proposed,
            confidence_adjustment=conf_adj,
            objections=[],
            requirements=requirements,
        )

    def apply_veto_to_advice(
        self,
        advice: Dict,
        verdict: RiskVerdict,
        deliberation: Optional[Dict] = None,
        *,
        follow_ai_direction: bool = False,
        preserve_action: Optional[str] = None,
    ) -> Dict:
        """将风控裁决写入 trade_advice。

        follow_ai_direction + preserve_action(LONG/SHORT)：
        - 否决 → WAIT（允许）
        - 批准 → 保留 AI/Advisor 方向，议会方向仅作参考，禁止改向
        """
        advice = dict(advice)
        keep = str(preserve_action or "").upper() if follow_ai_direction else ""
        if verdict.vetoed:
            advice["raw_action"] = advice.get("raw_action") or advice.get("action")
            advice["action"] = "WAIT"
            advice["passed_gate"] = False
            reasons = list(advice.get("gate_reasons") or [])
            reasons.insert(0, f"🛑 风控否决: {verdict.veto_reason}")
            for obj in verdict.objections[1:3]:
                reasons.append(obj)
            advice["gate_reasons"] = reasons
            advice["risk_vetoed"] = True
        else:
            proposed = verdict.action.value
            if keep in ("LONG", "SHORT"):
                advice["action"] = keep
                advice["council_advisory_action"] = proposed
                if proposed not in (keep, "WAIT"):
                    reasons = list(advice.get("gate_reasons") or [])
                    msg = (
                        f"follow_ai: 保留 AI 方向 {keep}，"
                        f"议会建议 {proposed} 仅作仓位/参考"
                    )
                    if msg not in reasons:
                        reasons.append(msg)
                    advice["gate_reasons"] = reasons
            else:
                advice["action"] = proposed
            advice["risk_vetoed"] = False
            conf = float(advice.get("confidence") or 0.5)
            advice["confidence"] = clamp(conf + verdict.confidence_adjustment, 0.0, 1.0)

        if deliberation:
            advice["multi_agent_deliberation"] = deliberation
        advice["risk_verdict"] = verdict.to_dict()
        return advice

    @staticmethod
    def _agent_agreement(researcher: AgentOpinion, quant: AgentOpinion) -> float:
        """计算两位专家的方向一致度 0~1。"""
        if researcher.action == quant.action and researcher.action != Action.WAIT:
            return 0.7 + min(researcher.confidence, quant.confidence) * 0.3

        r_sign = 1 if researcher.score > 0.1 else (-1 if researcher.score < -0.1 else 0)
        q_sign = 1 if quant.score > 0.1 else (-1 if quant.score < -0.1 else 0)

        if r_sign == 0 or q_sign == 0:
            return 0.45
        if r_sign == q_sign:
            return 0.55 + abs(researcher.score - quant.score) * -0.2 + 0.2
        return 0.15

    @staticmethod
    def _compute_risk_score(
        researcher: AgentOpinion,
        quant: AgentOpinion,
        context: MarketContext,
        objections: List[str],
    ) -> float:
        score = len(objections) * 0.12

        concerns_count = len(researcher.concerns) + len(quant.concerns)
        score += concerns_count * 0.05

        return clamp(score, 0.0, 1.0)
