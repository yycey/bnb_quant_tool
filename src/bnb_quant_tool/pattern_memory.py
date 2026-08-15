"""
BNB量化交易工具 - AI 模式记忆 (Pattern Memory)
================================================
核心职责：
1. 将每笔已平仓交易的「指标快照」提取为 10 维归一化特征指纹
2. 当新分析产生时，查找历史相似局面并统计胜率
3. 输出人可读洞察："当前结构与过去 N 次中的 M 次类似，其中 X 次盈利"

设计原则：
- 纯 Python 实现（无 numpy/sklearn 依赖）
- 使用余弦相似度衡量指纹相似性
- SQLite 持久化指纹数据
- 首次启动自动回填已有交易记录
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 10 维特征定义
FEATURE_NAMES = [
    "rsi",                # RSI / 100
    "bb_position",        # BB_Position / 100
    "macd_direction",     # MACD_Histogram sign → 0/0.5/1
    "volume_ratio",       # min(Volume_Ratio / 3, 1)
    "stoch_k",            # Stoch_K / 100
    "ai_confidence",      # [0, 1]
    "institutional_conf", # consensus_confidence [0, 1]
    "news_sentiment",     # (polarity + 1) / 2 → [0, 1]
    "volatility_level",   # min(ATR/price*100 / 5, 1)
    "ema_trend",          # sigmoid((price - EMA25) / ATR)
]


class PatternMemory:
    """AI 模式记忆 — 记录历史局面指纹，查找相似模式统计胜率"""

    def __init__(self, db_path: str = None, paper_db_path: str = None,
                 learning_db_path: str = None):
        """
        Args:
            db_path: 模式记忆专用 DB（默认与 ai_learning.db 同目录）
            paper_db_path: 模拟盘 DB 路径（回填时需要）
            learning_db_path: AI 学习系统 DB 路径（回填时需要）
        """
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.db_path = str(get_localized_db_path('pattern_memory'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent / "data"
                if not base_dir.exists():
                    base_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(base_dir / "pattern_memory.db")
        else:
            from bnb_quant_tool.data_localization import resolve_db_path
            self.db_path = resolve_db_path(db_path, "pattern_memory")

        if paper_db_path:
            from bnb_quant_tool.data_localization import resolve_db_path
            paper_db_path = resolve_db_path(paper_db_path, "paper_trading")
        if learning_db_path:
            from bnb_quant_tool.data_localization import resolve_db_path
            learning_db_path = resolve_db_path(learning_db_path, "ai_learning")

        self._paper_db_path = paper_db_path
        self._learning_db_path = learning_db_path
        self._local = threading.local()
        self._init_db()

        # 首次启动自动回填
        self._auto_backfill_if_needed()

        logger.info(f"PatternMemory initialized, db={self.db_path}")

    # ============================================================
    # DB 初始化
    # ============================================================
    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self.db_path, timeout=60.0, row_factory=True)
        return self._local.conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接（导入/外部写入后刷新用）。"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _init_db(self):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pattern_fingerprints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                record_id INTEGER,
                side TEXT,
                fingerprint_json TEXT NOT NULL,
                outcome TEXT,
                pnl_usdt REAL DEFAULT 0,
                symbol TEXT DEFAULT 'BNBUSDT',
                notes TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pf_outcome "
            "ON pattern_fingerprints(outcome)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backfill_status (
                id INTEGER PRIMARY KEY,
                last_backfill_ts TEXT,
                count INTEGER DEFAULT 0
            )
        """)
        conn.commit()

    # ============================================================
    # 公开接口: 记录指纹
    # ============================================================
    def record_pattern(self, analysis_result: Dict, outcome: str,
                       pnl: float, record_id: int = None,
                       side: str = None) -> Optional[int]:
        """
        从分析结果提取指纹并存入 DB。

        Args:
            analysis_result: 完整分析结果字典 (含 indicators, ai_analysis 等)
            outcome: WIN / LOSS / BREAK_EVEN
            pnl: 盈亏金额 (USDT)
            record_id: 对应的 analysis_records.id
            side: LONG / SHORT

        Returns:
            插入的 fingerprint ID，失败返回 None
        """
        fp = self._extract_fingerprint(analysis_result)
        if fp is None:
            logger.warning("record_pattern: 无法提取指纹（指标缺失）")
            return None

        now = datetime.now().isoformat(timespec="seconds")
        symbol = analysis_result.get("symbol", "BNBUSDT")
        payload = (now, record_id, side, json.dumps(fp), outcome, pnl, symbol)

        def _op():
            conn = self._get_conn()
            cur = conn.cursor()
            try:
                from bnb_quant_tool.sqlite_util import begin_immediate
                begin_immediate(conn)
                cur.execute("""
                    INSERT INTO pattern_fingerprints
                    (timestamp, record_id, side, fingerprint_json, outcome, pnl_usdt, symbol)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, payload)
                fid = cur.lastrowid
                conn.commit()
                return fid
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                self.reset_connection()
                raise

        from bnb_quant_tool.sqlite_util import run_db
        try:
            fid = run_db(_op, label="pattern_record", on_locked=self.reset_connection)
        except sqlite3.OperationalError as e:
            logger.warning("record_pattern locked/fail: %s", e)
            return None
        logger.info(f"PatternMemory: recorded #{fid} outcome={outcome} pnl={pnl:.2f}")
        return fid

    # ============================================================
    # 公开接口: 查找相似局面
    # ============================================================
    def find_similar(self, current_result: Dict, top_k: int = 10,
                     min_similarity: float = 0.65) -> List[Dict]:
        """
        找 top_k 最相似的历史局面。

        Args:
            current_result: 当前分析结果字典
            top_k: 最多返回几条
            min_similarity: 最低相似度阈值

        Returns:
            [{"id", "similarity", "side", "outcome", "pnl_usdt", "timestamp"}, ...]
        """
        current_fp = self._extract_fingerprint(current_result)
        if current_fp is None:
            return []

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, side, fingerprint_json, outcome, pnl_usdt, timestamp "
            "FROM pattern_fingerprints WHERE outcome IS NOT NULL"
        )
        rows = cur.fetchall()

        results = []
        for row in rows:
            try:
                stored_fp = json.loads(row["fingerprint_json"])
                sim = self._cosine_similarity(current_fp, stored_fp)
                if sim >= min_similarity:
                    results.append({
                        "id": row["id"],
                        "similarity": round(sim, 4),
                        "side": row["side"],
                        "outcome": row["outcome"],
                        "pnl_usdt": row["pnl_usdt"],
                        "timestamp": row["timestamp"],
                    })
            except (json.JSONDecodeError, TypeError):
                continue

        # 按相似度降序排列
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    def get_insight(self, current_result: Dict,
                    min_similarity: float = 0.65) -> Dict:
        """
        返回模式记忆洞察。

        Returns:
            {
                "matched": int,       # 匹配到几条历史
                "win_count": int,
                "loss_count": int,
                "even_count": int,
                "win_rate": float,    # 0-1
                "avg_pnl": float,     # 平均盈亏
                "avg_similarity": float,
                "text": str,          # 人可读文本
            }
        """
        similar = self.find_similar(current_result, top_k=20,
                                    min_similarity=min_similarity)
        matched = len(similar)
        if matched == 0:
            return {
                "matched": 0,
                "win_count": 0,
                "loss_count": 0,
                "even_count": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "avg_similarity": 0.0,
                "text": "📊 模式记忆：暂无足够相似的历史局面",
            }

        win_count = sum(1 for s in similar if s["outcome"] == "WIN")
        loss_count = sum(1 for s in similar if s["outcome"] == "LOSS")
        even_count = matched - win_count - loss_count
        win_rate = win_count / matched if matched > 0 else 0.0
        avg_pnl = sum(s["pnl_usdt"] for s in similar) / matched
        avg_sim = sum(s["similarity"] for s in similar) / matched

        # 生成人可读文本
        text = self._format_insight_text(
            matched, win_count, loss_count, win_rate, avg_pnl, avg_sim
        )

        return {
            "matched": matched,
            "win_count": win_count,
            "loss_count": loss_count,
            "even_count": even_count,
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "avg_similarity": round(avg_sim, 4),
            "text": text,
        }

    # ============================================================
    # 回填
    # ============================================================
    def backfill_from_history(self, paper_db_path: str = None,
                              learning_db_path: str = None) -> int:
        """
        从已有交易历史回填指纹。

        Returns:
            成功回填的记录数
        """
        paper_db = paper_db_path or self._paper_db_path
        learn_db = learning_db_path or self._learning_db_path

        if not paper_db or not learn_db:
            logger.warning("backfill: 未提供 paper_db / learning_db 路径")
            return 0

        try:
            paper_conn = sqlite3.connect(paper_db, timeout=60)
            paper_conn.row_factory = sqlite3.Row
            from bnb_quant_tool.sqlite_util import apply_writer_pragmas
            apply_writer_pragmas(paper_conn, autocommit=False)
            paper_conn.execute("PRAGMA busy_timeout=30000")
            learn_conn = sqlite3.connect(learn_db, timeout=60)
            learn_conn.row_factory = sqlite3.Row
            apply_writer_pragmas(learn_conn, autocommit=False)
            learn_conn.execute("PRAGMA busy_timeout=30000")
        except Exception as e:
            logger.error(f"backfill: 无法连接数据库: {e}")
            return 0

        # 获取已回填的 record_ids 避免重复
        my_conn = self._get_conn()
        cur = my_conn.cursor()
        cur.execute("SELECT record_id FROM pattern_fingerprints WHERE record_id IS NOT NULL")
        existing_ids = {row["record_id"] for row in cur.fetchall()}

        # 读取已平仓交易
        p_cur = paper_conn.cursor()
        p_cur.execute(
            "SELECT id, side, realized_pnl_usdt, learning_record_id, closed_at "
            "FROM paper_positions WHERE status='CLOSED' AND learning_record_id IS NOT NULL"
        )
        closed_trades = p_cur.fetchall()

        count = 0
        for trade in closed_trades:
            record_id = trade["learning_record_id"]
            if record_id in existing_ids:
                continue

            # 从 learning DB 取 analysis_records
            l_cur = learn_conn.cursor()
            l_cur.execute(
                "SELECT indicators, ai_confidence, consensus_confidence, "
                "current_price, symbol FROM analysis_records WHERE id=?",
                (record_id,)
            )
            rec = l_cur.fetchone()
            if rec is None:
                continue

            # 构造 mini analysis_result 用于指纹提取
            indicators = {}
            try:
                indicators = json.loads(rec["indicators"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass

            mini_result = {
                "indicators": indicators,
                "ai_analysis": {"confidence": rec["ai_confidence"]},
                "institutional_strategies": {
                    "consensus_confidence": rec["consensus_confidence"]
                },
                "current_price": rec["current_price"],
                "symbol": rec["symbol"] or "BNBUSDT",
                "news_summary": {},  # 历史新闻情绪不可回溯，用 0.5 默认
            }

            # 判定 outcome
            pnl = trade["realized_pnl_usdt"] or 0
            if pnl > 0.5:
                outcome = "WIN"
            elif pnl < -0.5:
                outcome = "LOSS"
            else:
                outcome = "BREAK_EVEN"

            fid = self.record_pattern(
                mini_result, outcome=outcome, pnl=pnl,
                record_id=record_id, side=trade["side"]
            )
            if fid:
                count += 1

        # 注意：paper_conn 和 learn_conn 是一次性跨库连接，回填完成后关闭
        paper_conn.close()
        learn_conn.close()

        # 记录回填状态
        cur.execute(
            "INSERT OR REPLACE INTO backfill_status (id, last_backfill_ts, count) "
            "VALUES (1, ?, ?)",
            (datetime.now().isoformat(), count)
        )
        my_conn.commit()
        logger.info(f"PatternMemory backfill complete: {count} patterns recorded")
        return count

    def _auto_backfill_if_needed(self):
        """首次启动时自动回填（如果 DB 为空且有历史数据可用）"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM pattern_fingerprints")
        row = cur.fetchone()
        if row and row["cnt"] > 0:
            return  # 已有数据，跳过

        if self._paper_db_path and self._learning_db_path:
            logger.info("PatternMemory: 首次启动，自动回填历史指纹...")
            n = self.backfill_from_history()
            if n > 0:
                logger.info(f"PatternMemory: 自动回填完成，共 {n} 条")

    # ============================================================
    # 特征提取
    # ============================================================
    def _extract_fingerprint(self, result: Dict) -> Optional[List[float]]:
        """
        从分析结果提取 10 维归一化指纹。

        Returns:
            [0-1] 范围的 10 维列表，缺失指标用 0.5 填充
        """
        indicators = result.get("indicators") or {}
        ai_analysis = result.get("ai_analysis") or {}
        inst = result.get("institutional_strategies") or {}
        news = result.get("news_summary") or {}
        price = float(result.get("current_price") or 0)

        # 1. RSI / 100
        rsi = self._safe_float(indicators.get("RSI"), 50.0) / 100.0

        # 2. BB_Position / 100
        bb_pos = self._safe_float(indicators.get("BB_Position"), 50.0) / 100.0

        # 3. MACD_Histogram → sign mapping: >0 → 1, ==0 → 0.5, <0 → 0
        macd_hist = self._safe_float(indicators.get("MACD_Histogram"), 0)
        if macd_hist > 0:
            macd_dir = 1.0
        elif macd_hist < 0:
            macd_dir = 0.0
        else:
            macd_dir = 0.5

        # 4. Volume_Ratio: min(x/3, 1)
        vol_ratio = self._safe_float(indicators.get("Volume_Ratio"), 1.0)
        vol_norm = min(vol_ratio / 3.0, 1.0)

        # 5. Stoch_K / 100
        stoch_k = self._safe_float(indicators.get("Stoch_K"), 50.0) / 100.0

        # 6. AI confidence [0, 1]
        ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)
        ai_conf = max(0.0, min(1.0, ai_conf))

        # 7. Institutional consensus confidence [0, 1]
        inst_conf = self._safe_float(inst.get("consensus_confidence"), 0.5)
        inst_conf = max(0.0, min(1.0, inst_conf))

        # 8. News sentiment polarity: (x+1)/2 → [0,1]
        polarity = self._safe_float(news.get("polarity"), 0.0)
        news_norm = (polarity + 1.0) / 2.0
        news_norm = max(0.0, min(1.0, news_norm))

        # 9. Volatility level: min(ATR/price*100 / 5, 1)
        atr = self._safe_float(indicators.get("ATR"), 0)
        if price > 0 and atr > 0:
            vol_level = min((atr / price * 100.0) / 5.0, 1.0)
        else:
            vol_level = 0.5

        # 10. EMA trend: sigmoid((price - EMA25) / ATR)
        ema25 = self._safe_float(indicators.get("EMA_25"), price)
        if price > 0 and atr > 0:
            z = (price - ema25) / atr
            ema_trend = self._sigmoid(z)
        else:
            ema_trend = 0.5

        fingerprint = [
            round(rsi, 4),
            round(bb_pos, 4),
            round(macd_dir, 4),
            round(vol_norm, 4),
            round(stoch_k, 4),
            round(ai_conf, 4),
            round(inst_conf, 4),
            round(news_norm, 4),
            round(vol_level, 4),
            round(ema_trend, 4),
        ]
        return fingerprint

    # ============================================================
    # 相似度计算
    # ============================================================
    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """余弦相似度，纯 Python 实现"""
        if len(a) != len(b) or len(a) == 0:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ============================================================
    # 辅助方法
    # ============================================================
    @staticmethod
    def _sigmoid(x: float) -> float:
        """Sigmoid 函数，输出 [0, 1]"""
        # 防止溢出
        x = max(-10.0, min(10.0, x))
        return 1.0 / (1.0 + math.exp(-x))

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """安全转 float"""
        if value is None:
            return default
        try:
            v = float(value)
            if math.isnan(v) or math.isinf(v):
                return default
            return v
        except (TypeError, ValueError):
            return default

    def _format_insight_text(self, matched: int, win_count: int,
                             loss_count: int, win_rate: float,
                             avg_pnl: float, avg_sim: float) -> str:
        """生成人可读洞察文本"""
        lines = []
        lines.append("━" * 36)
        lines.append("📊 模式记忆洞察 (Pattern Memory)")
        lines.append("━" * 36)
        lines.append(
            f"当前市场结构与历史 {matched} 次相似局面匹配"
            f"（平均相似度 {avg_sim:.0%}）"
        )
        lines.append(f"  ✅ 盈利: {win_count} 次")
        lines.append(f"  ❌ 亏损: {loss_count} 次")
        even = matched - win_count - loss_count
        if even > 0:
            lines.append(f"  ➖ 持平: {even} 次")
        lines.append(f"  📈 历史胜率: {win_rate:.0%}")
        lines.append(f"  💰 平均盈亏: ${avg_pnl:+.2f}")

        # 信心评级
        if win_rate >= 0.7:
            lines.append("  🟢 信心评级: 高 (历史胜率 ≥ 70%)")
        elif win_rate >= 0.5:
            lines.append("  🟡 信心评级: 中 (历史胜率 50-70%)")
        else:
            lines.append("  🔴 信心评级: 低 (历史胜率 < 50%)")

        lines.append("━" * 36)
        return "\n".join(lines)

    def get_pattern_count(self) -> int:
        """返回已存储的指纹总数"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM pattern_fingerprints")
        row = cur.fetchone()
        return row["cnt"] if row else 0
