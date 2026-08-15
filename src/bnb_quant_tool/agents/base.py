"""多智能体协同 — 基础类型与 Agent 抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentRole(str, Enum):
    RESEARCHER = "researcher"
    QUANT = "quant"
    RISK = "risk"
    TRADING = "trading"
    LEARNING = "learning"


class Stance(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass
class AgentOpinion:
    """单个 Agent 的结构化观点。"""

    role: AgentRole
    stance: Stance
    action: Action
    confidence: float  # 0.0 ~ 1.0
    score: float  # -1.0 ~ +1.0，正=偏多，负=偏空
    summary: str
    evidence: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role.value,
            "stance": self.stance.value,
            "action": self.action.value,
            "confidence": round(self.confidence, 4),
            "score": round(self.score, 4),
            "summary": self.summary,
            "evidence": self.evidence,
            "concerns": self.concerns,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


@dataclass
class DebateRound:
    """研究员与量化 Agent 之间的辩论回合。"""

    topic: str
    researcher_point: str
    quant_point: str
    conflict: bool
    resolution: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "researcher_point": self.researcher_point,
            "quant_point": self.quant_point,
            "conflict": self.conflict,
            "resolution": self.resolution,
        }


@dataclass
class RiskVerdict:
    """风控 Agent 裁决 — 拥有一票否决权。"""

    approved: bool
    vetoed: bool
    veto_reason: str
    action: Action
    confidence_adjustment: float  # 对最终置信度的修正 (-1 ~ +1)
    objections: List[str] = field(default_factory=list)
    requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "action": self.action.value,
            "confidence_adjustment": round(self.confidence_adjustment, 4),
            "objections": self.objections,
            "requirements": self.requirements,
        }


@dataclass
class TradingResult:
    """交易 Agent 执行结果。"""

    executed: bool
    position_id: Optional[int]
    message: str
    feedback: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executed": self.executed,
            "position_id": self.position_id,
            "message": self.message,
            "feedback": self.feedback,
        }


@dataclass
class DeliberationResult:
    """多智能体协同完整输出。"""

    researcher: AgentOpinion
    quant: AgentOpinion
    risk_verdict: RiskVerdict
    debate_rounds: List[DebateRound]
    final_action: Action
    final_confidence: float
    consensus: bool
    transcript: str
    trading_result: Optional[TradingResult] = None
    learning: Optional[AgentOpinion] = None
    council: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        votes = []
        if self.council and isinstance(self.council.get("votes"), list):
            votes = self.council["votes"]
        return {
            "researcher": self.researcher.to_dict(),
            "quant": self.quant.to_dict(),
            "learning": self.learning.to_dict() if self.learning else None,
            "risk_verdict": self.risk_verdict.to_dict(),
            "debate_rounds": [d.to_dict() for d in self.debate_rounds],
            "final_action": self.final_action.value,
            "final_confidence": round(self.final_confidence, 4),
            "consensus": self.consensus,
            "transcript": self.transcript,
            "trading_result": self.trading_result.to_dict() if self.trading_result else None,
            "council": self.council,
            "agent_votes": votes,
            "votes": votes,
        }


@dataclass
class MarketContext:
    """传递给各 Agent 的市场上下文。"""

    symbol: str
    timeframe: str
    current_price: float
    indicators: Dict[str, Any]
    trade_advice: Dict[str, Any]
    ai_analysis: Optional[Dict[str, Any]] = None
    institutional: Optional[Dict[str, Any]] = None
    multi_timeframe: Optional[Dict[str, Any]] = None
    sentiment: Optional[Dict[str, Any]] = None
    news_summary: Optional[Dict[str, Any]] = None
    news_items: Optional[List[Dict]] = None
    onchain: Optional[Dict[str, Any]] = None
    macro: Optional[Dict[str, Any]] = None
    bnb_factors: Optional[Dict[str, Any]] = None
    market_regime: Optional[Dict[str, Any]] = None
    learning_insights: Optional[Dict[str, Any]] = None
    pattern_insight: Optional[Dict[str, Any]] = None


class BaseAgent(ABC):
    """Agent 基类。"""

    role: AgentRole

    @abstractmethod
    def analyze(self, context: MarketContext) -> AgentOpinion:
        """分析市场并输出结构化观点。"""


def stance_from_score(score: float, bull_threshold: float = 0.15, bear_threshold: float = -0.15) -> Stance:
    if score >= bull_threshold:
        return Stance.BULLISH
    if score <= bear_threshold:
        return Stance.BEARISH
    return Stance.NEUTRAL


def action_from_stance(stance: Stance) -> Action:
    if stance == Stance.BULLISH:
        return Action.LONG
    if stance == Stance.BEARISH:
        return Action.SHORT
    return Action.WAIT


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))
