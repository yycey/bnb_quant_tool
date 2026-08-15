"""data_fetcher 缓存与价格回退 — 行为不变，仅减少重复请求。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.data_fetcher import BinanceDataFetcher


def _sample_df(n: int = 50) -> pd.DataFrame:
    rows = []
    base = 1_700_000_000_000
    for i in range(n):
        rows.append({
            "open_time": pd.Timestamp(base + i * 3_600_000, unit="ms"),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000.0,
            "quote_volume": 100000.0,
            "trades": 10,
        })
    return pd.DataFrame(rows)


class TestDataFetcherCache:
    def test_kline_cache_returns_copy_and_avoids_repeat_fetch(self, monkeypatch):
        fetcher = BinanceDataFetcher(kline_cache_seconds=60.0)
        calls = {"n": 0}

        def fake_request(url, method="GET", max_retries=3, **kwargs):
            calls["n"] += 1
            sample = _sample_df()

            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    rows = []
                    for _, row in sample.iterrows():
                        ts = int(row["open_time"].timestamp() * 1000)
                        rows.append([
                            ts, str(row["open"]), str(row["high"]), str(row["low"]),
                            str(row["close"]), str(row["volume"]), ts + 3599999,
                            str(row["quote_volume"]), int(row["trades"]),
                            "0", "0", "0",
                        ])
                    return rows

            return Resp()

        monkeypatch.setattr(fetcher, "_request_with_retry", fake_request)
        fetcher.base_url = "https://test.local"

        df1 = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=50)
        df2 = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=50)

        assert calls["n"] == 1
        assert len(df1) == len(df2) == 50
        df1.iloc[0, df1.columns.get_loc("close")] = 999.0
        assert float(df2["close"].iloc[0]) != 999.0

    def test_historical_cache_hit(self, monkeypatch):
        fetcher = BinanceDataFetcher(historical_cache_seconds=60.0)
        calls = {"n": 0}
        sample = _sample_df(120)

        def fake_get_klines(**kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                return pd.DataFrame()
            return sample.copy()

        monkeypatch.setattr(fetcher, "get_klines", fake_get_klines)
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        h1 = fetcher.get_historical_klines(
            symbol="BNBUSDT", interval="1h", start_str="5 days ago"
        )
        calls_after_first = calls["n"]
        h2 = fetcher.get_historical_klines(
            symbol="BNBUSDT", interval="1h", start_str="5 days ago"
        )

        assert calls["n"] == calls_after_first
        assert len(h1) == len(h2) == 120

    def test_price_with_fallback_uses_cache(self, monkeypatch):
        fetcher = BinanceDataFetcher(price_cache_seconds=30.0)
        calls = {"n": 0}

        def fake_last_price(symbol):
            calls["n"] += 1
            return 612.34

        monkeypatch.setattr(fetcher, "get_last_price", fake_last_price)

        p1 = fetcher.get_price_with_fallback("BNBUSDT")
        p2 = fetcher.get_price_with_fallback("BNBUSDT")

        assert p1 == p2 == 612.34
        assert calls["n"] == 1

    def test_price_fallback_to_ticker(self, monkeypatch):
        fetcher = BinanceDataFetcher(price_cache_seconds=0.0)

        monkeypatch.setattr(fetcher, "get_last_price", lambda s: 0.0)
        monkeypatch.setattr(
            fetcher,
            "get_ticker",
            lambda s: {"lastPrice": "598.12"},
        )

        assert fetcher.get_price_with_fallback("BNBUSDT") == 598.12
