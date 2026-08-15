"""analysis_mode + structural strategies tests"""

from bnb_quant_tool.structural_strategies import compute_funding_carry_signal, compute_structural_vote
from bnb_quant_tool.trade_advisor import TradeAdvisor, ACTION_LONG, ACTION_SHORT, ACTION_WAIT


def test_funding_carry_extreme_long():
    sig = compute_funding_carry_signal(
        sentiment={"funding_rate": {"rate": 0.0012}},
        config={"funding_carry": {"enabled": True}},
    )
    assert sig["signal"] == "SELL"
    assert float(sig["bias"]) < 0


def test_structural_vote_short_bias():
    vote = compute_structural_vote(
        sentiment={"funding_rate": {"rate": 0.0011}},
        config={"funding_carry": {"enabled": True, "vote_weight": 0.12}},
    )
    assert float(vote.get("short_score") or 0) > float(vote.get("long_score") or 0)


def test_analysis_mode_ai_only():
    advisor = TradeAdvisor({})
    votes = {}
    action, _ = advisor._apply_analysis_mode(
        "ai_only",
        ACTION_WAIT,
        "弱",
        {"signal": "买入", "confidence": 0.72},
        {"consensus_signal": "SELL", "consensus_confidence": 0.8},
        None,
        votes,
    )
    assert action == ACTION_LONG
    assert votes.get("decision_reason") == "analysis_mode_ai_only"


def test_analysis_mode_institutional_only():
    advisor = TradeAdvisor({})
    votes = {}
    action, _ = advisor._apply_analysis_mode(
        "institutional_only",
        ACTION_LONG,
        "中",
        {"signal": "买入", "confidence": 0.9},
        {"consensus_signal": "SELL", "consensus_confidence": 0.75},
        None,
        votes,
    )
    assert action == ACTION_SHORT
    assert votes.get("decision_reason") == "analysis_mode_institutional_only"


def test_institutional_skew_buy_majority():
    """BUY=5 SELL=2 HOLD=11 时不应出现 0/0 投票。"""
    advisor = TradeAdvisor({"direction_vote_threshold": 0.08})
    action, _, votes = advisor._decide_action(
        {"signal": "持有", "confidence": 0.35, "trend": "震荡偏多",
         "analysis": "震荡偏多格局，技术指标略偏强"},
        {"consensus_signal": "HOLD", "consensus_confidence": 0.5,
         "buy_signals": 5, "sell_signals": 2, "hold_signals": 11},
        learning_insights={
            "institutional_conviction": {
                "conviction": 0.12,
                "factors": [
                    {"name": "技术指标偏向", "score": 0.7, "weight": 10},
                    {"name": "BTC领先指标", "score": 0.21, "weight": 10},
                ],
            },
            "pattern_memory": {
                "matched": 20, "win_rate": 0.95, "avg_pnl_usdt": 39.0,
            },
        },
    )
    assert float(votes.get("long_score") or 0) > 0.08
    assert action == ACTION_LONG


def test_analysis_mode_technical_only():
    advisor = TradeAdvisor({})
    votes = {}
    action, _ = advisor._apply_analysis_mode(
        "technical_only",
        ACTION_WAIT,
        "弱",
        {"signal": "持有", "confidence": 0.5},
        {"consensus_signal": "BUY"},
        {"final_signal": "SELL", "confidence": 0.65},
        votes,
    )
    assert action == ACTION_SHORT
