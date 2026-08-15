"""多智能体编排器 — 协调研究员/量化/风控/交易 Agent 协同决策。
优先走 6 人交易员议会（独立 LLM），失败或关闭时回退经典四 Agent。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import (
    Action,
    AgentOpinion,
    DebateRound,
    DeliberationResult,
    MarketContext,
    Stance,
)
from .council import TraderCouncil
from .learning import LearningAgent
from .quant import QuantAgent
from .researcher import ResearcherAgent
from .risk_controller import RiskControllerAgent
from .trading import TradingAgent

logger = logging.getLogger(__name__)


class MultiAgentOrchestrator:
    """
    多专家协同决策编排器。

    优先流程（trader_council.enabled）:
      6 交易员独立研判 → 讨论投票 → 风控 → 交易执行

    回退流程:
      1. 研究员 Agent → 宏观/新闻/链上/情绪
      2. 量化 Agent → 技术/多周期/机构/AI
      3. 辩论阶段 → 检测冲突并记录
      4. 风控 Agent → 审查 + 一票否决
      5. 交易 Agent → 模拟盘执行（可选）
    """

    def __init__(
        self,
        config: Optional[Dict] = None,
        news_collector=None,
        sentiment_engine=None,
        onchain_analyzer=None,
        macro_layer=None,
        pattern_memory=None,
        paper_engine=None,
        ai_analyzer=None,
        project_root: Optional[str] = None,
    ):
        self.config = config or {}
        self.ai_analyzer = ai_analyzer
        self.project_root = project_root

        self.researcher = ResearcherAgent(
            news_collector=news_collector,
            sentiment_engine=sentiment_engine,
            onchain_analyzer=onchain_analyzer,
            macro_layer=macro_layer,
            config=self.config,
        )
        self.quant = QuantAgent(config=self.config)
        self.risk = RiskControllerAgent(
            pattern_memory=pattern_memory,
            config=self.config,
        )
        self.trading = TradingAgent(
            paper_engine=paper_engine,
            config=self.config,
        )
        self.learning = LearningAgent(config=self.config)

        ma_cfg = self.config.get("multi_agent", {})
        self.enabled = bool(ma_cfg.get("enabled", True))
        self.auto_execute = bool(ma_cfg.get("auto_execute", False))
        self.llm_debate = bool(ma_cfg.get("llm_debate_summary", False))

        self.council: Optional[TraderCouncil] = None
        tc_cfg = self.config.get("trader_council") or {}
        if tc_cfg.get("enabled", True):
            try:
                self.council = TraderCouncil(
                    config=self.config,
                    ai_analyzer=ai_analyzer,
                    project_root=project_root,
                )
            except Exception as e:
                logger.warning("trader council init failed: %s", e)
                self.council = None

    def deliberate(
        self,
        context: MarketContext,
        execute: bool = False,
        learning_record_id: Optional[int] = None,
        equity_usdt: Optional[float] = None,
    ) -> DeliberationResult:
        """运行完整多智能体协同决策（优先交易员议会）。"""
        if self.council and self.council.enabled:
            try:
                return self._deliberate_with_council(
                    context,
                    execute=execute,
                    learning_record_id=learning_record_id,
                    equity_usdt=equity_usdt,
                )
            except Exception as e:
                logger.error("council deliberation failed, fallback: %s", e)

        return self._deliberate_classic(
            context,
            execute=execute,
            learning_record_id=learning_record_id,
            equity_usdt=equity_usdt,
        )

    def _deliberate_with_council(
        self,
        context: MarketContext,
        *,
        execute: bool,
        learning_record_id: Optional[int],
        equity_usdt: Optional[float],
    ) -> DeliberationResult:
        assert self.council is not None
        if self.ai_analyzer and not self.council.ai_analyzer:
            self.council.ai_analyzer = self.ai_analyzer

        council_summary = self.council.deliberate(context)
        researcher_opinion, quant_opinion, learning_opinion = (
            self.council.as_proxy_opinions(council_summary)
        )
        debate_rounds = list(council_summary.debate_rounds)

        # 议会最终方向写入建议，供风控提案使用
        if context.trade_advice is not None:
            context.trade_advice = dict(context.trade_advice)
            context.trade_advice["council_action"] = council_summary.final_action.value
            context.trade_advice["council_confidence"] = council_summary.final_confidence
            context.trade_advice["council_size_factor"] = float(
                council_summary.size_factor or 0.0
            )
            if council_summary.dqn_shadow:
                context.trade_advice["dqn_shadow"] = council_summary.dqn_shadow
            # follow_ai：不覆盖 Advisor 方向到 raw_action（议会仅 advisory）
            follow_ai = bool(
                ((self.config or {}).get("ai_trading") or {}).get(
                    "follow_ai_direction", False
                )
            )
            if (
                not follow_ai
                and council_summary.final_action != Action.WAIT
            ):
                context.trade_advice["raw_action"] = council_summary.final_action.value
            elif follow_ai:
                context.trade_advice["council_advisory_action"] = (
                    council_summary.final_action.value
                )

        risk_verdict = self.risk.review(
            researcher_opinion,
            quant_opinion,
            context,
            debate_rounds=debate_rounds,
        )

        # 议会共识强化/削弱置信
        follow_ai = bool(
            ((self.config or {}).get("ai_trading") or {}).get(
                "follow_ai_direction", False
            )
        )
        final_action = risk_verdict.action
        base_conf = float(council_summary.final_confidence or 0.5)
        if risk_verdict.vetoed:
            final_action = Action.WAIT
        elif follow_ai:
            # 批准时保留 Advisor 方向，不采纳议会改向
            keep = str((context.trade_advice or {}).get("action") or "").upper()
            if keep in ("LONG", "SHORT"):
                final_action = Action(keep)
        risk_adj = risk_verdict.confidence_adjustment
        if learning_opinion and learning_opinion.action == Action.WAIT and learning_opinion.score < -0.3:
            risk_adj -= 0.1
        final_confidence = max(0.0, min(1.0, base_conf + risk_adj))
        consensus = council_summary.consensus and not risk_verdict.vetoed

        transcript = council_summary.transcript
        if risk_verdict.vetoed:
            transcript += f"\n🛡️ 风控否决: {risk_verdict.veto_reason}"
        elif follow_ai:
            transcript += (
                f"\n🛡️ 风控批准 → 保留 AI 方向 {final_action.value} "
                f"(议会建议 {risk_verdict.action.value}, 置信 {final_confidence:.0%})"
            )
        else:
            transcript += f"\n🛡️ 风控批准 → {final_action.value} (置信 {final_confidence:.0%})"

        trading_result = None
        if execute and self.auto_execute and not risk_verdict.vetoed:
            advice = dict(context.trade_advice or {})
            advice["action"] = final_action.value
            advice["confidence"] = final_confidence
            trading_result = self.trading.execute(
                advice, risk_verdict, context,
                learning_record_id=learning_record_id,
                equity_usdt=equity_usdt,
            )

        return DeliberationResult(
            researcher=researcher_opinion,
            quant=quant_opinion,
            risk_verdict=risk_verdict,
            debate_rounds=debate_rounds,
            final_action=final_action,
            final_confidence=final_confidence,
            consensus=consensus,
            transcript=transcript,
            trading_result=trading_result,
            learning=learning_opinion,
            council=council_summary.to_dict(),
        )

    def _deliberate_classic(
        self,
        context: MarketContext,
        *,
        execute: bool = False,
        learning_record_id: Optional[int] = None,
        equity_usdt: Optional[float] = None,
    ) -> DeliberationResult:
        """经典研究员/量化/学习流程。"""
        researcher_opinion = self.researcher.analyze(context)
        quant_opinion = self.quant.analyze(context)
        learning_opinion = self.learning.analyze(context)

        debate_rounds = self._debate(researcher_opinion, quant_opinion, context)
        if self.llm_debate and self.ai_analyzer:
            debate_rounds = self._llm_debate_summary(debate_rounds, context)

        risk_verdict = self.risk.review(
            researcher_opinion,
            quant_opinion,
            context,
            debate_rounds=debate_rounds,
        )

        agent_weights = self._agent_accuracy_weights(context.learning_insights)

        learn_weight = agent_weights.get("learning", 1.0)
        learn_adj = float(learning_opinion.score) * 0.12 * learn_weight
        if learning_opinion.action == Action.WAIT and learning_opinion.score < -0.3:
            if risk_verdict.action != Action.WAIT:
                risk_verdict.confidence_adjustment -= 0.15 * learn_weight
                risk_verdict.objections.append(
                    learning_opinion.concerns[0] if learning_opinion.concerns
                    else "学习 Agent 建议观望"
                )

        final_action = risk_verdict.action
        base_conf = float(
            (context.trade_advice or {}).get("confidence")
            or quant_opinion.confidence
            or 0.5
        )
        r_w = agent_weights.get("researcher", 1.0)
        q_w = agent_weights.get("quant", 1.0)
        if researcher_opinion.action == quant_opinion.action == final_action:
            if final_action != Action.WAIT:
                base_conf = min(1.0, base_conf * (0.85 + 0.075 * r_w + 0.075 * q_w))

        risk_adj = risk_verdict.confidence_adjustment * agent_weights.get("risk_controller", 1.0)
        final_confidence = max(
            0.0,
            min(1.0, base_conf + risk_adj + learn_adj),
        )

        consensus = (
            researcher_opinion.action == quant_opinion.action
            and final_action == quant_opinion.action
            and final_action != Action.WAIT
        )

        transcript = self._build_transcript(
            researcher_opinion, quant_opinion, learning_opinion,
            debate_rounds, risk_verdict,
            final_action, final_confidence, consensus,
        )

        trading_result = None
        if execute and self.auto_execute and not risk_verdict.vetoed:
            advice = dict(context.trade_advice or {})
            advice["action"] = final_action.value
            advice["confidence"] = final_confidence
            trading_result = self.trading.execute(
                advice, risk_verdict, context,
                learning_record_id=learning_record_id,
                equity_usdt=equity_usdt,
            )

        return DeliberationResult(
            researcher=researcher_opinion,
            quant=quant_opinion,
            risk_verdict=risk_verdict,
            debate_rounds=debate_rounds,
            final_action=final_action,
            final_confidence=final_confidence,
            consensus=consensus,
            transcript=transcript,
            trading_result=trading_result,
            learning=learning_opinion,
            council=None,
        )

    def _llm_debate_summary(
        self,
        debate_rounds: List[DebateRound],
        context: MarketContext,
    ) -> List[DebateRound]:
        """用 DeepSeek 生成辩论摘要附加到最后一轮。"""
        if not debate_rounds or not self.ai_analyzer:
            return debate_rounds
        try:
            lines = []
            for rnd in debate_rounds:
                lines.append(f"[{rnd.topic}] {rnd.resolution}")
            prompt = (
                "以下是多智能体辩论要点，请用 2-3 句中文总结关键分歧与建议：\n"
                + "\n".join(lines)
            )
            summary = ""
            if hasattr(self.ai_analyzer, "quick_summarize"):
                summary = self.ai_analyzer.quick_summarize(prompt) or ""
            if summary:
                debate_rounds = list(debate_rounds)
                debate_rounds.append(DebateRound(
                    topic="AI 辩论摘要",
                    researcher_point=summary[:120],
                    quant_point="DeepSeek 综合",
                    conflict=False,
                    resolution=summary[:200],
                ))
        except Exception as e:
            logger.debug("llm debate summary skipped: %s", e)
        return debate_rounds

    def apply_to_advice(
        self,
        advice: Dict[str, Any],
        deliberation: DeliberationResult,
    ) -> Dict[str, Any]:
        """将协同决策结果合并进 trade_advice。

        follow_ai_direction=true 时：保留 Advisor/AI 方向，议会/风控只能否决为 WAIT，
        不得把 LONG 改成 SHORT（或反之）。
        """
        ai_cfg = (self.config or {}).get("ai_trading") or {}
        follow_ai = bool(ai_cfg.get("follow_ai_direction", False))
        advisor_action = str(advice.get("action") or "").upper()

        out = self.risk.apply_veto_to_advice(
            advice,
            deliberation.risk_verdict,
            deliberation=deliberation.to_dict(),
            follow_ai_direction=follow_ai,
            preserve_action=advisor_action if follow_ai else None,
        )
        council = deliberation.council if isinstance(deliberation.council, dict) else {}
        if council.get("size_factor") is not None:
            out["council_size_factor"] = float(council.get("size_factor") or 0)
        if council.get("dqn_shadow"):
            out["dqn_shadow"] = council.get("dqn_shadow")
        # 记录议会建议方向（即使未覆盖）
        if council.get("final_action"):
            out["council_action"] = council.get("final_action")
        return out

    @staticmethod
    def _agent_accuracy_weights(learning_insights: Optional[Dict]) -> Dict[str, float]:
        """将历史 agent 准确率转为 0.5~1.5 权重，样本不足时保持 1.0。"""
        weights = {
            "researcher": 1.0,
            "quant": 1.0,
            "learning": 1.0,
            "risk_controller": 1.0,
        }
        rows = (learning_insights or {}).get("agent_accuracy") or []
        role_map = {
            "researcher": "researcher",
            "研究员": "researcher",
            "quant": "quant",
            "量化": "quant",
            "learning": "learning",
            "学习": "learning",
            "risk": "risk_controller",
            "risk_controller": "risk_controller",
            "风控": "risk_controller",
        }
        for row in rows:
            role = role_map.get(str(row.get("agent_role") or "").lower(), "")
            if not role:
                continue
            total = int(row.get("total") or 0)
            if total < 5:
                continue
            acc = float(row.get("accuracy") or 0.5)
            weights[role] = max(0.5, min(1.5, 0.5 + acc))
        return weights

    def _debate(
        self,
        researcher: AgentOpinion,
        quant: AgentOpinion,
        context: MarketContext,
    ) -> List[DebateRound]:
        """结构化专家辩论 — 检测研究员与量化的观点冲突。"""
        rounds: List[DebateRound] = []

        # 辩论 1: 方向一致性
        r_dir = researcher.action.value
        q_dir = quant.action.value
        conflict = (
            researcher.action != Action.WAIT
            and quant.action != Action.WAIT
            and researcher.action != quant.action
        )
        resolution = "方向一致，无冲突" if not conflict else "方向冲突，需风控裁决"
        if researcher.action == Action.WAIT or quant.action == Action.WAIT:
            conflict = False
            resolution = "至少一方建议观望，降低冲突等级"

        rounds.append(DebateRound(
            topic="交易方向",
            researcher_point=f"研究员: {r_dir} ({researcher.summary})",
            quant_point=f"量化: {q_dir} ({quant.summary})",
            conflict=conflict,
            resolution=resolution,
        ))

        # 辩论 2: 置信度分歧
        conf_gap = abs(researcher.confidence - quant.confidence)
        conf_conflict = conf_gap > 0.35
        rounds.append(DebateRound(
            topic="置信度评估",
            researcher_point=f"研究员置信 {researcher.confidence:.0%}",
            quant_point=f"量化置信 {quant.confidence:.0%}",
            conflict=conf_conflict,
            resolution=(
                f"置信差距 {conf_gap:.0%}，{'存在分歧' if conf_conflict else '基本一致'}"
            ),
        ))

        # 辩论 3: 宏观 vs 技术面
        macro_vs_tech = (
            (researcher.stance == Stance.BULLISH and quant.score < -0.15)
            or (researcher.stance == Stance.BEARISH and quant.score > 0.15)
        )
        rounds.append(DebateRound(
            topic="宏观与技术面背离",
            researcher_point=f"宏观 score={researcher.score:+.2f} ({researcher.stance.value})",
            quant_point=f"技术 score={quant.score:+.2f} ({quant.stance.value})",
            conflict=macro_vs_tech,
            resolution=(
                "宏观与技术面背离，风控应谨慎" if macro_vs_tech
                else "宏观与技术面方向一致"
            ),
        ))

        # 辩论 4: 风险关注点
        all_concerns = researcher.concerns + quant.concerns
        rounds.append(DebateRound(
            topic="风险关注点",
            researcher_point="; ".join(researcher.concerns[:2]) or "无明显风险",
            quant_point="; ".join(quant.concerns[:2]) or "无明显风险",
            conflict=len(all_concerns) >= 3,
            resolution=f"共 {len(all_concerns)} 项风险提示",
        ))

        return rounds

    def _build_transcript(
        self,
        researcher: AgentOpinion,
        quant: AgentOpinion,
        learning: AgentOpinion,
        debate_rounds: List[DebateRound],
        risk_verdict,
        final_action: Action,
        final_confidence: float,
        consensus: bool,
    ) -> str:
        lines = [
            "═══ 多智能体协同决策 ═══",
            "",
            f"📰 研究员 Agent: {researcher.summary}",
        ]
        for ev in researcher.evidence[:4]:
            lines.append(f"   • {ev}")

        lines.extend([
            "",
            f"📊 量化 Agent: {quant.summary}",
        ])
        for ev in quant.evidence[:4]:
            lines.append(f"   • {ev}")

        lines.extend([
            "",
            f"🎓 学习 Agent: {learning.summary}",
        ])
        for ev in learning.evidence[:3]:
            lines.append(f"   • {ev}")
        for c in learning.concerns[:2]:
            lines.append(f"   ⚠ {c}")

        lines.extend(["", "💬 专家辩论:"])
        for rnd in debate_rounds:
            icon = "⚡" if rnd.conflict else "✓"
            lines.append(f"  {icon} [{rnd.topic}]")
            lines.append(f"     研究员: {rnd.researcher_point[:80]}")
            lines.append(f"     量化:   {rnd.quant_point[:80]}")
            lines.append(f"     → {rnd.resolution}")

        lines.extend([
            "",
            f"🛡️ 风控 Agent: {'✅ 批准' if risk_verdict.approved else '🛑 否决'}",
        ])
        if risk_verdict.vetoed:
            lines.append(f"   否决原因: {risk_verdict.veto_reason}")
            for obj in risk_verdict.objections[:3]:
                lines.append(f"   ⚠ {obj}")
        elif risk_verdict.requirements:
            for req in risk_verdict.requirements[:3]:
                lines.append(f"   ℹ {req}")

        lines.extend([
            "",
            f"📋 最终决策: {final_action.value} (置信 {final_confidence:.0%})",
            f"   专家共识: {'是' if consensus else '否'}",
            "═══════════════════════",
        ])
        return "\n".join(lines)
