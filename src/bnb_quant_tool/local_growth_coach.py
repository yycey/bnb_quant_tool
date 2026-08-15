"""
LocalGrowthCoach — 围绕「下单盈利」的本地阶梯成长教练。

原则（对齐用户北星）：
1. 主线是开平仓赚钱能力（自动单 E[R] / PF / gave_back），不是堆更多云端 AI
2. AI 一步步成长：每次只推 1 个 next_lesson，避免同时拧十个旋钮
3. 本地很强：用纸面库 + 知识卡 + DQN 影子 + 本地 K 线，不依赖再加第 4 家 LLM

阶段：
  SEED        样本不足 → 攒高质量自动单
  STOP_BLEED  E[R]≤0 或亏损端失控 → 先止血
  EDGE        有正期望但不够稳 → 放大优势、压回吐
  COMPOUND    达标 → 晋升策略 / 本地 DQN / 影子 A/B
  MASTER      长期稳态 → 只做微调，禁止乱收紧
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGES = ("SEED", "STOP_BLEED", "EDGE", "COMPOUND", "MASTER")


@dataclass
class GrowthPlan:
    stage: str
    next_lesson: str
    why: str
    focus: str  # open_quality | exit_discipline | expectancy | local_compound | validation_flow
    actions: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    local_strength: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not d.get("updated_at"):
            d["updated_at"] = _now()
        return d


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _workspace(config: Optional[Dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager
        return Path(get_localization_manager().workspace)
    except Exception:
        return Path(__file__).resolve().parents[2]


def plan_path(config: Optional[Dict] = None) -> Path:
    cfg = (config or {}).get("local_growth") or {}
    if cfg.get("path"):
        return Path(cfg["path"])
    return _workspace(config) / "data" / "local_growth_plan.json"


class LocalGrowthCoach:
    """本地盈利成长教练：诊断 → 定阶 → 单点下一课。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.cfg = dict(self.config.get("local_growth") or {})

    def diagnose(
        self,
        *,
        paper_stats: Optional[Dict[str, Any]] = None,
        growth_snapshot: Optional[Dict[str, Any]] = None,
        funnel: Optional[Dict[str, Any]] = None,
        dqn_meta: Optional[Dict[str, Any]] = None,
        last_trade: Optional[Dict[str, Any]] = None,
    ) -> GrowthPlan:
        ps = paper_stats or {}
        n = int(ps.get("total_trades") or 0)
        er = float(ps.get("expectancy_r") or 0.0)
        wr = float(ps.get("win_rate") or 0.0)
        pf = float(ps.get("profit_factor") or 0.0)
        avg_win_r = float(ps.get("avg_win_r") or 0.0)
        avg_loss_r = float(ps.get("avg_loss_r") or 0.0)
        gave_back = int(ps.get("gave_back_count") or 0)
        gave_back_rate = (gave_back / n) if n else 0.0
        loss_win_ratio = (
            abs(avg_loss_r) / avg_win_r if avg_win_r > 1e-9 else (99.0 if avg_loss_r < 0 else 0.0)
        )

        min_seed = int(self.cfg.get("seed_min_trades", 8))
        master_n = int(self.cfg.get("master_min_trades", 50))
        master_er = float(self.cfg.get("master_min_er", 0.10))
        compound_er = float(self.cfg.get("compound_min_er", 0.0))
        compound_pf = float(self.cfg.get("compound_min_pf", 1.2))
        bleed_loss_mult = float(self.cfg.get("bleed_loss_win_mult", 3.0))

        metrics = {
            "total_trades": n,
            "expectancy_r": round(er, 4),
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 4),
            "avg_win_r": round(avg_win_r, 4),
            "avg_loss_r": round(avg_loss_r, 4),
            "loss_win_r_ratio": round(loss_win_ratio, 3),
            "gave_back_count": gave_back,
            "gave_back_rate": round(gave_back_rate, 4),
            "capability_level": (growth_snapshot or {}).get("capability_level"),
            "learning_maturity": (growth_snapshot or {}).get("learning_maturity"),
            "knowledge_cards": (growth_snapshot or {}).get("knowledge_cards"),
            "funnel_opened": int((funnel or {}).get("opened") or 0),
            "funnel_triggered": int((funnel or {}).get("analysis_triggered") or 0),
        }

        local_strength = self._local_strength(growth_snapshot, dqn_meta)

        try:
            from bnb_quant_tool.validation_trading import (
                is_validation_trading_enabled,
                should_encourage_validation_opens,
            )
            validation_on = is_validation_trading_enabled(self.config)
            need_flow = should_encourage_validation_opens(
                self.config, ps, funnel
            )
        except Exception:
            validation_on = False
            need_flow = False

        # ── stage machine ──
        if n < min_seed:
            stage = "SEED"
            focus = "validation_flow"
            if validation_on or need_flow:
                next_lesson = (
                    "多开多平、认错学习：有方向就开小仓，错了尽快平 "
                    "(ADMIT_WRONG)，把对错标签喂给 AI — 没有成交就没有成长"
                )
                why = f"自动单 {n}/{min_seed}；开仓密度不足时需靠验证单积累样本"
                actions = [
                    "trading_profile=validation；目标 ≥2 笔/天开平",
                    "软门控拦下时走 validation_probe 小仓（硬拦仍生效）",
                    "认错：逆向≥0.35R 或 MTF 反向 → ADMIT_WRONG；未止盈≥6h 软平",
                    "每笔平仓必学：validation_log.jsonl + trade_close_learning",
                ]
            else:
                next_lesson = (
                    "攒样本也要开单：净 RR 与置信达标就开，"
                    "平仓是唯一验证策略对错的方式"
                )
                why = f"自动单仅 {n}/{min_seed}，需要更多开平仓闭环"
                actions = [
                    "检查漏斗：analysis_triggered vs opened 转化是否过低",
                    "每笔平仓走 trade_close_learning + 知识卡提炼",
                    "本地 K 线归档保持同步",
                ]
        elif er <= 0 or (n >= 10 and loss_win_ratio >= bleed_loss_mult):
            stage = "STOP_BLEED"
            focus = "exit_discipline"
            if loss_win_ratio >= bleed_loss_mult:
                next_lesson = "止血：压缩单笔亏损端（MFE 锁盈 / 更早移保本），别先追求胜率"
                why = f"|avg_loss_r|≈{loss_win_ratio:.1f}×avg_win_r，盈亏比结构坏了"
            else:
                next_lesson = "止血：E[R]≤0 — 仍要小仓验证单流动，但缩小仓位、加严 LONG"
                why = f"E[R]={er:+.3f}≤0（n={n}）"
            actions = [
                "确认 paper_trading.mfe_lock.enabled=true",
                "验证模式：probe 仓位×0.35，用平仓结果验想法而非停手",
                "亏损局面写入 anti-memory，禁止同局面立刻再开",
                "E[R] 转正前拒绝继续收紧无关参数（避免饿死开仓）",
            ]
        elif gave_back_rate >= float(self.cfg.get("edge_gave_back_rate", 0.25)) and n >= 12:
            stage = "EDGE"
            focus = "exit_discipline"
            next_lesson = "压回吐：MFE≥0.5R 却最终亏的单太多 — 锁盈阶梯前置，勿等 TP1"
            why = f"gave_back_rate={gave_back_rate:.0%}（{gave_back}/{n}）"
            actions = [
                "检查 mfe_lock 阶梯是否在实盘 watcher 生效",
                "复盘 gave_back 单的 regime，必要时禁该 regime 开仓",
                "本地知识卡：提炼「浮盈回吐」反模式",
            ]
        elif er > compound_er and pf >= compound_pf and n >= int(self.cfg.get("compound_min_trades", 15)):
            if n >= master_n and er >= master_er and gave_back_rate < 0.15:
                stage = "MASTER"
                focus = "local_compound"
                next_lesson = "稳态微调：只做影子 A/B 与策略晋升，禁止大范围改门控"
                why = f"n={n} E[R]={er:+.3f} PF={pf:.2f}，已达本地高手门槛"
                actions = [
                    "PromotionFunnel 只晋升 OOS+纸面双过策略",
                    "DQN 影子准确率达标前不提高 vote_weight",
                    "每周一次 meta consolidation，不天天拧旋钮",
                ]
            else:
                stage = "COMPOUND"
                focus = "local_compound"
                next_lesson = "复利：把已验证边晋升进投票池，并用本地 DQN/知识卡放大"
                why = f"E[R]={er:+.3f} PF={pf:.2f} 已过线，该放大本地优势"
                actions = [
                    "跑 StrategyLab/Hyperopt，经 PromotionFunnel 晋升",
                    "平仓驱动本地 DQN 增量训练（短周期）",
                    "Scoreboard health=healthy 时才考虑略增仓位上限",
                ]
        else:
            stage = "EDGE"
            focus = "expectancy"
            next_lesson = "把正期望做厚：提高净 RR 与开仓质量，胜率只作参考"
            why = f"E[R]={er:+.3f} PF={pf:.2f} 未同时达标（目标 PF≥{compound_pf}）"
            actions = [
                "拒开净 RR 不足的单",
                "订单流/机构信念冲突时优先 WAIT",
                "用本地反事实区分 alpha vs beta，不更新运气单权重",
            ]

        # 最近一笔可微调 lesson 文案
        if last_trade and stage in ("STOP_BLEED", "EDGE"):
            side = str(last_trade.get("side") or "").upper()
            pnl = float(last_trade.get("realized_pnl_usdt") or last_trade.get("pnl") or 0)
            mfe_r = last_trade.get("mfe_r")
            if pnl < 0 and mfe_r is not None and float(mfe_r) >= 0.5:
                next_lesson = (
                    f"上一笔 {side} 浮盈 {float(mfe_r):.1f}R 却收亏 — "
                    "本课强制：触及 0.6R 必须锁保本"
                )
                focus = "exit_discipline"

        return GrowthPlan(
            stage=stage,
            next_lesson=next_lesson,
            why=why,
            focus=focus,
            actions=actions,
            metrics=metrics,
            local_strength=local_strength,
            updated_at=_now(),
        )

    def _local_strength(
        self,
        growth_snapshot: Optional[Dict],
        dqn_meta: Optional[Dict],
    ) -> Dict[str, Any]:
        gs = growth_snapshot or {}
        dm = dqn_meta or {}
        archive_ok = False
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
            # 有合并 K 线或 chunk 即视为本地行情底座可用
            ka = root / "data" / "kline_archive"
            archive_ok = ka.exists() and any(ka.rglob("*.parquet"))
        except Exception:
            archive_ok = False

        hits = int(dm.get("shadow_hits") or 0)
        correct = int(dm.get("shadow_correct") or 0)
        dqn_acc = (correct / hits) if hits >= 10 else None

        score = 0
        score += min(30, int(gs.get("knowledge_cards") or 0) // 2)
        score += min(25, int(gs.get("feedback_count") or 0))
        score += 20 if archive_ok else 0
        score += min(15, int(dm.get("train_count") or 0) * 3)
        if dqn_acc is not None:
            score += int(dqn_acc * 10)
        score = min(100, score)

        return {
            "score": score,
            "kline_archive_ready": archive_ok,
            "knowledge_cards": gs.get("knowledge_cards") or 0,
            "feedback_count": gs.get("feedback_count") or 0,
            "dqn_train_count": dm.get("train_count") or 0,
            "dqn_shadow_accuracy": round(dqn_acc, 4) if dqn_acc is not None else None,
            "verdict": (
                "本地底座强" if score >= 70
                else ("本地在长" if score >= 40 else "本地仍弱 — 先攒平仓样本与 K 线归档")
            ),
        }

    def publish(
        self,
        plan: GrowthPlan,
        *,
        also_scoreboard: bool = True,
        paper_engine=None,
        orderflow: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        path = plan_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = plan.to_dict()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)

        if also_scoreboard and self.cfg.get("publish_scoreboard", True):
            try:
                from bnb_quant_tool.scoreboard import publish_scoreboard
                publish_scoreboard(
                    paper_engine=paper_engine,
                    config=self.config,
                    orderflow=orderflow,
                    extra={"local_growth": payload},
                )
            except Exception as e:
                logger.debug("growth→scoreboard: %s", e)
        return payload

    def load(self) -> Dict[str, Any]:
        path = plan_path(self.config)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}


