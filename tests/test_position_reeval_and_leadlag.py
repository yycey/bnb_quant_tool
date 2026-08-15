from bnb_quant_tool.position_reeval import (
    should_admit_wrong_direction,
    should_signal_early_close,
)
from bnb_quant_tool.onchain_lead_lag_backtest import _pearson, scan_horizons


def test_should_signal_early_close_stress_low_pnl():
    ok, reason = should_signal_early_close(
        entry_regime="TRENDING",
        last_regime="TRENDING",
        current_regime="HIGH_VOLATILITY",
        unrealized_pnl_pct=0.4,
        mtf_action="WAIT",
        side="LONG",
    )
    assert ok is True
    assert "HIGH_VOLATILITY" in reason


def test_should_not_signal_when_pnl_high():
    ok, _ = should_signal_early_close(
        entry_regime="RANGING",
        last_regime="RANGING",
        current_regime="PANIC",
        unrealized_pnl_pct=2.5,
        mtf_action="SHORT",
        side="LONG",
    )
    assert ok is False


def test_should_not_signal_non_stress():
    ok, _ = should_signal_early_close(
        entry_regime="TRENDING",
        last_regime="TRENDING",
        current_regime="TRENDING",
        unrealized_pnl_pct=0.1,
        mtf_action="SHORT",
        side="LONG",
    )
    assert ok is False


def test_admit_wrong_on_mtf_flip():
    ok, reason = should_admit_wrong_direction(
        side="LONG",
        mtf_action="SHORT",
        unrealized_pnl_pct=0.1,
        config={"position_reeval": {"admit_wrong": {"enabled": True, "mtf_flip": True}}},
    )
    assert ok is True
    assert "认错" in reason


def test_admit_wrong_skips_when_in_profit():
    ok, _ = should_admit_wrong_direction(
        side="LONG",
        mtf_action="SHORT",
        unrealized_pnl_pct=1.0,
        config={"position_reeval": {"admit_wrong": {"max_unrealized_pnl_pct": 0.35}}},
    )
    assert ok is False


def test_pearson_and_scan_horizons():
    import pandas as pd
    import numpy as np

    assert abs(_pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) - 1.0) < 1e-6

    n = 80
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    # 价格缓慢上涨
    close = np.linspace(100, 120, n)
    df = pd.DataFrame({"close": close, "volume": np.ones(n) * 1000}, index=idx)
    # 事件在前半段，正向信号
    events = [(idx[10], 1.0), (idx[20], 1.0), (idx[30], -1.0), (idx[40], 1.0),
              (idx[50], -1.0), (idx[55], 1.0), (idx[60], 1.0), (idx[65], -1.0)]
    out = scan_horizons(df, events, horizons=(1, 2, 4))
    assert out["ok"] is True
    assert "best_horizon_hours" in out
    assert str(out["best_horizon_hours"]) in out["horizons"]
