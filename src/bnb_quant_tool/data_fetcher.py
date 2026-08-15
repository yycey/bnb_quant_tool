"""
BNB量化交易工具 - 币安数据获取模块
负责从币安API获取BNB的历史和实时数据
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from .bitget_fetcher import BitgetDataFetcher
from .kline_archive import KlineArchive
from .kline_local_fetch import needs_tip_refresh
from .kline_sync import fetch_historical_from_api as _fetch_historical_api_chain
from .kline_sync import sync_archive_recent

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class BinanceDataFetcher:
    """币安数据获取器（支持多镜像自动切换 + Bitget 备用）"""

    # API 镜像列表：按优先级排序，自动回退
    # .me 是官方国内镜像，.com 是国际站
    MIRROR_URLS = [
        "https://api.binance.me",
        "https://api.binance.com",
    ]
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        *,
        kline_cache_seconds: float = 60.0,
        historical_cache_seconds: float = 60.0,
        price_cache_seconds: float = 5.0,
        bitget_config: Optional[Dict] = None,
        kline_archive_config: Optional[Dict] = None,
        mexc_config: Optional[Dict] = None,
        default_symbol: str = "BNBUSDT",
    ):
        """
        初始化币安数据获取器

        Args:
            api_key: 币安API Key（可选，公开接口不需要）
            api_secret: 币安API Secret（可选，公开接口不需要）
            kline_cache_seconds: 单次 K 线请求缓存 TTL（秒）
            historical_cache_seconds: 历史 K 线整段结果缓存 TTL（秒）
            price_cache_seconds: 实时价缓存 TTL（秒）
            bitget_config: Bitget 备用 {enabled, fallback, base_url}
            kline_archive_config: 本地 K 线归档 {enabled, prefer_local, workspace}
            default_symbol: 默认交易对（本地归档路径用）
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.default_symbol = default_symbol.upper()
        self.base_url: str = ""
        self._kline_cache_ttl = max(0.0, float(kline_cache_seconds))
        self._historical_cache_ttl = max(0.0, float(historical_cache_seconds))
        self._price_cache_ttl = max(0.0, float(price_cache_seconds))
        self._cache_lock = threading.Lock()
        self._kline_cache: Dict[Tuple, Tuple[pd.DataFrame, float]] = {}
        self._historical_cache: Dict[Tuple, Tuple[pd.DataFrame, float]] = {}
        self._price_cache: Dict[str, Tuple[float, float]] = {}
        self.last_data_source: str = "binance"

        bg_cfg = bitget_config or {}
        self._bitget_enabled = bool(bg_cfg.get("enabled", True))
        self._bitget_fallback = bool(bg_cfg.get("fallback", True))
        self._bitget: Optional[BitgetDataFetcher] = None
        if self._bitget_enabled:
            self._bitget = BitgetDataFetcher(base_url=bg_cfg.get("base_url"))

        ka_cfg = kline_archive_config or {}
        self._archive_enabled = bool(ka_cfg.get("enabled", True))
        # false = 本地优先增量；true = 仅读本地（离线）
        self._archive_prefer_local = bool(ka_cfg.get("prefer_local", False))
        # 有本地归档时：只拉「上次最后一根 → 现在」，缺历史再补缺口
        self._archive_incremental = bool(ka_cfg.get("incremental", True))
        self._archive_auto_sync = bool(ka_cfg.get("auto_sync_on_analysis", True))
        self._archive_sync_lookback = int(ka_cfg.get("sync_lookback_days", 3))
        self._archive_sync_min_seconds = float(ka_cfg.get("sync_min_seconds", 60))
        self._archive_sync_source = str(ka_cfg.get("download_source", "auto"))
        self._archive_workspace = ka_cfg.get("workspace") or str(_PACKAGE_ROOT)
        self._mexc_cfg = mexc_config or {}
        self._bitget_cfg = bitget_config or {}
        self._last_archive_sync: Dict[Tuple[str, str], float] = {}

        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-MBX-APIKEY": api_key})
        # 设置默认 Headers
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
        })
        # 启动时探测可用镜像
        self._probe_mirror()

    def _cache_get(
        self,
        store: Dict[Tuple, Tuple[pd.DataFrame, float]],
        key: Tuple,
        ttl: float,
    ) -> Optional[pd.DataFrame]:
        if ttl <= 0:
            return None
        now = time.time()
        with self._cache_lock:
            entry = store.get(key)
            if entry and now - entry[1] < ttl:
                return entry[0].copy()
        return None

    def _cache_set(
        self,
        store: Dict[Tuple, Tuple[pd.DataFrame, float]],
        key: Tuple,
        df: pd.DataFrame,
    ) -> None:
        with self._cache_lock:
            store[key] = (df.copy(), time.time())

    def _probe_mirror(self):
        """启动时探测可用 API 镜像，选第一个可达的"""
        for mirror in self.MIRROR_URLS:
            try:
                r = requests.get(f"{mirror}/api/v3/ping", timeout=5)
                if r.status_code == 200:
                    self.base_url = mirror
                    logger.info(f"Binance API 镜像选中: {mirror}")
                    return
            except Exception:
                continue
        # 全部失败则用默认
        self.base_url = self.MIRROR_URLS[0]
        logger.warning(f"所有 Binance 镜像不可达，使用默认: {self.base_url}")

    def _switch_mirror(self):
        """当前镜像失败后，切换到下一个镜像"""
        current_idx = self.MIRROR_URLS.index(self.base_url) if self.base_url in self.MIRROR_URLS else -1
        next_idx = current_idx + 1
        if next_idx < len(self.MIRROR_URLS):
            self.base_url = self.MIRROR_URLS[next_idx]
            logger.info(f"切换 Binance API 镜像到: {self.base_url}")
            return True
        # 所有镜像都试过了，回到第一个
        self.base_url = self.MIRROR_URLS[0]
        return False

    def _can_bitget_fallback(self, interval: Optional[str] = None) -> bool:
        if not self._bitget_fallback or not self._bitget:
            return False
        if interval and not BitgetDataFetcher.supports_interval(interval):
            return False
        return True

    def _archive_for(self, symbol: str, interval: str) -> KlineArchive:
        return KlineArchive(self._archive_workspace, symbol=symbol, interval=interval)

    def _load_from_archive(
        self,
        symbol: str,
        interval: str,
        start_str: str,
        end_str: Optional[str],
        cache_key: Tuple,
    ) -> Optional[pd.DataFrame]:
        """API 不可用时的本地归档兜底。"""
        if not self._archive_enabled:
            return None
        archive = self._archive_for(symbol, interval)
        if not archive.has_local_data():
            return None
        df = archive.load_range(start_str, end_str)
        if df.empty:
            return None
        logger.info(
            "本地 K 线归档命中: %s %s (%d 条, %s ~ %s)",
            symbol,
            interval,
            len(df),
            df["open_time"].iloc[0],
            df["open_time"].iloc[-1],
        )
        self.last_data_source = "local"
        self._cache_set(self._historical_cache, cache_key, df)
        return df.copy()

    def _invalidate_historical_cache(self, symbol: str, interval: str) -> None:
        with self._cache_lock:
            drop = [k for k in self._historical_cache if k[0] == symbol and k[1] == interval]
            for k in drop:
                del self._historical_cache[k]

    def sync_kline_archive(
        self,
        symbol: Optional[str] = None,
        interval: str = "1h",
        *,
        fresh_df: Optional[pd.DataFrame] = None,
        force: bool = False,
    ) -> Dict:
        """增量同步最新 K 线到本地归档（分析/更新时调用）。"""
        if not self._archive_enabled or not self._archive_auto_sync:
            return {"updated": False, "skipped": True, "reason": "disabled"}

        symbol = (symbol or self.default_symbol).upper()
        key = (symbol, interval)
        now = time.time()
        if (
            not force
            and fresh_df is None
            and now - self._last_archive_sync.get(key, 0) < self._archive_sync_min_seconds
        ):
            return {"updated": False, "skipped": True, "reason": "throttled"}

        archive = self._archive_for(symbol, interval)
        try:
            result = sync_archive_recent(
                archive,
                symbol,
                interval,
                self._archive_sync_lookback,
                source=self._archive_sync_source,
                mexc_base_url=self._mexc_cfg.get("base_url"),
                bitget_base_url=self._bitget_cfg.get("base_url"),
                binance_fetcher=self,
                fresh_df=fresh_df,
                data_source=self.last_data_source if fresh_df is not None else None,
            )
            if result.get("updated"):
                self._invalidate_historical_cache(symbol, interval)
            if fresh_df is None:
                self._last_archive_sync[key] = now
            return result
        except Exception as exc:
            logger.warning("K线归档增量同步失败 %s %s: %s", symbol, interval, exc)
            return {"updated": False, "error": str(exc)}

    def fetch_historical_from_api(
        self,
        symbol: str = "BNBUSDT",
        interval: str = "1h",
        start_str: str = "1 day ago",
        end_str: Optional[str] = None,
    ) -> pd.DataFrame:
        """API 链拉取（MEXC → Binance → Bitget），不读本地。"""
        df, src = _fetch_historical_api_chain(
            symbol,
            interval,
            start_str,
            end_str,
            source=self._archive_sync_source,
            mexc_base_url=self._mexc_cfg.get("base_url"),
            bitget_base_url=self._bitget_cfg.get("base_url"),
            binance_fetcher=self,
        )
        if not df.empty:
            self.last_data_source = src
        return df

    def _fetch_binance_api(
        self,
        symbol: str,
        interval: str,
        start_str: str,
        end_str: Optional[str],
    ) -> pd.DataFrame:
        """Binance REST 分批拉取，失败则 Bitget。"""
        if end_str:
            end_ts = pd.Timestamp(end_str)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            end_time = int(end_ts.timestamp() * 1000)
        else:
            end_time = int(datetime.now(timezone.utc).timestamp() * 1000)

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
            start_time = int((datetime.now(timezone.utc) - delta).timestamp() * 1000)
        else:
            ts = pd.Timestamp(start_str)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            start_time = int(ts.timestamp() * 1000)

        all_data: List[pd.DataFrame] = []
        current_time = start_time
        try:
            while current_time < end_time:
                df = self.get_klines(
                    symbol=symbol,
                    interval=interval,
                    limit=1000,
                    start_time=current_time,
                    end_time=end_time,
                    allow_fallback=False,
                )
                if df.empty:
                    break
                all_data.append(df)
                current_time = int(df["open_time"].iloc[-1].timestamp() * 1000) + 1
                if len(df) < 1000:
                    break
                time.sleep(0.3)
        except Exception as exc:
            logger.warning("Binance API 历史拉取中断: %s", exc)
            all_data = []

        if all_data:
            result = pd.concat(all_data, ignore_index=True).drop_duplicates(subset=["open_time"])
            self.last_data_source = "binance"
            return result.copy()

        if self._can_bitget_fallback(interval):
            result = self._bitget.get_historical_klines(
                symbol=symbol, interval=interval, start_str=start_str, end_str=end_str,
            )
            if not result.empty:
                self.last_data_source = "bitget"
                return result.copy()
        return pd.DataFrame()

    def _fallback_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: Optional[int],
        end_time: Optional[int],
        cache_key: Tuple,
        reason: Exception,
    ) -> pd.DataFrame:
        if not self._can_bitget_fallback(interval):
            raise reason
        logger.warning("Binance K线失败，切换 Bitget 备用: %s", reason)
        df = self._bitget.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        self.last_data_source = "bitget"
        self._cache_set(self._kline_cache, cache_key, df)
        return df.copy()

    def _request_with_retry(self, url: str, method: str = "GET",
                            max_retries: int = 3, **kwargs) -> requests.Response:
        """
        带重试和镜像切换的请求。
        
        特性：
        - 强制 Connection: close 头，避免 keep-alive 导致的 SSL EOF
        - 连接失败时自动切换镜像重试
        - 指数退避重试
        """
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        kwargs["headers"]["Connection"] = "close"

        last_error = None
        mirrors_tried = 0
        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                if attempt < max_retries - 1:
                    # 每隔 2 次重试切换一次镜像
                    if mirrors_tried < len(self.MIRROR_URLS) - 1:
                        self._switch_mirror()
                        mirrors_tried += 1
                        # 用新镜像重建 URL
                        for mirror in self.MIRROR_URLS:
                            url = url.replace(mirror, self.base_url)
                    wait = min(2 ** attempt, 8)
                    logger.warning(f"请求失败 (尝试 {attempt+1}/{max_retries}): {e}，{wait}s 后重试 (镜像: {self.base_url})...")
                    time.sleep(wait)
                    # 重试前关闭可能的坏连接
                    self.session.close()
                    self.session = requests.Session()
                    self.session.headers.update({
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate, br",
                    })
            except requests.exceptions.HTTPError as e:
                # 4xx/5xx 错误不重试，直接抛出
                raise

        raise last_error
    
    def get_klines(self, symbol: str = "BNBUSDT", interval: str = "1h", 
                   limit: int = 500, start_time: Optional[int] = None,
                   end_time: Optional[int] = None,
                   *, allow_fallback: bool = True) -> pd.DataFrame:
        """
        获取K线数据（优先本地归档，只补缺口，结果写回本地）。
        """
        endpoint = "/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        cache_key = (
            symbol,
            interval,
            limit,
            start_time,
            end_time,
        )
        cached = self._cache_get(self._kline_cache, cache_key, self._kline_cache_ttl)
        if cached is not None:
            logger.debug("K线缓存命中: %s %s", symbol, interval)
            return cached

        # —— 本地优先：无显式 start/end 时用归档尾巴 + 增量 tip ——
        use_local = (
            bool(getattr(self, "_archive_enabled", False))
            and start_time is None
            and end_time is None
            and int(limit or 0) > 0
        )
        if use_local:
            try:
                from bnb_quant_tool.kline_local_fetch import (
                    load_local_tail,
                    merge_tip,
                    needs_tip_refresh,
                    tip_start_ms,
                )
                archive = self._archive_for(symbol, interval)
                local_df = load_local_tail(archive, int(limit))
                if local_df is not None and not local_df.empty:
                    if not needs_tip_refresh(local_df, interval):
                        logger.info(
                            "K线本地命中 %s %s ×%s（无需联网）",
                            symbol, interval, len(local_df),
                        )
                        self.last_data_source = "local_archive"
                        self._cache_set(self._kline_cache, cache_key, local_df)
                        return local_df.copy()
                    # 只拉 tip
                    tip_start = tip_start_ms(local_df, interval)
                    tip_params = {
                        "symbol": symbol,
                        "interval": interval,
                        "limit": min(int(limit), 200),
                    }
                    if tip_start:
                        tip_params["startTime"] = tip_start
                    try:
                        response = self._request_with_retry(
                            f"{self.base_url}{endpoint}",
                            params=tip_params,
                            timeout=(10, 30),
                        )
                        tip_raw = response.json()
                        tip_df = self._klines_json_to_df(tip_raw)
                        merged = merge_tip(local_df, tip_df, int(limit))
                        if not merged.empty:
                            self.last_data_source = "local+tip"
                            try:
                                self.sync_kline_archive(
                                    symbol, interval, fresh_df=tip_df, force=True
                                )
                            except Exception as se:
                                logger.debug("archive tip sync: %s", se)
                            logger.info(
                                "K线增量更新 %s %s：本地%d + tip%d → %d",
                                symbol, interval, len(local_df),
                                len(tip_df), len(merged),
                            )
                            self._cache_set(self._kline_cache, cache_key, merged)
                            return merged.copy()
                    except Exception as tip_e:
                        # tip 失败不得直接锁死本地归档：继续走完整拉取 / Bitget 备用源
                        logger.warning(
                            "K线 tip 拉取失败，尝试完整拉取/备用源 %s %s: %s",
                            symbol, interval, tip_e,
                        )
            except Exception as le:
                logger.debug("local kline path: %s", le)
        
        try:
            response = self._request_with_retry(
                f"{self.base_url}{endpoint}",
                params=params,
                timeout=(10, 30),    # (连接超时, 读取超时)
            )
            data = response.json()
            df = self._klines_json_to_df(data)
            
            logger.info(f"成功获取 {len(df)} 条 {symbol} {interval} K线数据")
            self.last_data_source = "binance"
            self._cache_set(self._kline_cache, cache_key, df)
            # 写回本地归档，供下次增量
            if self._archive_enabled and not df.empty and start_time is None:
                try:
                    self.sync_kline_archive(symbol, interval, fresh_df=df)
                except Exception as se:
                    logger.debug("archive sync after get_klines: %s", se)
            return df.copy()
            
        except requests.exceptions.RequestException as e:
            logger.error(f"获取K线数据失败: {e}")
            if allow_fallback:
                return self._fallback_klines(
                    symbol, interval, limit, start_time, end_time, cache_key, e
                )
            raise
        except Exception as e:
            logger.error(f"获取K线数据异常: {e}")
            if allow_fallback:
                return self._fallback_klines(
                    symbol, interval, limit, start_time, end_time, cache_key, e
                )
            raise

    def _klines_json_to_df(self, data) -> pd.DataFrame:
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = df[col].astype(float)
        return df[['open_time', 'open', 'high', 'low', 'close', 'volume', 'quote_volume', 'trades']]
    
    def get_recent_trades(self, symbol: str = "BNBUSDT", limit: int = 500) -> List[Dict]:
        """
        获取最近成交记录
        
        Args:
            symbol: 交易对
            limit: 返回条数，默认500，最大1000
            
        Returns:
            成交记录列表
        """
        endpoint = "/api/v3/trades"
        params = {"symbol": symbol, "limit": limit}
        
        try:
            response = self._request_with_retry(
                f"{self.base_url}{endpoint}",
                params=params,
                timeout=(10, 30),
            )
            trades = response.json()
            logger.info(f"成功获取 {len(trades)} 条成交记录")
            return trades
        except requests.exceptions.RequestException as e:
            logger.error(f"获取成交记录失败: {e}")
            raise
    
    def get_order_book(self, symbol: str = "BNBUSDT", limit: int = 100) -> Dict:
        """
        获取订单簿
        
        Args:
            symbol: 交易对
            limit: 返回深度，5, 10, 20, 50, 100, 500, 1000, 5000
            
        Returns:
            订单簿数据，包含bids和asks
        """
        endpoint = "/api/v3/depth"
        params = {"symbol": symbol, "limit": limit}
        
        try:
            response = self._request_with_retry(
                f"{self.base_url}{endpoint}",
                params=params,
                timeout=(10, 30),
            )
            order_book = response.json()
            logger.info(f"成功获取 {symbol} 订单簿")
            return order_book
        except requests.exceptions.RequestException as e:
            logger.error(f"获取订单簿失败: {e}")
            raise
    
    def get_ticker(self, symbol: str = "BNBUSDT") -> Dict:
        """
        获取24小时价格变动情况
        
        Args:
            symbol: 交易对
            
        Returns:
            价格变动统计信息
        """
        endpoint = "/api/v3/ticker/24hr"
        params = {"symbol": symbol}
        
        try:
            response = self._request_with_retry(
                f"{self.base_url}{endpoint}",
                params=params,
                timeout=(10, 30),
            )
            ticker = response.json()
            logger.info(f"成功获取 {symbol} 24小时行情")
            self.last_data_source = "binance"
            return ticker
        except requests.exceptions.RequestException as e:
            logger.error(f"获取行情失败: {e}")
            if self._can_bitget_fallback():
                logger.warning("Binance ticker 失败，切换 Bitget 备用")
                self.last_data_source = "bitget"
                return self._bitget.get_ticker(symbol)
            raise

    def get_price_with_fallback(
        self,
        symbol: str = "BNBUSDT",
        *,
        cache_ttl: Optional[float] = None,
    ) -> float:
        """获取实时价：优先 ticker/price，失败回退 24h ticker；带短 TTL 缓存。"""
        ttl = self._price_cache_ttl if cache_ttl is None else max(0.0, float(cache_ttl))
        now = time.time()
        if ttl > 0:
            with self._cache_lock:
                cached = self._price_cache.get(symbol)
                if cached and now - cached[1] < ttl:
                    return cached[0]

        price = 0.0
        try:
            price = self.get_last_price(symbol)
        except Exception:
            pass
        if price <= 0:
            try:
                ticker = self.get_ticker(symbol)
                price = float(ticker.get("lastPrice") or 0.0)
            except Exception:
                price = 0.0

        if price <= 0 and self._can_bitget_fallback():
            try:
                price = self._bitget.get_last_price(symbol)
                if price > 0:
                    self.last_data_source = "bitget"
            except Exception:
                pass

        if price > 0 and ttl > 0:
            with self._cache_lock:
                self._price_cache[symbol] = (price, now)
            return price

        # 失败时仅允许短暂沿用缓存（默认最多 60s），禁止把数小时前的价当现价
        if ttl > 0:
            hard_max = max(60.0, ttl * 12.0)
            with self._cache_lock:
                cached = self._price_cache.get(symbol)
                if cached and now - cached[1] < hard_max:
                    logger.debug(
                        "get_price_with_fallback 使用短暂过期缓存 %s age=%.1fs",
                        symbol,
                        now - cached[1],
                    )
                    return cached[0]
        return price

    def resolve_current_price(self, symbol: str, df: Optional[pd.DataFrame] = None) -> float:
        """现价优先走实时 ticker；失败才用 K 线最后收盘（并拒绝过旧 bar）。"""
        try:
            live = float(self.get_price_with_fallback(symbol) or 0.0)
            if live > 0:
                return live
        except Exception as exc:
            logger.debug("resolve_current_price ticker 失败 %s: %s", symbol, exc)

        if df is None or getattr(df, "empty", True) or "close" not in df.columns:
            return 0.0
        close = float(df["close"].iloc[-1])
        if "open_time" in df.columns:
            try:
                last_t = pd.Timestamp(df["open_time"].iloc[-1])
                now_utc = pd.Timestamp.now(tz="UTC")
                if last_t.tzinfo is None:
                    # 交易所 K 线一般为 UTC naive；测试/本地帧可能是本地 naive。
                    # 若按 UTC 解释会变成「未来 bar」，改按本地墙钟计龄。
                    age_sec = (now_utc - last_t.tz_localize("UTC")).total_seconds()
                    if age_sec < -600:
                        age_sec = (pd.Timestamp.now() - last_t).total_seconds()
                else:
                    age_sec = (now_utc - last_t.tz_convert("UTC")).total_seconds()
                # 超过约 2 根 1h 的容差则视为不可用，避免再写出假 604
                if age_sec > 3 * 3600:
                    logger.warning(
                        "resolve_current_price: K线过旧且无 ticker (%s age=%.0fs close=%.4f)",
                        symbol,
                        age_sec,
                        close,
                    )
                    return 0.0
            except Exception:
                pass
        return close

    def get_last_price(self, symbol: str = "BNBUSDT") -> float:
        """轻量实时价（模拟盘 watcher / 平仓专用，短超时、少重试）"""
        try:
            response = self._request_with_retry(
                f"{self.base_url}/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=(3, 6),
                max_retries=1,
            )
            return float(response.json().get("price") or 0.0)
        except Exception as e:
            logger.debug(f"get_last_price 失败 {symbol}: {e}")
            if self._can_bitget_fallback():
                try:
                    price = self._bitget.get_last_price(symbol)
                    if price > 0:
                        self.last_data_source = "bitget"
                        return price
                except Exception as bg_err:
                    logger.debug(f"Bitget get_last_price 失败 {symbol}: {bg_err}")
            return 0.0
    
    @staticmethod
    def _parse_time_bound(value: Optional[str], *, default_now: bool = False) -> datetime:
        """解析时间边界。无时区时按 UTC 处理（与币安 K 线 open_time 一致）。"""
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        if not value:
            return now_utc if default_now else now_utc
        if "ago" in value:
            num, unit = value.split(" ")[0], value.split(" ")[1]
            if "day" in unit:
                delta = timedelta(days=int(num))
            elif "hour" in unit:
                delta = timedelta(hours=int(num))
            elif "minute" in unit:
                delta = timedelta(minutes=int(num))
            else:
                raise ValueError(f"不支持的时间单位: {unit}")
            return now_utc - delta
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            return ts.tz_convert("UTC").tz_localize(None).to_pydatetime()
        # 裸时间戳按 UTC 理解，避免与本地 now() 混比
        return ts.to_pydatetime()

    def _fetch_incremental_from_archive(
        self,
        symbol: str,
        interval: str,
        start_str: str,
        end_str: Optional[str],
        hist_key: Tuple,
    ) -> Optional[pd.DataFrame]:
        """本地已有 → 只补「最后一根到现在」(+ 开头缺口)，再读本地范围。"""
        if not self._archive_enabled or not self._archive_incremental:
            return None
        archive = self._archive_for(symbol, interval)
        if not archive.has_local_data():
            return None

        try:
            req_start = self._parse_time_bound(start_str)
            req_end = self._parse_time_bound(end_str, default_now=True)
        except Exception:
            return None

        merged = archive.load_merged()
        if merged is None or merged.empty or "open_time" not in merged.columns:
            return None

        local_start = pd.Timestamp(merged["open_time"].iloc[0]).to_pydatetime()
        local_end = pd.Timestamp(merged["open_time"].iloc[-1]).to_pydatetime()
        added = 0

        # 1) 开头缺口：请求起点早于本地起点 → 补历史
        if local_start > req_start + timedelta(hours=2):
            try:
                gap_df = self.fetch_historical_from_api(
                    symbol=symbol,
                    interval=interval,
                    start_str=req_start.isoformat(sep=" ", timespec="seconds"),
                    end_str=local_start.isoformat(sep=" ", timespec="seconds"),
                )
                if gap_df is not None and not gap_df.empty:
                    archive.merge_bars(gap_df, source=self.last_data_source or "gap_backfill")
                    added += len(gap_df)
                    logger.info(
                        "K线历史缺口补齐 %s %s: +%d 条 (%s → %s)",
                        symbol, interval, len(gap_df), req_start, local_start,
                    )
            except Exception as e:
                logger.debug("kline gap backfill: %s", e)

        # 2) 尾部增量：按「间隔 × 根数」判断是否过期（禁止用固定 20 分钟，
        #    且 open_time/now 统一按 UTC，避免东八区把 4h 当根误判过期）
        if needs_tip_refresh(merged, interval, max_lag_bars=2):
            try:
                tip_end = end_str or req_end.isoformat(sep=" ", timespec="seconds")
                tip_df = self.fetch_historical_from_api(
                    symbol=symbol,
                    interval=interval,
                    start_str=local_end.isoformat(sep=" ", timespec="seconds"),
                    end_str=tip_end,
                )
                if tip_df is not None and not tip_df.empty:
                    archive.merge_bars(tip_df, source=self.last_data_source or "tip_sync")
                    added += len(tip_df)
                    logger.info(
                        "K线增量同步 %s %s: +%d 条 (自 %s)",
                        symbol, interval, len(tip_df), local_end,
                    )
                    merged = archive.load_merged()
                    if merged is not None and not merged.empty:
                        local_end = pd.Timestamp(merged["open_time"].iloc[-1]).to_pydatetime()
                # tip 拉空或合并后仍落后超过 2 根 → 交还给全量 API
                if merged is None or merged.empty or needs_tip_refresh(
                    merged, interval, max_lag_bars=2,
                ):
                    logger.warning(
                        "K线 tip 仍过期 %s %s: local_end=%s req_end_utc=%s，回退全量",
                        symbol, interval, local_end, req_end,
                    )
                    return None
            except Exception as e:
                logger.warning("K线增量同步失败，将回退全量: %s", e)
                return None

        result = archive.load_range(start_str, end_str)
        if result is None or result.empty:
            return None

        src = "local+incremental" if added else "local"
        self.last_data_source = src
        self._cache_set(self._historical_cache, hist_key, result)
        logger.info(
            "本地K线复用 %s %s: %d 条 [%s] (新增写入 %d)",
            symbol, interval, len(result), src, added,
        )
        return result.copy()

    def get_historical_klines(self, symbol: str = "BNBUSDT",
                              interval: str = "1h",
                              start_str: str = "1 day ago",
                              end_str: str = None,
                              *, skip_sync: bool = False) -> pd.DataFrame:
        """
        获取历史 K 线。

        默认策略（incremental=true）:
          本地已有 → 只拉「上次最后一根到现在」(+必要历史缺口) → 合并后读本地
          本地没有 → 全量 API → 写入本地
          离线 prefer_local → 只读本地
        """
        hist_key = (symbol, interval, start_str, end_str or "")
        cached = self._cache_get(self._historical_cache, hist_key, self._historical_cache_ttl)
        if cached is not None:
            logger.debug("历史K线缓存命中: %s %s", symbol, interval)
            return cached

        # 离线模式：只读本地
        if self._archive_prefer_local:
            local_df = self._load_from_archive(symbol, interval, start_str, end_str, hist_key)
            if local_df is not None:
                return local_df

        # 增量：本地优先，只补新段
        if not skip_sync:
            incr = self._fetch_incremental_from_archive(
                symbol, interval, start_str, end_str, hist_key,
            )
            if incr is not None and not incr.empty:
                return incr

        # 全量 API（首次或增量失败）
        result = self.fetch_historical_from_api(
            symbol=symbol, interval=interval, start_str=start_str, end_str=end_str,
        )

        if not result.empty:
            logger.info(
                "API 全量获取历史 K 线 %s %s 共 %d 条 (source=%s)",
                symbol, interval, len(result), self.last_data_source,
            )
            if not skip_sync and self._archive_auto_sync:
                try:
                    self.sync_kline_archive(
                        symbol, interval, fresh_df=result, force=True,
                    )
                except Exception as exc:
                    logger.warning("K线写入本地归档失败: %s", exc)
            self._cache_set(self._historical_cache, hist_key, result)
            return result.copy()

        # API 失败 → 本地兜底
        logger.warning("API 历史 K 线为空，尝试本地归档: %s %s", symbol, interval)
        local_df = self._load_from_archive(symbol, interval, start_str, end_str, hist_key)
        if local_df is not None:
            logger.info("已回退到本地归档 (%d 条)", len(local_df))
            return local_df

        return pd.DataFrame()


if __name__ == "__main__":
    # 测试代码
    fetcher = BinanceDataFetcher()
    
    # 获取最近24小时1小时K线
    df = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=24)
    print("最近24小时K线数据:")
    print(df.tail())
    
    # 获取24小时行情
    ticker = fetcher.get_ticker("BNBUSDT")
    print(f"\nBNB当前价格: ${ticker['lastPrice']}")
    print(f"24小时涨跌: {ticker['priceChangePercent']}%")
