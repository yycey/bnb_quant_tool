"""交易员议会单元测试 — 规则先验 / 投票共识 / 配置。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bnb_quant_tool.agents.base import Action, AgentRole, MarketContext
from bnb_quant_tool.agents.council import TraderCouncil, default_trader_council_config
from bnb_quant_tool.agents.llm_trader import LLMTrader
from bnb_quant_tool.agents.personas import DEFAULT_PERSONAS, PERSONA_BY_ID
from bnb_quant_tool.agents.trader_memory import TraderMemoryStore


def _ctx(**overrides) -> MarketContext:
    base = dict(
        symbol="BNBUSDT",
        timeframe="1h",
        current_price=600.0,
        indicators={"RSI": 72, "MACD": 1.0, "MACD_Signal": 0.5, "ADX": 28},
        trade_advice={"action": "WAIT", "confidence": 0.5},
        multi_timeframe={"alignment": "BULLISH"},
        sentiment={"score": 0.6},
        news_summary={"bias": "BULLISH"},
        institutional={"buy_signals": 8, "sell_signals": 2},
        onchain={"score": 0.3},
        bnb_factors={"trade_bias": "LONG"},
        market_regime={"regime": "TREND"},
    )
    base.update(overrides)
    return MarketContext(**base)


def test_six_default_personas():
    assert len(DEFAULT_PERSONAS) == 6
    ids = {p.id for p in DEFAULT_PERSONAS}
    assert ids == {
        "momentum", "mean_reversion", "macro", "structure", "flow", "contrarian",
    }


def test_default_config_has_six_traders():
    cfg = default_trader_council_config()
    assert cfg["enabled"] is True
    assert len(cfg["traders"]) == 6
    assert len(cfg["order"]) == 6


def test_momentum_prior_bullish():
    trader = LLMTrader(PERSONA_BY_ID["momentum"], use_llm=False, api_key="")
    op = trader.analyze(_ctx())
    assert op.action in (Action.LONG, Action.WAIT)
    assert op.score > 0
    assert op.metadata["source"] == "rule_prior"


def test_mean_reversion_overbought_bearish():
    trader = LLMTrader(PERSONA_BY_ID["mean_reversion"], use_llm=False, api_key="")
    op = trader.analyze(_ctx(indicators={"RSI": 78, "ADX": 18, "BB_upper": 590, "BB_lower": 550}))
    assert op.score < 0
    assert op.action in (Action.SHORT, Action.WAIT)


def test_contrarian_waits_when_not_extreme():
    trader = LLMTrader(PERSONA_BY_ID["contrarian"], use_llm=False, api_key="")
    op = trader.analyze(_ctx(
        sentiment={"score": 0.1},
        institutional={"buy_signals": 3, "sell_signals": 3},
        market_regime={"regime": "RANGE"},
    ))
    assert op.action == Action.WAIT


def test_council_votes_with_rule_priors(tmp_path: Path):
    cfg = {
        "deepseek": {"api_key": "sk-test", "model": "deepseek-chat"},
        "trader_council": {
            **default_trader_council_config(),
            "memory_db": str(tmp_path / "trader_memory.db"),
            "chair_llm_summary": False,
            "traders": [
                {**t, "use_llm": False} for t in default_trader_council_config()["traders"]
            ],
        },
    }
    council = TraderCouncil(config=cfg, project_root=str(tmp_path))
    assert len(council.traders) == 6
    summary = council.deliberate(_ctx())
    assert len(summary.votes) == 6
    assert summary.final_action in (Action.LONG, Action.SHORT, Action.WAIT)
    assert summary.transcript
    d = summary.to_dict()
    assert len(d["votes"]) == 6
    assert "final_action" in d


def test_council_consensus_under_strong_trend(tmp_path: Path):
    cfg = {
        "deepseek": {"api_key": "sk-test"},
        "trader_council": {
            **default_trader_council_config(),
            "memory_db": str(tmp_path / "tm.db"),
            "chair_llm_summary": False,
            "min_consensus": 0.4,
            "traders": [
                {**t, "use_llm": False, "enabled": True}
                for t in default_trader_council_config()["traders"]
            ],
        },
    }
    council = TraderCouncil(config=cfg, project_root=str(tmp_path))
    summary = council.deliberate(_ctx(
        indicators={"RSI": 55, "MACD": 2, "MACD_Signal": 0.5, "ADX": 40},
        multi_timeframe={
            "alignment": "BULLISH",
            "votes": {"1h": "LONG", "4h": "LONG", "1d": "LONG"},
        },
        sentiment={"score": 0.7},
        institutional={"buy_signals": 9, "sell_signals": 1},
        onchain={"score": 0.5},
        bnb_factors={"trade_bias": "LONG"},
    ))
    assert summary.final_action in (Action.LONG, Action.WAIT, Action.SHORT)
    assert 0 <= summary.final_confidence <= 1


def test_memory_accuracy_weight(tmp_path: Path):
    store = TraderMemoryStore(tmp_path / "m.db")
    for _ in range(6):
        store.record_outcome("momentum", correct=True, pnl=10)
    for _ in range(2):
        store.record_outcome("momentum", correct=False, pnl=-5)
    acc = store.get_accuracy("momentum")
    assert acc["total"] == 8
    assert acc["accuracy"] == pytest.approx(0.75)
    assert acc["weight"] > 1.0


def test_proxy_opinions_compatible(tmp_path: Path):
    cfg = {
        "deepseek": {"api_key": "sk-test"},
        "trader_council": {
            **default_trader_council_config(),
            "memory_db": str(tmp_path / "tm2.db"),
            "chair_llm_summary": False,
            "traders": [
                {**t, "use_llm": False} for t in default_trader_council_config()["traders"]
            ],
        },
    }
    council = TraderCouncil(config=cfg, project_root=str(tmp_path))
    summary = council.deliberate(_ctx())
    researcher, quant, learning = council.as_proxy_opinions(summary)
    assert researcher.role == AgentRole.RESEARCHER
    assert quant.role == AgentRole.QUANT
    assert learning.role == AgentRole.LEARNING
    assert quant.action == summary.final_action


def test_disabled_trader_skipped(tmp_path: Path):
    base = default_trader_council_config()
    traders = []
    for t in base["traders"]:
        row = {**t, "use_llm": False}
        if t["id"] == "contrarian":
            row["enabled"] = False
        traders.append(row)
    cfg = {
        "deepseek": {"api_key": "sk-test"},
        "trader_council": {
            **base,
            "memory_db": str(tmp_path / "tm3.db"),
            "chair_llm_summary": False,
            "traders": traders,
        },
    }
    council = TraderCouncil(config=cfg, project_root=str(tmp_path))
    summary = council.deliberate(_ctx())
    assert len(summary.votes) == 5
    ids = {v.metadata.get("trader_id") for v in summary.votes}
    assert "contrarian" not in ids


def test_dual_mode_builds_two_teams(tmp_path: Path):
    cfg = {
        "llm": {
            "mode": "dual",
            "provider": "dual",
            "dual_providers": ["deepseek", "qianwen"],
            "council_use_all_analyzers": True,
        },
        "deepseek": {"api_key": "sk-ds-test", "model": "deepseek-chat"},
        "qianwen": {
            "api_key": "sk-qw-test",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
        },
        "trader_council": {
            **default_trader_council_config(),
            "memory_db": str(tmp_path / "tm_dual.db"),
            "chair_llm_summary": False,
            "traders": [
                {**t, "use_llm": False}
                for t in default_trader_council_config()["traders"]
            ],
        },
    }
    council = TraderCouncil(config=cfg, project_root=str(tmp_path))
    assert len(council.traders) == 12
    providers = {t.provider for t in council.traders}
    assert providers == {"deepseek", "qianwen"}
    summary = council.deliberate(_ctx())
    assert len(summary.votes) == 12
    assert len(summary.teams) == 2
    assert summary.merge_note
    d = summary.to_dict()
    assert "teams" in d and "merge_note" in d
    assert "双模" in summary.transcript or "DeepSeek" in summary.transcript
