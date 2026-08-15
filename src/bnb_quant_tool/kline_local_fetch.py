"""
短周期 get_klines 本地优先 — 读归档尾巴，只向 API 要缺口，再写回本地。

不改动 get_historical_klines 的完整增量路径；专供 scanner / discover / 限量拉取。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def interval_to_ms(interval: str) -> int:
    return int(_INTERVAL_MS.get(str(interval), 3_600_000))


def load_local_tail(archive, limit: int) -> pd.DataFrame:
    """从归档取最近 limit 根（空则空表）。"""
    if archive is None or not getattr(archive, "has_local_data", lambda: False)():
        return pd.DataFrame()
    try:
        merged = archive.load_merged()
    except Exception as e:
        logger.debug("load_merged failed: %s", e)
        return pd.DataFrame()
    if merged is None or merged.empty:
        return pd.DataFrame()
    if "open_time" not in merged.columns:
        return pd.DataFrame()
    out = merged.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last")
    return out.tail(int(limit)).reset_index(drop=True)


def local_end_ms(df: pd.DataFrame) -> Optional[int]:
    if df is None or df.empty or "open_time" not in df.columns:
        return None
    t = pd.Timestamp(df["open_time"].iloc[-1])
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.timestamp() * 1000)


def needs_tip_refresh(local_df: pd.DataFrame, interval: str, *, max_lag_bars: int = 2) -> bool:
    """本地末根距今是否落后超过 max_lag_bars。"""
    end_ms = local_end_ms(local_df)
    if end_ms is None:
        return True
    step = interval_to_ms(interval)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lag = now_ms - end_ms
    return lag > step * max(1, int(max_lag_bars))


def merge_tip(local_df: pd.DataFrame, tip_df: pd.DataFrame, limit: int) -> pd.DataFrame:
    if tip_df is None or tip_df.empty:
        base = local_df
    elif local_df is None or local_df.empty:
        base = tip_df
    else:
        base = pd.concat([local_df, tip_df], ignore_index=True)
        base = base.drop_duplicates(subset=["open_time"], keep="last").sort_values("open_time")
    if base is None or base.empty:
        return pd.DataFrame()
    return base.tail(int(limit)).reset_index(drop=True)


def tip_start_ms(local_df: pd.DataFrame, interval: str) -> Optional[int]:
    """从本地末根下一根开始拉 tip（重叠 1 根防空洞）。"""
    end_ms = local_end_ms(local_df)
    if end_ms is None:
        return None
    step = interval_to_ms(interval)
    return max(0, end_ms - step)
