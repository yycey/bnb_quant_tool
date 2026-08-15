# -*- coding: utf-8 -*-
"""
数据库工件存储 — 深度学习模型、策略库等二进制/结构化数据统一存入 SQLite。

表结构（位于 ai_learning.db）:
- artifact_blobs: 模型 pickle 等二进制工件
- discovered_strategies: StrategyLab 自动发现策略（逐条存储）
- storage_meta: 策略库版本等元数据
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEEP_LEARNING_BLOB = "deep_learning_model"
PATTERN_RECOGNIZER_BLOB = "pattern_recognizer"


class DbArtifactStore:
    """ai_learning.db 内的模型与策略库持久化。"""

    _instances: List[weakref.ref] = []

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()
        DbArtifactStore._instances.append(weakref.ref(self))

    @classmethod
    def close_all_for_path(cls, db_path: str) -> int:
        target = str(Path(db_path).resolve())
        closed = 0
        for ref in list(cls._instances):
            inst = ref()
            if inst is None:
                continue
            if inst.db_path == target:
                inst.reset_connection()
                closed += 1
        cls._instances = [r for r in cls._instances if r() is not None]
        return closed

    def reset_connection(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            try:
                self._local.conn = connect_writer(self.db_path, timeout=60.0)
            except sqlite3.DatabaseError:
                self._local.conn = None
                raise
        return self._local.conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS artifact_blobs (
                name TEXT PRIMARY KEY,
                format TEXT NOT NULL,
                data BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS discovered_strategies (
                strategy_id TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                metrics_json TEXT,
                saved_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS storage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()

    def save_blob(self, name: str, data: bytes, fmt: str = "pickle") -> None:
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO artifact_blobs "
            "(name, format, data, updated_at, size_bytes) VALUES (?,?,?,?,?)",
            (name, fmt, data, now, len(data)),
        )
        conn.commit()
        logger.info(f"DbArtifactStore: saved blob '{name}' ({len(data)} bytes)")

    def load_blob(self, name: str) -> Optional[bytes]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT data FROM artifact_blobs WHERE name=?", (name,))
        row = cur.fetchone()
        return row[0] if row else None

    def has_blob(self, name: str) -> bool:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM artifact_blobs WHERE name=? LIMIT 1", (name,))
        return cur.fetchone() is not None

    def delete_blob(self, name: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM artifact_blobs WHERE name=?", (name,))
        conn.commit()

    def save_strategies(self, strategies: List[Dict[str, Any]]) -> None:
        now = datetime.now().isoformat()
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM discovered_strategies")
        for spec in strategies:
            sid = str(spec.get("id") or "")
            if not sid:
                continue
            metrics = spec.get("metrics")
            cur.execute(
                "INSERT INTO discovered_strategies "
                "(strategy_id, spec_json, metrics_json, saved_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (
                    sid,
                    json.dumps(spec, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False) if metrics is not None else None,
                    now,
                    now,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta (key, value) VALUES (?,?)",
            ("strategy_library_version", "1"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta (key, value) VALUES (?,?)",
            ("strategy_library_saved_at", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta (key, value) VALUES (?,?)",
            ("strategy_library_count", str(len(strategies))),
        )
        conn.commit()
        logger.info(f"DbArtifactStore: saved {len(strategies)} discovered strategies")

    def load_strategies(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT spec_json FROM discovered_strategies ORDER BY strategy_id"
        )
        out: List[Dict[str, Any]] = []
        for (spec_json,) in cur.fetchall():
            try:
                out.append(json.loads(spec_json))
            except json.JSONDecodeError:
                continue
        return out

    def strategy_count(self) -> int:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM discovered_strategies")
        return int(cur.fetchone()[0])

    def blob_summary(self) -> Dict[str, Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT name, format, size_bytes, updated_at FROM artifact_blobs"
        )
        return {
            row[0]: {
                "format": row[1],
                "size_bytes": row[2],
                "updated_at": row[3],
            }
            for row in cur.fetchall()
        }

    @staticmethod
    def _load_json_strategies(path: Path) -> List[Dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            return list(data.get("strategies") or [])
        except Exception as e:
            logger.warning(f"DbArtifactStore: failed to read {path}: {e}")
            return []

    @staticmethod
    def _merge_strategies(
        existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {str(s.get("id")): s for s in existing if s.get("id")}
        for spec in incoming:
            sid = str(spec.get("id") or "")
            if not sid:
                continue
            old = by_id.get(sid)
            if old is None:
                by_id[sid] = spec
                continue
            old_sharpe = (old.get("metrics") or {}).get("sharpe_ratio", -1)
            new_sharpe = (spec.get("metrics") or {}).get("sharpe_ratio", -1)
            if new_sharpe >= old_sharpe:
                by_id[sid] = spec
        return sorted(by_id.values(), key=lambda s: str(s.get("id") or ""))

    def migrate_legacy_files(self, workspace: Optional[str] = None) -> Dict[str, str]:
        """将旧 .pkl / .json 文件一次性迁入数据库（幂等）。"""
        results: Dict[str, str] = {}
        ws = Path(workspace or Path(self.db_path).parent.parent).resolve()
        data_dir = ws / "data"
        models_dir = data_dir / "models"

        strategy_paths = [
            ws / "discovered_strategies.json",
            models_dir / "strategy_library.json",
            data_dir / "strategy_library.json",
        ]
        if self.strategy_count() == 0:
            merged: List[Dict[str, Any]] = []
            for p in strategy_paths:
                specs = self._load_json_strategies(p)
                if specs:
                    merged = self._merge_strategies(merged, specs)
                    results[f"migrate_strategies_from_{p.name}"] = str(len(specs))
            if merged:
                self.save_strategies(merged)
        else:
            results["migrate_strategies"] = "skipped_db_has_data"

        blob_sources = [
            (DEEP_LEARNING_BLOB, [
                models_dir / "deep_learning_model.pkl",
                ws / "deep_learning_model.pkl",
                data_dir / "deep_learning_model.pkl",
            ]),
            (PATTERN_RECOGNIZER_BLOB, [
                models_dir / "pattern_recognizer.pkl",
                ws / "pattern_recognizer.pkl",
            ]),
        ]
        for blob_name, paths in blob_sources:
            if self.has_blob(blob_name):
                results[f"migrate_{blob_name}"] = "skipped_db_has_data"
                continue
            imported = False
            for p in paths:
                if not p.is_file():
                    continue
                try:
                    data = p.read_bytes()
                    if data:
                        self.save_blob(blob_name, data, "pickle")
                        results[f"migrate_{blob_name}"] = f"from_{p.name}"
                        imported = True
                        break
                except Exception as e:
                    logger.warning(f"DbArtifactStore: migrate {p} failed: {e}")
            if not imported:
                results[f"migrate_{blob_name}"] = "not_found"

        return results


def get_artifact_store(db_path: Optional[str] = None) -> DbArtifactStore:
    if db_path is None:
        from bnb_quant_tool.data_localization import get_localized_db_path
        db_path = str(get_localized_db_path("ai_learning"))
    store = DbArtifactStore(db_path)
    store.migrate_legacy_files()
    return store
