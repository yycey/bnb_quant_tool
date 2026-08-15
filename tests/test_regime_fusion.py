"""Regime 多信号融合 + Funding 方向门控测试"""

import pandas as pd

from bnb_quant_tool.ai_trading_context import apply_funding_direction_gate
from bnb_quant_tool.regime_fusion import fuse_regime


def _sample_df(n=60, trend=0.002):
    import numpy as np
    prices = 600 * (1 + np.arange(n) * trend)
    return pd.DataFrame({
        "open": prices,
        "high": prices * 1.01,
        "low": prices * 0.99,
        "close": prices,
        "volume": [1000] * n,
    })


def test_fuse_regime_trending():
    df = _sample_df(trend=0.003)
    indicators = {"ATR": 5, "MA_20": float(df["close"].iloc[-1]) * 0.98, "RSI": 55}
    sentiment = {
        "fear_greed": {"value": 65},
        "funding_rate": {"rate": 0.0002},
    }
    fusion = fuse_regime(df, indicators=indicators, sentiment=sentiment, config={"fusion_min_agreement": 2})
    assert fusion["regime_votes"]
    assert len(fusion["regime_votes"]) == 4
    assert fusion["regime"] in ("TRENDING", "RANGING", "HIGH_VOLATILITY", "EUPHORIA")


def test_funding_gate_blocks_crowded_long():
    advice = apply_funding_direction_gate(
        {"action": "LONG", "passed_gate": True, "gate_reasons": []},
        sentiment={"funding_rate": {"rate": 0.0012}},
        config={"ai_trading": {"funding_direction_gate": {"enabled": True}}},
    )
    assert advice["action"] == "WAIT"
    assert advice.get("funding_blocked")


def test_bnb_btc_weakness_in_conviction():
    from bnb_quant_tool.institutional_conviction import compute_institutional_conviction

    result = compute_institutional_conviction(
        inst_results={"strategy_details": {}, "buy_signals": 1, "sell_signals": 0},
        market_regime={"regime": "TRENDING"},
        bnb_factors={
            "risk_sentry": {
                "bnb_btc_weakness": {
                    "weak": True,
                    "ratio_change_pct": -2.5,
                    "interpretation": "BNB 跑输 BTC",
                }
            }
        },
        indicators={"RSI": 50, "MACD": 0, "BB_Position": 50},
    )
    names = [f["name"] for f in result["factors"]]
    assert "BNB/BTC相对强度" in names
    rs_factor = next(f for f in result["factors"] if f["name"] == "BNB/BTC相对强度")
    assert rs_factor["score"] < 0
