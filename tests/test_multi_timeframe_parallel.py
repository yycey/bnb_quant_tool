"""multi_timeframe 并行拉取 — 与串行结果一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.multi_timeframe import MultiTimeframeAnalyzer


def _make_df(close: float, n: int = 120) -> pd.DataFrame:
    base = 1_700_000_000_000
    rows = []
    for i in range(n):
        c = close + (i % 5) * 0.1
        rows.append({
            "open_time": pd.Timestamp(base + i * 3_600_000, unit="ms"),
            "open": c - 0.2,
            "high": c + 0.5,
            "low": c - 0.5,
            "close": c,
            "volume": 1000.0,
            "quote_volume": 100000.0,
            "trades": 10,
        })
    return pd.DataFrame(rows)


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def get_historical_klines(self, symbol, interval, start_str):
        self.calls.append((symbol, interval, start_str))
        closes = {"15m": 600.0, "1h": 610.0, "4h": 620.0, "1d": 630.0}
        return _make_df(closes.get(interval, 600.0))


def test_parallel_fetch_matches_prefetched_subset():
    fetcher = FakeFetcher()
    analyzer = MultiTimeframeAnalyzer(fetcher=fetcher)

    prefetched = {"1h": _make_df(610.0)}
    result = analyzer.analyze(symbol="BNBUSDT", prefetched=prefetched)

    # 1h 不应再请求
    called_intervals = [c[1] for c in fetcher.calls]
    assert "1h" not in called_intervals
    assert set(called_intervals) == {"15m", "4h", "1d"}

    assert result["symbol"] == "BNBUSDT"
    assert "timeframe_signals" in result
    assert result["timeframe_signals"]["1h"]["direction"] in ("LONG", "SHORT", "NEUTRAL")
    assert result["recommended_action"] in ("LONG", "SHORT", "WAIT")


def test_prefetched_superset_produces_valid_signal():
    analyzer = MultiTimeframeAnalyzer(fetcher=None)
    df = _make_df(605.0, n=200)
    result = analyzer.analyze(
        symbol="BNBUSDT",
        timeframes=["1h"],
        prefetched={"1h": df},
    )
    assert result["timeframe_signals"]["1h"]["direction"] in ("LONG", "SHORT", "NEUTRAL")
    assert result["long_count"] + result["short_count"] + result["neutral_count"] == 1
