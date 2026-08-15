"""
BNB量化交易工具 - 加密新闻采集模块
================================================
免费 RSS 源聚合 + 关键词过滤 + 去重 + 时间筛选。
不依赖任何第三方付费 API，也不引入新的 pip 依赖。

支持的源：
- CoinDesk
- CoinTelegraph
- Bitcoinist
- CryptoSlate
- Binance Square 公告 (RSS)
- Odaily快讯 / 文章 (REST API，见 https://github.com/ODAILY/REST-API；可回退 RSS)
- BlockBeats快讯 (API, 每天拉一次, 缓存 24h)
- BlockBeats重要 (API, 每天拉一次, 缓存 24h)
- TikHub Twitter (用户发帖 + 关键词搜索, 每天拉一次, 磁盘缓存 24h)

输出格式（每条）:
{
    "title": str,
    "summary": str,
    "url": str,
    "source": str,
    "published": "YYYY-MM-DD HH:MM",
    "published_ts": int,         # Unix 秒
    "matched_keywords": [str],
}

典型用法:
    from bnb_quant_tool.news_collector import NewsCollector
    nc = NewsCollector()
    items = nc.collect(symbol="BNB", hours=24, max_items=30)
    for it in items:
        print(it['published'], '|', it['source'], '|', it['title'])
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

import requests

from .tikhub_twitter import TikHubTwitterClient

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 默认 RSS 源（全部免费 / 无需 token）
# ----------------------------------------------------------------------
DEFAULT_SOURCES: List[Dict] = [
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "lang": "en",
    },
    {
        "name": "CoinTelegraph",
        "url": "https://cointelegraph.com/rss",
        "lang": "en",
    },
    {
        "name": "Bitcoinist",
        "url": "https://bitcoinist.com/feed/",
        "lang": "en",
    },
    {
        "name": "CryptoSlate",
        "url": "https://cryptoslate.com/feed/",
        "lang": "en",
    },
    {
        "name": "BinanceSquare",
        "url": "https://www.binance.com/en/feed/rss/news",
        "lang": "en",
        "optional": True,  # 国内可能不可达，失败时静默跳过
    },
    # Odaily 星球日报（中文 RSS）
    {
        "name": "Odaily快讯",
        "url": "https://rss.odaily.news/rss/newsflash",
        "lang": "cn",
    },
    {
        "name": "Odaily文章",
        "url": "https://rss.odaily.news/rss/post",
        "lang": "cn",
    },
]


# 不同币种关联的关键词（含中文，适配 BlockBeats 等中文源）
SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "BNB": ["BNB", "Binance Coin", "BNB Chain", "Binance", "BSC",
            "币安", "BNB链", "币安链", "币安智能链"],
    "BTC": ["BTC", "Bitcoin", "比特币"],
    "ETH": ["ETH", "Ethereum", "Vitalik", "以太坊"],
    "SOL": ["SOL", "Solana"],
    "DOGE": ["DOGE", "Dogecoin", "狗狗币"],
    "XRP": ["XRP", "Ripple", "瑞波"],
}

# 通用加密关键词（对任何币种都有间接影响，含中文）
MACRO_KEYWORDS = [
    "SEC", "ETF", "Federal Reserve", "Fed ", "interest rate",
    "regulation", "halving", "stablecoin", "CBDC", "crypto market",
    "market crash", "rally", "hack", "exploit", "exchange",
    "美联储", "降息", "加息", "监管", "稳定币", "减半",
    "加密市场", "暴跌", "暴涨", "黑客攻击", "交易所",
]

# BlockBeats API 基础地址
BLOCKBEATS_API_BASE = "https://api-pro.theblockbeats.info"

# Odaily REST API（无需鉴权）https://github.com/ODAILY/REST-API
ODAILY_API_BASE = "https://api.odaily.news"

# 默认启用的 Odaily REST 端点
DEFAULT_ODAILY_ENDPOINTS: List[Dict[str, Any]] = [
    {"name": "Odaily快讯", "path": "/api/v1/newsflash"},
    {"name": "Odaily重要快讯", "path": "/api/v1/newsflash", "params": {"isImportant": "true"}},
    {"name": "Odaily文章", "path": "/api/v1/article"},
    {"name": "Odaily宏观政策", "path": "/api/v1/newsflash/macro-policy"},
    {"name": "Odaily交易所公告", "path": "/api/v1/newsflash/exchange-announcement"},
]


class NewsCollector:
    """加密新闻采集器（多源聚合 + 过滤 + 去重）

    支持的源类型:
    - RSS (默认): CoinDesk / CoinTelegraph / Bitcoinist / CryptoSlate / BinanceSquare
    - Odaily REST: 快讯 / 文章 / 宏观 / 交易所公告（免费，无需 token）
    - BlockBeats API (律动): 中文加密快讯，需要 api_key
    """

    def __init__(
        self,
        sources: Optional[List[Dict]] = None,
        timeout: int = 10,
        user_agent: str = "Mozilla/5.0 (compatible; BNBQuantTool/1.0)",
        blockbeats_api_key: Optional[str] = None,
        blockbeats_lang: str = "cn",
        blockbeats_size: int = 50,
        blockbeats_cache_seconds: int = 86400,
        rss_cache_seconds: int = 1800,
        cache_dir: str = "data/news_cache",
        tikhub_api_key: Optional[str] = None,
        tikhub_config: Optional[Dict] = None,
        odaily_config: Optional[Dict] = None,
    ):
        self.sources = list(DEFAULT_SOURCES if sources is None else sources)
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent, "Accept": "application/rss+xml,text/xml,*/*"}
        self._seen_hashes: Set[str] = set()  # 去重
        self._cache_dir = Path(cache_dir)
        self._incremental_fetch = True

        # 持久化新闻库：已拉取的永久保留，下次按时间窗直接读
        from bnb_quant_tool.news_store import NewsStore
        store_path = self._cache_dir.parent / "news_store.db"
        try:
            self._news_store = NewsStore(store_path)
        except Exception as e:
            logger.warning("[news] NewsStore init failed: %s", e)
            self._news_store = None

        # BlockBeats 配置（磁盘 + 内存缓存，默认 24h 只拉一次 API）
        self._bb_api_key = blockbeats_api_key
        self._bb_lang = blockbeats_lang
        self._bb_size = min(max(blockbeats_size, 1), 50)
        self._bb_cache: Dict[str, List[Dict]] = {}
        self._bb_cache_ts: Dict[str, float] = {}
        self._bb_cache_seconds = max(0, int(blockbeats_cache_seconds))

        # RSS 缓存（免费源，默认 30 分钟，减少重复请求）
        self._rss_cache: Dict[str, List[Dict]] = {}
        self._rss_cache_ts: Dict[str, float] = {}
        self._rss_cache_seconds = max(0, int(rss_cache_seconds))

        # Odaily REST（免费、无需 token）
        od_cfg = dict(odaily_config or {})
        self._odaily_enabled = bool(od_cfg.get("enabled", True))
        self._odaily_base = str(od_cfg.get("base_url") or ODAILY_API_BASE).rstrip("/")
        self._odaily_lang = str(od_cfg.get("lang") or "zh-cn")
        self._odaily_size = min(max(int(od_cfg.get("size", 30)), 1), 50)
        self._odaily_cache_seconds = max(
            0, int(od_cfg.get("cache_seconds", rss_cache_seconds))
        )
        self._odaily_cache: Dict[str, List[Dict]] = {}
        self._odaily_cache_ts: Dict[str, float] = {}
        if self._odaily_enabled:
            # REST 优先，去掉同名 RSS，避免重复打网
            self.sources = [
                s for s in self.sources
                if not str(s.get("name") or "").startswith("Odaily")
            ]
            endpoints = od_cfg.get("endpoints") or DEFAULT_ODAILY_ENDPOINTS
            for ep in endpoints:
                if not isinstance(ep, dict) or not ep.get("path"):
                    continue
                self.sources.append({
                    "name": str(ep.get("name") or f"Odaily{ep['path']}"),
                    "type": "odaily",
                    "path": str(ep["path"]),
                    "params": dict(ep.get("params") or {}),
                })

        # 如果有 BlockBeats API key，自动加入两个源（全部快讯 + 重要快讯）
        if self._bb_api_key:
            self.sources.append({
                "name": "BlockBeats快讯",
                "type": "blockbeats",
                "endpoint": "/v1/newsflash",
            })
            self.sources.append({
                "name": "BlockBeats重要",
                "type": "blockbeats",
                "endpoint": "/v1/newsflash/important",
            })

        # TikHub Twitter（用户发帖 + 搜索）
        th_cfg = dict(tikhub_config or {})
        self._tikhub_client = TikHubTwitterClient(
            api_key=tikhub_api_key or th_cfg.get("api_key"),
            timeout=int(th_cfg.get("timeout", 20)),
            cache_seconds=int(th_cfg.get("cache_seconds", 86400)),
            cache_dir=th_cfg.get("cache_dir", "data/tikhub_cache"),
            rest_id_overrides=th_cfg.get("rest_id_overrides"),
            request_interval=float(th_cfg.get("request_interval", 0.25)),
            max_retries=int(th_cfg.get("max_retries", 2)),
        )
        self._tikhub_enabled = bool(th_cfg.get("enabled", True)) and self._tikhub_client.enabled
        self._tikhub_accounts = list(th_cfg.get("accounts") or [])
        self._tikhub_search_queries = list(th_cfg.get("search_queries") or [])
        self._tikhub_search_type = str(th_cfg.get("search_type", "Latest"))
        self._tikhub_max_per_source = int(th_cfg.get("max_per_source", 20))
        if self._tikhub_enabled:
            self.sources.append({
                "name": "TikHub Twitter",
                "type": "tikhub_twitter",
            })
        self._tikhub_cache_seconds = int(th_cfg.get("cache_seconds", 86400))

    # ============================================================
    # 主入口
    # ============================================================
    def collect(
        self,
        symbol: str = "BNB",
        hours: int = 24,
        max_items: int = 40,
        include_macro: bool = True,
    ) -> List[Dict]:
        """采集近 N 小时与 symbol 相关的新闻。

        Args:
            symbol: 币种代码 (BNB / BTC / ETH 等)
            hours: 只保留近多少小时的新闻
            max_items: 最多返回多少条
            include_macro: 是否包含宏观加密新闻 (SEC/ETF/美联储等)

        Returns:
            新闻列表 (按时间倒序)
        """
        symbol = (symbol or "BNB").upper().replace("USDT", "")
        kw = SYMBOL_KEYWORDS.get(symbol, [symbol])
        if include_macro:
            kw_set = kw + MACRO_KEYWORDS
        else:
            kw_set = list(kw)

        cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
        self._seen_hashes.clear()

        # 1) 持久化库：已存新闻直接复用
        pooled: List[Dict] = []
        if self._news_store is not None:
            try:
                stored = self._news_store.query_since(cutoff_ts, limit=max(max_items * 4, 100))
                pooled.extend(stored)
                if stored:
                    logger.info("[news] 本地新闻库命中 %d 条 (近 %dh)", len(stored), hours)
            except Exception as e:
                logger.debug("[news] store query: %s", e)

        # 2) 各源：水位内且本地已有数据 → 跳过联网；否则抓增量并写库
        incremental = bool(getattr(self, "_incremental_fetch", True))
        now_ts = int(time.time())
        for src in self.sources:
            name = str(src.get("name") or src.get("url") or "unknown")
            try:
                ttl = self._source_ttl_seconds(src)
                last_fetch = 0
                if self._news_store is not None and incremental:
                    try:
                        last_fetch = int(self._news_store.get_last_fetch_ts(name) or 0)
                    except Exception:
                        last_fetch = 0
                if (
                    incremental
                    and last_fetch > 0
                    and (now_ts - last_fetch) < ttl
                    and self._news_store is not None
                    and self._news_store.count_since(cutoff_ts) > 0
                ):
                    logger.info(
                        "[news] %s 跳过联网（距上次 %ds < TTL %ds，用本地）",
                        name,
                        now_ts - last_fetch,
                        ttl,
                    )
                    continue

                items = self._fetch_source(src)
                # 只保留「上次抓取之后」的条目（RSS 全量里筛增量）
                if incremental and last_fetch > 0 and items:
                    fresh = [
                        it for it in items
                        if int(it.get("published_ts") or 0) >= max(0, last_fetch - 3600)
                    ]
                    if fresh:
                        items = fresh
                logger.info(f"[news] {name} 抓取到 {len(items)} 条")
                if self._news_store is not None:
                    try:
                        if items:
                            self._news_store.upsert_many(items)
                        self._news_store.set_last_fetch(name, count=len(items), ts=now_ts)
                    except Exception as e:
                        logger.debug("[news] store upsert/meta: %s", e)
                pooled.extend(items)
            except Exception as e:
                logger.warning(f"[news] {name} 抓取失败: {e}")
                continue

        # 3) 时间窗 + 关键词 + 去重
        uniq: List[Dict] = []
        for it in pooled:
            if not isinstance(it, dict):
                continue
            if int(it.get("published_ts") or 0) < cutoff_ts:
                continue
            src_name = str(it.get("source") or "")
            if "twitter" in src_name.lower() or "tikhub" in src_name.lower():
                matched = it.get("matched_keywords") or ["twitter"]
            else:
                matched = it.get("matched_keywords") or self._match_keywords(
                    (it.get("title") or "") + " " + (it.get("summary") or ""), kw_set,
                )
            if not matched:
                continue
            it = dict(it)
            it["matched_keywords"] = matched
            h = self._hash_item(it)
            if h in self._seen_hashes:
                continue
            self._seen_hashes.add(h)
            uniq.append(it)

        uniq.sort(key=lambda x: x.get("published_ts", 0), reverse=True)
        return uniq[:max_items]

    def _source_ttl_seconds(self, src: Dict) -> int:
        t = str(src.get("type") or "rss").lower()
        if t == "blockbeats":
            return int(self._bb_cache_seconds or 86400)
        if t == "odaily":
            return int(self._odaily_cache_seconds or self._rss_cache_seconds or 1800)
        if t == "tikhub_twitter":
            return int(getattr(self, "_tikhub_cache_seconds", None) or 86400)
        return int(self._rss_cache_seconds or 1800)

    # ============================================================
    # 单源抓取 (分发: RSS / Odaily / BlockBeats / TikHub)
    # ============================================================
    def _fetch_source(self, src: Dict) -> List[Dict]:
        if src.get("type") == "blockbeats":
            return self._fetch_blockbeats(src)
        if src.get("type") == "odaily":
            return self._fetch_odaily(src)
        if src.get("type") == "tikhub_twitter":
            return self._fetch_tikhub_twitter(src)
        return self._fetch_rss(src)

    def _fetch_rss(self, src: Dict) -> List[Dict]:
        """RSS 抓取（带磁盘缓存）。"""
        url = src.get("url") or ""
        name = src.get("name") or url
        cache_key = hashlib.md5(url.encode("utf-8")).hexdigest()
        now = time.time()

        mem_ts = self._rss_cache_ts.get(cache_key, 0)
        if cache_key in self._rss_cache and self._is_cache_valid(mem_ts, self._rss_cache_seconds):
            return self._rss_cache[cache_key]

        disk = self._read_disk_cache(self._rss_cache_path(cache_key))
        if disk is not None:
            ts, items = disk
            if self._is_cache_valid(ts, self._rss_cache_seconds) and isinstance(items, list):
                self._rss_cache[cache_key] = items
                self._rss_cache_ts[cache_key] = ts
                logger.info(
                    "[news] %s 使用磁盘缓存 (%d 条, 距上次 %d 分钟)",
                    name, len(items), int((now - ts) / 60),
                )
                return items

        try:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            items = self._parse_rss(resp.content, name)
        except Exception as exc:
            if src.get("optional"):
                logger.debug("[news] %s 可选源跳过: %s", name, exc)
                return self._rss_cache.get(cache_key, [])
            raise

        self._rss_cache[cache_key] = items
        self._rss_cache_ts[cache_key] = now
        self._write_disk_cache(self._rss_cache_path(cache_key), now, items)
        return items

    def _fetch_tikhub_twitter(self, src: Dict) -> List[Dict]:
        """通过 TikHub API 拉取 Twitter 用户发帖与搜索。"""
        if not self._tikhub_enabled or not self._tikhub_client.enabled:
            return []
        return self._tikhub_client.collect_news_items(
            accounts=self._tikhub_accounts,
            search_queries=self._tikhub_search_queries,
            search_type=self._tikhub_search_type,
            max_per_source=self._tikhub_max_per_source,
        )

    def refresh_tikhub(self) -> None:
        """清除 TikHub Twitter 缓存（内存 + 磁盘），下次 collect 将重新拉取 API。"""
        if self._tikhub_client:
            self._tikhub_client.clear_cache()
        logger.info("[news] TikHub Twitter 缓存已清除，下次 collect 将重新拉取")

    def _odaily_cache_key(self, src: Dict) -> str:
        path = str(src.get("path") or "")
        params = src.get("params") or {}
        raw = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _odaily_cache_path(self, cache_key: str) -> Path:
        return self._cache_dir / f"odaily_{cache_key}.json"

    def _fetch_odaily(self, src: Dict) -> List[Dict]:
        """通过 Odaily REST API 拉取快讯/文章（https://github.com/ODAILY/REST-API）。"""
        if not self._odaily_enabled:
            return []
        path = str(src.get("path") or "/api/v1/newsflash")
        cache_key = self._odaily_cache_key(src)
        now = time.time()
        last_ts = self._odaily_cache_ts.get(cache_key, 0)

        if cache_key in self._odaily_cache and self._is_cache_valid(
            last_ts, self._odaily_cache_seconds
        ):
            return self._odaily_cache[cache_key]

        disk = self._read_disk_cache(self._odaily_cache_path(cache_key))
        if disk is not None:
            ts, items = disk
            if self._is_cache_valid(ts, self._odaily_cache_seconds) and isinstance(items, list):
                self._odaily_cache[cache_key] = items
                self._odaily_cache_ts[cache_key] = ts
                logger.info(
                    "[news] %s 使用磁盘缓存 (%d 条, 距上次 %d 分钟)",
                    src["name"], len(items), int((now - ts) / 60),
                )
                return items

        url = f"{self._odaily_base}{path}"
        params: Dict[str, Any] = {
            "page": 1,
            "size": self._odaily_size,
            "lang": self._odaily_lang,
        }
        extra = src.get("params") or {}
        params.update(extra)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.headers["User-Agent"],
        }
        resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success") and int(body.get("code") or 0) != 200:
            logger.warning(
                "[news] Odaily API 返回错误: code=%s msg=%s",
                body.get("code"), body.get("msg"),
            )
            return self._odaily_cache.get(cache_key, [])

        data = body.get("data") or {}
        data_list = data.get("list") if isinstance(data, dict) else None
        if not isinstance(data_list, list):
            data_list = []

        items: List[Dict] = []
        source_name = src["name"]
        for item in data_list:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            summary_raw = (
                item.get("summary")
                or item.get("content")
                or ""
            )
            summary = self._truncate(self._clean_html(str(summary_raw)), 320)
            link = (
                str(item.get("link") or "").strip()
                or str(item.get("sourceUrl") or "").strip()
            )
            ts = self._parse_odaily_time(
                item.get("publishTimestamp"),
                item.get("publishDate"),
            )
            items.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": source_name,
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "published_ts": ts,
            })

        self._odaily_cache[cache_key] = items
        self._odaily_cache_ts[cache_key] = now
        self._write_disk_cache(self._odaily_cache_path(cache_key), now, items)
        logger.info("[news] %s 拉取并缓存 %d 条", src["name"], len(items))
        return items

    def refresh_odaily(self) -> None:
        """清除 Odaily REST 缓存（内存 + 磁盘）。"""
        self._odaily_cache.clear()
        self._odaily_cache_ts.clear()
        for path in self._cache_dir.glob("odaily_*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        logger.info("[news] Odaily 缓存已清除，下次 collect 将重新拉取")

    @staticmethod
    def _parse_odaily_time(ts_ms: Any, date_str: Any = None) -> int:
        """解析 Odaily 时间：优先 publishTimestamp(ms)，其次 publishDate。"""
        try:
            if ts_ms is not None and str(ts_ms).strip() != "":
                val = int(float(ts_ms))
                if val > 10_000_000_000:  # ms
                    val //= 1000
                if val > 0:
                    return val
        except (TypeError, ValueError):
            pass
        raw = str(date_str or "").strip()
        if raw:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                # Odaily 文档时间为本地展示串，按北京时间理解
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                return int(dt.timestamp())
            except ValueError:
                pass
        return int(datetime.now(timezone.utc).timestamp())

    def _fetch_blockbeats(self, src: Dict) -> List[Dict]:
        """通过 BlockBeats REST API 拉取快讯（磁盘 + 内存缓存）。"""
        endpoint = src.get("endpoint", "/v1/newsflash")
        now = time.time()
        last_ts = self._bb_cache_ts.get(endpoint, 0)

        if endpoint in self._bb_cache and self._is_cache_valid(last_ts, self._bb_cache_seconds):
            logger.info(
                "[news] %s 使用内存缓存 (%d 条, 距上次 %d 分钟)",
                src["name"], len(self._bb_cache[endpoint]), int((now - last_ts) / 60),
            )
            return self._bb_cache[endpoint]

        disk = self._read_disk_cache(self._bb_cache_path(endpoint))
        if disk is not None:
            ts, items = disk
            if self._is_cache_valid(ts, self._bb_cache_seconds) and isinstance(items, list):
                self._bb_cache[endpoint] = items
                self._bb_cache_ts[endpoint] = ts
                logger.info(
                    "[news] %s 使用磁盘缓存 (%d 条, 距上次 %d 分钟)",
                    src["name"], len(items), int((now - ts) / 60),
                )
                return items

        url = f"{BLOCKBEATS_API_BASE}{endpoint}"
        headers = {
            "api-key": self._bb_api_key,
            "User-Agent": self.headers["User-Agent"],
        }
        params = {"lang": self._bb_lang, "size": self._bb_size, "page": 1}
        resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 0:
            logger.warning(f"[news] BlockBeats API 返回错误: {body.get('message')}")
            return self._bb_cache.get(endpoint, [])  # 失败时返回旧缓存

        items: List[Dict] = []
        data_list = body.get("data", {}).get("data", [])
        source_name = src["name"]
        for item in data_list:
            title = item.get("title", "")
            content_html = item.get("content", "")
            summary = self._truncate(self._clean_html(content_html), 320)
            create_time = item.get("create_time", "")
            ts = self._parse_blockbeats_time(create_time)
            link = item.get("link", "") or item.get("url", "")
            items.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": source_name,
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "published_ts": ts,
            })

        self._bb_cache[endpoint] = items
        self._bb_cache_ts[endpoint] = now
        self._write_disk_cache(self._bb_cache_path(endpoint), now, items)
        logger.info("[news] %s 拉取并缓存 %d 条", src["name"], len(items))
        return items

    def refresh_blockbeats(self) -> None:
        """手动刷新 BlockBeats 缓存（内存 + 磁盘）。"""
        self._bb_cache.clear()
        self._bb_cache_ts.clear()
        for path in self._cache_dir.glob("blockbeats_*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        logger.info("[news] BlockBeats 缓存已清除，下次 collect 将重新拉取")

    def refresh_rss(self) -> None:
        """清除 RSS 缓存（内存 + 磁盘）。"""
        self._rss_cache.clear()
        self._rss_cache_ts.clear()
        for path in self._cache_dir.glob("rss_*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        logger.info("[news] RSS 缓存已清除")

    @staticmethod
    def _is_cache_valid(ts: float, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            return False
        return time.time() - ts < ttl_seconds

    def _bb_cache_path(self, endpoint: str) -> Path:
        safe = hashlib.md5(endpoint.encode("utf-8")).hexdigest()
        return self._cache_dir / f"blockbeats_{safe}.json"

    def _rss_cache_path(self, cache_key: str) -> Path:
        return self._cache_dir / f"rss_{cache_key}.json"

    @staticmethod
    def _read_disk_cache(path: Path) -> Optional[Tuple[float, Any]]:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            return float(payload.get("ts", 0)), payload.get("data")
        except Exception as exc:
            logger.debug("[news] 读取缓存失败 %s: %s", path, exc)
            return None

    def _write_disk_cache(self, path: Path, ts: float, data: Any) -> None:
        """写入磁盘缓存；若已有旧快照则按 hash 合并保留历史条目。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = data
            if isinstance(data, list) and path.exists():
                old = self._read_disk_cache(path)
                if old and isinstance(old[1], list):
                    by_hash: Dict[str, Dict] = {}
                    for it in old[1] + data:
                        if not isinstance(it, dict):
                            continue
                        by_hash[self._hash_item(it)] = it
                    merged = sorted(
                        by_hash.values(),
                        key=lambda x: int(x.get("published_ts") or 0),
                        reverse=True,
                    )[:500]
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ts": ts, "data": merged}, fh, ensure_ascii=False)
            tmp.replace(path)
        except Exception as exc:
            logger.warning("[news] 写入缓存失败 %s: %s", path, exc)

    @staticmethod
    def _parse_blockbeats_time(raw: str) -> int:
        """解析 BlockBeats 时间 'YYYY-MM-DD HH:MM:SS' (北京时间 UTC+8)"""
        if not raw:
            return int(datetime.now(timezone.utc).timestamp())
        try:
            dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
            # BlockBeats 返回北京时间，转为 UTC
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp())
        except ValueError:
            return int(datetime.now(timezone.utc).timestamp())

    def _parse_rss(self, content: bytes, source_name: str) -> List[Dict]:
        """解析标准 RSS 2.0 / Atom feed"""
        items: List[Dict] = []
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.warning(f"[news] {source_name} XML解析失败: {e}")
            return items

        # 处理命名空间
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "content": "http://purl.org/rss/1.0/modules/content/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }

        # RSS 2.0: <channel><item>
        for item in root.iter("item"):
            title = self._text(item.find("title"))
            link = self._text(item.find("link"))
            desc = self._text(item.find("description")) or self._text(
                item.find("content:encoded", ns)
            )
            pub_raw = self._text(item.find("pubDate")) or self._text(
                item.find("dc:date", ns)
            )
            ts = self._parse_pubdate(pub_raw)
            items.append({
                "title": self._clean_html(title),
                "summary": self._truncate(self._clean_html(desc), 320),
                "url": link,
                "source": source_name,
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "published_ts": ts,
            })

        # Atom: <entry>
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = self._text(entry.find("{http://www.w3.org/2005/Atom}title"))
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href") if link_el is not None else ""
            summary = self._text(entry.find("{http://www.w3.org/2005/Atom}summary")) or \
                      self._text(entry.find("{http://www.w3.org/2005/Atom}content"))
            pub_raw = self._text(entry.find("{http://www.w3.org/2005/Atom}updated")) or \
                      self._text(entry.find("{http://www.w3.org/2005/Atom}published"))
            ts = self._parse_pubdate(pub_raw)
            items.append({
                "title": self._clean_html(title),
                "summary": self._truncate(self._clean_html(summary), 320),
                "url": link,
                "source": source_name,
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "published_ts": ts,
            })

        return items

    # ============================================================
    # 工具函数
    # ============================================================
    @staticmethod
    def _text(el) -> str:
        if el is None:
            return ""
        return (el.text or "").strip()

    @staticmethod
    def _clean_html(text: str) -> str:
        if not text:
            return ""
        # 去 CDATA / HTML 标签
        text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _truncate(text: str, n: int) -> str:
        if not text:
            return ""
        return text if len(text) <= n else text[: n - 1] + "…"

    @staticmethod
    def _parse_pubdate(raw: str) -> int:
        """解析多种 RSS 时间格式 → Unix 秒"""
        if not raw:
            return int(datetime.now(timezone.utc).timestamp())
        raw = raw.strip()
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",   # RFC 822
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",         # ISO
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%d %H:%M:%S",
        ]
        # 处理 GMT
        raw_norm = raw.replace("GMT", "+0000").replace("UTC", "+0000")
        for fmt in formats:
            try:
                dt = datetime.strptime(raw_norm, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
        # 兜底：返回当前时间
        return int(datetime.now(timezone.utc).timestamp())

    @staticmethod
    def _match_keywords(text: str, keywords: List[str]) -> List[str]:
        if not text:
            return []
        text_lower = text.lower()
        matched: List[str] = []
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.append(kw)
        return matched

    @staticmethod
    def _hash_item(item: Dict) -> str:
        s = (item.get("title", "") + "|" + item.get("url", "")).encode("utf-8", errors="ignore")
        return hashlib.md5(s).hexdigest()

    # ============================================================
    # 离线打分（无需 AI 也可粗判利好/利空）
    # ============================================================
    BULLISH_WORDS = [
        "approve", "approval", "approved", "etf", "rally", "surge", "soars",
        "bull", "bullish", "all-time high", "ath", "breakout", "partnership",
        "adoption", "upgrade", "launch", "integration", "buy", "accumulate",
        "上涨", "突破", "利好", "看涨", "牛市", "增持", "通过", "批准",
    ]
    BEARISH_WORDS = [
        "ban", "banned", "hack", "hacked", "exploit", "lawsuit", "sue", "sued",
        "crash", "plunge", "plummet", "dump", "bear", "bearish", "fud",
        "regulation crackdown", "delist", "halt", "fraud", "scam",
        "下跌", "暴跌", "利空", "看跌", "熊市", "黑客", "诉讼", "禁止", "下架",
    ]

    @classmethod
    def quick_polarity(cls, items: List[Dict]) -> Dict:
        """无需 AI 的本地快速利好/利空粗判 (备用方案)"""
        bull = bear = 0
        for it in items:
            text = (it.get("title", "") + " " + it.get("summary", "")).lower()
            for w in cls.BULLISH_WORDS:
                if w in text:
                    bull += 1
                    break
            for w in cls.BEARISH_WORDS:
                if w in text:
                    bear += 1
                    break
        total = bull + bear
        if total == 0:
            polarity = "neutral"
            score = 0.0
        else:
            score = (bull - bear) / total  # [-1, 1]
            if score > 0.3:
                polarity = "bullish"
            elif score < -0.3:
                polarity = "bearish"
            else:
                polarity = "neutral"
        return {
            "polarity": polarity,
            "score": round(score, 3),
            "bullish_count": bull,
            "bearish_count": bear,
            "total_items": len(items),
        }
