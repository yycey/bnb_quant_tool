"""Bitget 备用数据源与 Binance 回退逻辑。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.bitget_fetcher import BitgetDataFetcher
from bnb_quant_tool.data_fetcher import BinanceDataFetcher


BITGET_CANDLE = [
    "1700000000000", "600.0", "605.0", "598.0", "602.5", "1000.0", "601000.0", "601000.0"
]

BITGET_TICKER = [{
    "symbol": "BNBUSDT",
    "lastPr": "602.50",
    "high24h": "610.0",
    "low24h": "590.0",
    "open": "595.0",
    "change24h": "0.012",
    "baseVolume": "10000",
    "quoteVolume": "6000000",
}]


class TestBitgetFetcher:
    def test_interval_mapping(self):
        assert BitgetDataFetcher.to_granularity("1h") == "1h"
        assert BitgetDataFetcher.to_granularity("15m") == "15min"
        assert not BitgetDataFetcher.supports_interval("3m")

    def test_candles_to_dataframe(self):
        fetcher = BitgetDataFetcher()
        df = fetcher._candles_to_dataframe([BITGET_CANDLE], "1h")
        assert len(df) == 1
        assert float(df["close"].iloc[0]) == 602.5

    def test_get_last_price(self, monkeypatch):
        fetcher = BitgetDataFetcher()

        def fake_get(path, params=None):
            return BITGET_TICKER

        monkeypatch.setattr(fetcher, "_get", fake_get)
        assert fetcher.get_last_price("BNBUSDT") == 602.50

    def test_get_ticker_binance_compatible(self, monkeypatch):
        fetcher = BitgetDataFetcher()
        monkeypatch.setattr(fetcher, "_get", lambda path, params=None: BITGET_TICKER)
        ticker = fetcher.get_ticker("BNBUSDT")
        assert ticker["lastPrice"] == "602.50"
        assert ticker["_source"] == "bitget"


class TestBinanceBitgetFallback:
    def test_klines_fallback_to_bitget(self, monkeypatch):
        fetcher = BinanceDataFetcher(bitget_config={"enabled": True, "fallback": True})
        monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)
        fetcher.base_url = "https://api.binance.com"

        def fail_request(*args, **kwargs):
            raise requests.exceptions.ConnectionError("binance down")

        monkeypatch.setattr(fetcher, "_request_with_retry", fail_request)

        sample = pd.DataFrame([{
            "open_time": pd.Timestamp("2024-01-01"),
            "open": 600.0, "high": 605.0, "low": 598.0, "close": 602.0,
            "volume": 100.0, "quote_volume": 60000.0, "trades": 0,
        }])
        monkeypatch.setattr(fetcher._bitget, "get_klines", lambda **kw: sample.copy())

        df = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=10)
        assert len(df) == 1
        assert fetcher.last_data_source == "bitget"

    def test_price_fallback_to_bitget(self, monkeypatch):
        fetcher = BinanceDataFetcher(
            bitget_config={"enabled": True, "fallback": True},
            price_cache_seconds=0.0,
        )
        monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)
        monkeypatch.setattr(fetcher, "get_last_price", lambda s: 0.0)
        monkeypatch.setattr(fetcher, "get_ticker", lambda s: (_ for _ in ()).throw(Exception("fail")))
        monkeypatch.setattr(fetcher._bitget, "get_last_price", lambda s: 601.23)

        assert fetcher.get_price_with_fallback("BNBUSDT") == 601.23
        assert fetcher.last_data_source == "bitget"
