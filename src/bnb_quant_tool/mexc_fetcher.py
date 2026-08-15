"""
MEXC 现货 K 线 API — 用于历史数据批量下载。

文档: https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/klinecandlestick-data
官网下载页: https://www.mexc.com/zh-MY/market-data-download/BNB
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# 通用 interval → MEXC REST interval（1h 必须用 60m）
INTERVAL_TO_MEXC: Dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "60m": "60m",
    "4h": "4h",
    "1d": "1d",
    "1w": "1W",
    "1M": "1M",
}

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close",
    "volume", "quote_volume", "trades",
]


class MexcDataFetcher:
    """MEXC 公开 K 线接口（无需 API Key）。"""

    DEFAULT_BASE_URL = "https://api.mexc.com"
    MAX_LIMIT = 500

    def __init__(self, base_url: Optional[str] = None, *, timeout: tuple[float, float] = (10.0, 30.0)):
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; bnb-quant-tool/1.0)",
            "Accept": "application/json",
        })
        self.last_data_source = "mexc"

    @staticmethod
    def supports_interval(interval: str) -> bool:
        return interval in INTERVAL_TO_MEXC

    @staticmethod
    def to_mexc_interval(interval: str) -> str:
        key = interval if interval in INTERVAL_TO_MEXC else interval
        gran = INTERVAL_TO_MEXC.get(key)
        if not gran:
            raise ValueError(f"MEXC 不支持 K 线周期: {interval}")
        return gran

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/api/v3/ping", timeout=(5, 10))
            return r.status_code == 200
        except Exception as exc:
            logger.debug("MEXC ping 失败: %s", exc)
            return False

    def _rows_to_dataframe(self, rows: List) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        records = []
        for row in rows:
            if not row or len(row) < 6:
                continue
            records.append({
                "open_time": pd.to_datetime(int(row[0]), unit="ms"),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "quote_volume": float(row[7] if len(row) > 7 else row[6]),
                "trades": 0,
            })
        df = pd.DataFrame(records)
        if df.empty:
            return df
        return df.sort_values("open_time").drop_duplicates(subset=["open_time"])

    def get_klines(
        self,
        symbol: str = "BNBUSDT",
        interval: str = "1h",
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> pd.DataFrame:
        params: Dict[str, str | int] = {
            "symbol": symbol.upper(),
            "interval": self.to_mexc_interval(interval),
            "limit": min(max(int(limit), 1), self.MAX_LIMIT),
        }
        if start_time:
            params["startTime"] = int(start_time)
        if end_time:
            params["endTime"] = int(end_time)

        response = self.session.get(
            f"{self.base_url}/api/v3/klines",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0, "0"):
            raise requests.exceptions.HTTPError(f"MEXC API: {data.get('msg', data)}")
        rows = data if isinstance(data, list) else []
        df = self._rows_to_dataframe(rows)
        logger.info("MEXC 获取 %d 条 %s %s K线", len(df), symbol, interval)
        return df

    def get_range_klines(
        self,
        symbol: str,
        interval: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> pd.DataFrame:
        """分页拉取时间范围内全部 K 线（每月 ~720 根 1h）。"""
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000) - 1
        all_frames: List[pd.DataFrame] = []
        cursor = start_ms

        while cursor < end_ms:
            df = self.get_klines(
                symbol=symbol,
                interval=interval,
                limit=self.MAX_LIMIT,
                start_time=cursor,
                end_time=end_ms,
            )
            if df.empty:
                break
            all_frames.append(df)
            last_ms = int(df["open_time"].iloc[-1].timestamp() * 1000)
            next_cursor = last_ms + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(df) < self.MAX_LIMIT:
                break
            time.sleep(0.15)

        if not all_frames:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        out = pd.concat(all_frames, ignore_index=True)
        out = out[(out["open_time"] >= start_dt) & (out["open_time"] < end_dt)]
        return out.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)

    def get_historical_klines(
        self,
        symbol: str = "BNBUSDT",
        interval: str = "1h",
        start_str: str = "1 day ago",
        end_str: Optional[str] = None,
    ) -> pd.DataFrame:
        if "ago" in start_str:
            num, unit = start_str.split(" ")[0], start_str.split(" ")[1]
            if "day" in unit:
                delta = timedelta(days=int(num))
            elif "hour" in unit:
                delta = timedelta(hours=int(num))
            elif "minute" in unit:
                delta = timedelta(minutes=int(num))
            else:
                raise ValueError(f"不支持的时间单位: {unit}")
            start_dt = datetime.now() - delta
        else:
            start_dt = pd.Timestamp(start_str).to_pydatetime()
        end_dt = pd.Timestamp(end_str).to_pydatetime() if end_str else datetime.now()
        return self.get_range_klines(symbol, interval, start_dt, end_dt)
