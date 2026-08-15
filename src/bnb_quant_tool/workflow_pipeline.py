"""
WorkflowPipeline — AI 量化工作流编排门面。

SignalLayer → Proposal → RiskEngine → Execute → Scoreboard

对齐开源能力合流（QuantDinger / Freqtrade / AI-Trader / AICoin）：
- Signal：技术 + 机构 + 订单流 + 情绪/链上
- Proposal：LLM / Advisor 提案（可跳过）
- Risk：唯一否决权（analysis_pipeline gates）
- Execute：纸面（默认）/ 未来 LiveBrokerAdapter
- Scoreboard：E[R] 与漏斗可审计

本模块不重写现有引擎，只提供统一入口与阶段快照，便于扩展与文档对齐。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class WorkflowSnapshot:
    stage: str
    ok: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not d.get("at"):
            d["at"] = _now()
        return d


class WorkflowPipeline:
    """轻量阶段记录器；实际计算委托既有模块。"""

    STAGES = ("signal", "proposal", "risk", "execute", "scoreboard")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.snapshots: Dict[str, WorkflowSnapshot] = {}

    def mark(self, stage: str, *, ok: bool = True, **detail: Any) -> WorkflowSnapshot:
        snap = WorkflowSnapshot(stage=stage, ok=ok, detail=detail, at=_now())
        self.snapshots[stage] = snap
        return snap

    def collect_signals(
        self,
        *,
        symbol: str = "BNBUSDT",
        sentiment: Optional[Dict] = None,
        orderflow: Optional[Dict] = None,
        institutional: Optional[Dict] = None,
        fetch_orderflow: bool = True,
    ) -> Dict[str, Any]:
        """SignalLayer：聚合软信号；可选现场拉取订单流。"""
        of = orderflow
        if of is None and fetch_orderflow:
            of_cfg = (self.config.get("orderflow") or {})
            if of_cfg.get("enabled", True):
                try:
                    from bnb_quant_tool.orderflow_signal import fetch_orderflow as _fetch

                    of = _fetch(symbol, self.config)
                except Exception as e:
                    logger.debug("workflow orderflow: %s", e)
                    of = {"available": False, "reason": str(e)}

        soft_votes = []
        if of and of.get("soft_vote"):
            soft_votes.append(of["soft_vote"])

        pack = {
            "symbol": symbol,
            "sentiment_score": (sentiment or {}).get("sentiment_score"),
            "orderflow": of or {},
            "institutional_consensus": (institutional or {}).get("consensus_signal"),
            "soft_votes": soft_votes,
        }
        self.mark("signal", available_orderflow=bool((of or {}).get("available")), **{
            k: pack[k] for k in ("sentiment_score", "institutional_consensus")
        })
        return pack

    def publish_scoreboard(self, paper_engine=None, orderflow: Optional[Dict] = None) -> Dict[str, Any]:
        from bnb_quant_tool.scoreboard import publish_scoreboard

        board = publish_scoreboard(
            paper_engine=paper_engine,
            config=self.config,
            orderflow=orderflow,
            extra={"workflow": {k: v.to_dict() for k, v in self.snapshots.items()}},
        )
        self.mark("scoreboard", health=board.get("health"), verdict=board.get("verdict"))
        return board

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stages": list(self.STAGES),
            "snapshots": {k: v.to_dict() for k, v in self.snapshots.items()},
            "updated_at": _now(),
        }


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