def run_growth_coach_after_close(
    *,
    config: Optional[Dict[str, Any]],
    paper_engine=None,
    learner=None,
    last_trade: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """平仓后统一入口：读本地统计 → 出下一课 → 落盘。"""
    cfg = config or {}
    if (cfg.get("local_growth") or {}).get("enabled", True) is False:
        return {"enabled": False}

    paper_stats: Dict[str, Any] = {}
    if paper_engine is not None:
        try:
            paper_stats = paper_engine.get_stats(auto_only=True) or {}
        except Exception as e:
            logger.debug("growth coach paper stats: %s", e)
    else:
        try:
            from bnb_quant_tool.promotion_funnel import load_paper_stats_for_funnel
            paper_stats = load_paper_stats_for_funnel(config=cfg)
        except Exception:
            paper_stats = {}

    growth_snapshot: Dict[str, Any] = {}
    if learner is not None and hasattr(learner, "get_growth_snapshot"):
        try:
            growth_snapshot = learner.get_growth_snapshot() or {}
        except Exception as e:
            logger.debug("growth snapshot: %s", e)

    funnel: Dict[str, Any] = {}
    try:
        from bnb_quant_tool.trading_profile import get_decision_funnel
        funnel = get_decision_funnel(cfg) or {}
    except Exception:
        pass

    dqn_meta: Dict[str, Any] = {}
    try:
        from bnb_quant_tool.dqn_shadow import _load_meta
        dqn_meta = _load_meta(cfg)
    except Exception:
        pass

    coach = LocalGrowthCoach(cfg)
    plan = coach.diagnose(
        paper_stats=paper_stats,
        growth_snapshot=growth_snapshot,
        funnel=funnel,
        dqn_meta=dqn_meta,
        last_trade=last_trade,
    )
    payload = coach.publish(plan, paper_engine=paper_engine)
    logger.info(
        "[LocalGrowth] stage=%s lesson=%s | %s",
        plan.stage,
        plan.next_lesson[:80],
        plan.local_strength.get("verdict"),
    )
    return payload


def growth_brief_for_prompt(config: Optional[Dict] = None) -> str:
    """注入 LLM / 经验 brief：当前成长阶段与下一课（本地文件，零额外 API）。"""
    plan = LocalGrowthCoach(config).load()
    parts: List[str] = []
    if plan:
        parts.append(
            f"[本地成长] 阶段={plan.get('stage')} | "
            f"下一课={plan.get('next_lesson')} | "
            f"原因={plan.get('why')} | "
            f"本地强度={((plan.get('local_strength') or {}).get('verdict'))}"
        )
    try:
        from bnb_quant_tool.validation_trading import get_validation_stats, is_validation_trading_enabled
        if is_validation_trading_enabled(config):
            vs = get_validation_stats(config)
            if vs.get("closed"):
                parts.append(
                    f"[验证流] 已验证{vs['closed']}笔 正确率{vs.get('accuracy_pct')}% "
                    f"— 每笔平仓都是策略/想法的对错答案"
                )
            else:
                parts.append("[验证流] 持续开平小仓验证；没有成交就没有收益也没有验证")
    except Exception:
        pass
    return " | ".join(parts)
