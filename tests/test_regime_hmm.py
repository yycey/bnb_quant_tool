"""HMM Regime + BTC 领先指标 + StrategyLab OOS 测试"""

import numpy as np
import pandas as pd

from bnb_quant_tool.btc_lead_indicator import compute_btc_lead_indicator
from bnb_quant_tool.regime_hmm import infer_hmm_regime, merge_hmm_into_regime


def _trend_df(n=120, drift=0.003, start=600.0):
    prices = start * (1 + np.arange(n) * drift)
    return pd.DataFrame({
        "open": prices,
        "high": prices * 1.005,
        "low": prices * 0.995,
        "close": prices,
        "volume": [1000.0] * n,
    })


def test_hmm_regime_trending():
    df = _trend_df(drift=0.004)
    hmm = infer_hmm_regime(df, config={"lookback": 100, "min_bars": 60})
    assert hmm.get("hmm_enabled") is True
    assert hmm.get("hmm_regime") in ("TRENDING", "RANGING", "HIGH_VOLATILITY", "PANIC")
    assert 0 <= float(hmm.get("hmm_confidence") or 0) <= 1


def test_hmm_merge_boosts_agreement():
    base = {"regime": "TRENDING", "fusion_confidence": 0.5, "reasons": []}
    hmm = {
        "hmm_enabled": True,
        "hmm_regime": "TRENDING",
        "hmm_confidence": 0.8,
        "hmm_detail": "test",
    }
    merged = merge_hmm_into_regime(base, hmm)
    assert merged.get("hmm_agreement") is True
    assert float(merged.get("fusion_confidence") or 0) > 0.5


def test_btc_lead_bullish_catch_up():
    bnb = _trend_df(n=80, drift=0.0, start=600)
    btc = _trend_df(n=80, drift=0.006, start=60000)
    lead = compute_btc_lead_indicator(bnb, btc, config={"lookback_bars": 24})
    assert lead.get("available") is True
    assert float(lead.get("lead_score") or 0) > 0
    assert lead.get("divergence") in ("bullish_catch_up", None) or lead.get("follow_mode") == "bnb_lagging"


def test_btc_lead_insufficient_data():
    lead = compute_btc_lead_indicator(None, None)
    assert lead.get("available") is False
