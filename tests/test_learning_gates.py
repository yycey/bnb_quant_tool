"""Tests for unified learning_gates module."""

from __future__ import annotations

from bnb_quant_tool.factor_attribution_learner import apply_factor_attribution_gate
from bnb_quant_tool.learning_gates import apply_win_rate_gate


def test_apply_win_rate_gate_blocks_long():
    advice = {"action": "LONG", "passed_gate": True, "gate_reasons": []}
    lc = {
        "win_rate_context": {
            "enabled": True,
            "block_long": True,
            "block_short": False,
        }
    }
    out = apply_win_rate_gate(advice, lc, {"win_rate_optimizer": {"enabled": True}})
    assert out["action"] == "WAIT"
    assert out.get("win_rate_blocked") is True
    assert any("做多" in r for r in out.get("gate_reasons") or [])


def test_apply_win_rate_gate_disabled():
    advice = {"action": "LONG", "passed_gate": True}
    lc = {"win_rate_context": {"enabled": True, "block_long": True}}
    out = apply_win_rate_gate(advice, lc, {"win_rate_optimizer": {"enabled": False}})
    assert out["action"] == "LONG"


def test_apply_factor_attribution_gate_blocks_low_confidence():
    advice = {"action": "LONG", "passed_gate": True, "confidence": 0.55, "gate_reasons": []}
    lc = {
        "factor_attribution": [
            {"factor_key": "ai_confidence", "wins": 1, "losses": 8, "win_rate": 0.11},
            {"factor_key": "rsi_signal", "wins": 2, "losses": 7, "win_rate": 0.22},
            {"factor_key": "news_sentiment", "wins": 1, "losses": 6, "win_rate": 0.14},
        ]
    }
    out = apply_factor_attribution_gate(
        advice, lc, {"factor_attribution_gate": {"enabled": True}}
    )
    assert out["action"] == "WAIT"
    assert out.get("attribution_blocked") is True
