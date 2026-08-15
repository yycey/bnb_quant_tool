"""多智能体协同架构 — 交易员议会 + 研究员/量化/风控/交易。"""

from .base import (
    Action,
    AgentOpinion,
    AgentRole,
    DebateRound,
    DeliberationResult,
    MarketContext,
    RiskVerdict,
    Stance,
    TradingResult,
)
from .council import TraderCouncil, CouncilVoteSummary, default_trader_council_config
from .learning import LearningAgent
from .llm_trader import LLMTrader
from .orchestrator import MultiAgentOrchestrator
from .personas import DEFAULT_PERSONAS, TraderPersona, get_persona
from .quant import QuantAgent
from .researcher import ResearcherAgent
from .risk_controller import RiskControllerAgent
from .trader_memory import TraderMemoryStore
from .trading import TradingAgent

__all__ = [
    "Action",
    "AgentOpinion",
    "AgentRole",
    "CouncilVoteSummary",
    "DebateRound",
    "DEFAULT_PERSONAS",
    "DeliberationResult",
    "LLMTrader",
    "MarketContext",
    "LearningAgent",
    "MultiAgentOrchestrator",
    "QuantAgent",
    "ResearcherAgent",
    "RiskControllerAgent",
    "RiskVerdict",
    "Stance",
    "TraderCouncil",
    "TraderMemoryStore",
    "TraderPersona",
    "TradingAgent",
    "TradingResult",
    "default_trader_council_config",
    "get_persona",
]
