"""
Bitget 现货行情 API — 作为 Binance 的备用数据源。

文档: https://www.bitget.com/zh-CN/api-doc/spot/market/Get-Candle-Data
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Binance interval → Bitget granularity
INTERVAL_TO_GRANULARITY: Dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1day",
    "3d": "3day",
    "1w": "1week",
    "1M": "1M",
}

# 估算 close_time 偏移（毫秒）
INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}


class BitgetDataFetcher:
    """Bitget 现货公开行情（无需 API Key）。"""

    DEFAULT_BASE_URL = "https://api.bitget.com"

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: tuple[float, float] = (10.0, 30.0),
    ):
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; bnb-quant-tool/1.0)",
            "Accept": "application/json",
        })

    @staticmethod
    def supports_interval(interval: str) -> bool:
        return interval in INTERVAL_TO_GRANULARITY

    @staticmethod
    def to_granularity(interval: str) -> str:
        gran = INTERVAL_TO_GRANULARITY.get(interval)
        if not gran:
            raise ValueError(f"Bitget 不支持 K 线周期: {interval}")
        return gran

    def _get(self, path: str, params: Optional[Dict] = None) -> object:
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params or {}, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "00000":
            msg = payload.get("msg") or payload.get("code")
            raise requests.exceptions.HTTPError(f"Bitget API 错误: {msg}", response=response)
        return payload.get("data")

    def ping(self) -> bool:
        try:
            self.get_coins()
            return True
        except Exception as exc:
            logger.debug("Bitget ping 失败: %s", exc)
            return False

    def get_coins(self, coin: Optional[str] = None) -> List[Dict]:
        """GET /api/v2/spot/public/coins — 获取币种信息。"""
        params = {"coin": coin} if coin else None
        data = self._get("/api/v2/spot/public/coins", params)
        return data if isinstance(data, list) else []

    def get_last_price(self, symbol: str = "BNBUSDT") -> float:
        tickers = self._get("/api/v2/spot/market/tickers", {"symbol": symbol})
        if not tickers:
            return 0.0
        item = tickers[0] if isinstance(tickers, list) else tickers
        return float(item.get("lastPr") or 0.0)

    def get_ticker(self, symbol: str = "BNBUSDT") -> Dict:
        """返回与 Binance 24hr ticker 兼容的字段子集。"""
        tickers = self._get("/api/v2/spot/market/tickers", {"symbol": symbol})
        if not tickers:
            raise requests.exceptions.HTTPError(f"Bitget 无 {symbol} 行情")
        item = tickers[0] if isinstance(tickers, list) else tickers
        change = float(item.get("change24h") or 0.0)
        return {
            "symbol": symbol,
            "lastPrice": item.get("lastPr"),
            "priceChangePercent": str(change * 100),
            "highPrice": item.get("high24h"),
            "lowPrice": item.get("low24h"),
            "openPrice": item.get("open"),
            "volume": item.get("baseVolume"),
            "quoteVolume": item.get("quoteVolume") or item.get("usdtVolume"),
            "_source": "bitget",
        }

    def _candles_to_dataframe(self, rows: List, interval: str) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        interval_ms = INTERVAL_MS.get(interval, 3_600_000)
        records = []
        for row in rows:
            if not row or len(row) < 6:
                continue
            open_ms = int(row[0])
            records.append({
                "open_time": pd.to_datetime(open_ms, unit="ms"),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": pd.to_datetime(open_ms + interval_ms - 1, unit="ms"),
                "quote_volume": float(row[7] if len(row) > 7 else row[6]),
                "trades": 0,
            })

        df = pd.DataFrame(records)
        if df.empty:
            return df
        df = df.sort_values("open_time").drop_duplicates(subset=["open_time"])
        return df[["open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades"]]

    def get_klines(
        self,
        symbol: str = "BNBUSDT",
        interval: str = "1h",
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> pd.DataFrame:
        granularity = self.to_granularity(interval)
        params: Dict[str, str] = {
            "symbol": symbol,
            "granularity": granularity,
            "limit": str(min(max(int(limit), 1), 1000)),
        }
        if start_time:
            params["startTime"] = str(start_time)
        if end_time:
            params["endTime"] = str(end_time)

        rows = self._get("/api/v2/spot/market/candles", params)
        if not isinstance(rows, list):
            rows = []
        df = self._candles_to_dataframe(rows, interval)
        logger.info("Bitget 获取 %d 条 %s %s K线", len(df), symbol, interval)
        return df

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
            start_time = int((datetime.now() - delta).timestamp() * 1000)
        else:
            start_time = int(pd.Timestamp(start_str).timestamp() * 1000)

        if end_str:
            end_time = int(pd.Timestamp(end_str).timestamp() * 1000)
        else:
            end_time = int(datetime.now().timestamp() * 1000)

        all_data: List[pd.DataFrame] = []
        current_time = start_time

        while current_time < end_time:
            df = self.get_klines(
                symbol=symbol,
                interval=interval,
                limit=1000,
                start_time=current_time,
                end_time=end_time,
            )
            if df.empty:
                break
            all_data.append(df)
            last_open_ms = int(df["open_time"].iloc[-1].timestamp() * 1000)
            next_time = last_open_ms + 1
            if next_time <= current_time:
                break
            current_time = next_time
            if len(df) < 1000:
                break
            time.sleep(0.2)

        if not all_data:
            return pd.DataFrame()
        result = pd.concat(all_data, ignore_index=True)
        result = result.drop_duplicates(subset=["open_time"])
        logger.info("Bitget 历史 K 线共 %d 条", len(result))
        return result
