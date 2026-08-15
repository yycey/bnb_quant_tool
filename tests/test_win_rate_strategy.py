"""Tests for win_rate_strategy — strategy-level win rate optimization."""

from __future__ import annotations

from bnb_quant_tool.win_rate_strategy import (
    adjust_weight_by_performance,
    analyze_institutional_consensus,
    penalize_weights_by_performance,
)


def test_adjust_weight_disables_very_bad_strategy():
    w = adjust_weight_by_performance(
        1.0,
        {"win_rate": 0.20, "total": 10, "regime_wr": 0.22, "regime_total": 8},
        {"min_samples_disable": 8, "disable_wr": 0.25},
    )
    assert w == 0.0


def test_adjust_weight_boosts_good_strategy():
    w = adjust_weight_by_performance(
        0.5,
        {"win_rate": 0.62, "total": 12},
        {"boost_wr": 0.58, "boost_mult": 1.35},
    )
    assert w > 0.5


def test_penalize_weights_renormalizes():
    weights = {"A": 0.5, "B": 0.5}
    perf = {
        "A": {"win_rate": 0.65, "total": 10},
        "B": {"win_rate": 0.25, "total": 10},
    }
    out = penalize_weights_by_performance(weights, perf)
    assert abs(sum(out.values()) - 1.0) < 0.001
    assert out["A"] > out["B"]


def test_analyze_consensus_penalizes_bad_buy_votes():
    inst = {
        "strategy_details": {
            "s1": {"strategy": "Bad Strat", "signal": "BUY", "vote_weight": 0.4},
            "s2": {"strategy": "Good Strat", "signal": "HOLD", "vote_weight": 0.3},
        }
    }
    perf = {
        "Bad Strat": {"win_rate": 0.28, "total": 8},
        "Good Strat": {"win_rate": 0.62, "total": 10},
    }
    ca = analyze_institutional_consensus(inst, perf, {})
    assert ca["long_penalty"] > 0
    assert ca["bad_strategies"]
