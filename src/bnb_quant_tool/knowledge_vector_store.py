"""
知识向量库 — ChromaDB 语义检索，不可用时降级为本地 TF-IDF 余弦相似度。
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CHROMA_AVAILABLE = False
try:
    import chromadb
    from chromadb.config import Settings
    _CHROMA_AVAILABLE = True
except ImportError:
    chromadb = None  # type: ignore


def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    # 中英文混合分词：英文按词，中文按字+双字
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    bigrams = [
        text[i : i + 2]
        for i in range(len(text) - 1)
        if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i + 1] <= "\u9fff"
    ]
    return tokens + bigrams


def _tfidf_vector(tokens: List[str], idf: Dict[str, float]) -> Dict[str, float]:
    if not tokens:
        return {}
    tf = Counter(tokens)
    total = len(tokens)
    vec = {}
    for t, c in tf.items():
        weight = (c / total) * idf.get(t, 1.0)
        if weight > 0:
            vec[t] = weight
    return vec


def _cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in set(a) & set(b))
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class KnowledgeVectorStore:
    """知识卡片向量存储与语义检索。"""

    COLLECTION = "trading_knowledge"

    def __init__(
        self,
        persist_dir: str,
        sqlite_path: str,
        backend: str = "auto",
    ):
        self.persist_dir = Path(persist_dir)
        self.sqlite_path = sqlite_path
        self.backend = backend
        self._local = threading.local()
        self._chroma_client = None
        self._chroma_collection = None
        self._use_chroma = False
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._init_fallback_db()
        self._init_backend()

    def _init_backend(self) -> None:
        want_chroma = self.backend in ("auto", "chroma")
        if want_chroma and _CHROMA_AVAILABLE:
            try:
                self._chroma_client = chromadb.PersistentClient(
                    path=str(self.persist_dir),
                    settings=Settings(anonymized_telemetry=False),
                )
                self._chroma_collection = self._chroma_client.get_or_create_collection(
                    name=self.COLLECTION,
                    metadata={"hnsw:space": "cosine"},
                )
                self._use_chroma = True
                logger.info(f"KnowledgeVectorStore: ChromaDB @ {self.persist_dir}")
                return
            except Exception as e:
                logger.warning(f"ChromaDB init failed, fallback to tfidf: {e}")
        self._use_chroma = False
        logger.info("KnowledgeVectorStore: using TF-IDF fallback")

    def _init_fallback_db(self) -> None:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_vectors (
                card_id INTEGER PRIMARY KEY,
                embedding_text TEXT NOT NULL,
                tokens_json TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self.sqlite_path, timeout=60.0)
        return self._local.conn

    @staticmethod
    def build_embedding_text(card: Dict[str, Any]) -> str:
        """用于向量化的完整语义文本。"""
        cat = card.get("category", "")
        return " ".join(
            str(card.get(k) or "")
            for k in (
                "category",
                "title",
                "trigger_condition",
                "action_rule",
                "lesson",
                "symbol",
            )
            if card.get(k)
        ) + (f" tags:{' '.join(card.get('tags') or [])}" if card.get("tags") else "")

    def upsert(self, card_id: int, card: Dict[str, Any]) -> None:
        text = self.build_embedding_text(card)
        doc_id = str(card_id)

        if self._use_chroma and self._chroma_collection is not None:
            try:
                self._chroma_collection.upsert(
                    ids=[doc_id],
                    documents=[text],
                    metadatas=[{
                        "card_id": card_id,
                        "category": card.get("category", ""),
                        "confidence": float(card.get("confidence") or 0.5),
                        "source": card.get("source", ""),
                    }],
                )
            except Exception as e:
                logger.warning(f"Chroma upsert failed for #{card_id}: {e}")

        tokens = _tokenize(text)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_vectors (card_id, embedding_text, tokens_json) "
            "VALUES (?,?,?)",
            (card_id, text, json.dumps(tokens, ensure_ascii=False)),
        )
        conn.commit()

    def search(
        self,
        query_text: str,
        top_k: int = 5,
        min_similarity: float = 0.15,
    ) -> List[Dict[str, Any]]:
        if not query_text:
            return []

        results: List[Tuple[int, float]] = []

        if self._use_chroma and self._chroma_collection is not None:
            try:
                n = self._chroma_collection.count()
                if n > 0:
                    res = self._chroma_collection.query(
                        query_texts=[query_text],
                        n_results=min(top_k, n),
                        include=["distances", "metadatas"],
                    )
                    ids = (res.get("ids") or [[]])[0]
                    dists = (res.get("distances") or [[]])[0]
                    for doc_id, dist in zip(ids, dists):
                        # cosine distance -> similarity
                        sim = max(0.0, 1.0 - float(dist))
                        if sim >= min_similarity:
                            results.append((int(doc_id), sim))
            except Exception as e:
                logger.debug(f"Chroma search failed: {e}")

        if not results:
            results = self._search_tfidf(query_text, top_k, min_similarity)

        out = []
        for card_id, sim in results[:top_k]:
            out.append({"card_id": card_id, "similarity": round(sim, 4)})
        return out

    def _search_tfidf(
        self,
        query_text: str,
        top_k: int,
        min_similarity: float,
    ) -> List[Tuple[int, float]]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT card_id, tokens_json FROM knowledge_vectors")
        rows = cur.fetchall()
        if not rows:
            return []

        all_docs: List[Tuple[int, List[str]]] = []
        for card_id, tokens_json in rows:
            try:
                tokens = json.loads(tokens_json)
            except json.JSONDecodeError:
                tokens = _tokenize(tokens_json)
            all_docs.append((int(card_id), tokens))

        n = len(all_docs)
        df: Dict[str, int] = {}
        for _, tokens in all_docs:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

        q_vec = _tfidf_vector(_tokenize(query_text), idf)
        scored: List[Tuple[int, float]] = []
        for card_id, tokens in all_docs:
            d_vec = _tfidf_vector(tokens, idf)
            sim = _cosine_sparse(q_vec, d_vec)
            if sim >= min_similarity:
                scored.append((card_id, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def delete(self, card_id: int) -> None:
        if self._use_chroma and self._chroma_collection is not None:
            try:
                self._chroma_collection.delete(ids=[str(card_id)])
            except Exception:
                pass
        conn = self._get_conn()
        conn.execute("DELETE FROM knowledge_vectors WHERE card_id=?", (card_id,))
        conn.commit()

    @property
    def backend_name(self) -> str:
        return "chroma" if self._use_chroma else "tfidf"
