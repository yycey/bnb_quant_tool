"""新闻持久化仓库 — 已拉取的新闻追加保存，按时间窗直接读取。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NewsStore:
    """SQLite 追加存储，按 item_hash 去重。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS news_items (
                        item_hash TEXT PRIMARY KEY,
                        source TEXT,
                        title TEXT,
                        summary TEXT,
                        url TEXT,
                        published_ts INTEGER,
                        payload TEXT NOT NULL,
                        created_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_news_pub ON news_items(published_ts DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fetch_meta (
                        source TEXT PRIMARY KEY,
                        last_fetch_ts INTEGER NOT NULL,
                        last_count INTEGER DEFAULT 0,
                        updated_at REAL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def get_last_fetch_ts(self, source: str) -> int:
        src = str(source or "").strip()
        if not src:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT last_fetch_ts FROM fetch_meta WHERE source=?",
                    (src,),
                ).fetchone()
                return int(row["last_fetch_ts"] or 0) if row else 0
            finally:
                conn.close()

    def set_last_fetch(self, source: str, *, count: int = 0, ts: Optional[int] = None) -> None:
        import time as _time

        src = str(source or "").strip()
        if not src:
            return
        now_ts = int(ts if ts is not None else _time.time())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO fetch_meta (source, last_fetch_ts, last_count, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        last_fetch_ts=excluded.last_fetch_ts,
                        last_count=excluded.last_count,
                        updated_at=excluded.updated_at
                    """,
                    (src, now_ts, int(count), float(_time.time())),
                )
                conn.commit()
            finally:
                conn.close()

    def count_since(self, cutoff_ts: int, *, source: Optional[str] = None) -> int:
        with self._lock:
            conn = self._connect()
            try:
                if source:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM news_items WHERE published_ts >= ? AND source=?",
                        (int(cutoff_ts), str(source)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM news_items WHERE published_ts >= ?",
                        (int(cutoff_ts),),
                    ).fetchone()
                return int(row["n"] or 0) if row else 0
            finally:
                conn.close()

    @staticmethod
    def item_hash(item: Dict[str, Any]) -> str:
        raw = "|".join([
            str(item.get("source") or ""),
            str(item.get("url") or ""),
            str(item.get("title") or ""),
            str(item.get("published_ts") or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def upsert_many(self, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        import time

        now = time.time()
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            h = self.item_hash(it)
            rows.append((
                h,
                str(it.get("source") or "")[:80],
                str(it.get("title") or "")[:300],
                str(it.get("summary") or "")[:800],
                str(it.get("url") or "")[:500],
                int(it.get("published_ts") or 0),
                json.dumps(it, ensure_ascii=False, default=str),
                now,
            ))
        if not rows:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT INTO news_items
                    (item_hash, source, title, summary, url, published_ts, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_hash) DO UPDATE SET
                        payload=excluded.payload,
                        published_ts=excluded.published_ts
                    """,
                    rows,
                )
                conn.commit()
                return max(0, conn.total_changes - before)
            finally:
                conn.close()

    def query_since(
        self,
        cutoff_ts: int,
        *,
        limit: int = 200,
        sources: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                if sources:
                    placeholders = ",".join("?" * len(sources))
                    sql = (
                        f"SELECT payload FROM news_items "
                        f"WHERE published_ts >= ? AND source IN ({placeholders}) "
                        f"ORDER BY published_ts DESC LIMIT ?"
                    )
                    cur = conn.execute(sql, (int(cutoff_ts), *sources, int(limit)))
                else:
                    cur = conn.execute(
                        """
                        SELECT payload FROM news_items
                        WHERE published_ts >= ?
                        ORDER BY published_ts DESC LIMIT ?
                        """,
                        (int(cutoff_ts), int(limit)),
                    )
                out: List[Dict[str, Any]] = []
                for row in cur.fetchall():
                    try:
                        obj = json.loads(row["payload"])
                        if isinstance(obj, dict):
                            out.append(obj)
                    except Exception:
                        continue
                return out
            finally:
                conn.close()
