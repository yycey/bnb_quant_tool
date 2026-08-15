"""
知识记忆 (Capability Memory) v2
================================
1. 能力提炼：AI 将交易/复盘提炼为结构化知识卡片（非原始聊天）
2. 本地存储：SQLite 结构化字段 + ChromaDB/TF-IDF 向量语义索引
3. 检索调用：按当前市场局面语义检索，注入 Prompt Context
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from bnb_quant_tool.knowledge_extractor import (
    CATEGORY_LABELS,
    KnowledgeExtractor,
    build_analysis_context,
    build_market_query_text,
    build_trade_context,
)
from bnb_quant_tool.knowledge_vector_store import KnowledgeVectorStore

logger = logging.getLogger(__name__)


class CapabilityMemory:
    """结构化知识卡片 + 向量语义检索。"""

    def __init__(self, db_path: str, config: Optional[Dict] = None):
        self.db_path = db_path
        self.config = config or {}
        mem_cfg = self.config.get("capability_memory") or {}
        self.enabled = mem_cfg.get("enabled", True)
        self.require_on_analysis = bool(mem_cfg.get("require_on_analysis", True))
        self.max_cards_in_prompt = int(mem_cfg.get("max_cards_in_prompt", 8))
        self.min_confidence = float(mem_cfg.get("min_confidence", 0.3))
        self.min_similarity = float(mem_cfg.get("min_similarity", 0.15))
        self.use_ai_extract = mem_cfg.get("use_ai_extract", True)
        self.vector_backend = mem_cfg.get("vector_backend", "auto")
        gov = self.config.get("memory_governance") or {}
        self._hot_cache_days = float(gov.get("hot_cache_days", 7) or 7)
        self._half_life_days = float(gov.get("half_life_days", 30) or 30)
        # 热层：近期高权重卡 id → (card_dict, expires_ts)
        self._hot_cards: Dict[int, Any] = {}
        self._hot_lock = threading.Lock()
        self._local = threading.local()
        self._vector_store: Optional[KnowledgeVectorStore] = None
        self._extractor: Optional[KnowledgeExtractor] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self.db_path, timeout=60.0, row_factory=True)
        return self._local.conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接，下次读取重新打开（跨线程写入后刷新用）。"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def checkpoint_wal(self, mode: str = "PASSIVE") -> None:
        """批量写入后刷盘。默认 PASSIVE，避免交易中 TRUNCATE 强锁阻塞平仓。"""
        try:
            conn = self._get_conn()
            conn.execute(f"PRAGMA wal_checkpoint({mode})")
            if conn.isolation_level is not None:
                conn.commit()
        except Exception as e:
            logger.debug("knowledge WAL checkpoint: %s", e)

    def verify_persisted_count(self) -> int:
        """用独立连接读取库内有效卡片数（跨线程写入后校验用）。"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            row = conn.execute(
                "SELECT COUNT(*) FROM knowledge_cards WHERE is_active=1"
            ).fetchone()
            conn.close()
            return int(row[0] or 0)
        except Exception as e:
            logger.debug("verify_persisted_count failed: %s", e)
            return -1

    def _vector_dir(self) -> str:
        base = Path(self.db_path).parent
        return str(base / "chroma_knowledge")

    @property
    def vector_store(self) -> KnowledgeVectorStore:
        if self._vector_store is None:
            self._vector_store = KnowledgeVectorStore(
                persist_dir=self._vector_dir(),
                sqlite_path=self.db_path,
                backend=self.vector_backend,
            )
        return self._vector_store

    @property
    def extractor(self) -> KnowledgeExtractor:
        if self._extractor is None:
            from bnb_quant_tool.llm_provider import get_llm_credentials
            llm = get_llm_credentials(self.config)
            self._extractor = KnowledgeExtractor(
                api_key=llm["api_key"],
                model=llm["model"],
                base_url=llm["base_url"],
            )
        return self._extractor

    def _init_db(self) -> None:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                trigger_condition TEXT,
                action_rule TEXT,
                lesson TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                symbol TEXT,
                times_validated INTEGER DEFAULT 0,
                times_contradicted INTEGER DEFAULT 0,
                record_id INTEGER,
                trade_id INTEGER,
                tags TEXT,
                dedupe_key TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_kc_dedupe ON knowledge_cards(dedupe_key)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_kc_active ON knowledge_cards(is_active, confidence)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_kc_category ON knowledge_cards(category)"
        )
        # 复用召回次数 ≠ 交易验证次数
        try:
            cur.execute("ALTER TABLE knowledge_cards ADD COLUMN times_recalled INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_backfill_status (
                trade_id INTEGER PRIMARY KEY,
                processed_at TEXT NOT NULL,
                mode TEXT,
                cards_saved INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS injected_knowledge_log (
                record_id INTEGER NOT NULL,
                card_id INTEGER NOT NULL,
                injected_at TEXT NOT NULL,
                PRIMARY KEY (record_id, card_id)
            )
        """)
        conn.commit()
        self._migrate_legacy_cards(cur)
        conn.commit()

    def _migrate_legacy_cards(self, cur) -> None:
        """将旧 capability_cards 表数据迁移到 knowledge_cards。"""
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_cards'"
        )
        if not cur.fetchone():
            return
        cur.execute("SELECT COUNT(*) FROM knowledge_cards")
        if cur.fetchone()[0] > 0:
            return
        cur.execute("""
            SELECT timestamp, source, condition, lesson, confidence,
                   times_validated, record_id, trade_id, tags, dedupe_key
            FROM capability_cards WHERE is_active=1
        """)
        for row in cur.fetchall():
            cat = "error_lesson" if row["source"] == "trade" else "market_review"
            cur.execute(
                "INSERT INTO knowledge_cards "
                "(timestamp, source, category, title, trigger_condition, action_rule, "
                "lesson, confidence, times_validated, record_id, trade_id, tags, dedupe_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["timestamp"], row["source"], cat,
                    (row["lesson"] or "")[:80],
                    row["condition"], "",
                    row["lesson"], row["confidence"], row["times_validated"],
                    row["record_id"], row["trade_id"], row["tags"], row["dedupe_key"],
                ),
            )
        logger.info("Migrated legacy capability_cards → knowledge_cards")

    @staticmethod
    def _dedupe_key(card: Dict[str, Any]) -> str:
        # 局面键优先：相同市场结构反复出现时强化同一张卡，而不是每轮新建
        sit = str(card.get("situation_key") or "").strip()
        if not sit:
            trigger = str(card.get("trigger_condition") or "")
            for part in trigger.replace("|", " ").split():
                if part.startswith("situation_key="):
                    sit = part.split("=", 1)[-1].strip()
                    break
            if not sit:
                tags = card.get("tags") or []
                if isinstance(tags, list):
                    for t in tags:
                        ts = str(t)
                        if "|" in ts and ("RSI" in ts.upper() or "rsi" in ts or "BNBUSDT" in ts.upper()):
                            sit = ts
                            break
        if sit:
            raw = "|".join([
                "sit",
                card.get("category", ""),
                sit,
                str((card.get("tags") or [""])[0] if isinstance(card.get("tags"), list) else ""),
            ]).lower().strip()
            return hashlib.md5(raw.encode("utf-8")).hexdigest()
        raw = "|".join([
            card.get("category", ""),
            card.get("title", ""),
            card.get("trigger_condition", ""),
            # 不含 lesson：避免 record_id 导致每轮分析都“看起来不同”
            str(card.get("action_rule") or "")[:120],
        ]).lower().strip()
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def save_knowledge_card(
        self,
        card: Dict[str, Any],
        source: str,
        symbol: Optional[str] = None,
        record_id: Optional[int] = None,
        trade_id: Optional[int] = None,
    ) -> Optional[int]:
        """保存结构化知识卡片并写入向量索引。"""
        if not self.enabled:
            return None
        lesson = (card.get("lesson") or "").strip()
        title = (card.get("title") or "").strip()
        if not lesson and not title:
            return None

        # 保证 situation_key 写入 trigger/tags，复用与去重才能稳定命中
        sit = str(card.get("situation_key") or "").strip()
        if not sit:
            trigger0 = str(card.get("trigger_condition") or "")
            for part in trigger0.replace("|", " ").split():
                if part.startswith("situation_key="):
                    sit = part.split("=", 1)[-1].strip()
                    break
        if sit:
            card = dict(card)
            card["situation_key"] = sit
            trigger = str(card.get("trigger_condition") or "")
            if "situation_key=" not in trigger:
                card["trigger_condition"] = (
                    f"situation_key={sit} | {trigger}".strip(" |")
                )[:500]
            tags = list(card.get("tags") or []) if isinstance(card.get("tags"), list) else []
            if sit not in tags:
                tags.append(sit)
            if "analysis_snapshot" not in tags:
                tags.append("analysis_snapshot")
            card["tags"] = tags

        category = card.get("category") or "error_lesson"
        confidence = max(0.1, min(1.0, float(card.get("confidence") or 0.5)))
        dedupe = self._dedupe_key(card)
        tags_json = json.dumps(card.get("tags") or [], ensure_ascii=False)
        now = datetime.now().isoformat()

        full_card = {
            **card,
            "category": category,
            "title": title or lesson[:40],
            "confidence": confidence,
            "source": source,
            "symbol": symbol,
        }

        for attempt in range(5):
            try:
                conn = self._get_conn()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, times_validated, confidence FROM knowledge_cards "
                    "WHERE dedupe_key=? AND is_active=1 ORDER BY id DESC LIMIT 1",
                    (dedupe,),
                )
                existing = cur.fetchone()
                if existing:
                    if source == "reuse":
                        # 复用只计召回，不抬 times_validated / 置信度（防虚增）
                        try:
                            cur.execute(
                                "UPDATE knowledge_cards SET "
                                "times_recalled=COALESCE(times_recalled,0)+1, timestamp=? "
                                "WHERE id=?",
                                (now, existing["id"]),
                            )
                        except sqlite3.OperationalError:
                            cur.execute(
                                "UPDATE knowledge_cards SET timestamp=? WHERE id=?",
                                (now, existing["id"]),
                            )
                        conn.commit()
                        card_id = int(existing["id"])
                        full_card["confidence"] = float(existing["confidence"] or confidence)
                        self.vector_store.upsert(card_id, full_card)
                        return card_id
                    new_conf = min(
                        1.0,
                        float(existing["confidence"]) * 0.7 + confidence * 0.3 + 0.02,
                    )
                    cur.execute(
                        "UPDATE knowledge_cards SET "
                        "times_validated=times_validated+1, confidence=?, timestamp=? "
                        "WHERE id=?",
                        (new_conf, now, existing["id"]),
                    )
                    conn.commit()
                    card_id = int(existing["id"])
                    full_card["confidence"] = new_conf
                    self.vector_store.upsert(card_id, full_card)
                    return card_id

                cur.execute(
                    "INSERT INTO knowledge_cards "
                    "(timestamp, source, category, title, trigger_condition, action_rule, "
                    "lesson, confidence, symbol, times_validated, record_id, trade_id, "
                    "tags, dedupe_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                    (
                        now, source, category,
                        full_card["title"][:200],
                        (card.get("trigger_condition") or "")[:500],
                        (card.get("action_rule") or "")[:500],
                        lesson[:1000],
                        confidence,
                        symbol,
                        record_id, trade_id, tags_json, dedupe,
                    ),
                )
                conn.commit()
                card_id = cur.lastrowid
                self.vector_store.upsert(card_id, full_card)
                logger.info(
                    f"KnowledgeCard saved #{card_id} [{category}] {full_card['title'][:30]}"
                )
                return card_id
            except sqlite3.OperationalError as e:
                try:
                    conn = getattr(self._local, "conn", None)
                    if conn is not None:
                        conn.rollback()
                except Exception:
                    pass
                self.reset_connection()
                if ("locked" in str(e).lower() or "busy" in str(e).lower()) and attempt < 4:
                    time.sleep(0.15 * (2 ** attempt))
                else:
                    logger.warning(f"save_knowledge_card failed: {e}")
                    return None
        return None

    def _row_to_card(self, row: sqlite3.Row, similarity: float = 0.0) -> Dict[str, Any]:
        tags = []
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "category": row["category"],
            "category_label": CATEGORY_LABELS.get(row["category"], row["category"]),
            "title": row["title"],
            "trigger_condition": row["trigger_condition"],
            "action_rule": row["action_rule"],
            "lesson": row["lesson"],
            "confidence": round(float(row["confidence"] or 0), 3),
            "symbol": row["symbol"],
            "times_validated": int(row["times_validated"] or 0),
            "times_contradicted": int(row["times_contradicted"] or 0),
            "times_recalled": int(row["times_recalled"] or 0) if "times_recalled" in row.keys() else 0,
            "tags": tags,
            "similarity": round(similarity, 4) if similarity else None,
            # 兼容旧字段名
            "condition": row["trigger_condition"],
        }

    def _card_rank_score(self, card: Dict[str, Any]) -> float:
        """检索排序：相关度 × 可信度 × 验证加权 × 时间衰减 × 热层加成。"""
        sim = float(card.get("similarity") or 0.55)
        conf = float(card.get("confidence") or 0.5)
        validated = int(card.get("times_validated") or 0)
        contradicted = int(card.get("times_contradicted") or 0)
        validation_factor = 1.0 + 0.15 * validated - 0.35 * contradicted
        decay = 1.0
        hot = 1.0
        try:
            from bnb_quant_tool.memory_governance import time_decay_weight, _parse_ts

            half = float(getattr(self, "_half_life_days", 30) or 30)
            ts_raw = card.get("updated_at") or card.get("created_at")
            decay = time_decay_weight(ts_raw, half_life_days=half)
            ts = _parse_ts(ts_raw)
            if ts is not None:
                age_days = max(0.0, (time.time() - ts) / 86400.0)
                hot_days = float(getattr(self, "_hot_cache_days", 7) or 7)
                if age_days <= hot_days:
                    hot = 1.15
        except Exception:
            decay = 1.0
        # 反记忆卡优先
        anti_boost = 1.25 if "anti_memory" in (card.get("tags") or []) or (
            card.get("category") == "anti_memory"
        ) else 1.0
        return max(0.01, sim * conf * max(0.2, validation_factor) * decay * hot * anti_boost)

    def _hot_put(self, cards: List[Dict[str, Any]]) -> None:
        """写入热层缓存（近 N 天高置信卡片）。"""
        if not cards:
            return
        ttl = float(getattr(self, "_hot_cache_days", 7) or 7) * 86400.0
        expires = time.time() + ttl
        with self._hot_lock:
            for c in cards:
                try:
                    cid = int(c.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cid <= 0:
                    continue
                if float(c.get("confidence") or 0) < 0.55:
                    continue
                self._hot_cards[cid] = (dict(c), expires)
            # 限制热层大小
            if len(self._hot_cards) > 64:
                items = sorted(
                    self._hot_cards.items(),
                    key=lambda kv: float((kv[1][0] or {}).get("confidence") or 0),
                    reverse=True,
                )
                self._hot_cards = dict(items[:48])

    def _hot_merge(self, cards: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        """热层优先并入检索结果。"""
        now = time.time()
        hot_list: List[Dict[str, Any]] = []
        with self._hot_lock:
            dead = [cid for cid, (_, exp) in self._hot_cards.items() if exp < now]
            for cid in dead:
                self._hot_cards.pop(cid, None)
            for cid, (card, _) in self._hot_cards.items():
                hot_list.append(dict(card))
        if not hot_list:
            return cards
        seen = {int(c.get("id") or 0) for c in cards}
        merged = list(cards)
        for h in hot_list:
            hid = int(h.get("id") or 0)
            if hid and hid not in seen:
                h.setdefault("similarity", 0.62)
                merged.append(h)
                seen.add(hid)
        merged.sort(key=self._card_rank_score, reverse=True)
        return merged[: max(top_k, len(cards))]

    def record_injected_cards(self, record_id: int, card_ids: List[int]) -> int:
        """记录本次分析注入 Prompt 的知识卡片，供平仓后验证。"""
        if not self.enabled or not record_id or not card_ids:
            return 0
        now = datetime.now().isoformat()
        saved = 0
        conn = self._get_conn()
        cur = conn.cursor()
        for cid in card_ids:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO injected_knowledge_log "
                    "(record_id, card_id, injected_at) VALUES (?,?,?)",
                    (int(record_id), int(cid), now),
                )
                saved += 1
            except Exception:
                pass
        conn.commit()
        return saved

    def _adjust_card_outcome(self, card_id: int, positive: bool) -> None:
        """根据交易结果验证或证伪单张卡片。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT confidence, times_validated, times_contradicted "
            "FROM knowledge_cards WHERE id=? AND is_active=1",
            (int(card_id),),
        )
        row = cur.fetchone()
        if not row:
            return
        conf = float(row["confidence"] or 0.5)
        now = datetime.now().isoformat()
        if positive:
            new_conf = min(1.0, conf * 0.85 + 0.15)
            cur.execute(
                "UPDATE knowledge_cards SET "
                "times_validated=times_validated+1, confidence=?, timestamp=? "
                "WHERE id=?",
                (new_conf, now, int(card_id)),
            )
        else:
            contradicted = int(row["times_contradicted"] or 0) + 1
            new_conf = max(0.1, conf * 0.75 - 0.05)
            deactivate = 1 if contradicted >= 2 and new_conf < 0.35 else 0
            cur.execute(
                "UPDATE knowledge_cards SET "
                "times_contradicted=times_contradicted+1, confidence=?, "
                "is_active=CASE WHEN ? THEN 0 ELSE is_active END, timestamp=? "
                "WHERE id=?",
                (new_conf, deactivate, now, int(card_id)),
            )
            if deactivate:
                logger.info("KnowledgeCard #%s retired (contradicted %d)", card_id, contradicted)
        conn.commit()

    def validate_cards_for_feedback(
        self,
        record_id: int,
        outcome: str,
        quality: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """平仓后验证当时注入的知识卡片。"""
        if not self.enabled or not record_id:
            return {"validated": 0, "contradicted": 0}
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT card_id FROM injected_knowledge_log WHERE record_id=?",
            (int(record_id),),
        )
        card_ids = [int(r[0]) for r in cur.fetchall()]
        if not card_ids:
            return {"validated": 0, "contradicted": 0}

        tier = (quality or {}).get("tier", "C")
        positive = (
            outcome == "WIN" and tier in ("A", "B")
        ) or (
            outcome == "BREAK_EVEN" and tier in ("A", "B", "C")
        )
        negative = outcome == "LOSS" or tier == "D"

        validated = 0
        contradicted = 0
        for cid in card_ids:
            if positive and not negative:
                self._adjust_card_outcome(cid, True)
                validated += 1
            elif negative:
                self._adjust_card_outcome(cid, False)
                contradicted += 1
        return {"validated": validated, "contradicted": contradicted}

    def save_counterfactual_lesson(
        self,
        cf_result: Dict[str, Any],
        trade_row: Dict[str, Any],
        analysis_record: Optional[Dict[str, Any]] = None,
    ) -> int:
        """反事实分析结果写入知识库。"""
        if not self.enabled or not cf_result:
            return 0
        best = str(cf_result.get("best_scenario") or "")
        if best in ("ACTUAL", "UNKNOWN", ""):
            return 0

        side = str(trade_row.get("side") or "LONG").upper()
        regime = ""
        if analysis_record:
            try:
                ind = analysis_record.get("indicators")
                if isinstance(ind, str):
                    ind = json.loads(ind)
            except (json.JSONDecodeError, TypeError):
                ind = {}
            regime = str((ind or {}).get("regime") or "")

        label_map = {
            "NO_TRADE": "不交易更优",
            "REVERSE": "反向更优",
            "LATE_ENTRY": "晚进场更优",
        }
        title = f"反事实: {label_map.get(best, best)}"
        trigger = (
            f"方向={side} | 决策评分={cf_result.get('decision_score', 0):.0f} | "
            f"实际PnL={cf_result.get('actual_pnl', 0):+.2f}"
        )
        if regime:
            trigger = f"市场状态={regime} | {trigger}"

        action_rule = {
            "NO_TRADE": "相似局面优先 WAIT，避免过度交易",
            "REVERSE": "检查方向判断与多周期是否矛盾",
            "LATE_ENTRY": "等待确认后再入场，避免过早进场",
        }.get(best, "复盘此类决策模式")

        lesson = cf_result.get("text") or (
            f"反事实最优={best}，实际决策非最优，应调整入场纪律"
        )
        card = {
            "category": "error_lesson",
            "title": title[:200],
            "trigger_condition": trigger[:500],
            "action_rule": action_rule[:500],
            "lesson": str(lesson)[:1000],
            "confidence": 0.62,
            "tags": ["counterfactual", best, side],
        }
        saved = 1 if self.save_knowledge_card(
            card,
            source="counterfactual",
            symbol=trade_row.get("symbol"),
            trade_id=int(trade_row.get("id") or 0) or None,
        ) else 0
        return saved

    def extract_post_trade_reflection(
        self,
        trade_row: Dict[str, Any],
        analysis_record: Optional[Dict[str, Any]] = None,
        outcome: str = "",
        quality: Optional[Dict[str, Any]] = None,
    ) -> int:
        """平仓后 AI 结构化反思（独立 Prompt，比通用提炼更聚焦）。"""
        if not self.enabled:
            return 0
        from bnb_quant_tool.knowledge_extractor import build_trade_context

        ctx = build_trade_context(trade_row, analysis_record, outcome, quality)
        cards: List[Dict[str, Any]] = []
        if self.use_ai_extract and self.extractor.available:
            cards = self.extractor.extract_post_trade_reflection(ctx)
        if not cards:
            return self.extract_and_save_from_trade(
                trade_row, analysis_record, outcome, quality
            )

        saved = 0
        symbol = ctx.get("symbol", "BNBUSDT")
        record_id = trade_row.get("learning_record_id")
        trade_id = trade_row.get("id")
        for card in cards:
            if self.save_knowledge_card(
                card,
                source="reflection",
                symbol=symbol,
                record_id=int(record_id) if record_id else None,
                trade_id=int(trade_id) if trade_id else None,
            ):
                saved += 1
        if saved:
            logger.info("KnowledgeMemory: +%d cards from post-trade reflection", saved)
        return saved

    def consolidate_knowledge_cards(self, max_merge: int = 5) -> Dict[str, Any]:
        """周期元学习：合并冗余卡片，提炼元规则。"""
        if not self.enabled:
            return {"merged_count": 0}
        before = self.count_active_cards()
        if before < 10:
            return {"cards_before": before, "cards_after": before, "merged_count": 0}

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM knowledge_cards WHERE is_active=1 "
            "ORDER BY confidence DESC LIMIT 60"
        )
        rows = [self._row_to_card(r) for r in cur.fetchall()]
        merged_cards: List[Dict[str, Any]] = []
        if self.use_ai_extract and self.extractor.available:
            merged_cards = self.extractor.consolidate_cards(rows, max_rules=max_merge)

        merged_count = 0
        for card in merged_cards:
            if self.save_knowledge_card(card, source="meta_learning"):
                merged_count += 1

        # 退役低可信度且长期未验证的卡片
        cur.execute(
            "UPDATE knowledge_cards SET is_active=0 "
            "WHERE is_active=1 AND confidence < 0.25 "
            "AND times_validated=0 AND times_contradicted >= 1"
        )
        conn.commit()
        after = self.count_active_cards()
        return {
            "cards_before": before,
            "cards_after": after,
            "merged_count": merged_count,
        }

    def retrieve_for_analysis(
        self,
        market_context: Dict[str, Any],
        top_k: Optional[int] = None,
    ) -> tuple[List[Dict[str, Any]], str]:
        """分析流水线专用：必定检索知识库；语义无命中时多级回退。"""
        if not self.enabled:
            return [], "disabled"
        top_k = top_k or self.max_cards_in_prompt
        cards = self.retrieve_relevant(market_context, top_k=top_k)
        if cards:
            return cards, "semantic"

        relaxed_min = max(0.25, self.min_confidence * 0.75)
        if relaxed_min < self.min_confidence:
            old_min = self.min_confidence
            try:
                self.min_confidence = relaxed_min
                cards = self.retrieve_relevant(market_context, top_k=top_k)
                if cards:
                    return cards, "semantic_relaxed"
            finally:
                self.min_confidence = old_min

        cards = self.get_recent_cards(limit=top_k)
        if cards:
            return cards, "recent"

        cards = self._get_any_active_cards(limit=top_k)
        if cards:
            return cards, "fallback_any"
        return [], "empty"

    def retrieve_relevant(
        self,
        market_context: Dict[str, Any],
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """语义检索与当前局面最相关的知识卡片。"""
        if not self.enabled:
            return []
        top_k = top_k or self.max_cards_in_prompt
        query = build_market_query_text(market_context)
        hits = self.vector_store.search(
            query, top_k=max(top_k * 2, 12), min_similarity=self.min_similarity
        )
        if not hits:
            return self.get_recent_cards(limit=top_k)

        conn = self._get_conn()
        cur = conn.cursor()
        cards = []
        for hit in hits:
            cur.execute(
                "SELECT * FROM knowledge_cards WHERE id=? AND is_active=1 "
                "AND confidence >= ?",
                (hit["card_id"], self.min_confidence),
            )
            row = cur.fetchone()
            if row:
                cards.append(self._row_to_card(row, similarity=hit["similarity"]))

        if len(cards) < top_k:
            seen = {c["id"] for c in cards}
            for rc in self.get_recent_cards(limit=top_k):
                if rc["id"] not in seen:
                    cards.append(rc)
                    seen.add(rc["id"])
                if len(cards) >= top_k:
                    break

        cards.sort(key=self._card_rank_score, reverse=True)
        cards = self.prefilter_cards_for_context(cards, market_context)
        cards = self._hot_merge(cards, top_k)
        self._hot_put(cards[:top_k])
        return cards[:top_k]

    def prefilter_cards_for_context(
        self,
        cards: List[Dict[str, Any]],
        market_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """事前适用性预审：证伪过多跳过，Regime 不匹配降权。"""
        if not cards:
            return []
        regime = str(
            market_context.get("regime")
            or market_context.get("market_regime")
            or ""
        ).upper()
        cfg = self.config.get("capability_memory") or {}
        use_ai = bool(cfg.get("prefilter_ai", False))

        rule_filtered: List[Dict[str, Any]] = []
        for c in cards:
            validated = int(c.get("times_validated") or 0)
            contradicted = int(c.get("times_contradicted") or 0)
            if contradicted >= 2 and contradicted > validated:
                continue
            if contradicted > validated + 1:
                continue

            card = dict(c)
            trigger = str(card.get("trigger_condition") or "")
            tags = card.get("tags") or []
            applicability = "applicable"

            if regime and regime not in ("", "GLOBAL"):
                regime_hit = (
                    regime in trigger.upper()
                    or regime in [str(t).upper() for t in tags]
                )
                if not regime_hit and validated == 0:
                    applicability = "verify"
                    card["confidence"] = float(card.get("confidence", 0.5)) * 0.82

            card["applicability"] = applicability
            rule_filtered.append(card)

        if use_ai and self.use_ai_extract and self.extractor.available:
            try:
                ai_result = self.extractor.prefilter_cards_applicability(
                    rule_filtered, market_context
                )
                if ai_result:
                    allowed_ids = {
                        int(x["id"]) for x in ai_result
                        if x.get("applicability") != "skip" and x.get("id")
                    }
                    if allowed_ids:
                        rule_filtered = [
                            c for c in rule_filtered if int(c["id"]) in allowed_ids
                        ]
            except Exception as e:
                logger.debug("AI card prefilter skipped: %s", e)

        rule_filtered.sort(
            key=lambda x: (
                0 if x.get("applicability") == "applicable" else 1,
                -self._card_rank_score(x),
            )
        )
        return rule_filtered

    def count_active_cards(self) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_cards WHERE is_active=1"
        ).fetchone()
        return int(row[0] or 0)

    def list_cards_for_ui(
        self,
        limit: int = 200,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GUI 列表：展示全部有效卡片（不做置信度过滤）。"""
        if not self.enabled:
            return []
        conn = self._get_conn()
        cur = conn.cursor()
        if category:
            cur.execute(
                "SELECT * FROM knowledge_cards WHERE is_active=1 AND category=? "
                "ORDER BY id DESC LIMIT ?",
                (category, int(limit)),
            )
        else:
            cur.execute(
                "SELECT * FROM knowledge_cards WHERE is_active=1 "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        return [self._row_to_card(r) for r in cur.fetchall()]

    def get_recent_cards(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """无检索上下文时，返回最近高可信度卡片。"""
        if not self.enabled:
            return []
        limit = limit or self.max_cards_in_prompt
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM knowledge_cards WHERE is_active=1 AND confidence >= ? "
            "ORDER BY confidence DESC, times_validated DESC, id DESC LIMIT ?",
            (self.min_confidence, int(limit)),
        )
        return [self._row_to_card(r) for r in cur.fetchall()]

    def _get_any_active_cards(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """回退：忽略置信度门槛，取库内有效卡片。"""
        if not self.enabled:
            return []
        limit = limit or self.max_cards_in_prompt
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM knowledge_cards WHERE is_active=1 "
            "ORDER BY confidence DESC, times_validated DESC, id DESC LIMIT ?",
            (int(limit),),
        )
        return [self._row_to_card(r) for r in cur.fetchall()]

    def get_active_cards(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """兼容旧 API。"""
        return self.get_recent_cards(limit)

    @staticmethod
    def format_for_prompt(cards: List[Dict[str, Any]], retrieval_mode: str = "") -> str:
        """格式化为 Prompt Context。"""
        if not cards:
            return ""
        mode_hint = "（语义检索匹配）" if retrieval_mode == "semantic" else ""
        lines = [
            "",
            f"【本地知识库 — 结构化历史经验{mode_hint}，务必参考】",
            "=" * 52,
        ]
        for i, c in enumerate(cards, 1):
            cat = c.get("category_label") or c.get("category", "")
            sim = c.get("similarity")
            sim_str = f" | 相关度 {sim:.0%}" if sim else ""
            lines.append(f"{i}. [{cat}] {c.get('title', '')}{sim_str}")
            if c.get("trigger_condition"):
                lines.append(f"   适用条件: {c['trigger_condition']}")
            if c.get("action_rule"):
                lines.append(f"   执行规则: {c['action_rule']}")
            lines.append(f"   核心教训: {c.get('lesson', '')}")
            lines.append(
                f"   可信度: {c.get('confidence', 0):.0%} | 验证 {c.get('times_validated', 0)} 次"
            )
            lines.append("")
        lines.append("规则: 当前局面若与某条「适用条件」吻合，优先遵循其「执行规则」。")
        lines.append("=" * 52)
        return "\n".join(lines)

    def extract_and_save_from_trade(
        self,
        trade_row: Dict[str, Any],
        analysis_record: Optional[Dict[str, Any]],
        outcome: str,
        quality: Optional[Dict[str, Any]] = None,
    ) -> int:
        """AI 提炼交易经验 → 结构化知识卡片 → 向量入库。"""
        if not self.enabled:
            return 0

        ctx = build_trade_context(trade_row, analysis_record, outcome, quality)
        symbol = ctx.get("symbol", "BNBUSDT")
        record_id = trade_row.get("learning_record_id")
        trade_id = trade_row.get("id")
        saved = 0

        cards: List[Dict[str, Any]] = []
        learn_cfg = (self.config or {}).get("learning") or {}
        use_ai = bool(self.use_ai_extract) or bool(
            learn_cfg.get("use_ai_extract_on_close", False)
        )
        if use_ai and self.extractor.available:
            cards = self.extractor.extract_from_trade(ctx)

        # AI 不可用或返回空时，用规则兜底（仍结构化，非聊天记录）
        if not cards:
            cards = self._fallback_cards_from_trade(ctx, outcome, quality)

        for card in cards:
            if self.save_knowledge_card(
                card,
                source="trade",
                symbol=symbol,
                record_id=int(record_id) if record_id else None,
                trade_id=int(trade_id) if trade_id else None,
            ):
                saved += 1

        if saved:
            logger.info(f"KnowledgeMemory: +{saved} cards from trade (AI={bool(cards)})")
        return saved

    def extract_and_save_from_review(
        self,
        review_result: Dict[str, Any],
        stats: Optional[Dict[str, Any]] = None,
    ) -> int:
        """AI 提炼复盘结论 → 知识卡片。"""
        if not self.enabled or not review_result:
            return 0

        saved = 0
        cards: List[Dict[str, Any]] = []
        if self.use_ai_extract and self.extractor.available:
            cards = self.extractor.extract_from_review(review_result, stats)

        if not cards:
            cards = self._fallback_cards_from_review(review_result)

        for card in cards:
            if self.save_knowledge_card(card, source="review"):
                saved += 1

        if saved:
            logger.info(f"KnowledgeMemory: +{saved} cards from review")
        return saved

        if saved:
            logger.info(f"KnowledgeMemory: +{saved} cards from review")
        return saved

    def save_analysis_snapshot(
        self,
        result: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        """每次分析后沉淀局面快照（规则兜底，不依赖 API，重复局面会强化置信度）。"""
        if not self.enabled:
            return 0
        # 知识复用轮次已在 reinforce_on_reuse 强化过，避免同一轮 +2
        ai = result.get("ai_analysis") or {}
        if isinstance(ai, dict) and (
            ai.get("_reused") or ai.get("_provider") == "knowledge_reuse"
        ):
            logger.debug(
                "KnowledgeMemory: skip snapshot on reuse record #%s", record_id
            )
            return 0
        ctx = build_analysis_context(result, record_id)
        card = self._fallback_card_from_analysis(ctx)
        if not card:
            return 0
        saved = 1 if self.save_knowledge_card(
            card,
            source="analysis",
            symbol=ctx.get("symbol"),
            record_id=int(record_id) if record_id else None,
        ) else 0
        if saved:
            logger.info(
                "KnowledgeMemory: analysis snapshot #%s [%s]",
                record_id, card.get("title", "")[:40],
            )
        return saved

    def extract_and_save_from_analysis(
        self,
        result: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        """AI 提炼分析快照 → 知识卡片（可选，规则快照已在 save_analysis_snapshot 完成）。"""
        if not self.enabled:
            return 0
        ctx = build_analysis_context(result, record_id)
        symbol = ctx.get("symbol", "BNBUSDT")
        saved = 0
        cards: List[Dict[str, Any]] = []
        if self.use_ai_extract and self.extractor.available:
            cards = self.extractor.extract_from_analysis(ctx)
        if not cards:
            return self.save_analysis_snapshot(result, record_id)
        for card in cards:
            if self.save_knowledge_card(
                card,
                source="analysis",
                symbol=symbol,
                record_id=int(record_id) if record_id else None,
            ):
                saved += 1
        if saved:
            logger.info(f"KnowledgeMemory: +{saved} cards from analysis AI")
        return saved

    @staticmethod
    def _fallback_card_from_analysis(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        regime = str(ctx.get("market_regime") or "UNKNOWN")
        signal = str(ctx.get("final_signal") or ctx.get("trade_action") or "HOLD")
        indicators = ctx.get("indicators") or {}
        rsi = indicators.get("RSI")
        bb = indicators.get("BB_Position")
        conf = float(ctx.get("ai_confidence") or ctx.get("consensus_confidence") or 0.5)
        mtf = ctx.get("mtf_action") or ""
        active = ", ".join(ctx.get("active_strategies") or []) or "无明确方向策略"
        rsi_s = f"{float(rsi):.0f}" if rsi is not None else "?"
        bb_s = f"{float(bb):.2f}" if bb is not None else "?"
        try:
            from bnb_quant_tool.analysis_reuse import situation_key
            sit_key = situation_key(
                indicators,
                regime,
                symbol=str(ctx.get("symbol") or "BNBUSDT"),
            )
        except Exception:
            sit_key = f"{regime}|rsi{rsi_s}|bb{bb_s}|{signal}"
        trigger = (
            f"situation_key={sit_key} | 市场状态={regime} | RSI={rsi_s} | BB位置={bb_s} | "
            f"机构共识={ctx.get('consensus_signal')} | 多周期={mtf or '?'}"
        )
        action_rule = (
            f"参考信号 {signal}；机构 BUY={ctx.get('buy_signals')} "
            f"SELL={ctx.get('sell_signals')}；活跃策略: {active}"
        )
        # 不含 record_id，保证相同局面可去重强化
        lesson = (
            f"{regime} 下 {signal}，AI置信{conf:.0%}，"
            f"局面键={sit_key}；重复出现时强化此认知，待交易结果验证"
        )
        return {
            "category": "market_review",
            "title": f"{regime} · {signal} 局面认知",
            "trigger_condition": trigger[:500],
            "action_rule": action_rule[:500],
            "lesson": lesson[:1000],
            "confidence": max(0.25, min(0.55, conf * 0.6)),
            "situation_key": sit_key,
            "tags": [regime, signal, "analysis_snapshot", sit_key],
        }

    # 兼容旧方法名
    def record_from_closed_trade(self, *args, **kwargs) -> int:
        return self.extract_and_save_from_trade(*args, **kwargs)

    def record_from_review(self, review_result: Dict[str, Any], stats: Optional[Dict] = None) -> int:
        return self.extract_and_save_from_review(review_result, stats)

    def add_card(self, source: str, condition: str, lesson: str, **kwargs) -> Optional[int]:
        """兼容旧 API：手动添加。"""
        return self.save_knowledge_card({
            "category": kwargs.pop("category", "error_lesson"),
            "title": lesson[:80],
            "trigger_condition": condition,
            "action_rule": kwargs.pop("action_rule", ""),
            "lesson": lesson,
            "confidence": kwargs.get("confidence", 0.5),
            "tags": kwargs.get("tags") or [],
        }, source=source, record_id=kwargs.get("record_id"), trade_id=kwargs.get("trade_id"))

    @staticmethod
    def _fallback_cards_from_trade(
        ctx: Dict[str, Any],
        outcome: str,
        quality: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """AI 不可用时的结构化兜底卡片。"""
        cards = []
        side = ctx.get("side", "")
        mfe_r = float(ctx.get("mfe_r") or 0)
        close_reason = str(ctx.get("close_reason") or "")
        rsi = (ctx.get("indicators_at_entry") or {}).get("RSI")

        if outcome == "LOSS" and "STOP_LOSS" in close_reason.upper() and mfe_r >= 0.5:
            cards.append({
                "category": "stop_loss_rule",
                "title": "止损过紧导致扫损",
                "trigger_condition": f"{side} 止损前曾有 ≥{mfe_r:.1f}R 浮盈",
                "action_rule": "放宽 ATR 止损倍数，或等回踩确认后再入场",
                "lesson": "价格曾朝有利方向移动，说明止损可能设太近",
                "confidence": 0.65,
                "tags": ["stop_loss", "mfe"],
            })
        if outcome == "LOSS" and rsi and float(rsi) >= 68 and side == "LONG":
            cards.append({
                "category": "error_lesson",
                "title": "超买区追多亏损",
                "trigger_condition": f"RSI≈{rsi:.0f} 时做多",
                "action_rule": "RSI>65 时提高开仓门槛，需多周期共振确认",
                "lesson": "超买区域追多胜率低，应更保守",
                "confidence": 0.6,
                "tags": ["rsi", "long"],
            })
        if quality and quality.get("tier") == "D":
            cards.append({
                "category": "error_lesson",
                "title": "低质量交易过程",
                "trigger_condition": "交易质量评分 D 级",
                "action_rule": "复盘入场时机与止损设置，类似信号降级处理",
                "lesson": quality.get("text", "过程质量差，不应仅凭盈亏判断"),
                "confidence": 0.5,
                "tags": ["quality"],
            })
        if not cards and outcome == "LOSS":
            cards.append({
                "category": "error_lesson",
                "title": f"{side} 方向亏损",
                "trigger_condition": f"{side} 平仓亏损，原因={close_reason}",
                "action_rule": "类似条件下降低置信度或选择 WAIT",
                "lesson": f"本次 {side} 亏损 ${float(ctx.get('pnl_usdt') or 0):+.2f}，需复盘入场逻辑",
                "confidence": 0.45,
                "tags": ["loss"],
            })
        return cards

    @staticmethod
    def _fallback_cards_from_review(review_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        cards = []
        base_conf = float(review_result.get("confidence") or 0.5)
        for p in review_result.get("loss_patterns") or []:
            cards.append({
                "category": "error_lesson",
                "title": "复盘-亏损共性",
                "trigger_condition": "近期亏损交易",
                "action_rule": "遇到类似局面时提高门槛",
                "lesson": str(p),
                "confidence": min(0.85, base_conf + 0.1),
                "tags": ["review"],
            })
        for p in review_result.get("win_patterns") or []:
            cards.append({
                "category": "trading_logic",
                "title": "复盘-盈利共性",
                "trigger_condition": "近期盈利交易",
                "action_rule": "优先复用有效模式",
                "lesson": str(p),
                "confidence": min(0.8, base_conf + 0.05),
                "tags": ["review"],
            })
        sl = review_result.get("sl_diagnosis")
        if sl:
            cards.append({
                "category": "stop_loss_rule",
                "title": "止损诊断",
                "trigger_condition": "止损相关亏损",
                "action_rule": "按诊断结论调整 SL 参数",
                "lesson": str(sl),
                "confidence": min(0.9, base_conf + 0.15),
                "tags": ["stop_loss"],
            })
        return cards[:4]

    def get_summary(self) -> Dict[str, Any]:
        total = self.count_active_cards()
        cards = self.list_cards_for_ui(limit=50)
        by_cat: Dict[str, int] = {}
        conn = self._get_conn()
        for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM knowledge_cards "
            "WHERE is_active=1 GROUP BY category"
        ):
            by_cat[str(row["category"])] = int(row["n"])
        return {
            "total_active": total,
            "db_path": self.db_path,
            "top_cards": cards[:5],
            "by_category": by_cat,
            "vector_backend": self.vector_store.backend_name,
            "ai_extract_enabled": self.use_ai_extract,
        }

    def get_backfill_status(self, paper_db_path: Optional[str] = None) -> Dict[str, Any]:
        """历史交易回填进度。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM knowledge_backfill_status")
        processed = int(cur.fetchone()[0])
        total_closed = 0
        if paper_db_path:
            try:
                pconn = sqlite3.connect(paper_db_path, timeout=10)
                total_closed = int(
                    pconn.execute(
                        "SELECT COUNT(*) FROM paper_positions WHERE status='CLOSED'"
                    ).fetchone()[0]
                )
                pconn.close()
            except Exception:
                pass
        return {
            "processed_trades": processed,
            "total_closed_trades": total_closed,
            "pending_trades": max(0, total_closed - processed),
        }

    def _get_processed_trade_ids(self) -> set:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT trade_id FROM knowledge_backfill_status")
        ids = {int(r[0]) for r in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT trade_id FROM knowledge_cards "
            "WHERE trade_id IS NOT NULL"
        )
        ids.update(int(r[0]) for r in cur.fetchall())
        return ids

    def _mark_trade_processed(self, trade_id: int, mode: str, cards_saved: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_backfill_status "
            "(trade_id, processed_at, mode, cards_saved) VALUES (?,?,?,?)",
            (trade_id, datetime.now().isoformat(), mode, cards_saved),
        )
        conn.commit()

    @staticmethod
    def _trade_outcome(pnl: float, quality: Optional[Dict] = None) -> str:
        if pnl > 0.5:
            result = "WIN"
        elif pnl < -0.5:
            result = "LOSS"
        else:
            result = "BREAK_EVEN"
        if quality:
            suggested = quality.get("suggest_feedback")
            if suggested and suggested != result:
                return suggested
        return result

    def _extract_rules_only(
        self,
        trade_row: Dict[str, Any],
        analysis_record: Optional[Dict[str, Any]],
        outcome: str,
        quality: Optional[Dict[str, Any]],
    ) -> int:
        """仅规则提炼（不调用 API），适合大批量。"""
        ctx = build_trade_context(trade_row, analysis_record, outcome, quality)
        cards = self._fallback_cards_from_trade(ctx, outcome, quality)
        saved = 0
        symbol = trade_row.get("symbol", "BNBUSDT")
        record_id = trade_row.get("learning_record_id")
        trade_id = trade_row.get("id")
        for card in cards:
            if self.save_knowledge_card(
                card, source="trade_backfill", symbol=symbol,
                record_id=int(record_id) if record_id else None,
                trade_id=int(trade_id) if trade_id else None,
            ):
                saved += 1
        return saved

    @staticmethod
    def _aggregate_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trades:
            return {}
        wins = [t for t in trades if float(t.get("realized_pnl_usdt") or 0) > 0.5]
        losses = [t for t in trades if float(t.get("realized_pnl_usdt") or 0) < -0.5]
        sl_trades = [t for t in trades if "STOP_LOSS" in str(t.get("close_reason") or "").upper()]
        sl_tight = [
            t for t in sl_trades
            if float(t.get("mfe_r") or 0) >= 0.5 and float(t.get("realized_pnl_usdt") or 0) < 0
        ]
        reasons: Dict[str, int] = {}
        for t in trades:
            r = str(t.get("close_reason") or "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0,
            "total_pnl_usdt": round(sum(float(t.get("realized_pnl_usdt") or 0) for t in trades), 2),
            "avg_r_multiple": round(
                sum(float(t.get("r_multiple") or 0) for t in trades if t.get("r_multiple") is not None)
                / max(1, sum(1 for t in trades if t.get("r_multiple") is not None)),
                3,
            ),
            "stop_loss_count": len(sl_trades),
            "stop_loss_tight_count": len(sl_tight),
            "stop_loss_tight_pct": round(len(sl_tight) / max(1, len(sl_trades)) * 100, 1),
            "exit_reasons": reasons,
            "long_count": sum(1 for t in trades if t.get("side") == "LONG"),
            "short_count": sum(1 for t in trades if t.get("side") == "SHORT"),
        }

    @staticmethod
    def _pick_representative_trades(trades: List[Dict[str, Any]], limit: int = 25) -> List[Dict[str, Any]]:
        """挑选最有提炼价值的代表性交易。"""
        scored = []
        for t in trades:
            pnl = float(t.get("realized_pnl_usdt") or 0)
            mfe_r = float(t.get("mfe_r") or 0)
            mae_r = float(t.get("mae_r") or 0)
            r_mult = float(t.get("r_multiple") or 0)
            reason = str(t.get("close_reason") or "")
            score = 0.0
            if "STOP_LOSS" in reason.upper() and mfe_r >= 0.5:
                score += 5.0
            if pnl < -5:
                score += 3.0 + min(2.0, abs(pnl) / 20)
            if r_mult >= 1.5:
                score += 2.5
            if mae_r <= -1.5:
                score += 2.0
            if reason.upper() in ("TIMEOUT", "EXPIRED") and pnl < 0:
                score += 1.5
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [t for _, t in scored[:limit]]
        if len(picked) < limit:
            rest = [t for t in trades if t not in picked]
            picked.extend(rest[: limit - len(picked)])
        out = []
        for t in picked:
            out.append({
                "id": t.get("id"),
                "side": t.get("side"),
                "pnl_usdt": t.get("realized_pnl_usdt"),
                "close_reason": t.get("close_reason"),
                "r_multiple": t.get("r_multiple"),
                "mfe_r": t.get("mfe_r"),
                "mae_r": t.get("mae_r"),
                "entry_price": t.get("entry_price"),
                "close_price": t.get("close_avg_price"),
            })
        return out

    def backfill_from_paper_trades(
        self,
        paper_db_path: str,
        learner=None,
        mode: str = "rules",
        skip_processed: bool = True,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """
        从模拟盘历史交易批量提炼知识卡片。

        Args:
            paper_db_path: paper_trading.db 路径
            learner: AILearningSystem（读取 analysis_records）
            mode: rules=规则全量(快) | ai_summary=统计+AI抽样(推荐500+笔)
            skip_processed: 跳过已处理 trade_id
            progress_callback: fn(done, total, msg)

        Returns:
            {status, trades_processed, cards_saved, mode, ...}
        """
        result = {
            "status": "ok",
            "mode": mode,
            "trades_processed": 0,
            "trades_skipped": 0,
            "cards_saved": 0,
            "ai_cards_saved": 0,
        }
        if not self.enabled:
            result["status"] = "disabled"
            return result

        try:
            pconn = sqlite3.connect(paper_db_path, timeout=30)
            pconn.row_factory = sqlite3.Row
            rows = pconn.execute(
                "SELECT * FROM paper_positions WHERE status='CLOSED' ORDER BY id ASC"
            ).fetchall()
            pconn.close()
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            return result

        trades = [dict(r) for r in rows]
        total = len(trades)
        if total == 0:
            result["status"] = "empty"
            return result

        processed_ids = self._get_processed_trade_ids() if skip_processed else set()
        pending = [t for t in trades if int(t["id"]) not in processed_ids]
        result["trades_skipped"] = total - len(pending)

        if progress_callback:
            progress_callback(0, total, f"待处理 {len(pending)} / 共 {total} 笔")

        # ---- 模式: AI 汇总（适合 500+ 笔，仅 1 次 API）----
        if mode == "ai_summary" and pending:
            stats = self._aggregate_trade_stats(trades)
            samples = self._pick_representative_trades(pending, limit=25)
            ai_cards = []
            if self.extractor.available:
                ai_cards = self.extractor.extract_from_trades_batch(samples, stats)
            if not ai_cards:
                ai_cards = self._fallback_cards_from_review({
                    "loss_patterns": [
                        f"历史 {stats.get('losses', 0)} 笔亏损，胜率 {stats.get('win_rate', 0):.1%}",
                        f"止损过紧占比 {stats.get('stop_loss_tight_pct', 0)}%",
                    ],
                    "win_patterns": [f"盈利 {stats.get('wins', 0)} 笔，累计 PnL {stats.get('total_pnl_usdt', 0):+.2f}U"],
                    "sl_diagnosis": f"SL出场 {stats.get('stop_loss_count', 0)} 次，其中 {stats.get('stop_loss_tight_count', 0)} 次曾有浮盈",
                    "confidence": 0.65,
                })
            for card in ai_cards:
                if self.save_knowledge_card(card, source="backfill_ai"):
                    result["ai_cards_saved"] += 1
            result["cards_saved"] += result["ai_cards_saved"]

        # ---- 逐笔规则提炼 ----
        from bnb_quant_tool.trade_quality import score_closed_trade

        use_rules = mode in ("rules", "ai_summary", "rules_and_ai")
        if use_rules:
            for i, trade in enumerate(pending):
                tid = int(trade["id"])
                pnl = float(trade.get("realized_pnl_usdt") or 0)
                quality = score_closed_trade(trade)
                outcome = self._trade_outcome(pnl, quality)
                analysis_rec = None
                rid = trade.get("learning_record_id")
                if learner and rid:
                    analysis_rec = load_analysis_record(learner, int(rid))

                n = self._extract_rules_only(trade, analysis_rec, outcome, quality)
                result["cards_saved"] += n
                result["trades_processed"] += 1
                self._mark_trade_processed(tid, mode, n)

                if progress_callback and (i % 20 == 0 or i == len(pending) - 1):
                    progress_callback(
                        i + 1, len(pending),
                        f"规则提炼 {i + 1}/{len(pending)} · 已生成 {result['cards_saved']} 张卡片",
                    )

        elif mode == "ai_per_trade" and pending:
            # 逐笔 AI（慢，仅建议小批量）
            for i, trade in enumerate(pending[:50]):
                tid = int(trade["id"])
                pnl = float(trade.get("realized_pnl_usdt") or 0)
                quality = score_closed_trade(trade)
                outcome = self._trade_outcome(pnl, quality)
                analysis_rec = None
                rid = trade.get("learning_record_id")
                if learner and rid:
                    analysis_rec = load_analysis_record(learner, int(rid))
                n = self.extract_and_save_from_trade(trade, analysis_rec, outcome, quality)
                result["cards_saved"] += n
                result["trades_processed"] += 1
                self._mark_trade_processed(tid, mode, n)
                if progress_callback:
                    progress_callback(i + 1, min(50, len(pending)), f"AI 提炼 {i + 1}...")

        if progress_callback:
            progress_callback(total, total, "完成")

        self.checkpoint_wal()
        self.reset_connection()

        logger.info(
            f"Backfill done: mode={mode} processed={result['trades_processed']} "
            f"cards={result['cards_saved']}"
        )
        return result


def load_analysis_record(learner, record_id: int) -> Optional[Dict[str, Any]]:
    """从 AILearningSystem 读取分析快照。"""
    if learner is None or not record_id:
        return None
    try:
        conn = learner._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT indicators, ai_confidence, consensus_confidence, "
            "current_price, symbol, final_signal FROM analysis_records WHERE id=?",
            (int(record_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "indicators": row[0],
            "ai_confidence": row[1],
            "consensus_confidence": row[2],
            "current_price": row[3],
            "symbol": row[4],
            "final_signal": row[5],
        }
    except Exception as e:
        logger.debug(f"load_analysis_record failed: {e}")
        return None


def extract_knowledge_async(
    memory: CapabilityMemory,
    fn_name: str,
    **kwargs,
) -> None:
    """后台线程执行 AI 提炼，避免阻塞平仓/复盘 UI。"""
    def _run():
        saved = 0
        try:
            fn = getattr(memory, fn_name)
            saved = int(fn(**kwargs) or 0)
            memory.checkpoint_wal()
            memory.reset_connection()
            if saved:
                logger.info(
                    "async knowledge extract done: %s saved=%s db=%s",
                    fn_name, saved, memory.db_path,
                )
        except Exception as e:
            logger.warning(f"async knowledge extract failed ({fn_name}): {e}")

    threading.Thread(target=_run, daemon=True).start()
