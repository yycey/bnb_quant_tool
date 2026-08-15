"""Tests for win_rate_optimizer — learning-driven vote and gate adjustments."""

from __future__ import annotations

from bnb_quant_tool.learning_analytics import (
    apply_direction_blocks,
    apply_vote_adjustments,
    build_win_rate_context,
    format_win_rate_for_prompt,
    gate_adjustments_from_context,
)


def test_paper_low_wr_tightens_gate():
    insights = {
        "paper_trading": {"closed_trades": 20, "win_rate": 0.38, "consecutive_losses": 0},
    }
    ctx = build_win_rate_context(insights, regime="TRENDING_UP")
    assert ctx["gate_tightening"] > 0
    gt, gr = gate_adjustments_from_context(ctx)
    assert gt > 0
    assert gr == 0


def test_pattern_memory_penalty():
    insights = {
        "pattern_memory": {
            "matched": 8,
            "win_rate": 0.25,
            "avg_pnl": 1.2,
        },
    }
    ctx = build_win_rate_context(insights)
    assert ctx["long_penalty"] > 0
    ls, ss = apply_vote_adjustments(0.5, 0.3, ctx)
    assert ls < 0.5
    assert ss == 0.3


def test_regime_direction_blocks_long():
    insights = {
        "loss_patterns": [
            {
                "id": "regime_TREND_LONG",
                "type": "regime_direction",
                "title": "趋势市 + LONG 反复亏损",
                "loss_count": 5,
                "sample_count": 6,
                "suggested_tightening": 0.05,
            }
        ],
    }
    ctx = build_win_rate_context(insights, regime="TRENDING")
    action, reason = apply_direction_blocks("LONG", ctx)
    assert action == "WAIT"
    assert "做多" in reason or "LONG" in reason.upper() or "亏损" in reason


def test_consecutive_losses_tighten():
    insights = {
        "paper_trading": {"closed_trades": 10, "win_rate": 0.5, "consecutive_losses": 4},
    }
    ctx = build_win_rate_context(insights)
    assert ctx["gate_tightening"] >= 0.08


def test_format_prompt_includes_hints():
    insights = {
        "paper_trading": {"closed_trades": 20, "win_rate": 0.38, "consecutive_losses": 3},
    }
    ctx = build_win_rate_context(insights)
    text = format_win_rate_for_prompt(ctx)
    assert "胜率学习" in text
    assert "连亏" in text or "模拟盘" in text


def test_high_paper_wr_relaxes_gate():
    insights = {
        "paper_trading": {"closed_trades": 25, "win_rate": 0.58, "consecutive_losses": 0},
    }
    ctx = build_win_rate_context(insights)
    _, gr = gate_adjustments_from_context(ctx)
    assert gr > 0
