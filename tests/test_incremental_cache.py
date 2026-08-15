"""增量数据缓存：K线本地复用 / 新闻持久化 / 磁盘TTL。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.data_fetcher import BinanceDataFetcher
from bnb_quant_tool.disk_ttl_cache import DiskTTLCache
from bnb_quant_tool.kline_archive import KlineArchive
from bnb_quant_tool.news_store import NewsStore


def _sample_df(n: int = 100, start_ms: int = 1_700_000_000_000) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "open_time": pd.Timestamp(start_ms + i * 3_600_000, unit="ms"),
            "open": 600.0 + i * 0.01,
            "high": 601.0,
            "low": 599.0,
            "close": 600.5 + i * 0.01,
            "volume": 1000.0,
            "quote_volume": 600000.0,
            "trades": 0,
        })
    return pd.DataFrame(rows)


def test_kline_incremental_only_fetches_tip(monkeypatch, tmp_path):
    """本地已有历史时，只请求 tip 增量，不整段重拉。"""
    archive = KlineArchive(tmp_path, symbol="BNBUSDT", interval="1h")
    # 统一用 UTC，避免 pandas 把 naive now.timestamp() 当 UTC 造成假过期
    now_utc = pd.Timestamp.now(tz="UTC").floor("h")
    local_end = now_utc - pd.Timedelta(hours=3)  # lag=3h > 2 根 → 需要 tip
    start_ms = int((local_end - pd.Timedelta(hours=49)).timestamp() * 1000)
    local = _sample_df(50, start_ms=start_ms)
    archive.merge_bars(local, source="seed")

    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "incremental": True,
            "prefer_local": False,
            "auto_sync_on_analysis": True,
            "workspace": str(tmp_path),
        },
        historical_cache_seconds=0,
    )
    calls = []

    def _fake_api(**kw):
        calls.append(kw)
        last = local["open_time"].iloc[-1]
        last_ms = int(pd.Timestamp(last).tz_localize("UTC").timestamp() * 1000)
        tip = _sample_df(3, start_ms=last_ms + 3_600_000)
        return tip

    monkeypatch.setattr(fetcher, "fetch_historical_from_api", _fake_api)
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)

    start = (local["open_time"].iloc[0] - pd.Timedelta(hours=1)).isoformat(sep=" ")
    df = fetcher.get_historical_klines("BNBUSDT", "1h", start_str=start)
    assert not df.empty
    assert len(calls) >= 1
    assert all("ago" not in str(c.get("start_str") or "") for c in calls)
    assert "local" in fetcher.last_data_source


def test_kline_4h_current_bar_not_false_stale(monkeypatch, tmp_path):
    """UTC 04:00 开盘的 4h 根在 UTC 06:00 仍新鲜，不应 tip 失败回退全量。"""
    from datetime import datetime, timezone as dt_timezone

    archive = KlineArchive(tmp_path, symbol="BNBUSDT", interval="4h")
    now_utc = datetime(2026, 8, 14, 6, 0, 0, tzinfo=dt_timezone.utc)
    open_utc = datetime(2026, 8, 14, 4, 0, 0, tzinfo=dt_timezone.utc)
    rows = []
    for i in range(20):
        t = pd.Timestamp(open_utc - pd.Timedelta(hours=4 * (19 - i))).tz_localize(None)
        rows.append({
            "open_time": t,
            "open": 600.0,
            "high": 601.0,
            "low": 599.0,
            "close": 600.5,
            "volume": 1.0,
            "quote_volume": 600.0,
            "trades": 1,
        })
    local = pd.DataFrame(rows)
    archive.merge_bars(local, source="seed")

    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "incremental": True,
            "prefer_local": False,
            "workspace": str(tmp_path),
        },
        historical_cache_seconds=0,
    )
    calls = []
    monkeypatch.setattr(
        fetcher, "fetch_historical_from_api",
        lambda **kw: calls.append(kw) or local.tail(1).copy(),
    )
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)

    import bnb_quant_tool.kline_local_fetch as klf

    class _FakeDT:
        timezone = dt_timezone

        @staticmethod
        def now(tz=None):
            return now_utc if tz is not None else now_utc.replace(tzinfo=None)

    monkeypatch.setattr(klf, "datetime", _FakeDT)

    start = local["open_time"].iloc[0].isoformat(sep=" ")
    df = fetcher.get_historical_klines("BNBUSDT", "4h", start_str=start)
    assert not df.empty
    assert "local" in (fetcher.last_data_source or "")
    assert len(calls) == 0  # 未过期，无需 tip / gap


def test_kline_tip_empty_fails_closed(monkeypatch, tmp_path):
    """tip 拉空且本地过期时，增量路径返回 None，交给全量。"""
    archive = KlineArchive(tmp_path, symbol="BNBUSDT", interval="1h")
    old = _sample_df(10, start_ms=1_700_000_000_000)  # 2023
    archive.merge_bars(old, source="seed")

    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "incremental": True,
            "prefer_local": False,
            "auto_sync_on_analysis": False,
            "workspace": str(tmp_path),
        },
        historical_cache_seconds=0,
    )
    monkeypatch.setattr(fetcher, "fetch_historical_from_api", lambda **kw: pd.DataFrame())
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)

    out = fetcher._fetch_incremental_from_archive(
        "BNBUSDT", "1h", "7 days ago", None, ("BNBUSDT", "1h", "7 days ago", ""),
    )
    assert out is None


def test_kline_first_time_full_fetch(monkeypatch, tmp_path):
    fetcher = BinanceDataFetcher(
        kline_archive_config={
            "enabled": True,
            "incremental": True,
            "prefer_local": False,
            "auto_sync_on_analysis": False,
            "workspace": str(tmp_path),
        },
        historical_cache_seconds=0,
    )
    api_df = _sample_df(40)
    monkeypatch.setattr(fetcher, "fetch_historical_from_api", lambda **kw: api_df.copy())
    monkeypatch.setattr(fetcher, "_probe_mirror", lambda: None)
    df = fetcher.get_historical_klines("BNBUSDT", "1h", "30 days ago", skip_sync=True)
    assert len(df) == 40


def test_news_store_upsert_and_query(tmp_path):
    store = NewsStore(tmp_path / "news.db")
    items = [
        {"title": "A", "url": "u1", "source": "s", "published_ts": 1000, "summary": ""},
        {"title": "B", "url": "u2", "source": "s", "published_ts": 2000, "summary": ""},
    ]
    store.upsert_many(items)
    store.upsert_many(items)  # 去重
    got = store.query_since(1500, limit=10)
    assert len(got) == 1
    assert got[0]["title"] == "B"


def test_disk_ttl_cache(tmp_path):
    c = DiskTTLCache(tmp_path, prefix="t")
    c.set("k", {"v": 1})
    assert c.get("k", ttl_seconds=999) == {"v": 1}
    assert c.get("k", ttl_seconds=0) is None
