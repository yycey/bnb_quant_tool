"""KlineArchive.rebuild_merged 必须以 chunk 为准，不能沿用过期 merged。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.kline_archive import KlineArchive


def _bars(times: list[str], close_start: float = 500.0) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(times):
        c = close_start + i
        rows.append({
            "open_time": pd.Timestamp(t),
            "open": c,
            "high": c + 1,
            "low": c - 1,
            "close": c,
            "volume": 10.0,
            "quote_volume": 5000.0,
            "trades": 0,
        })
    return pd.DataFrame(rows)


def test_rebuild_merged_uses_chunks_not_stale_merged(tmp_path):
    arch = KlineArchive(tmp_path, symbol="BNBUSDT", interval="1h")

    # 先写入过期 merged（模拟旧 bug 产物）
    stale = _bars(["2026-06-14 18:00:00"], close_start=604.37)
    stale.to_parquet(arch.merged_path, index=False)
    stale.to_csv(arch.merged_csv_path, index=False)

    # chunk 已有更新数据
    arch.save_chunk(2026, 6, _bars(["2026-06-14 18:00:00"], close_start=604.37), source="test")
    arch.save_chunk(
        2026,
        7,
        _bars(["2026-07-31 08:00:00", "2026-07-31 09:00:00"], close_start=590.0),
        source="test",
    )

    rebuilt = arch.rebuild_merged()
    assert len(rebuilt) == 3
    assert float(rebuilt["close"].iloc[-1]) == 591.0
    assert pd.Timestamp(rebuilt["open_time"].iloc[-1]) == pd.Timestamp("2026-07-31 09:00:00")

    # load_merged 应自愈，不再返回 604.37
    loaded = arch.load_merged()
    assert float(loaded["close"].iloc[-1]) == 591.0


def test_load_merged_self_heals_when_chunk_ahead(tmp_path):
    arch = KlineArchive(tmp_path, symbol="BNBUSDT", interval="1h")
    stale = _bars(["2026-06-14 18:00:00"], close_start=604.37)
    stale.to_parquet(arch.merged_path, index=False)

    arch.save_chunk(2026, 6, stale, source="test")
    arch.save_chunk(
        2026,
        7,
        _bars(["2026-07-31 09:00:00"], close_start=590.68),
        source="test",
    )

    loaded = arch.load_merged()
    assert float(loaded["close"].iloc[-1]) == 590.68
    # 自愈后磁盘 merged 也应更新
    disk = pd.read_parquet(arch.merged_path)
    assert float(disk["close"].iloc[-1]) == 590.68


def test_merge_bars_updates_same_open_time_ohlc(tmp_path):
    arch = KlineArchive(tmp_path, symbol="BNBUSDT", interval="1h")
    first = _bars(["2026-07-31 09:00:00"], close_start=590.0)
    arch.merge_bars(first, source="seed")
    refreshed = _bars(["2026-07-31 09:00:00"], close_start=591.5)
    refreshed.loc[0, ["open", "high", "low", "close"]] = [590.0, 592.0, 589.0, 591.5]
    result = arch.merge_bars(refreshed, source="tip")
    assert result["updated"] is True
    loaded = arch.load_merged()
    assert float(loaded["close"].iloc[-1]) == 591.5
    assert float(loaded["high"].iloc[-1]) == 592.0
