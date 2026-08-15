"""
智能交易闭环 — 感知 → 决策 → 执行 → 反思 → 记忆

把已有模块串成显式五阶段闭环，确保：
1. 每次决策带着过往经验（知识卡片 / 模拟盘 / 议会教训 / 模式记忆）
2. 每次平仓必反思并回写记忆
3. 分析前预热记忆，分析后巩固，平仓后进化
4. 对外提供 loop_health，便于 GUI / 日志审计

不替代 headless / GUI 业务流程，只做「统一编排 + 经验 enrichment」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGES = ("perceive", "decide", "execute", "reflect", "memory")

STAGE_LABELS = {
    "perceive": "感知",
    "decide": "决策",
    "execute": "执行",
    "reflect": "反思",
    "memory": "记忆",
}


@dataclass
class LoopStageResult:
    stage: str
    ok: bool = True
    detail: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "label": STAGE_LABELS.get(self.stage, self.stage),
            "ok": self.ok,
            "detail": self.detail,
            "metrics": self.metrics,
        }


@dataclass
class CycleReport:
    """单次分析周期的闭环审计快照。"""

    started_at: str = ""
    symbol: str = "BNBUSDT"
    stages: List[LoopStageResult] = field(default_factory=list)
    reused: bool = False
    action: str = "WAIT"
    record_id: Optional[int] = None
    position_id: Optional[int] = None
    experience_injected: bool = False
    notes: List[str] = field(default_factory=list)

    def add(self, stage: str, ok: bool = True, detail: str = "", **metrics: Any) -> None:
        self.stages.append(
            LoopStageResult(stage=stage, ok=ok, detail=detail, metrics=dict(metrics))
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "symbol": self.symbol,
            "reused": self.reused,
            "action": self.action,
            "record_id": self.record_id,
            "position_id": self.position_id,
            "experience_injected": self.experience_injected,
            "notes": self.notes,
            "stages": [s.to_dict() for s in self.stages],
            "loop": "perceive→decide→execute→reflect→memory",
        }


def _loop_cfg(config: Optional[Dict]) -> Dict[str, Any]:
    return dict((config or {}).get("intelligence_loop") or {})


def loop_enabled(config: Optional[Dict] = None) -> bool:
    cfg = _loop_cfg(config)
    return cfg.get("enabled", True) is not False


class IntelligenceLoop:
    """五阶段闭环编排器。"""

    def __init__(
        self,
        *,
        learner=None,
        config: Optional[Dict[str, Any]] = None,
        paper_engine=None,
        pattern_memory=None,
        counterfactual=None,
        trader_memory=None,
        project_root: Optional[str] = None,
    ):
        self.learner = learner
        self.config = config or {}
        self.paper_engine = paper_engine
        self.pattern_memory = pattern_memory
        self.counterfactual = counterfactual
        self._trader_memory = trader_memory
        self.project_root = project_root
        self._last_report: Optional[CycleReport] = None

    @property
    def trader_memory(self):
        if self._trader_memory is not None:
            return self._trader_memory
        try:
            from bnb_quant_tool.agents.trader_memory import TraderMemoryStore

            tc = (self.config or {}).get("trader_council") or {}
            db_path = tc.get("memory_db") or "data/trader_memory.db"
            if not Path(db_path).is_absolute():
                root = self.project_root
                if root:
                    db_path = str(Path(root) / db_path)
                else:
                    try:
                        from bnb_quant_tool.data_localization import get_localization_manager

                        db_path = str(Path(get_localization_manager().workspace) / db_path)
                    except Exception:
                        pass
            self._trader_memory = TraderMemoryStore(db_path)
        except Exception as e:
            logger.debug("trader_memory init: %s", e)
            self._trader_memory = None
        return self._trader_memory

    # ------------------------------------------------------------------
    # 记忆预热（分析前）
    # ------------------------------------------------------------------
    def preflight(
        self,
        *,
        symbol: str = "BNBUSDT",
        current_price: float = 0.0,
    ) -> Dict[str, Any]:
        """决策前预热：刷新连接、软反馈、议会回填、反思队列。

        让「本次决策」吃到上一轮刚产生的经验，而不是等到分析结束才消化。
        """
        out: Dict[str, Any] = {"ok": True, "steps": {}}
        if not loop_enabled(self.config):
            out["ok"] = False
            out["reason"] = "disabled"
            return out

        learner = self.learner
        if learner is None:
            out["ok"] = False
            out["reason"] = "no_learner"
            return out

        try:
            if hasattr(learner, "refresh_before_analysis"):
                learner.refresh_before_analysis()
            out["steps"]["refresh"] = True
        except Exception as e:
            out["steps"]["refresh"] = str(e)[:120]

        learn_cfg = (self.config or {}).get("learning") or {}
        cfg = _loop_cfg(self.config)

        # 软反馈：观望样本延时打标 → 策略权重可进化
        if cfg.get("preflight_soft_feedback", True) and current_price and current_price > 0:
            try:
                n = int(learner.drain_soft_analysis_feedback(float(current_price), symbol=symbol) or 0)
                out["steps"]["soft_feedback"] = n
            except Exception as e:
                out["steps"]["soft_feedback"] = f"err:{e}"[:80]

        # 议会胜负回填
        if cfg.get("preflight_council_backfill", True) and learn_cfg.get(
            "backfill_council_on_analysis", True
        ):
            try:
                from bnb_quant_tool.trade_close_learning import backfill_missing_council_outcomes

                n = backfill_missing_council_outcomes(
                    self.config,
                    limit=int(learn_cfg.get("backfill_council_batch", 8) or 8),
                )
                out["steps"]["council_backfill"] = int(n or 0)
            except Exception as e:
                out["steps"]["council_backfill"] = f"err:{e}"[:80]

        # 反思队列
        if cfg.get("preflight_drain_reflections", True) and learn_cfg.get(
            "drain_reflections_on_analysis", True
        ):
            try:
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator

                ev = LearningEvolutionCoordinator(
                    learner,
                    capability_memory=getattr(learner, "capability_memory", None),
                    counterfactual=self.counterfactual,
                    config=self.config,
                )
                drained = ev.drain_pending_reflections(force=False) or {}
                out["steps"]["reflections"] = drained
            except Exception as e:
                out["steps"]["reflections"] = f"err:{e}"[:80]

        # 知识卡消毒（幂等）：停用 HOLD 禁开通配卡
        if cfg.get("preflight_knowledge_hygiene", True):
            try:
                from bnb_quant_tool.knowledge_hygiene import sanitize_hold_ban_cards
                hy = sanitize_hold_ban_cards()
                out["steps"]["knowledge_hygiene"] = int(hy.get("count") or 0)
            except Exception as e:
                out["steps"]["knowledge_hygiene"] = f"err:{e}"[:80]

        return out

    # ------------------------------------------------------------------
    # 经验注入决策（记忆 → 决策）
    # ------------------------------------------------------------------
    def enrich_learning_context(
        self,
        learning_context: Optional[Dict[str, Any]],
        *,
        symbol: str = "BNBUSDT",
        indicators: Optional[Dict[str, Any]] = None,
        regime: Any = None,
    ) -> Dict[str, Any]:
        """把外部记忆汇总成 experience_brief / council_memory / loop_health，注入决策。"""
        ctx = dict(learning_context or {})
        if not loop_enabled(self.config):
            return ctx

        cfg = _loop_cfg(self.config)
        if not cfg.get("inject_experience_brief", True) and not cfg.get(
            "inject_council_memory", True
        ):
            return ctx

        council = {}
        if cfg.get("inject_council_memory", True):
            council = self._build_council_memory()
            if council:
                ctx["council_memory"] = council

        health = self.get_loop_health(symbol=symbol)
        ctx["loop_health"] = health
        ctx["intelligence_loop"] = {
            "stages": list(STAGES),
            "labels": dict(STAGE_LABELS),
            "enabled": True,
        }

        if cfg.get("inject_experience_brief", True):
            brief = self.format_experience_brief(ctx, regime=regime)
            if brief:
                ctx["experience_brief"] = brief
                ctx["experience_injected"] = True

        # 明确标记：本次决策带着记忆，不是从零开始
        ctx["memory_driven"] = True
        return ctx

    def _build_council_memory(self) -> Dict[str, Any]:
        tm = self.trader_memory
        if tm is None:
            return {}
        personas = ["macro", "momentum", "flow", "structure", "mean_reversion", "contrarian"]
        traders: List[Dict[str, Any]] = []
        lessons: List[str] = []
        try:
            from bnb_quant_tool.llm_provider import list_council_providers

            providers = list_council_providers(self.config) or []
        except Exception:
            providers = []

        ids: List[str] = []
        if len(providers) > 1:
            # 双模：只查带 provider 后缀的 id，避免混入单模时代裸 persona 样本
            for p in providers:
                for base in personas:
                    ids.append(f"{base}__{p}")
        elif len(providers) == 1:
            p = providers[0]
            for base in personas:
                ids.append(base)
                ids.append(f"{base}__{p}")
        else:
            ids = list(personas)

        seen = set()
        for tid in ids:
            if tid in seen:
                continue
            seen.add(tid)
            try:
                acc = tm.get_accuracy(tid)
                if int(acc.get("total") or 0) <= 0 and "__" in tid:
                    continue
                lesson = (tm.get_lessons(tid, max_chars=180) or "").strip()
                row = {
                    "trader_id": tid,
                    "total": int(acc.get("total") or 0),
                    "accuracy": float(acc.get("accuracy") or 0.5),
                    "weight": float(acc.get("weight") or 1.0),
                    "lesson": lesson[:160] if lesson else "",
                }
                if row["total"] > 0 or lesson:
                    traders.append(row)
                if lesson and row["total"] > 0:
                    lessons.append(f"{tid}: {lesson.splitlines()[0][:100]}")
            except Exception:
                continue

        traders.sort(key=lambda x: (-x["total"], -x["accuracy"]))
        return {
            "traders": traders[:12],
            "top_lessons": lessons[:6],
            "sample_traders": sum(1 for t in traders if t["total"] > 0),
        }

    def format_experience_brief(
        self,
        ctx: Dict[str, Any],
        *,
        regime: Any = None,
    ) -> str:
        """压缩成主分析 LLM 可读的「带着经验交易」摘要。"""
        lines: List[str] = []
        lines.append("【智能闭环经验摘要 — 你不是从零开始，必须参考以下记忆】")
        lines.append("闭环: 感知→决策→执行→反思→记忆（本次决策处在「决策」阶段）")

        growth = ctx.get("growth") or {}
        maturity = growth.get("learning_maturity") or ctx.get("learning_maturity") or "BEGINNER"
        lines.append(
            f"能力 L{growth.get('capability_level', 0)}/100 | 成熟度 {maturity} | "
            f"分析{growth.get('analysis_count', ctx.get('total_analyses', 0))} "
            f"反馈{growth.get('feedback_count', ctx.get('total_feedbacks', 0))} "
            f"知识卡{growth.get('knowledge_cards', 0)}"
        )

        paper = ctx.get("paper_trading") or {}
        if paper.get("closed_trades"):
            lines.append(
                f"模拟盘: {paper['closed_trades']}笔 胜率{float(paper.get('win_rate') or 0):.1%} "
                f"累计{float(paper.get('total_pnl_usdt') or 0):+.1f}U "
                f"连亏{int(paper.get('consecutive_losses') or 0)}"
            )

        # 本地盈利成长下一课（零额外 API）
        try:
            from bnb_quant_tool.local_growth_coach import growth_brief_for_prompt
            gb = growth_brief_for_prompt(self.config)
            if gb:
                lines.append(gb)
                lines.append("要求: 本课优先落实「下一课」，不要同时改一堆无关规则。")
        except Exception:
            pass

        if regime:
            rname = regime.get("regime") if isinstance(regime, dict) else regime
            if rname:
                lines.append(f"当前局面 regime={rname}")

        pm = ctx.get("pattern_memory") or {}
        if int(pm.get("matched") or 0) > 0:
            lines.append(f"模式记忆: {pm.get('text') or pm}"[:200])

        cards = ctx.get("capability_cards") or []
        if cards:
            lines.append(f"相关知识卡片 {len(cards)} 条（详见下方完整卡片）")
            for c in cards[:3]:
                title = (c.get("title") or "")[:40]
                lesson = (c.get("lesson") or c.get("action_rule") or "")[:80]
                lines.append(f"  · [{c.get('category', '?')}] {title}: {lesson}")

        council = ctx.get("council_memory") or {}
        for les in (council.get("top_lessons") or [])[:4]:
            lines.append(f"议会教训: {les}")

        recs = ctx.get("recommendations") or []
        for r in recs[:3]:
            lines.append(f"纪律: {r}")

        health = ctx.get("loop_health") or {}
        if health:
            lines.append(
                f"闭环健康: 记忆驱动={health.get('memory_driven')} "
                f"议会样本={health.get('council_outcome_samples', 0)} "
                f"知识卡={health.get('knowledge_cards', 0)}"
            )

        lines.append(
            "要求: 结合上述经验给出判断；若历史同类局面曾亏损，必须说明如何避免重蹈覆辙。"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 周期审计
    # ------------------------------------------------------------------
    def begin_cycle(self, symbol: str = "BNBUSDT") -> CycleReport:
        report = CycleReport(
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            symbol=symbol,
        )
        self._last_report = report
        return report

    @property
    def last_report(self) -> Optional[CycleReport]:
        return self._last_report

    def after_analysis(
        self,
        report: Optional[CycleReport],
        *,
        record_id: Optional[int] = None,
        action: str = "WAIT",
        reused: bool = False,
        learning_context: Optional[Dict] = None,
        config: Optional[Dict] = None,
        project_root: Optional[str] = None,
    ) -> CycleReport:
        """决策落库后：绑定议会投票、标记记忆注入。"""
        rep = report or self.begin_cycle()
        rep.record_id = record_id
        rep.action = action or "WAIT"
        rep.reused = bool(reused)
        ctx = learning_context or {}
        rep.experience_injected = bool(
            ctx.get("experience_injected") or ctx.get("memory_driven")
        )
        rep.add(
            "decide",
            ok=True,
            detail=f"action={rep.action} reused={rep.reused}",
            record_id=record_id,
            cards=len(ctx.get("capability_cards") or []),
            experience=rep.experience_injected,
        )

        # 唯一绑定入口：复用跳过议会时不绑，避免串票
        if record_id:
            try:
                from bnb_quant_tool.analysis_reuse import bind_council_votes_to_record

                n = bind_council_votes_to_record(
                    config or self.config,
                    int(record_id),
                    project_root=project_root or self.project_root,
                    within_minutes=5,
                    skip=bool(reused),
                    created_after_iso=rep.started_at or None,
                )
                detail = (
                    "skip_bind_reuse"
                    if reused
                    else f"bound_council_votes={n}"
                )
                rep.add("memory", ok=True, detail=detail, votes=n)
            except Exception as e:
                rep.add("memory", ok=False, detail=str(e)[:120])

        self._last_report = rep
        return rep

    def mark_perceive(self, report: Optional[CycleReport], **metrics: Any) -> None:
        rep = report or self.begin_cycle()
        rep.add("perceive", ok=True, detail="market/news/regime collected", **metrics)
        self._last_report = rep

    def mark_execute(
        self,
        report: Optional[CycleReport],
        *,
        position_id: Optional[int] = None,
        opened: bool = False,
    ) -> None:
        rep = report or self.begin_cycle()
        rep.position_id = position_id
        rep.add(
            "execute",
            ok=True,
            detail=("opened" if opened else "no_open"),
            position_id=position_id,
        )
        self._last_report = rep

    def mark_reflect(
        self,
        report: Optional[CycleReport],
        *,
        position_id: Optional[int] = None,
        outcome: str = "",
        progressed: bool = False,
    ) -> None:
        rep = report or self.begin_cycle()
        rep.add(
            "reflect",
            ok=progressed,
            detail=f"close#{position_id} {outcome}",
            position_id=position_id,
            outcome=outcome,
        )
        self._last_report = rep

    # ------------------------------------------------------------------
    # 健康度
    # ------------------------------------------------------------------
    def get_loop_health(self, symbol: str = "BNBUSDT") -> Dict[str, Any]:
        """闭环是否真正在运转（供 GUI / 状态栏）。"""
        health: Dict[str, Any] = {
            "enabled": loop_enabled(self.config),
            "memory_driven": True,
            "stages": list(STAGES),
            "symbol": symbol,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        learner = self.learner
        if learner is None:
            health["memory_driven"] = False
            health["error"] = "no_learner"
            return health

        try:
            insights = {}
            if hasattr(learner, "get_learning_insights"):
                insights = learner.get_learning_insights() or {}
            health["total_analyses"] = int(insights.get("total_analyses") or 0)
            health["total_feedbacks"] = int(insights.get("total_feedbacks") or 0)
            health["overall_accuracy"] = float(insights.get("overall_accuracy") or 0)
            health["learning_maturity"] = insights.get("learning_maturity") or "BEGINNER"
        except Exception as e:
            health["insights_error"] = str(e)[:80]

        try:
            mem = getattr(learner, "capability_memory", None)
            if mem is not None and hasattr(mem, "verify_persisted_count"):
                health["knowledge_cards"] = int(mem.verify_persisted_count() or 0)
            else:
                health["knowledge_cards"] = 0
        except Exception:
            health["knowledge_cards"] = 0

        try:
            from bnb_quant_tool.ai_trading_context import get_paper_trading_stats

            paper = get_paper_trading_stats(self.paper_engine) or {}
            health["paper_closed"] = int(paper.get("closed_trades") or 0)
            health["paper_win_rate"] = float(paper.get("win_rate") or 0)
            health["consecutive_losses"] = int(paper.get("consecutive_losses") or 0)
        except Exception:
            pass

        try:
            tm = self.trader_memory
            if tm is not None:
                conn = tm._connect()
                try:
                    n = conn.execute("SELECT COUNT(*) AS c FROM trader_outcomes").fetchone()
                    health["council_outcome_samples"] = int(n["c"] if n else 0)
                finally:
                    conn.close()
        except Exception:
            health["council_outcome_samples"] = 0

        # 闭环完整性打分（粗粒度）
        score = 0
        if health.get("total_feedbacks", 0) >= 3:
            score += 25
        if health.get("knowledge_cards", 0) >= 5:
            score += 25
        if health.get("paper_closed", 0) >= 5:
            score += 25
        if health.get("council_outcome_samples", 0) >= 3:
            score += 25
        health["completeness_score"] = score
        health["completeness_label"] = (
            "完整" if score >= 75 else ("成形" if score >= 50 else ("起步" if score >= 25 else "雏形"))
        )
        return health


def get_or_create_loop(
    owner: Any,
    *,
    learner=None,
    config: Optional[Dict] = None,
    paper_engine=None,
    pattern_memory=None,
    counterfactual=None,
    project_root: Optional[str] = None,
) -> IntelligenceLoop:
    """在 GUI / headless 对象上缓存 IntelligenceLoop 单例。"""
    existing = getattr(owner, "intelligence_loop", None)
    if isinstance(existing, IntelligenceLoop):
        if learner is not None:
            existing.learner = learner
        if config is not None:
            existing.config = config
        if paper_engine is not None:
            existing.paper_engine = paper_engine
        return existing
    loop = IntelligenceLoop(
        learner=learner or getattr(owner, "learner", None),
        config=config or getattr(owner, "config", {}) or {},
        paper_engine=paper_engine or getattr(owner, "paper_engine", None),
        pattern_memory=pattern_memory or getattr(owner, "pattern_memory", None),
        counterfactual=counterfactual or getattr(owner, "counterfactual", None),
        project_root=project_root
        or str(getattr(owner, "project_root", "") or "")
        or None,
    )
    try:
        setattr(owner, "intelligence_loop", loop)
    except Exception:
        pass
    return loop


def format_loop_health_for_prompt(health: Optional[Dict[str, Any]]) -> str:
    if not health:
        return ""
    return (
        f"闭环健康度 {health.get('completeness_score', 0)}/100（{health.get('completeness_label', '?')}）| "
        f"反馈{health.get('total_feedbacks', 0)} 知识卡{health.get('knowledge_cards', 0)} "
        f"模拟盘{health.get('paper_closed', 0)} 议会样本{health.get('council_outcome_samples', 0)}"
    )
