"""resolve_current_price: ticker 优先，过旧 K 线拒绝当现价。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.data_fetcher import BinanceDataFetcher


def test_resolve_prefers_ticker(monkeypatch):
    fetcher = BinanceDataFetcher(price_cache_seconds=0)
    monkeypatch.setattr(fetcher, "get_price_with_fallback", lambda s: 591.25)
    df = pd.DataFrame({
        "open_time": [pd.Timestamp("2026-06-14 18:00:00")],
        "close": [604.37],
    })
    assert fetcher.resolve_current_price("BNBUSDT", df) == 591.25


def test_resolve_rejects_stale_bar_without_ticker(monkeypatch):
    fetcher = BinanceDataFetcher(price_cache_seconds=0)
    monkeypatch.setattr(fetcher, "get_price_with_fallback", lambda s: 0.0)
    df = pd.DataFrame({
        "open_time": [pd.Timestamp("2026-06-14 18:00:00")],
        "close": [604.37],
    })
    assert fetcher.resolve_current_price("BNBUSDT", df) == 0.0


def test_resolve_allows_recent_bar_fallback(monkeypatch):
    fetcher = BinanceDataFetcher(price_cache_seconds=0)
    monkeypatch.setattr(fetcher, "get_price_with_fallback", lambda s: 0.0)
    # open_time 与交易所一致：UTC naive
    now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    df = pd.DataFrame({
        "open_time": [now],
        "close": [590.5],
    })
    assert fetcher.resolve_current_price("BNBUSDT", df) == 590.5


def test_price_cache_hard_max(monkeypatch):
    fetcher = BinanceDataFetcher(price_cache_seconds=5.0)
    fetcher._price_cache["BNBUSDT"] = (600.0, __import__("time").time() - 120)
    monkeypatch.setattr(fetcher, "get_last_price", lambda s: 0.0)
    monkeypatch.setattr(fetcher, "get_ticker", lambda s: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(fetcher, "_can_bitget_fallback", lambda: False)
    assert fetcher.get_price_with_fallback("BNBUSDT") == 0.0
