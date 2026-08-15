"""
PromotionFunnel — 策略晋升漏斗（研究 → 回测 → OOS → 纸面 → 可选实盘）。

借鉴：Freqtrade dry-run→live、QuantDinger 研究/回测/执行分层、AICoin 信号→预警→实盘。
将 StrategyLab / Mutator / LearningEvolution 的零散晋升逻辑收成可审计阶段机。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PromotionStage(str, Enum):
    DISCOVERED = "discovered"
    BACKTEST_PASS = "backtest_pass"
    OOS_PASS = "oos_pass"
    PAPER_ELIGIBLE = "paper_eligible"
    PAPER_VALIDATED = "paper_validated"
    LIVE_ELIGIBLE = "live_eligible"  # 仅人工解锁；本工具默认不自动实盘
    REJECTED = "rejected"


STAGE_ORDER = [
    PromotionStage.DISCOVERED,
    PromotionStage.BACKTEST_PASS,
    PromotionStage.OOS_PASS,
    PromotionStage.PAPER_ELIGIBLE,
    PromotionStage.PAPER_VALIDATED,
    PromotionStage.LIVE_ELIGIBLE,
]


@dataclass
class PromotionRecord:
    strategy_id: str
    name: str = ""
    stage: str = PromotionStage.DISCOVERED.value
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    live_unlocked: bool = False  # 人工确认才可进 LIVE_ELIGIBLE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PromotionFunnel:
    """策略晋升阶段机 + 持久化看板。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, db_path: Optional[str] = None):
        self.config = config or {}
        self.pf_cfg = dict(self.config.get("promotion_funnel") or {})
        self.ev_cfg = dict(self.config.get("learning_evolution") or {})
        self.db_path = db_path
        self._path = self._resolve_path()

    def _resolve_path(self) -> Path:
        custom = self.pf_cfg.get("path")
        if custom:
            return Path(custom)
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
        except Exception:
            root = Path(__file__).resolve().parents[2]
        return root / "data" / "promotion_funnel.json"

    # ── evaluate one strategy spec ─────────────────────────────

    def evaluate_spec(
        self,
        spec: Dict[str, Any],
        *,
        paper_stats: Optional[Dict[str, Any]] = None,
        live_unlocked: bool = False,
    ) -> PromotionRecord:
        sid = str(spec.get("id") or "unknown")
        name = str(spec.get("name") or sid)
        metrics = dict(spec.get("metrics") or {})
        wf = dict(spec.get("walk_forward") or {})
        reasons: List[str] = []
        stage = PromotionStage.DISCOVERED

        min_bt_trades = int(self.pf_cfg.get("min_backtest_trades", 20))
        min_bt_wr = float(self.pf_cfg.get("min_backtest_wr", 0.52))
        min_bt_pf = float(self.pf_cfg.get("min_backtest_pf", 1.2))
        require_oos = bool(
            self.pf_cfg.get(
                "require_oos",
                self.ev_cfg.get("strategy_lab_require_oos", True),
            )
        )
        min_paper_trades = int(
            self.pf_cfg.get(
                "min_paper_trades",
                self.ev_cfg.get("strategy_lab_min_live_trades", 15),
            )
        )
        min_paper_wr = float(
            self.pf_cfg.get(
                "min_paper_wr",
                self.ev_cfg.get("strategy_lab_min_live_wr", 0.45),
            )
        )
        min_er = float(self.pf_cfg.get("min_expectancy_r", 0.0))
        allow_live = bool(self.pf_cfg.get("allow_live_eligible", False))

        bt_trades = int(metrics.get("total_trades") or 0)
        bt_wr = float(metrics.get("win_rate") or 0.0)
        bt_pf = float(metrics.get("profit_factor") or 0.0)
        bt_ret = float(metrics.get("total_return_pct") or 0.0)

        if bt_trades >= min_bt_trades and bt_wr >= min_bt_wr and bt_pf >= min_bt_pf and bt_ret > 0:
            stage = PromotionStage.BACKTEST_PASS
            reasons.append(
                f"回测通过 trades={bt_trades} wr={bt_wr:.1%} pf={bt_pf:.2f}"
            )
        else:
            stage = PromotionStage.REJECTED if bt_trades > 0 else PromotionStage.DISCOVERED
            reasons.append(
                f"回测未达标 trades={bt_trades}/{min_bt_trades} "
                f"wr={bt_wr:.1%}/{min_bt_wr:.0%} pf={bt_pf:.2f}/{min_bt_pf}"
            )
            return PromotionRecord(
                strategy_id=sid,
                name=name,
                stage=stage.value,
                reasons=reasons,
                metrics=metrics,
                updated_at=_now(),
                live_unlocked=live_unlocked,
            )

        oos_ok = bool(wf.get("passed"))
        if require_oos:
            if oos_ok:
                stage = PromotionStage.OOS_PASS
                reasons.append(f"OOS 通过: {wf.get('reason') or 'ok'}")
            else:
                stage = PromotionStage.REJECTED
                reasons.append(f"OOS 未过: {wf.get('reason') or 'missing'}")
                return PromotionRecord(
                    strategy_id=sid,
                    name=name,
                    stage=stage.value,
                    reasons=reasons,
                    metrics={**metrics, "walk_forward": wf},
                    updated_at=_now(),
                    live_unlocked=live_unlocked,
                )
        else:
            stage = PromotionStage.OOS_PASS
            reasons.append("OOS 要求已关闭，视为通过")

        # OOS 通过即可进入纸面候选
        stage = PromotionStage.PAPER_ELIGIBLE
        reasons.append("可进入纸面投票池")

        ps = paper_stats or {}
        paper_n = int(ps.get("total_trades") or 0)
        paper_wr = float(ps.get("win_rate") or 0.0)
        paper_er = float(ps.get("expectancy_r") or 0.0)

        if paper_n >= min_paper_trades:
            if paper_wr >= min_paper_wr and paper_er >= min_er:
                stage = PromotionStage.PAPER_VALIDATED
                reasons.append(
                    f"纸面验证通过 n={paper_n} wr={paper_wr:.1%} E[R]={paper_er:.3f}"
                )
            else:
                reasons.append(
                    f"纸面未达标 n={paper_n} wr={paper_wr:.1%}/{min_paper_wr:.0%} "
                    f"E[R]={paper_er:.3f}/{min_er}"
                )
        else:
            reasons.append(f"纸面样本不足 {paper_n}/{min_paper_trades}")

        if (
            stage == PromotionStage.PAPER_VALIDATED
            and allow_live
            and (live_unlocked or bool(spec.get("live_unlocked")))
        ):
            stage = PromotionStage.LIVE_ELIGIBLE
            reasons.append("人工解锁实盘候选（仍须 LiveBrokerAdapter）")

        return PromotionRecord(
            strategy_id=sid,
            name=name,
            stage=stage.value,
            reasons=reasons,
            metrics={
                **metrics,
                "walk_forward": wf,
                "paper_total_trades": paper_n,
                "paper_win_rate": paper_wr,
                "paper_expectancy_r": paper_er,
            },
            updated_at=_now(),
            live_unlocked=bool(live_unlocked or spec.get("live_unlocked")),
        )

    def evaluate_all(
        self,
        specs: List[Dict[str, Any]],
        *,
        paper_stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        records = [self.evaluate_spec(s, paper_stats=paper_stats) for s in specs]
        by_stage: Dict[str, int] = {}
        for r in records:
            by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
        payload = {
            "updated_at": _now(),
            "counts": by_stage,
            "strategies": [r.to_dict() for r in records],
            "pipeline": [s.value for s in STAGE_ORDER],
        }
        self.save(payload)
        return payload

    def promote_eligible_to_voting_pool(
        self,
        specs: List[Dict[str, Any]],
        *,
        paper_stats: Optional[Dict[str, Any]] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将 PAPER_ELIGIBLE+ 策略标记 promoted，供 InstitutionalStrategies 加载。"""
        from bnb_quant_tool.strategy_lab import StrategyLab

        funnel = self.evaluate_all(specs, paper_stats=paper_stats)
        eligible_stages = {
            PromotionStage.PAPER_ELIGIBLE.value,
            PromotionStage.PAPER_VALIDATED.value,
            PromotionStage.LIVE_ELIGIBLE.value,
        }
        eligible_ids = {
            s["strategy_id"]
            for s in funnel.get("strategies") or []
            if s.get("stage") in eligible_stages
        }
        promoted: List[str] = []
        for spec in specs:
            sid = str(spec.get("id") or "")
            if sid not in eligible_ids:
                continue
            if spec.get("promoted"):
                continue
            # PAPER_VALIDATED 更严；若要求纸面验证才晋升
            require_paper = bool(self.pf_cfg.get("require_paper_validated", False))
            rec = next(
                (x for x in funnel["strategies"] if x["strategy_id"] == sid),
                None,
            )
            if require_paper and (not rec or rec["stage"] not in (
                PromotionStage.PAPER_VALIDATED.value,
                PromotionStage.LIVE_ELIGIBLE.value,
            )):
                continue
            spec["promoted"] = True
            spec["promoted_at"] = _now()
            spec["promotion_stage"] = rec["stage"] if rec else PromotionStage.PAPER_ELIGIBLE.value
            spec["promotion_reasons"] = (rec or {}).get("reasons") or []
            promoted.append(sid)

        if promoted:
            StrategyLab.save_discovered(specs, db_path=db_path or self.db_path)
            logger.info("PromotionFunnel promoted: %s", promoted)

        return {
            "status": "ok",
            "promoted": promoted,
            "funnel": funnel,
        }

    # ── persistence ────────────────────────────────────────────

    def save(self, payload: Dict[str, Any]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)

    def load(self) -> Dict[str, Any]:
        if not self._path.is_file():
            return {"strategies": [], "counts": {}, "updated_at": ""}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("promotion funnel load: %s", e)
            return {"strategies": [], "counts": {}, "updated_at": ""}

    def summary(self) -> Dict[str, Any]:
        data = self.load()
        return {
            "updated_at": data.get("updated_at"),
            "counts": data.get("counts") or {},
            "pipeline": data.get("pipeline") or [s.value for s in STAGE_ORDER],
            "n_strategies": len(data.get("strategies") or []),
        }


def load_paper_stats_for_funnel(
    paper_db_path: Optional[str] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """从纸面库取自动单统计供晋升门控。"""
    try:
        from bnb_quant_tool.paper_trading import PaperTradingEngine

        path = paper_db_path
        if not path:
            try:
                from bnb_quant_tool.data_localization import get_localization_manager
                path = str(Path(get_localization_manager().workspace) / "data" / "paper_trading.db")
            except Exception:
                path = str(Path(__file__).resolve().parents[2] / "data" / "paper_trading.db")
        if not Path(path).is_file():
            return {}
        engine = PaperTradingEngine(db_path=path, config=config or {})
        return engine.get_stats(auto_only=True)
    except Exception as e:
        logger.debug("paper stats for funnel: %s", e)
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
