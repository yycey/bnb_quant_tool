"""通用磁盘 TTL 缓存 — 重启后仍可复用已拉取数据。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DiskTTLCache:
    """JSON 快照缓存：{ts, data}，按 key 分文件。"""

    def __init__(self, cache_dir: str | Path, *, prefix: str = "cache"):
        self.root = Path(cache_dir)
        self.prefix = prefix
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self.root / f"{self.prefix}_{safe}.json"

    def get(self, key: str, ttl_seconds: float) -> Optional[Any]:
        if ttl_seconds <= 0:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            ts = float(payload.get("ts") or 0)
            if time.time() - ts >= float(ttl_seconds):
                return None
            return payload.get("data")
        except Exception as e:
            logger.debug("disk cache read %s: %s", path, e)
            return None

    def set(self, key: str, data: Any) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "data": data}, fh, ensure_ascii=False, default=str)
            tmp.replace(path)
        except Exception as e:
            logger.debug("disk cache write %s: %s", path, e)

    def clear(self, key: Optional[str] = None) -> None:
        if key:
            try:
                self._path(key).unlink(missing_ok=True)
            except Exception:
                pass
            return
        for p in self.root.glob(f"{self.prefix}_*.json"):
            try:
                p.unlink()
            except OSError:
                pass
