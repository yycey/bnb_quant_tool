"""交易员独立学习记忆 — 每人独立准确率 / 笔记 / 偏好。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TraderMemoryStore:
    """SQLite 存储每位交易员的历史观点与准确率。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        from bnb_quant_tool.sqlite_util import connect_writer
        return connect_writer(str(self.db_path), timeout=60.0, row_factory=True)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS trader_votes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trader_id TEXT NOT NULL,
                        record_id INTEGER,
                        action TEXT NOT NULL,
                        confidence REAL,
                        score REAL,
                        summary TEXT,
                        source TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS trader_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trader_id TEXT NOT NULL,
                        vote_id INTEGER,
                        correct INTEGER,
                        pnl REAL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS trader_notes (
                        trader_id TEXT PRIMARY KEY,
                        lessons TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_tv_trader
                        ON trader_votes(trader_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_to_trader
                        ON trader_outcomes(trader_id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def record_vote(
        self,
        trader_id: str,
        action: str,
        confidence: float,
        score: float,
        summary: str,
        *,
        record_id: Optional[int] = None,
        source: str = "llm",
    ) -> int:
        from bnb_quant_tool.sqlite_util import begin_immediate, run_db

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = (
            trader_id,
            record_id,
            action,
            float(confidence),
            float(score),
            (summary or "")[:500],
            source,
            now,
        )

        def _op():
            with self._lock:
                conn = self._connect()
                try:
                    begin_immediate(conn)
                    cur = conn.execute(
                        """
                        INSERT INTO trader_votes
                        (trader_id, record_id, action, confidence, score, summary, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        payload,
                    )
                    conn.commit()
                    return int(cur.lastrowid)
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()

        return int(run_db(_op, label=f"trader_vote:{trader_id}"))

    def attach_record_to_recent_votes(
        self,
        record_id: int,
        *,
        within_minutes: int = 10,
        max_votes: int = 12,
        created_after_iso: Optional[str] = None,
    ) -> int:
        """把本轮议会投票挂到分析记录（防串票：只绑最近一批、有上限）。"""
        if not record_id:
            return 0
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = now_ts - max(1, within_minutes) * 60
        after_ts = None
        if created_after_iso:
            try:
                raw = str(created_after_iso).replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                after_ts = dt.timestamp() - 5
            except Exception:
                after_ts = None

        def _parse_ts(created: str) -> Optional[float]:
            try:
                raw = created.replace("Z", "+00:00")
                if "+" in raw[10:] or raw.endswith("+00:00"):
                    dt = datetime.fromisoformat(raw)
                else:
                    dt = datetime.strptime(
                        created[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                return None

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, created_at FROM trader_votes
                    WHERE record_id IS NULL
                    ORDER BY id DESC LIMIT 64
                    """
                ).fetchall()
                timed = []
                for r in rows:
                    ts = _parse_ts(str(r["created_at"] or ""))
                    if ts is None or ts < cutoff:
                        continue
                    if after_ts is not None and ts < after_ts:
                        continue
                    timed.append((int(r["id"]), ts))
                if not timed:
                    return 0
                newest_ts = timed[0][1]
                batch = [(i, t) for i, t in timed if newest_ts - t <= 180]
                ids = [i for i, _ in batch[: max(1, int(max_votes))]]
                if not ids:
                    return 0
                conn.executemany(
                    "UPDATE trader_votes SET record_id=? WHERE id=? AND record_id IS NULL",
                    [(int(record_id), i) for i in ids],
                )
                conn.commit()
                return len(ids)
            finally:
                conn.close()

    def record_outcome(
        self,
        trader_id: str,
        *,
        correct: bool,
        pnl: float = 0.0,
        vote_id: Optional[int] = None,
        note: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO trader_outcomes
                    (trader_id, vote_id, correct, pnl, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (trader_id, vote_id, 1 if correct else 0, float(pnl), note[:300], now),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _id_variants(trader_id: str) -> List[str]:
        """兼容 persona / persona__provider 两种 ID，保证胜负能作用到投票权重。"""
        tid = (trader_id or "").strip()
        if not tid:
            return []
        out = [tid]
        if "__" in tid:
            base = tid.split("__", 1)[0].strip()
            if base and base not in out:
                out.append(base)
        else:
            # 基座 ID 也匹配历史上带后缀的 outcomes / notes
            out.append(f"{tid}__%")
        return out

    def get_accuracy(self, trader_id: str, limit: int = 50) -> Dict[str, Any]:
        variants = self._id_variants(trader_id)
        if not variants:
            return {"total": 0, "correct": 0, "accuracy": 0.5, "weight": 1.0}

        with self._lock:
            conn = self._connect()
            try:
                # 精确 ID + 基座 ID（同 paper# 去重，避免历史双写膨胀）
                exact = [v for v in variants if not v.endswith("%")]
                if exact:
                    placeholders = ",".join("?" for _ in exact)
                    raw = conn.execute(
                        f"""
                        SELECT correct, note FROM trader_outcomes
                        WHERE trader_id IN ({placeholders})
                        ORDER BY id DESC LIMIT ?
                        """,
                        (*exact, max(limit * 3, 50)),
                    ).fetchall()
                else:
                    raw = []
                # 若无精确样本，用前缀模糊（基座 → 各 provider）
                if not raw and "__" not in (trader_id or ""):
                    raw = conn.execute(
                        """
                        SELECT correct, note FROM trader_outcomes
                        WHERE trader_id=? OR trader_id LIKE ?
                        ORDER BY id DESC LIMIT ?
                        """,
                        (trader_id, f"{trader_id}__%", max(limit * 3, 50)),
                    ).fetchall()
            finally:
                conn.close()

        seen_papers = set()
        rows = []
        for r in raw:
            note = str(r["note"] or "")
            paper_key = None
            if "paper#" in note:
                paper_key = note.split("paper#", 1)[-1].split()[0]
            if paper_key:
                if paper_key in seen_papers:
                    continue
                seen_papers.add(paper_key)
            rows.append(r)
            if len(rows) >= limit:
                break

        total = len(rows)
        if total == 0:
            return {"total": 0, "correct": 0, "accuracy": 0.5, "weight": 1.0}
        correct = sum(int(r["correct"]) for r in rows)
        acc = correct / total
        # ≥3 笔就开始拉开权重，让成长更快反馈到投票
        if total < 3:
            weight = 1.0
        else:
            weight = max(0.45, min(1.6, 0.45 + acc * 1.1))
        return {
            "total": total,
            "correct": correct,
            "accuracy": round(acc, 4),
            "weight": round(weight, 4),
        }

    def get_all_weights(self, trader_ids: List[str]) -> Dict[str, float]:
        return {tid: float(self.get_accuracy(tid)["weight"]) for tid in trader_ids}

    def get_lessons(self, trader_id: str, max_chars: int = 600) -> str:
        variants = [v for v in self._id_variants(trader_id) if not v.endswith("%")]
        with self._lock:
            conn = self._connect()
            try:
                texts: List[str] = []
                for tid in variants:
                    row = conn.execute(
                        "SELECT lessons FROM trader_notes WHERE trader_id=?",
                        (tid,),
                    ).fetchone()
                    if row and row["lessons"]:
                        texts.append(str(row["lessons"]))
                if not texts and "__" not in (trader_id or ""):
                    rows = conn.execute(
                        """
                        SELECT lessons FROM trader_notes
                        WHERE trader_id=? OR trader_id LIKE ?
                        """,
                        (trader_id, f"{trader_id}__%"),
                    ).fetchall()
                    texts = [str(r["lessons"]) for r in rows if r["lessons"]]
            finally:
                conn.close()
        if not texts:
            return ""
        text = "\n".join(texts)
        return text[:max_chars]

    def update_lessons(self, trader_id: str, lessons: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO trader_notes (trader_id, lessons, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(trader_id) DO UPDATE SET
                        lessons=excluded.lessons,
                        updated_at=excluded.updated_at
                    """,
                    (trader_id, (lessons or "")[:4000], now),
                )
                conn.commit()
            finally:
                conn.close()

    def append_lesson(self, trader_id: str, note: str) -> None:
        existing = self.get_lessons(trader_id, max_chars=3500)
        merged = (existing + "\n- " + note.strip()).strip() if existing else ("- " + note.strip())
        self.update_lessons(trader_id, merged[-3500:])

    def recent_votes(self, trader_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT action, confidence, summary, created_at, source
                    FROM trader_votes WHERE trader_id=?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (trader_id, limit),
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def dashboard(self, trader_ids: List[str]) -> Dict[str, Any]:
        out = {}
        for tid in trader_ids:
            acc = self.get_accuracy(tid)
            out[tid] = {
                **acc,
                "lessons_preview": self.get_lessons(tid, max_chars=120),
                "recent": self.recent_votes(tid, 3),
            }
        return out
