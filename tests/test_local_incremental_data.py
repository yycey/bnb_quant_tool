from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bnb_quant_tool.kline_archive import KlineArchive
from bnb_quant_tool.kline_local_fetch import (
    load_local_tail,
    merge_tip,
    needs_tip_refresh,
    tip_start_ms,
)
from bnb_quant_tool.news_store import NewsStore


def test_kline_local_tail_and_tip_helpers(tmp_path: Path):
    arch = KlineArchive(workspace=tmp_path, symbol="BNBUSDT", interval="1h")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(10):
        t = now - timedelta(hours=10 - i)
        rows.append(
            {
                "open_time": t,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1.0,
                "quote_volume": 100.0,
                "trades": 1,
            }
        )
    df = pd.DataFrame(rows)
    arch.merge_bars(df)
    tail = load_local_tail(arch, 5)
    assert len(tail) == 5
    assert tip_start_ms(tail, "1h") is not None
    # 末根是「现在」，不应需要 tip
    assert needs_tip_refresh(tail, "1h", max_lag_bars=2) is False

    tip = df.tail(2).copy()
    tip["close"] = tip["close"] + 1
    merged = merge_tip(tail, tip, 5)
    assert len(merged) == 5


def test_news_store_fetch_watermark(tmp_path: Path):
    db = tmp_path / "news_store.db"
    store = NewsStore(db)
    assert store.get_last_fetch_ts("RSS") == 0
    store.set_last_fetch("RSS", count=3, ts=1_700_000_000)
    assert store.get_last_fetch_ts("RSS") == 1_700_000_000
    store.upsert_many(
        [
            {
                "source": "RSS",
                "title": "BNB up",
                "summary": "x",
                "url": "http://a",
                "published_ts": 1_700_000_100,
            }
        ]
    )
    assert store.count_since(1_700_000_000) >= 1
