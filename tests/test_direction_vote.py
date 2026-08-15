"""方向投票与门控 — 避免长期 WAIT。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.trade_advisor import TradeAdvisor, ACTION_LONG, ACTION_SHORT, ACTION_WAIT


def _advisor(**overrides):
    cfg = {
        "direction_vote_threshold": 0.10,
        "ai_tiebreak_min_confidence": 0.58,
        "inst_tiebreak_min_confidence": 0.55,
        "inst_vote_skew_min": 0.42,
    }
    cfg.update(overrides)
    return TradeAdvisor(cfg)


def test_ai_tiebreak_when_scores_close():
    ta = _advisor()
    action, _, votes = ta._decide_action(
        {"signal": "BUY", "confidence": 0.58, "trend": "震荡"},
        {"consensus_signal": "SELL", "consensus_confidence": 0.62,
         "buy_signals": 2, "sell_signals": 5, "hold_signals": 6},
    )
    assert action == ACTION_LONG
    assert votes["decision_reason"] == "ai_tiebreak"


def test_inst_skew_when_consensus_hold():
    ta = _advisor()
    action, _, votes = ta._decide_action(
        {"signal": "持有", "confidence": 0.55, "trend": "看涨"},
        {"consensus_signal": "HOLD", "consensus_confidence": 0.5,
         "buy_signals": 7, "sell_signals": 2, "hold_signals": 4},
    )
    assert action == ACTION_LONG
    assert votes["long_score"] > votes["short_score"]


def test_bnb_trade_bias_promotes_short():
    ta = _advisor(direction_vote_threshold=0.15)
    action, _, votes = ta._decide_action(
        {"signal": "持有", "confidence": 0.52, "trend": "震荡"},
        {"consensus_signal": "HOLD", "consensus_confidence": 0.5,
         "buy_signals": 4, "sell_signals": 4, "hold_signals": 5},
        bnb_factors={
            "trade_bias": "SHORT",
            "bnb_score": -0.40,
            "event_cycle": {"suggest_short": True},
        },
    )
    assert action == ACTION_SHORT
    assert votes["decision_reason"] == "bnb_trade_bias"
    assert votes["short_score"] > votes["long_score"]


def test_event_cycle_flips_long_to_short():
    ta = _advisor()
    action, reason, _ = ta._apply_event_cycle_filter(
        ACTION_LONG,
        {"event_cycle": {
            "enabled": True,
            "phase": "unlock_dump",
            "phase_label": "解锁砸盘期",
            "block_long": True,
            "suggest_short": True,
        }},
    )
    assert action == ACTION_SHORT
    assert "转做空" in reason


def test_mining_hedge_flips_long_to_short():
    ta = _advisor()
    action, reason = ta._apply_mining_event_filter(
        ACTION_LONG,
        {"mining_event": {"suggest_hedge_short": True}},
    )
    assert action == ACTION_SHORT
    assert "转做空" in reason


def test_follow_ai_parses_sell_signal():
    ta = _advisor()
    assert ta._parse_ai_action({"signal": "卖出", "confidence": 0.72}) == ACTION_SHORT
    assert ta._parse_ai_action({"signal": "买入", "confidence": 0.72}) == ACTION_LONG
    assert ta._parse_ai_action({"signal": "持有", "confidence": 0.72}) == ACTION_WAIT


def test_follow_ai_direction_in_build_advice():
    ta = _advisor()
    ta.follow_ai_direction = True
    advice = ta.build_advice(
        symbol="BNBUSDT",
        timeframe="1h",
        current_price=600.0,
        indicators={"ATR": 8.0, "RSI": 55},
        ai_analysis={
            "signal": "卖出",
            "confidence": 0.72,
            "trend": "看跌",
            "entry_price": 600.0,
            "stop_loss": 612.0,
            "take_profit": 580.0,
        },
        institutional={
            "consensus_signal": "BUY",
            "consensus_confidence": 0.70,
            "buy_signals": 8, "sell_signals": 2, "hold_signals": 3,
        },
        multi_timeframe={"recommended_action": "LONG", "weighted_score": 0.8},
        news_summary={"polarity": "bullish", "confidence": 0.7},
    )
    assert advice["ai_action"] == ACTION_SHORT
    assert advice["raw_action"] == ACTION_SHORT
    assert advice["votes"]["decision_reason"] == "follow_ai"


def test_gate_wait_message_shows_scores():
    ta = _advisor()
    votes = {
        "decided_action": ACTION_WAIT,
        "long_score": 0.12,
        "short_score": 0.10,
        "vote_threshold": 0.10,
    }
    passed, reasons = ta._gate_check(
        ACTION_WAIT, "弱", None, {"confidence": 0.5}, {},
        votes=votes,
    )
    assert passed is False
    assert "综合投票未分出方向" in reasons[0]
    assert "0.12" in reasons[0]


def test_ai_explicit_sell_when_vote_close_and_inst_buy_skew():
    """AI 卖出 55% + 机构 HOLD 偏多票 → 应出 SHORT 而非 WAIT。"""
    ta = _advisor(min_confidence=0.50, ai_tiebreak_min_confidence= 0.58)
    action, _, votes = ta._decide_action(
        {
            "signal": "卖出",
            "confidence": 0.55,
            "trend": "看跌",
            "analysis": "逆势轻仓做空，目标看MA20",
        },
        {
            "consensus_signal": "HOLD",
            "consensus_confidence": 0.50,
            "buy_signals": 5,
            "sell_signals": 2,
            "hold_signals": 11,
        },
    )
    assert action == ACTION_SHORT
    assert votes["ai_direction"] == ACTION_SHORT


def test_ai_explicit_tiebreak_when_learning_boosts_opposite_side():
    """信念/模式记忆抬高对立方向时，AI 明确卖出仍应胜出。"""
    ta = _advisor(min_confidence=0.50)
    action, _, votes = ta._decide_action(
        {"signal": "卖出", "confidence": 0.55, "trend": "看跌"},
        {
            "consensus_signal": "HOLD",
            "consensus_confidence": 0.50,
            "buy_signals": 5,
            "sell_signals": 2,
            "hold_signals": 11,
        },
        learning_insights={
            "institutional_conviction": {
                "conviction": 0.65,
                "factors": [{"name": "技术指标偏向", "score": 0.45}],
            },
            "pattern_memory": {"matched": 20, "win_rate": 0.78, "avg_pnl_usdt": 15},
        },
        bnb_factors={"bnb_score": 0.38, "trade_bias": "LONG"},
    )
    assert action == ACTION_SHORT
    assert votes["long_score"] > votes["short_score"]
    assert abs(votes["long_score"] - votes["short_score"]) < 0.08
    assert votes["decision_reason"] == "ai_explicit_tiebreak"


def test_inst_skew_skipped_when_ai_says_opposite():
    ta = _advisor()
    _, _, votes = ta._decide_action(
        {"signal": "卖出", "confidence": 0.55, "trend": "看跌"},
        {
            "consensus_signal": "HOLD",
            "consensus_confidence": 0.50,
            "buy_signals": 7,
            "sell_signals": 2,
            "hold_signals": 4,
        },
    )
    assert votes["short_score"] >= 0.30


def test_gate_no_misleading_message_when_filter_blocked():
    ta = _advisor()
    votes = {"decided_action": ACTION_LONG, "long_score": 0.35, "short_score": 0.10}
    passed, reasons = ta._gate_check(
        ACTION_WAIT, "中", 2.0, {"confidence": 0.7}, {},
        votes=votes,
    )
    assert passed is False
    assert not any("多空力量接近" in r for r in reasons)
