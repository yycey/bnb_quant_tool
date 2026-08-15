"""
TikHub Twitter API 客户端
=========================
文档: https://docs.tikhub.io/191321711e0 (用户发帖)
      https://docs.tikhub.io/215701673e0 (搜索)

用于拉取指定 Twitter 用户发帖与关键词搜索，输出与 NewsCollector 兼容的新闻条目。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

TIKHUB_BASE = "https://api.tikhub.io"
USER_POST_PATH = "/api/v1/twitter/web/fetch_user_post_tweet"
USER_PROFILE_PATH = "/api/v1/twitter/web/fetch_user_profile"
SEARCH_PATH = "/api/v1/twitter/web/fetch_search_timeline"

# 部分账号 screen_name 会触发上游 400，但 rest_id 可正常拉帖（实测 cz_binance / ErikVoorhees）
REST_ID_OVERRIDES: Dict[str, int] = {
    "cz_binance": 902926941413453824,
    "erikvoorhees": 61417559,
}


class TikHubTwitterClient:
    """TikHub Twitter-Web-API 封装。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 20,
        cache_seconds: int = 86400,
        cache_dir: Optional[str] = None,
        base_url: str = TIKHUB_BASE,
        rest_id_overrides: Optional[Dict[str, int]] = None,
        request_interval: float = 0.25,
        max_retries: int = 2,
    ):
        self.api_key = (api_key or os.environ.get("TIKHUB_API_KEY") or "").strip()
        self.timeout = timeout
        self.cache_seconds = max(0, int(cache_seconds))
        self.cache_dir = Path(cache_dir or "data/tikhub_cache")
        self.base_url = base_url.rstrip("/")
        self.request_interval = max(0.0, float(request_interval))
        self.max_retries = max(0, int(max_retries))
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._rest_id_cache: Dict[str, int] = {}
        overrides = dict(REST_ID_OVERRIDES)
        for key, value in (rest_id_overrides or {}).items():
            name = str(key).strip().lstrip("@").lower()
            if name and value:
                overrides[name] = int(value)
        self._rest_id_overrides = overrides

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch_user_posts(
        self,
        screen_name: str,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        """获取用户发帖列表，返回 (tweets, next_cursor)。"""
        account = screen_name.strip().lstrip("@")
        if not account:
            return [], None

        data = self._fetch_user_post_data(account, cursor=cursor)
        if not data:
            return [], None
        tweets = self._extract_tweets(data)
        return tweets, data.get("next_cursor")

    def _fetch_user_post_data(
        self,
        account: str,
        cursor: Optional[str] = None,
    ) -> Optional[Dict]:
        """先 screen_name 拉帖；失败则自动解析 rest_id 重试。"""
        cache_key = f"user:{account.lower()}"
        if cursor:
            cache_key = f"{cache_key}:{cursor[:32]}"

        key = account.lower()
        known_rest_id = self._rest_id_overrides.get(key) or self._rest_id_cache.get(key)

        if not known_rest_id:
            params: Dict[str, Any] = {"screen_name": account}
            if cursor:
                params["cursor"] = cursor
            data = self._request(USER_POST_PATH, params, cache_key=cache_key)
            if data:
                return data

        rest_id = known_rest_id or self.resolve_rest_id(account)
        if not rest_id:
            return None

        rid_params: Dict[str, Any] = {"rest_id": rest_id}
        if cursor:
            rid_params["cursor"] = cursor
        rid_cache_key = f"user_id:{rest_id}"
        if cursor:
            rid_cache_key = f"{rid_cache_key}:{cursor[:32]}"

        data = self._request(USER_POST_PATH, rid_params, cache_key=rid_cache_key)
        if data and known_rest_id is None:
            logger.info("[tikhub] 用户 %s 使用 rest_id=%s 拉帖成功", account, rest_id)
        return data

    def resolve_rest_id(self, screen_name: str) -> Optional[int]:
        """解析 Twitter 用户 rest_id（缓存 + 配置覆盖 + profile API）。"""
        account = screen_name.strip().lstrip("@")
        if not account:
            return None

        key = account.lower()
        if key in self._rest_id_cache:
            return self._rest_id_cache[key]
        if key in self._rest_id_overrides:
            rest_id = self._rest_id_overrides[key]
            self._rest_id_cache[key] = rest_id
            return rest_id

        profile = self._request(
            USER_PROFILE_PATH,
            {"screen_name": account},
            cache_key=f"profile:{key}",
        )
        if not profile:
            return None

        rest_id = self._extract_rest_id(profile)
        if rest_id:
            self._rest_id_cache[key] = rest_id
        return rest_id

    @staticmethod
    def _extract_rest_id(profile: Dict[str, Any]) -> Optional[int]:
        for field in ("rest_id", "id", "id_str"):
            raw = profile.get(field)
            if raw is None or raw == "":
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return None

    def fetch_search(
        self,
        keyword: str,
        search_type: str = "Latest",
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        """搜索 Twitter 时间线，返回 (tweets, next_cursor)。"""
        params: Dict[str, Any] = {
            "keyword": keyword,
            "search_type": search_type or "Latest",
        }
        if cursor:
            params["cursor"] = cursor
        data = self._request(
            SEARCH_PATH,
            params,
            cache_key=f"search:{keyword.strip().lower()}:{search_type}",
        )
        if not data:
            return [], None
        tweets = self._extract_tweets(data)
        return tweets, data.get("next_cursor")

    def collect_news_items(
        self,
        accounts: Optional[List[str]] = None,
        search_queries: Optional[List[str]] = None,
        search_type: str = "Latest",
        max_per_source: int = 20,
    ) -> List[Dict]:
        """聚合用户发帖 + 搜索，转为 NewsCollector 兼容格式。"""
        if not self.enabled:
            return []

        agg_key = self._aggregate_cache_key(
            accounts, search_queries, search_type, max_per_source,
        )
        cached_items = self._get_aggregate_cache(agg_key)
        if cached_items is not None:
            return cached_items

        items: List[Dict] = []
        seen_ids: set = set()

        for account in accounts or []:
            account = account.strip().lstrip("@")
            if not account:
                continue
            try:
                if self.request_interval > 0:
                    time.sleep(self.request_interval)
                tweets, _ = self.fetch_user_posts(account)
                for tw in tweets[:max_per_source]:
                    item = self._tweet_to_news_item(tw, source_label=f"Twitter/@{account}")
                    tid = item.get("tweet_id") or item.get("url")
                    if tid and tid in seen_ids:
                        continue
                    if tid:
                        seen_ids.add(tid)
                    items.append(item)
            except Exception as e:
                logger.warning("[tikhub] 用户 %s 发帖拉取失败: %s", account, e)

        for query in search_queries or []:
            query = query.strip()
            if not query:
                continue
            try:
                tweets, _ = self.fetch_search(query, search_type=search_type)
                for tw in tweets[:max_per_source]:
                    item = self._tweet_to_news_item(
                        tw,
                        source_label=f"TwitterSearch:{query[:40]}",
                    )
                    tid = item.get("tweet_id") or item.get("url")
                    if tid and tid in seen_ids:
                        continue
                    if tid:
                        seen_ids.add(tid)
                    items.append(item)
            except Exception as e:
                logger.warning("[tikhub] 搜索 '%s' 失败: %s", query, e)

        items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)
        self._set_aggregate_cache(agg_key, items)
        return items

    def _request(
        self,
        path: str,
        params: Dict[str, Any],
        cache_key: str,
    ) -> Optional[Dict]:
        if not self.enabled:
            return None

        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.get(
                    url, headers=headers, params=params, timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    last_error = resp.text[:200]
                    if self._should_retry_http(resp.status_code, last_error) and attempt < self.max_retries:
                        time.sleep(0.6 * (attempt + 1))
                        continue
                    logger.warning(
                        "[tikhub] %s HTTP %s: %s",
                        path,
                        resp.status_code,
                        last_error,
                    )
                    return None

                body = resp.json()
                if body.get("code") != 200:
                    msg = body.get("message") or body.get("message_zh") or ""
                    last_error = str(msg)
                    if self._should_retry_api_code(body.get("code"), msg) and attempt < self.max_retries:
                        time.sleep(0.6 * (attempt + 1))
                        continue
                    logger.warning("[tikhub] %s API error: %s", path, msg)
                    return None

                data = body.get("data")
                if isinstance(data, str):
                    try:
                        import json
                        data = json.loads(data)
                    except Exception:
                        data = None
                if isinstance(data, dict):
                    self._set_cache(cache_key, data)
                return data if isinstance(data, dict) else None
            except Exception as e:
                last_error = str(e)
                if attempt < self.max_retries:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                logger.warning("[tikhub] 请求失败 %s: %s", path, e)
                return None
        logger.warning("[tikhub] 请求失败 %s: %s", path, last_error)
        return None

    @staticmethod
    def _should_retry_http(status_code: int, body_text: str) -> bool:
        if status_code in (429, 502, 503, 504):
            return True
        if status_code == 400 and "retry" in (body_text or "").lower():
            return True
        return False

    @staticmethod
    def _should_retry_api_code(code: Any, message: str) -> bool:
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            return False
        if code_int in (429, 502, 503, 504):
            return True
        if code_int == 400 and "retry" in (message or "").lower():
            return True
        return False

    @staticmethod
    def _extract_tweets(data: Dict) -> List[Dict]:
        tweets: List[Dict] = []
        pinned = data.get("pinned")
        if isinstance(pinned, dict) and pinned.get("text"):
            tweets.append(pinned)
        timeline = data.get("timeline") or []
        if isinstance(timeline, list):
            for tw in timeline:
                if isinstance(tw, dict) and tw.get("text"):
                    tweets.append(tw)
        return tweets

    @classmethod
    def _tweet_to_news_item(cls, tw: Dict, source_label: str) -> Dict:
        text = (tw.get("text") or "").strip()
        tweet_id = str(tw.get("tweet_id") or "")
        author = tw.get("author") or {}
        screen_name = author.get("screen_name") or ""
        author_name = author.get("name") or screen_name
        created_raw = tw.get("created_at") or ""
        ts = cls._parse_twitter_time(created_raw)

        title = text.replace("\n", " ")
        if len(title) > 160:
            title = title[:159] + "…"
        if screen_name and not title.lower().startswith(f"@{screen_name}".lower()):
            title = f"@{screen_name}: {title}"

        url = f"https://twitter.com/{screen_name}/status/{tweet_id}" if tweet_id and screen_name else ""
        if not url and tweet_id:
            url = f"https://x.com/i/web/status/{tweet_id}"

        summary = cls._clean_tweet_text(text)
        if author_name and author_name != screen_name:
            summary = f"[{author_name}] {summary}"

        return {
            "title": title,
            "summary": summary[:320],
            "url": url,
            "source": source_label,
            "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "published_ts": ts,
            "tweet_id": tweet_id,
            "platform": "twitter",
        }

    @staticmethod
    def _clean_tweet_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _parse_twitter_time(raw: str) -> int:
        """解析 Twitter 时间格式，如 'Sun Jun 14 15:01:13 +0000 2026'。"""
        if not raw:
            return int(datetime.now(timezone.utc).timestamp())
        raw = raw.strip()
        formats = [
            "%a %b %d %H:%M:%S %z %Y",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
        return int(datetime.now(timezone.utc).timestamp())

    def _get_cache(self, key: str) -> Optional[Dict]:
        now = time.time()
        if key in self._cache:
            ts, data = self._cache[key]
            if self.cache_seconds <= 0 or now - ts < self.cache_seconds:
                return data

        disk = self._read_disk_cache(self._api_cache_path(key))
        if disk is not None:
            ts, data = disk
            if self.cache_seconds <= 0 or now - ts < self.cache_seconds:
                self._cache[key] = (ts, data)
                return data
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        ts = time.time()
        self._cache[key] = (ts, data)
        self._write_disk_cache(self._api_cache_path(key), ts, data)

    @staticmethod
    def _aggregate_cache_key(
        accounts: Optional[List[str]],
        search_queries: Optional[List[str]],
        search_type: str,
        max_per_source: int,
    ) -> str:
        norm_accounts = sorted(
            a.strip().lstrip("@").lower()
            for a in (accounts or [])
            if a and a.strip()
        )
        norm_queries = sorted(q.strip() for q in (search_queries or []) if q and q.strip())
        raw = json.dumps(
            {
                "accounts": norm_accounts,
                "search_queries": norm_queries,
                "search_type": search_type or "Latest",
                "max_per_source": int(max_per_source),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _aggregate_cache_path(self, agg_key: str) -> Path:
        return self.cache_dir / f"aggregate_{agg_key}.json"

    def _get_aggregate_cache(self, agg_key: str) -> Optional[List[Dict]]:
        disk = self._read_disk_cache(self._aggregate_cache_path(agg_key))
        if disk is None:
            return None
        ts, data = disk
        if self.cache_seconds > 0 and time.time() - ts >= self.cache_seconds:
            return None
        if not isinstance(data, list):
            return None
        age_min = int((time.time() - ts) / 60)
        logger.info(
            "[tikhub] 使用本地缓存 (%d 条, 距上次拉取 %d 分钟, 有效期 %dh)",
            len(data),
            age_min,
            max(1, self.cache_seconds // 3600),
        )
        return data

    def _set_aggregate_cache(self, agg_key: str, items: List[Dict]) -> None:
        ts = time.time()
        self._write_disk_cache(self._aggregate_cache_path(agg_key), ts, items)
        logger.info(
            "[tikhub] 拉取并缓存 %d 条 (有效期 %dh, 目录 %s)",
            len(items),
            max(1, self.cache_seconds // 3600),
            self.cache_dir,
        )

    def _api_cache_path(self, key: str) -> Path:
        safe = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"api_{safe}.json"

    @staticmethod
    def _read_disk_cache(path: Path) -> Optional[Tuple[float, Any]]:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            ts = float(payload.get("ts", 0))
            if "data" not in payload:
                return None
            return ts, payload["data"]
        except Exception as exc:
            logger.debug("[tikhub] 读取缓存失败 %s: %s", path, exc)
            return None

    def _write_disk_cache(self, path: Path, ts: float, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ts": ts, "data": data}, fh, ensure_ascii=False)
            tmp.replace(path)
        except Exception as exc:
            logger.warning("[tikhub] 写入缓存失败 %s: %s", path, exc)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._rest_id_cache.clear()
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink()
            except Exception as exc:
                logger.debug("[tikhub] 删除缓存失败 %s: %s", path, exc)
