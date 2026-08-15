"""交易员议会 — 6 人独立 LLM 讨论、投票、形成共识。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    Action,
    AgentOpinion,
    DebateRound,
    DeliberationResult,
    MarketContext,
    Stance,
    clamp,
)
from .llm_trader import LLMTrader
from .personas import DEFAULT_PERSONAS, PERSONA_BY_ID, TraderPersona
from .trader_memory import TraderMemoryStore

logger = logging.getLogger(__name__)


@dataclass
class CouncilVoteSummary:
    """议会投票汇总。"""

    votes: List[AgentOpinion]
    long_weight: float
    short_weight: float
    wait_weight: float
    final_action: Action
    final_confidence: float
    consensus: bool
    agreement: float
    debate_rounds: List[DebateRound] = field(default_factory=list)
    transcript: str = ""
    chair_summary: str = ""
    teams: Dict[str, Any] = field(default_factory=dict)
    merge_note: str = ""
    size_factor: float = 0.0  # 0~1 仓位系数
    dqn_shadow: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "votes": [
                {
                    "trader_id": v.metadata.get("trader_id"),
                    "persona_id": v.metadata.get("persona_id") or str(v.metadata.get("trader_id") or "").split("__")[0],
                    "trader_name": v.metadata.get("trader_name"),
                    "emoji": v.metadata.get("emoji"),
                    "action": v.action.value,
                    "confidence": round(v.confidence, 4),
                    "score": round(v.score, 4),
                    "summary": v.summary,
                    "evidence": v.evidence,
                    "concerns": v.concerns,
                    "source": v.metadata.get("source"),
                    "has_llm_key": v.metadata.get("has_llm_key"),
                    "style": v.metadata.get("style"),
                    "color": v.metadata.get("color"),
                    "provider": v.metadata.get("provider"),
                    "model": v.metadata.get("model"),
                }
                for v in self.votes
            ],
            "long_weight": round(self.long_weight, 4),
            "short_weight": round(self.short_weight, 4),
            "wait_weight": round(self.wait_weight, 4),
            "final_action": self.final_action.value,
            "final_confidence": round(self.final_confidence, 4),
            "consensus": self.consensus,
            "agreement": round(self.agreement, 4),
            "size_factor": round(float(self.size_factor or 0), 4),
            "dqn_shadow": self.dqn_shadow,
            "debate_rounds": [d.to_dict() for d in self.debate_rounds],
            "transcript": self.transcript,
            "chair_summary": self.chair_summary,
            "teams": self.teams,
            "merge_note": self.merge_note,
        }


class TraderCouncil:
    """
    交易员议会（可双模）。

    单模: 6 人独立分析 → 加权投票 → 最终方向
    双模: DeepSeek 小队 + 千问小队（12 人）→ 各自小结 → 合并终局
    """

    def __init__(
        self,
        config: Optional[Dict] = None,
        *,
        memory: Optional[TraderMemoryStore] = None,
        ai_analyzer=None,
        project_root: Optional[str] = None,
    ):
        self.config = config or {}
        tc = self.config.get("trader_council") or {}
        self.enabled = bool(tc.get("enabled", True))
        self.parallel = bool(tc.get("parallel", True))
        self.min_consensus = float(tc.get("min_consensus", 0.45))
        self.wait_if_split = bool(tc.get("wait_if_split", True))
        self.chair_llm_summary = bool(tc.get("chair_llm_summary", True))
        self.max_workers = int(tc.get("max_workers", 6))

        db_path = tc.get("memory_db") or "data/trader_memory.db"
        if project_root and not str(db_path).startswith(str(project_root)):
            from pathlib import Path
            db_path = str(Path(project_root) / db_path)
        self.memory = memory or TraderMemoryStore(db_path)
        self.ai_analyzer = ai_analyzer

        self.traders: List[LLMTrader] = self._build_traders(tc)

    def _build_traders(self, tc: Dict) -> List[LLMTrader]:
        from bnb_quant_tool.llm_provider import (
            PROVIDER_LABELS,
            PROVIDER_SHORT,
            get_llm_credentials,
            list_council_providers,
        )

        providers = list_council_providers(self.config)
        dual = len(providers) > 1

        traders_cfg = tc.get("traders") or []
        by_id: Dict[str, Dict] = {}
        if isinstance(traders_cfg, list):
            for row in traders_cfg:
                if isinstance(row, dict) and row.get("id"):
                    by_id[str(row["id"])] = row
        elif isinstance(traders_cfg, dict):
            by_id = {str(k): (v if isinstance(v, dict) else {}) for k, v in traders_cfg.items()}

        order = tc.get("order") or [p.id for p in DEFAULT_PERSONAS]
        traders: List[LLMTrader] = []
        for provider in providers:
            creds = get_llm_credentials(self.config, provider=provider, fallback=False)
            short = PROVIDER_SHORT.get(provider, provider[:2].upper())
            label = PROVIDER_LABELS.get(provider, provider)
            for tid in order:
                persona = PERSONA_BY_ID.get(tid)
                if not persona:
                    continue
                row = by_id.get(tid) or {}
                if not bool(row.get("enabled", True)):
                    continue
                # 双模：每人复制到各 provider；单模仍允许 per-trader 覆盖 key
                if dual:
                    api_key = creds["api_key"]
                    base_url = creds["base_url"]
                    model = str(row.get("model") or "").strip() or creds["model"]
                    trader_key = f"{tid}__{provider}"
                    name_suffix = f"·{short}"
                else:
                    api_key = str(row.get("api_key") or "").strip() or creds["api_key"]
                    base_url = str(row.get("base_url") or "").strip() or creds["base_url"]
                    model = str(row.get("model") or "").strip() or creds["model"]
                    trader_key = tid
                    name_suffix = ""
                traders.append(
                    LLMTrader(
                        persona,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        enabled=True,
                        use_llm=bool(row.get("use_llm", True)),
                        temperature=float(row.get("temperature", 0.4)),
                        memory=self.memory,
                        timeout=int(row.get("timeout", tc.get("timeout", 60))),
                        provider=provider,
                        trader_key=trader_key,
                        name_suffix=name_suffix,
                    )
                )
            if dual:
                logger.info(
                    "议会小队已组建: %s (%s) × %d 人",
                    label, creds["model"], len(order),
                )

        if dual:
            # 双模默认并行度至少覆盖两队
            self.max_workers = max(self.max_workers, min(12, len(traders)))
        return traders

    def reload_from_config(self, config: Optional[Dict] = None) -> None:
        if config is not None:
            self.config = config
        tc = self.config.get("trader_council") or {}
        self.enabled = bool(tc.get("enabled", True))
        self.max_workers = int(tc.get("max_workers", 6))
        self.traders = self._build_traders(tc)

    def deliberate(self, context: MarketContext) -> CouncilVoteSummary:
        if not self.enabled or not self.traders:
            return CouncilVoteSummary(
                votes=[],
                long_weight=0,
                short_weight=0,
                wait_weight=1,
                final_action=Action.WAIT,
                final_confidence=0.0,
                consensus=False,
                agreement=0.0,
                transcript="交易员议会未启用",
            )

        votes = self._collect_votes(context)
        weights = self.memory.get_all_weights([t.trader_id for t in self.traders])
        debate = self._debate(votes)
        chair = ""
        if self.chair_llm_summary and self.ai_analyzer and votes:
            chair = self._chair_summary(votes, debate, context)
            if chair:
                debate.append(
                    DebateRound(
                        topic="主席综合",
                        researcher_point=chair[:160],
                        quant_point="议会主席",
                        conflict=False,
                        resolution=chair[:240],
                    )
                )

        dqn_shadow: Optional[Dict[str, Any]] = None
        try:
            from bnb_quant_tool.dqn_shadow import shadow_vote

            ind = {}
            if context.indicators:
                ind = dict(context.indicators)
            dqn_shadow = shadow_vote(ind, config=self.config)
        except Exception as e:
            logger.debug("dqn shadow in council: %s", e)

        long_w, short_w, wait_w = self._tally(votes, weights, dqn_shadow=dqn_shadow)
        final, conf, agreement, consensus = self._decide(long_w, short_w, wait_w)

        teams = self._team_breakdown(votes, weights)
        merge_note = ""
        if len(teams) >= 2:
            final, conf, consensus, merge_note = self._merge_team_decisions(
                teams, final, conf, consensus, agreement
            )
            debate.append(
                DebateRound(
                    topic="双模合并",
                    researcher_point=" | ".join(
                        f"{(v.get('label') or k)}→{v.get('final_action')}"
                        for k, v in teams.items()
                    ),
                    quant_point=merge_note or "加权合并",
                    conflict="对立" in (merge_note or ""),
                    resolution=merge_note or "已合并",
                )
            )

        final_conf = clamp(conf * (0.7 + 0.3 * agreement), 0.0, 1.0)
        if final == Action.WAIT:
            final_conf = max(final_conf, 0.35)
        if "同向" in merge_note:
            final_conf = min(1.0, final_conf * 1.08)

        size_factor = self._compute_size_factor(
            final, final_conf, agreement, votes, weights
        )

        transcript = self._build_transcript(
            votes, debate, final, final_conf, consensus,
            long_w, short_w, wait_w, chair, teams, merge_note,
        )
        if dqn_shadow and dqn_shadow.get("enabled"):
            transcript += (
                f"\n🤖 DQN影子: {dqn_shadow.get('action')} "
                f"w={dqn_shadow.get('vote_weight')} "
                f"acc={dqn_shadow.get('shadow_accuracy')}"
            )
        transcript += f"\n📐 仓位系数 size_factor={size_factor:.2f}"

        return CouncilVoteSummary(
            votes=votes,
            long_weight=long_w,
            short_weight=short_w,
            wait_weight=wait_w,
            final_action=final,
            final_confidence=final_conf,
            consensus=consensus,
            agreement=agreement,
            debate_rounds=debate,
            transcript=transcript,
            chair_summary=chair,
            teams=teams,
            merge_note=merge_note,
            size_factor=size_factor,
            dqn_shadow=dqn_shadow if (dqn_shadow or {}).get("enabled") else None,
        )

    @staticmethod
    def _compute_size_factor(
        final: Action,
        final_conf: float,
        agreement: float,
        votes: List[AgentOpinion],
        weights: Dict[str, float],
    ) -> float:
        """方向一致票的 confidence×历史权重 → 0~1 仓位系数。"""
        if final == Action.WAIT:
            return 0.0
        num = den = 0.0
        for v in votes:
            if v.action != final:
                continue
            tid = str(v.metadata.get("trader_id") or "")
            w = float(weights.get(tid, 1.0))
            c = max(0.0, min(1.0, float(v.confidence or 0)))
            num += w * c
            den += w
        base = (num / den) if den > 0 else float(final_conf or 0.4)
        # 一致度不足时压仓
        agree_f = 0.55 + 0.45 * max(0.0, min(1.0, float(agreement or 0)))
        conf_f = 0.5 + 0.5 * max(0.0, min(1.0, float(final_conf or 0)))
        return round(max(0.15, min(1.0, base * agree_f * conf_f)), 4)

    @staticmethod
    def _tally(
        votes: List[AgentOpinion],
        weights: Dict[str, float],
        dqn_shadow: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, float, float]:
        long_w = short_w = wait_w = 0.0
        for v in votes:
            tid = str(v.metadata.get("trader_id") or "")
            w = float(weights.get(tid, 1.0)) * max(0.15, float(v.confidence))
            if v.action == Action.LONG:
                long_w += w
            elif v.action == Action.SHORT:
                short_w += w
            else:
                wait_w += w
        # DQN 影子辅助票
        if dqn_shadow and dqn_shadow.get("enabled"):
            vw = float(dqn_shadow.get("vote_weight") or 0.15)
            conf = max(0.15, float(dqn_shadow.get("confidence") or 0.4))
            w = vw * conf * 3.0  # 放大到与单交易员可比量级
            act = str(dqn_shadow.get("action") or "WAIT").upper()
            if act == "LONG":
                long_w += w
            elif act == "SHORT":
                short_w += w
            else:
                wait_w += w
        return long_w, short_w, wait_w

    def _decide(
        self, long_w: float, short_w: float, wait_w: float
    ) -> Tuple[Action, float, float, bool]:
        total = long_w + short_w + wait_w or 1.0
        directional = long_w + short_w
        agreement = 0.0
        if directional > 0:
            agreement = max(long_w, short_w) / directional

        final = Action.WAIT
        conf = wait_w / total
        if long_w > short_w and long_w > wait_w:
            final = Action.LONG
            conf = long_w / total
        elif short_w > long_w and short_w > wait_w:
            final = Action.SHORT
            conf = short_w / total
        elif self.wait_if_split and abs(long_w - short_w) < 0.15 * total:
            final = Action.WAIT
            conf = max(wait_w / total, 0.4)

        consensus = agreement >= self.min_consensus and final != Action.WAIT
        if final != Action.WAIT and agreement < self.min_consensus:
            final = Action.WAIT
            consensus = False
        return final, conf, agreement, consensus

    def _team_breakdown(
        self, votes: List[AgentOpinion], weights: Dict[str, float]
    ) -> Dict[str, Any]:
        from bnb_quant_tool.llm_provider import PROVIDER_LABELS

        by_team: Dict[str, List[AgentOpinion]] = {}
        for v in votes:
            team = str(v.metadata.get("provider") or "default")
            by_team.setdefault(team, []).append(v)

        out: Dict[str, Any] = {}
        for team, tvs in by_team.items():
            lw, sw, ww = self._tally(tvs, weights)
            final, conf, agreement, consensus = self._decide(lw, sw, ww)
            out[team] = {
                "label": PROVIDER_LABELS.get(team, team),
                "count": len(tvs),
                "long_weight": round(lw, 4),
                "short_weight": round(sw, 4),
                "wait_weight": round(ww, 4),
                "final_action": final.value,
                "final_confidence": round(conf, 4),
                "agreement": round(agreement, 4),
                "consensus": consensus,
            }
        return out

    @staticmethod
    def _merge_team_decisions(
        teams: Dict[str, Any],
        pooled_final: Action,
        pooled_conf: float,
        pooled_consensus: bool,
        agreement: float,
    ) -> Tuple[Action, float, bool, str]:
        """两套人马终局合并：同向增强；多空对立 → WAIT。"""
        actions = {
            k: Action(str(v.get("final_action") or "WAIT"))
            for k, v in teams.items()
        }
        labels = [str(v.get("label") or k) for k, v in teams.items()]
        directional = {k: a for k, a in actions.items() if a != Action.WAIT}
        unique_dir = set(directional.values())

        if len(unique_dir) > 1:
            return (
                Action.WAIT,
                max(pooled_conf, 0.45),
                False,
                f"{' vs '.join(labels)} 方向对立 → 合并 WAIT",
            )
        if len(unique_dir) == 1 and len(directional) == len(actions):
            only = next(iter(unique_dir))
            return (
                only,
                max(pooled_conf, float(sum(float(v.get('final_confidence') or 0) for v in teams.values()) / len(teams))),
                True,
                f"{' + '.join(labels)} 同向 → {only.value}",
            )
        if len(unique_dir) == 1:
            only = next(iter(unique_dir))
            return (
                only,
                pooled_conf * 0.92,
                pooled_consensus,
                f"一队观望、一队 {only.value} → 取有方向侧（降权）",
            )
        return pooled_final, pooled_conf, pooled_consensus, f"{' + '.join(labels)} 均观望 → WAIT"


    def as_proxy_opinions(
        self, summary: CouncilVoteSummary
    ) -> Tuple[AgentOpinion, AgentOpinion, AgentOpinion]:
        """把议会结果映射为研究员/量化/学习，兼容旧风控接口。"""
        from .base import AgentRole
        from bnb_quant_tool.llm_provider import persona_base_id

        macro_ids = {"macro", "flow"}
        tech_ids = {"momentum", "mean_reversion", "structure"}
        contra = [
            v for v in summary.votes
            if persona_base_id(v.metadata.get("trader_id")) == "contrarian"
            or persona_base_id(v.metadata.get("persona_id")) == "contrarian"
        ]

        def _agg(subset: List[AgentOpinion], label: str, role: AgentRole) -> AgentOpinion:
            if not subset:
                return AgentOpinion(
                    role=role,
                    stance=Stance.NEUTRAL,
                    action=Action.WAIT,
                    confidence=0.3,
                    score=0.0,
                    summary=f"{label}: 无有效投票",
                )
            score = sum(v.score * v.confidence for v in subset) / (
                sum(v.confidence for v in subset) or 1.0
            )
            longs = sum(1 for v in subset if v.action == Action.LONG)
            shorts = sum(1 for v in subset if v.action == Action.SHORT)
            if longs > shorts:
                action = Action.LONG
            elif shorts > longs:
                action = Action.SHORT
            else:
                action = Action.WAIT
            stance = (
                Stance.BULLISH if action == Action.LONG
                else Stance.BEARISH if action == Action.SHORT
                else Stance.NEUTRAL
            )
            return AgentOpinion(
                role=role,
                stance=stance,
                action=action,
                confidence=clamp(
                    sum(v.confidence for v in subset) / len(subset), 0.2, 0.95
                ),
                score=clamp(score),
                summary=f"{label}: {action.value}（{len(subset)} 票聚合）",
                evidence=[v.summary for v in subset[:4]],
                concerns=[c for v in subset for c in v.concerns[:1]][:4],
                metadata={"proxy": True, "from_council": True},
            )

        researcher = _agg(
            [
                v for v in summary.votes
                if persona_base_id(v.metadata.get("persona_id") or v.metadata.get("trader_id")) in macro_ids
            ],
            "研究员代理(宏观/资金)",
            AgentRole.RESEARCHER,
        )
        quant = _agg(
            [
                v for v in summary.votes
                if persona_base_id(v.metadata.get("persona_id") or v.metadata.get("trader_id")) in tech_ids
            ],
            "量化代理(趋势/结构/回归)",
            AgentRole.QUANT,
        )
        # 议会最终方向优先反映到量化代理（风控以 quant.action 为提案）
        quant.action = summary.final_action
        quant.confidence = summary.final_confidence
        if summary.final_action == Action.LONG:
            quant.stance = Stance.BULLISH
            quant.score = max(0.2, summary.agreement)
        elif summary.final_action == Action.SHORT:
            quant.stance = Stance.BEARISH
            quant.score = -max(0.2, summary.agreement)
        else:
            quant.stance = Stance.NEUTRAL
            quant.score = 0.0

        learn_score = 0.0
        learn_concerns: List[str] = []
        learn_evidence: List[str] = []
        if contra:
            # 双模下可能有多张唱反调票，聚合而非只取第一张
            learn_score = (sum(c.score for c in contra) / len(contra)) * 0.5
            learn_evidence.extend(c.summary for c in contra[:4])
            for c in contra:
                learn_concerns.extend(c.concerns[:1])
            learn_concerns = learn_concerns[:4]
        if not summary.consensus and summary.final_action != Action.WAIT:
            learn_score -= 0.2
            learn_concerns.append("议会共识不足")
        learning = AgentOpinion(
            role=AgentRole.LEARNING,
            stance=Stance.NEUTRAL if abs(learn_score) < 0.15 else (
                Stance.BULLISH if learn_score > 0 else Stance.BEARISH
            ),
            action=Action.WAIT if learn_score < -0.3 else summary.final_action,
            confidence=0.5,
            score=clamp(learn_score),
            summary=f"学习/反共识视角: score={learn_score:+.2f}",
            evidence=learn_evidence,
            concerns=learn_concerns,
            metadata={"from_council": True},
        )
        return researcher, quant, learning

    def dashboard(self) -> Dict[str, Any]:
        from bnb_quant_tool.llm_provider import PROVIDER_LABELS, is_dual_mode

        ids = [t.trader_id for t in self.traders]
        mem = self.memory.dashboard(ids)
        cards = []
        for t in self.traders:
            m = mem.get(t.trader_id) or {}
            cards.append({
                "id": t.trader_id,
                "persona_id": t.persona.id,
                "name": t.display_name,
                "emoji": t.persona.emoji,
                "style": t.persona.style,
                "color": t.persona.color,
                "enabled": t.enabled,
                "use_llm": t.use_llm,
                "has_llm_key": t.has_llm,
                "provider": t.provider,
                "provider_label": PROVIDER_LABELS.get(t.provider, t.provider),
                "model": t.model,
                "api_key_set": bool(t.api_key and t.has_llm),
                "accuracy": m.get("accuracy", 0.5),
                "weight": m.get("weight", 1.0),
                "samples": m.get("total", 0),
                "lessons_preview": m.get("lessons_preview", ""),
                "recent": m.get("recent") or [],
            })
        return {
            "enabled": self.enabled,
            "traders": cards,
            "count": len(cards),
            "dual_mode": is_dual_mode(self.config),
            "providers": sorted({t.provider for t in self.traders if t.provider}),
        }

    # ── internal ───────────────────────────────────────────────

    def _collect_votes(self, context: MarketContext) -> List[AgentOpinion]:
        from bnb_quant_tool.llm_provider import persona_base_id

        active = [t for t in self.traders if t.enabled]
        if not active:
            return []
        if not self.parallel or len(active) == 1:
            return [t.analyze(context) for t in active]

        def _error_stub(trader: LLMTrader, err: str) -> AgentOpinion:
            tid = trader.trader_id
            persona = PERSONA_BY_ID.get(persona_base_id(tid))
            from .base import AgentRole
            return AgentOpinion(
                role=AgentRole.QUANT,
                stance=Stance.NEUTRAL,
                action=Action.WAIT,
                confidence=0.1,
                score=0.0,
                summary=f"{persona.name if persona else tid}: 分析异常",
                concerns=[err[:100]],
                metadata={
                    "trader_id": tid,
                    "persona_id": persona_base_id(tid),
                    "provider": getattr(trader, "provider", None) or "",
                    "source": "error",
                },
            )

        # 墙钟超时：避免 as_completed 无限等；对齐 trader timeout，并封顶
        tc = self.config.get("trader_council") or {}
        llm = self.config.get("llm") or {}
        try:
            per = int(tc.get("timeout") or 60)
        except (TypeError, ValueError):
            per = 60
        try:
            llm_to = float(llm.get("request_timeout_seconds") or 15)
        except (TypeError, ValueError):
            llm_to = 15.0
        wall = max(per, int(llm_to) + 5)
        wall = min(max(wall, 5), 120)

        results: Dict[str, AgentOpinion] = {}
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(active)))
        try:
            from concurrent.futures import wait as fut_wait

            futures = {pool.submit(t.analyze, context): t for t in active}
            done, not_done = fut_wait(list(futures.keys()), timeout=float(wall))
            for fut in not_done:
                trader = futures[fut]
                logger.error("trader %s timed out after %ss", trader.trader_id, wall)
                results[trader.trader_id] = _error_stub(
                    trader, f"议会超时({wall}s)"
                )
            for fut in done:
                trader = futures[fut]
                tid = trader.trader_id
                try:
                    results[tid] = fut.result()
                except Exception as e:
                    logger.error("trader %s failed: %s", tid, e)
                    results[tid] = _error_stub(trader, str(e))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # 保持配置顺序
        ordered = []
        for t in active:
            if t.trader_id in results:
                ordered.append(results[t.trader_id])
        return ordered

    def _debate(self, votes: List[AgentOpinion]) -> List[DebateRound]:
        rounds: List[DebateRound] = []
        if not votes:
            return rounds

        actions = {v.action for v in votes}
        conflict = Action.LONG in actions and Action.SHORT in actions
        longs = [v for v in votes if v.action == Action.LONG]
        shorts = [v for v in votes if v.action == Action.SHORT]
        waits = [v for v in votes if v.action == Action.WAIT]

        rounds.append(
            DebateRound(
                topic="方向分歧",
                researcher_point=(
                    "多头: " + ", ".join(
                        str(v.metadata.get("trader_name") or "?") for v in longs
                    ) or "无"
                ),
                quant_point=(
                    "空头: " + ", ".join(
                        str(v.metadata.get("trader_name") or "?") for v in shorts
                    ) or "无"
                ),
                conflict=conflict,
                resolution=(
                    f"多{len(longs)} / 空{len(shorts)} / 观望{len(waits)}"
                    + (" — 存在多空对打" if conflict else " — 无直接对打")
                ),
            )
        )

        confs = [v.confidence for v in votes]
        gap = max(confs) - min(confs) if confs else 0
        rounds.append(
            DebateRound(
                topic="置信度离散度",
                researcher_point=f"最高 {max(confs):.0%}" if confs else "—",
                quant_point=f"最低 {min(confs):.0%}" if confs else "—",
                conflict=gap > 0.35,
                resolution=f"置信差距 {gap:.0%}",
            )
        )

        all_concerns = [c for v in votes for c in v.concerns]
        rounds.append(
            DebateRound(
                topic="风险关切",
                researcher_point="; ".join(all_concerns[:2]) or "无明显风险",
                quant_point=f"共 {len(all_concerns)} 条顾虑",
                conflict=len(all_concerns) >= 5,
                resolution="顾虑较多，建议降仓或 WAIT" if len(all_concerns) >= 5 else "顾虑可控",
            )
        )
        return rounds

    def _chair_summary(
        self,
        votes: List[AgentOpinion],
        debate: List[DebateRound],
        context: MarketContext,
    ) -> str:
        try:
            lines = [f"{v.metadata.get('emoji','')} {v.metadata.get('trader_name')}: "
                     f"{v.action.value} ({v.confidence:.0%}) — {v.summary}" for v in votes]
            for d in debate:
                lines.append(f"[辩论:{d.topic}] {d.resolution}")
            prompt = (
                f"你是交易员议会主席。标的 {context.symbol} @ {context.current_price}。\n"
                "以下可能包含 DeepSeek 与千问两套人马的观点。"
                "用 2-3 句中文总结共识/分歧，并给出最终倾向"
                "（LONG/SHORT/WAIT）及一句执行建议：\n" + "\n".join(lines)
            )
            if hasattr(self.ai_analyzer, "quick_summarize"):
                return self.ai_analyzer.quick_summarize(prompt, max_tokens=220) or ""
        except Exception as e:
            logger.debug("chair summary skipped: %s", e)
        return ""

    @staticmethod
    def _build_transcript(
        votes: List[AgentOpinion],
        debate: List[DebateRound],
        final: Action,
        conf: float,
        consensus: bool,
        long_w: float,
        short_w: float,
        wait_w: float,
        chair: str,
        teams: Optional[Dict[str, Any]] = None,
        merge_note: str = "",
    ) -> str:
        n = len(votes)
        dual = bool(teams) and len(teams) >= 2
        title = f"交易员议会（{n}人{' · 双模' if dual else ''}）"
        lines = [
            f"═══ {title} ═══",
            "",
        ]
        for v in votes:
            emoji = v.metadata.get("emoji") or "•"
            name = v.metadata.get("trader_name") or v.metadata.get("trader_id")
            src = v.metadata.get("source") or "?"
            prov = v.metadata.get("provider") or ""
            tag = f"/{prov}" if prov else ""
            lines.append(f"{emoji} {name}: {v.action.value} 置信{v.confidence:.0%} [{src}{tag}]")
            lines.append(f"   {v.summary}")
            for ev in v.evidence[:2]:
                lines.append(f"   • {ev}")
            for c in v.concerns[:1]:
                lines.append(f"   ⚠ {c}")

        if dual:
            lines.extend(["", "🏟 分队小结:"])
            for team, info in (teams or {}).items():
                lines.append(
                    f"  · {info.get('label') or team}: {info.get('final_action')} "
                    f"置信{float(info.get('final_confidence') or 0):.0%} "
                    f"| 一致度{float(info.get('agreement') or 0):.0%} "
                    f"| {info.get('count')}人"
                )
            if merge_note:
                lines.append(f"  → 合并: {merge_note}")

        lines.extend(["", "💬 讨论:"])
        for rnd in debate:
            icon = "⚡" if rnd.conflict else "✓"
            lines.append(f"  {icon} [{rnd.topic}] {rnd.resolution}")

        if chair:
            lines.extend(["", f"🪑 主席: {chair}"])

        lines.extend([
            "",
            f"📊 加权票: 多 {long_w:.2f} | 空 {short_w:.2f} | 观望 {wait_w:.2f}",
            f"📋 最终决策: {final.value} (置信 {conf:.0%}) | 共识: {'是' if consensus else '否'}",
            "═══════════════════════",
        ])
        return "\n".join(lines)


def default_trader_council_config(global_model: str = "deepseek-chat") -> Dict[str, Any]:
    """生成 config.yaml 用的默认 trader_council 段。"""
    traders = []
    for p in DEFAULT_PERSONAS:
        traders.append({
            "id": p.id,
            "name": p.name,
            "enabled": True,
            "use_llm": True,
            "api_key": "",          # 空=继承 llm.provider 凭据
            "base_url": "",
            "model": "",
            "temperature": 0.4,
        })
    return {
        "enabled": True,
        "parallel": True,
        "max_workers": 12,
        "min_consensus": 0.45,
        "wait_if_split": True,
        "chair_llm_summary": True,
        "memory_db": "data/trader_memory.db",
        "timeout": 60,
        "order": [p.id for p in DEFAULT_PERSONAS],
        "traders": traders,
    }
