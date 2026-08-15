"""get_historical_klines: API 优先，本地兜底。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.data_fetcher import BinanceDataFetcher


def _sample_df(n: int = 100) -> pd.DataFrame:
    base = 1_700_000_000_000
    rows = []
    for i in range(n):
        rows.append({
            "open_time": pd.Timestamp(base + i * 3_600_000, unit="ms"),
            "open": 600.0 + i * 0.01,
            "high": 601.0,
            "low": 599.0,
            "close": 600.5 + i * 0.01,
            "volume": 1000.0,
            "quote_volume": 600000.0,
            "trades": 0,
        })
    return pd.DataFrame(rows)


def test_api_fail_fallback_local(monkeypatch, tmp_path):
    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "prefer_local": False,
            "auto_sync_on_analysis": False,
            "workspace": str(tmp_path),
        },
    )
    local_df = _sample_df(80)
    monkeypatch.setattr(fetcher, "fetch_historical_from_api", lambda **kw: pd.DataFrame())
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)

    def _local(symbol, interval, start_str, end_str, cache_key):
        fetcher.last_data_source = "local"
        return local_df.copy()

    monkeypatch.setattr(fetcher, "_load_from_archive", _local)

    df = fetcher.get_historical_klines("BNBUSDT", "1h", "60 days ago", skip_sync=True)
    assert len(df) == 80
    assert fetcher.last_data_source == "local"


def test_api_success_skips_local(monkeypatch, tmp_path):
    """无本地数据时仍走全量 API；有本地时改走增量（见 test_incremental_cache）。"""
    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "prefer_local": False,
            "incremental": True,
            "auto_sync_on_analysis": False,
            "workspace": str(tmp_path),
        },
        historical_cache_seconds=0,
    )
    api_df = _sample_df(50)
    monkeypatch.setattr(fetcher, "fetch_historical_from_api", lambda **kw: api_df.copy())
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)
    local_called = {"n": 0}

    def _local(*a, **kw):
        local_called["n"] += 1
        return api_df.copy()

    monkeypatch.setattr(fetcher, "_load_from_archive", _local)

    df = fetcher.get_historical_klines("BNBUSDT", "1h", "30 days ago", skip_sync=True)
    assert len(df) == 50
    # 空归档 → 不走 _load_from_archive（增量提前返回 None）
    assert local_called["n"] == 0
