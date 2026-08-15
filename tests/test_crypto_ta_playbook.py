"""Tests for TA playbook + new institutional / indicator strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bnb_quant_tool.crypto_ta_playbook import (
    build_dca_plan,
    build_playbook_prompt_section,
    recommend_styles_for_regime,
)
from bnb_quant_tool.institutional_strategies import InstitutionalStrategies
from bnb_quant_tool.technical_indicators import TechnicalIndicators


def _make_ohlcv(n: int = 260, seed: int = 42, trend: float = 0.15) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 600 + trend * t + np.cumsum(rng.normal(0, 2.0, n))
    high = close + rng.uniform(1, 4, n)
    low = close - rng.uniform(1, 4, n)
    open_ = close + rng.normal(0, 1, n)
    volume = rng.uniform(800, 5000, n)
    # 最后一根放量突破
    high[-1] = float(high[-20:-1].max()) + 5
    close[-1] = high[-1] - 0.5
    volume[-1] = float(volume[-20:].mean()) * 2.5
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def test_indicators_include_adx_obv_sr():
    df = _make_ohlcv()
    ind = TechnicalIndicators.calculate_all_indicators(df)
    for key in ("ADX", "Plus_DI", "Minus_DI", "OBV", "OBV_Slope", "Support", "Resistance", "Stoch_K"):
        assert key in ind
        assert ind[key] is not None
    assert ind["Resistance"] >= ind["Support"]


def test_new_strategies_registered_and_runnable():
    df = _make_ohlcv()
    inst = InstitutionalStrategies(load_discovered=False)
    expected = {
        "golden_death_cross",
        "adx_trend",
        "stochastic_momentum",
        "volume_price_obv",
        "range_sr_swing",
        "breakout_volume",
    }
    assert expected.issubset(set(inst.strategies.keys()))
    summary = inst.run_all_strategies(df)
    assert summary["total_strategies"] >= 19
    for key in expected:
        detail = summary["strategy_details"][key]
        assert detail["signal"] in ("BUY", "SELL", "HOLD", "ERROR")
        assert "confidence" in detail


def test_breakout_volume_detects_spike():
    df = _make_ohlcv()
    result = InstitutionalStrategies(load_discovered=False).breakout_volume_strategy(df)
    assert result["signal"] == "BUY"
    assert result.get("volume_ok") is True


def test_playbook_prompt_and_dca():
    styles = recommend_styles_for_regime("TRENDING")
    assert "trend" in styles
    prompt = build_playbook_prompt_section(
        regime="RANGING",
        indicators={"ADX": 15.0, "RSI": 28.0, "Support": 580.0, "Resistance": 620.0},
    )
    assert "Playbook" in prompt or "playbook" in prompt.lower() or "技术分析" in prompt
    assert "ADX" in prompt
    plan = build_dca_plan(15000, weeks=24)
    assert plan["per_week_usdt"] == 625.0
    assert plan["strategy"] == "DCA"


def test_ta_analysis_bundle():
    from bnb_quant_tool.crypto_ta_playbook import (
        build_ta_analysis_bundle,
        format_ta_cockpit_lines,
    )

    inst = {
        "total_strategies": 19,
        "consensus_signal": "BUY",
        "strategy_details": {
            "golden_death_cross": {
                "strategy": "Golden/Death Cross",
                "signal": "BUY",
                "confidence": 0.78,
                "reason": "黄金交叉",
            },
            "adx_trend": {
                "strategy": "ADX Trend",
                "signal": "HOLD",
                "confidence": 0.5,
            },
        },
    }
    bundle = build_ta_analysis_bundle(
        regime="TRENDING",
        indicators={"ADX": 28.0, "RSI": 55.0, "Support": 580.0, "Resistance": 620.0},
        inst_results=inst,
        config={"analysis": {"ta_playbook": {"enabled": True}}},
        account_balance=12000,
        symbol="BNBUSDT",
    )
    assert bundle.get("enabled") is True
    assert bundle.get("classic_ta_bias") == "BUY"
    assert bundle.get("institutional_total") == 19
    assert bundle.get("dca_plan", {}).get("per_week_usdt") == 500.0
    lines = format_ta_cockpit_lines(bundle)
    assert any("Playbook" in ln for ln in lines)
    assert any("黄金交叉" in ln or "Golden" in ln for ln in lines)


def test_attach_ta_playbook_pipeline():
    from bnb_quant_tool.analysis_pipeline import attach_ta_playbook

    payload = attach_ta_playbook(
        {
            "symbol": "BNBUSDT",
            "market_regime": {"regime": "RANGING"},
            "indicators": {"RSI": 32.0, "ADX": 18.0},
            "institutional_strategies": {"total_strategies": 19, "consensus_signal": "HOLD"},
        },
        config={"analysis": {"ta_playbook": {"enabled": True}}},
        account_balance=5000,
    )
    assert payload.get("ta_playbook", {}).get("enabled") is True


def test_ta_playbook_gate_blocks_conflicting_long():
    from bnb_quant_tool.ta_playbook_gate import apply_ta_playbook_gates

    action, reasons, tightening, relaxation = apply_ta_playbook_gates(
        "LONG",
        {
            "enabled": True,
            "classic_ta_bias": "SELL",
            "classic_ta_votes": {"BUY": 0, "SELL": 3, "HOLD": 1},
            "institutional_consensus": "SELL",
            "regime": "TRENDING",
        },
        indicators={"ADX": 30.0},
        market_regime={"regime": "TRENDING"},
        config={"enabled": True},
    )
    assert action == "WAIT"
    assert reasons
    assert tightening == 0.0
    assert relaxation == 0.0


def test_ta_playbook_gate_relaxes_aligned_short():
    from bnb_quant_tool.ta_playbook_gate import apply_ta_playbook_gates

    action, reasons, tightening, relaxation = apply_ta_playbook_gates(
        "SHORT",
        {
            "enabled": True,
            "classic_ta_bias": "SELL",
            "classic_ta_votes": {"BUY": 0, "SELL": 2, "HOLD": 0},
            "classic_ta_aligned_with_consensus": True,
            "institutional_consensus": "SELL",
            "regime": "TRENDING",
        },
        indicators={"ADX": 27.0},
        market_regime={"regime": "TRENDING"},
        config={"enabled": True, "alignment_relaxation": 0.03},
    )
    assert action == "SHORT"
    assert not reasons
    assert tightening == 0.0
    assert relaxation > 0.0
