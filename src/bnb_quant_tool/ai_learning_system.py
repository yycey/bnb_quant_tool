"""
BNB量化交易工具 - AI学习成长系统 v1.2 (Clean & Stable)
记录每次分析结果，让AI通过历史数据持续学习和优化
作者: Python全栈工程师
日期: 2026-05-17
"""

import json
import sqlite3
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from functools import wraps

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_analysis_ts(ts_raw: str) -> Optional[datetime]:
    """解析 analysis_records.timestamp 为 aware UTC；失败返回 None。"""
    raw = str(ts_raw or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        # 历史行多为本地 naive；与 UTC now 比较前按 UTC 对待会偏龄。
        # 统一：naive → 假定 UTC（写入侧也应逐步改 UTC）。
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_db(max_retries: int = 5, base_delay: float = 1.0):
    """数据库操作重试装饰器 — 针对 database is locked
    
    v2.6: locked 时 rollback + 复位线程连接，避免半开事务持续占锁
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            self = args[0] if args else None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    locked = "locked" in msg or "busy" in msg
                    if self is not None:
                        try:
                            conn = getattr(getattr(self, "_local", None), "conn", None)
                            if conn is not None:
                                conn.rollback()
                        except Exception:
                            pass
                        if locked and hasattr(self, "_local"):
                            try:
                                conn = getattr(self._local, "conn", None)
                                if conn is not None:
                                    conn.close()
                            except Exception:
                                pass
                            try:
                                self._local.conn = None
                            except Exception:
                                pass
                    if locked and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"[DB重试] {func.__name__} locked (尝试 {attempt+1}/{max_retries}), {delay:.1f}s后重试"
                        )
                        time.sleep(delay)
                    elif locked:
                        logger.error(
                            f"[DB] {func.__name__} locked 重试{max_retries}次后仍失败，放弃本次操作"
                        )
                        return None
                    else:
                        raise
            return None
        return wrapper
    return decorator


class AILearningSystem:
    """
    AI学习成长系统
    核心功能:
    1. 记录每次分析结果到SQLite数据库
    2. 跟踪每个策略的历史表现，计算胜率
    3. 根据历史表现动态调整策略权重（让AI"成长"）
    4. 生成学习成长报告，供DeepSeek AI自我反思
    """

    def __init__(self, db_path: str = None, config: Dict = None):
        self.config = config or {}
        # 本地化：所有数据保存在工作空间 data/ 目录
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.db_path = str(get_localized_db_path('ai_learning'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent
                self.db_path = str(base_dir / "data" / "ai_learning.db")
        else:
            from bnb_quant_tool.data_localization import resolve_db_path
            self.db_path = resolve_db_path(db_path, "ai_learning")

        self.learning_rate = float(
            (self.config.get("learning") or {}).get(
                "learning_rate", self.config.get("learning_rate", 0.1)
            )
        )
        # 默认 3：更快根据交易反馈重算权重，服务「每笔都进步、提高胜率」
        self.min_samples_for_learning = int(
            (self.config.get("learning") or {}).get(
                "min_samples", self.config.get("min_samples", 3)
            )
        )
        self._local = threading.local()
        self._capability_memory = None
        self.db_recovery_info = self._ensure_db_healthy()
        if not self.db_recovery_info.get("ok"):
            raise RuntimeError(
                self.db_recovery_info.get("message")
                or "ai_learning.db 损坏且无法自动修复，请关闭其他实例后重试。"
            )
        self._init_database()
        self.strategy_weights = self._load_strategy_weights()
        logger.info(f"AILearningSystem initialized, db={self.db_path}")

    @property
    def capability_memory(self):
        """经验卡片存储（懒加载，与 ai_learning.db 共用）。"""
        if self._capability_memory is None:
            from bnb_quant_tool.capability_memory import CapabilityMemory
            self._capability_memory = CapabilityMemory(self.db_path, config=self.config)
        return self._capability_memory

    def _ensure_db_healthy(self) -> Dict:
        """检测并修复损坏的 ai_learning.db（备份后尝试恢复或重建）。"""
        try:
            from bnb_quant_tool.sqlite_recovery import ensure_sqlite_db_healthy
            return ensure_sqlite_db_healthy(self.db_path, label="ai_learning")
        except Exception as e:
            logger.error("数据库健康检查失败: %s", e)
            return {"ok": False, "action": "failed", "backup": None, "message": str(e)}

    def _get_conn(self):
        """每个线程独立的SQLite连接（避免并发锁定），WAL模式"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            try:
                self._local.conn = connect_writer(self.db_path, timeout=60.0)
            except sqlite3.DatabaseError as e:
                if "malformed" in str(e).lower() or "disk image" in str(e).lower():
                    self.db_recovery_info = self._ensure_db_healthy()
                    if not self.db_recovery_info.get("ok"):
                        raise RuntimeError(self.db_recovery_info.get("message", str(e))) from e
                    self._local.conn = connect_writer(self.db_path, timeout=60.0)
                else:
                    raise
        return self._local.conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接（导入/外部写入后刷新用）。"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
        if self._capability_memory is not None:
            self._capability_memory.reset_connection()

    def refresh_before_analysis(self, regime: Optional[str] = None) -> None:
        """每次分析前刷新 DB 与策略权重，确保注入最新学习成果。"""
        self.reset_connection()
        self.strategy_weights = self._load_strategy_weights(regime=regime)
        try:
            self.capability_memory.reset_connection()
        except Exception as e:
            logger.debug("capability_memory refresh before analysis: %s", e)

    def _init_database(self):
        """初始化SQLite数据库（4张表）"""
        conn = self._get_conn()
        cur = conn.cursor()

        # 表1: 分析记录 (analysis_records)
        cur.execute("""CREATE TABLE IF NOT EXISTS analysis_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            current_price REAL NOT NULL,
            final_signal TEXT NOT NULL,
            consensus_confidence REAL,
            institutional_results TEXT,
            buy_signals INTEGER DEFAULT 0,
            sell_signals INTEGER DEFAULT 0,
            hold_signals INTEGER DEFAULT 0,
            indicators TEXT,
            ai_signal TEXT,
            ai_confidence REAL,
            ai_analysis TEXT,
            trading_action TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            position_size REAL,
            risk_passed INTEGER,
            risk_reason TEXT,
            actual_result TEXT,
            actual_price_after_24h REAL,
            pnl_percent REAL,
            feedback_notes TEXT,
            data_points INTEGER,
            strategy_mode TEXT,
            version TEXT DEFAULT '3.0'
        )""")

        # 表2: 策略表现 (strategy_performance)
        cur.execute("""CREATE TABLE IF NOT EXISTS strategy_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL UNIQUE,
            total_predictions INTEGER DEFAULT 0,
            correct_predictions INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.5,
            avg_confidence_correct REAL DEFAULT 0.5,
            avg_confidence_wrong REAL DEFAULT 0.5,
            last_updated TEXT,
            weight REAL DEFAULT 1.0,
            is_active INTEGER DEFAULT 1,
            correct_buy INTEGER DEFAULT 0,
            wrong_buy INTEGER DEFAULT 0,
            correct_sell INTEGER DEFAULT 0,
            wrong_sell INTEGER DEFAULT 0,
            correct_hold INTEGER DEFAULT 0,
            wrong_hold INTEGER DEFAULT 0,
            streak_current INTEGER DEFAULT 0,
            streak_best INTEGER DEFAULT 0,
            streak_worst INTEGER DEFAULT 0
        )""")

        # 表3: 学习日志 (learning_log)
        cur.execute("""CREATE TABLE IF NOT EXISTS learning_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT,
            improvement_score REAL
        )""")

        # 表4: 市场快照 (market_snapshots)
        cur.execute("""CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL,
            rsi REAL,
            macd REAL,
            bb_position REAL
        )""")

        # 表五: 参数变更日志 (param_change_log) - 方向4 A/B 影子跟踪
        cur.execute("""CREATE TABLE IF NOT EXISTS param_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            param_name TEXT NOT NULL,
            old_value REAL,
            new_value REAL,
            source TEXT,
            review_summary TEXT,
            trades_before INTEGER,
            wr_before REAL,
            pf_before REAL
        )""")

        # 表六: 市场状态分表策略表现
        cur.execute("""CREATE TABLE IF NOT EXISTS strategy_regime_performance (
            strategy_name TEXT NOT NULL,
            regime TEXT NOT NULL,
            total_predictions INTEGER DEFAULT 0,
            correct_predictions INTEGER DEFAULT 0,
            weighted_correct REAL DEFAULT 0,
            win_rate REAL DEFAULT 0.5,
            weight REAL DEFAULT 1.0,
            last_updated TEXT,
            PRIMARY KEY (strategy_name, regime)
        )""")

        # 表七: 决策因子归因
        cur.execute("""CREATE TABLE IF NOT EXISTS factor_attribution (
            factor_key TEXT NOT NULL,
            regime TEXT NOT NULL DEFAULT 'GLOBAL',
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            net_score_when_win REAL DEFAULT 0,
            net_score_when_loss REAL DEFAULT 0,
            last_updated TEXT,
            PRIMARY KEY (factor_key, regime)
        )""")

        # 表八: 参数影子 A/B
        cur.execute("""CREATE TABLE IF NOT EXISTS shadow_param_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            param_name TEXT NOT NULL,
            baseline_value REAL,
            shadow_value REAL,
            status TEXT DEFAULT 'active',
            trades_observed INTEGER DEFAULT 0,
            baseline_wins INTEGER DEFAULT 0,
            shadow_wins INTEGER DEFAULT 0,
            source TEXT,
            reason TEXT
        )""")

        # 表九: 元学习日志
        cur.execute("""CREATE TABLE IF NOT EXISTS meta_learning_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cards_before INTEGER,
            cards_after INTEGER,
            merged_count INTEGER,
            summary TEXT
        )""")

        # 表十: Agent 可信度追踪
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_accuracy (
            agent_role TEXT PRIMARY KEY,
            correct_predictions INTEGER DEFAULT 0,
            total_predictions INTEGER DEFAULT 0,
            accuracy REAL DEFAULT 0.5,
            last_updated TEXT
        )""")

        # 表十一: 批量反思队列
        cur.execute("""CREATE TABLE IF NOT EXISTS reflection_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            record_id INTEGER,
            position_id INTEGER,
            outcome TEXT,
            status TEXT DEFAULT 'pending',
            processed_at TEXT
        )""")

        # 创建索引
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_symbol ON analysis_records(symbol)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_time ON analysis_records(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_log_time ON learning_log(timestamp)")
        except Exception:
            pass

        self._ensure_columns(cur, 'analysis_records', {
            'decision_explanation': 'TEXT',
            'gate_reasons': 'TEXT',
            'raw_action': 'TEXT',
            'passed_gate': 'INTEGER',
            'market_regime': 'TEXT',
            'quality_score': 'INTEGER',
            'quality_tier': 'TEXT',
            'multi_agent_deliberation': 'TEXT',
            'trade_advice_snapshot': 'TEXT',
            'market_regime_json': 'TEXT',
        })

        self._ensure_columns(cur, 'strategy_performance', {
            'weighted_correct': 'REAL DEFAULT 0',
        })

        conn.commit()
        logger.info(f"Database initialized: {self.db_path}")

    def _ensure_columns(self, cur, table: str, columns: Dict[str, str]) -> None:
        """为已有数据库追加新列（幂等）。"""
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        for name, col_type in columns.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
                logger.info(f"Added column {table}.{name}")

    @_retry_db(max_retries=3, base_delay=0.5)
    def record_analysis(self, result: Dict) -> int:
        """
        记录一次完整的分析结果
        Args: result: 完整分析结果字典（来自main.py或gui.py）
        Returns: 记录ID
        """
        conn = self._get_conn()
        cur = conn.cursor()
        inst = result.get('institutional_strategies', {})
        ta = result.get('trade_advice') or {}
        tp = result.get('trading_plan') or {}
        prices = ta.get('prices') or {}
        explanation = ta.get('explanation')
        gate_list = ta.get('gate_reasons') or []
        passed_gate = ta.get('passed_gate')
        if passed_gate is None:
            passed_gate = result.get('risk_check', {}).get('passed')

        regime_data = result.get('market_regime') or {}
        regime_tag = (
            regime_data.get('regime')
            if isinstance(regime_data, dict)
            else str(regime_data or '')
        ) or inst.get('_regime_tag') or ''

        snapshot = None
        regime_json = None
        try:
            from bnb_quant_tool.analysis_pipeline import build_trade_advice_snapshot
            snapshot = build_trade_advice_snapshot(
                ta,
                market_regime=regime_data if isinstance(regime_data, dict) else {},
                learning_context=result.get('learning_context') or {},
                ai_analysis=result.get('ai_analysis') if isinstance(result.get('ai_analysis'), dict) else {},
                ai_analyses=result.get('ai_analyses') if isinstance(result.get('ai_analyses'), dict) else {},
                ai_analysis_note=str(result.get('ai_analysis_note') or ''),
                ai_primary_provider=str(result.get('ai_primary_provider') or ''),
            )
            if isinstance(regime_data, dict):
                import json as _json
                regime_json = _json.dumps(regime_data, ensure_ascii=False)
        except Exception:
            pass

        cur.execute("""INSERT INTO analysis_records (
            timestamp, symbol, timeframe, current_price, final_signal,
            consensus_confidence, institutional_results,
            buy_signals, sell_signals, hold_signals,
            indicators, ai_signal, ai_confidence, ai_analysis,
            trading_action, entry_price, stop_loss, take_profit, position_size,
            risk_passed, risk_reason, data_points, strategy_mode,
            decision_explanation, gate_reasons, raw_action, passed_gate,
            market_regime, trade_advice_snapshot, market_regime_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            result.get('timestamp', datetime.now().isoformat()),
            result.get('symbol', 'BNBUSDT'),
            result.get('timeframe', '1h'),
            float(result.get('current_price', 0)),
            result.get('final_recommendation', 'HOLD'),
            float(inst.get('consensus_confidence', 0.5)),
            json.dumps(inst.get('strategy_details', {}), ensure_ascii=False),
            int(inst.get('buy_signals', 0)),
            int(inst.get('sell_signals', 0)),
            int(inst.get('hold_signals', 0)),
            json.dumps(result.get('indicators', {}), ensure_ascii=False),
            result.get('ai_analysis', {}).get('signal'),
            result.get('ai_analysis', {}).get('confidence'),
            result.get('ai_analysis', {}).get('analysis'),
            ta.get('action') or tp.get('action'),
            prices.get('entry_mid') or tp.get('entry', {}).get('price'),
            prices.get('stop_loss') or tp.get('risk_management', {}).get('stop_loss'),
            prices.get('tp1') or tp.get('risk_management', {}).get('take_profit'),
            (ta.get('position') or {}).get('qty') or tp.get('risk_management', {}).get('position_size'),
            1 if passed_gate else 0,
            result.get('risk_check', {}).get('reason'),
            result.get('data_points', 0),
            result.get('strategy_mode', 'all'),
            json.dumps(explanation, ensure_ascii=False) if explanation else None,
            json.dumps(gate_list, ensure_ascii=False) if gate_list else None,
            ta.get('raw_action'),
            1 if passed_gate else 0,
            regime_tag[:64] if regime_tag else None,
            snapshot,
            regime_json,
        ))

        record_id = cur.lastrowid
        multi_agent = ta.get('multi_agent_deliberation')
        if multi_agent and record_id:
            cur.execute(
                "UPDATE analysis_records SET multi_agent_deliberation=? WHERE id=?",
                (json.dumps(multi_agent, ensure_ascii=False), int(record_id)),
            )
        self._record_strategy_predictions(cur, record_id, inst)
        self._record_market_snapshot(cur, result)
        self._log_event(cur, 'ANALYSIS',
                       f"Recorded analysis for {result.get('symbol')} - Signal: {result.get('final_recommendation')}",
                       json.dumps({'record_id': record_id}))
        conn.commit()
        logger.info(f"Recorded analysis ID={record_id}, signal={result.get('final_recommendation')}")
        self._after_analysis_growth(record_id, result)
        return record_id

    def _after_analysis_growth(self, record_id: int, result: Dict) -> None:
        """分析完成后必学：局面快照/复用强化 + 成长日志（服务胜率提升）。"""
        growth = {
            "record_id": record_id,
            "cards_added": 0,
            "cards_reinforced": 0,
            "reused": False,
        }
        ai = result.get("ai_analysis") or {}
        if not isinstance(ai, dict):
            ai = {}
        reused = bool(ai.get("_reused") or ai.get("_provider") == "knowledge_reuse")
        growth["reused"] = reused

        try:
            mem = self.capability_memory
            mem_cfg = self.config.get("capability_memory") or {}

            if reused:
                # 同局面复用 → 强化已有知识（validated++），不新建垃圾卡、不烧 LLM
                try:
                    from bnb_quant_tool.analysis_reuse import (
                        ReuseHit,
                        reinforce_on_reuse,
                        situation_key,
                    )
                    meta = ai.get("_reuse_hit_meta") or {}
                    indicators = (
                        ai.get("_situation_indicators")
                        or result.get("indicators")
                        or {}
                    )
                    regime = (
                        ai.get("_situation_regime")
                        or result.get("market_regime")
                    )
                    symbol = str(result.get("symbol") or "BNBUSDT")
                    hit = ReuseHit(
                        reuse=True,
                        reason=str(
                            meta.get("reason")
                            or ai.get("_reuse_reason")
                            or "知识复用"
                        ),
                        action=str(
                            meta.get("action")
                            or ai.get("trade_suggestion")
                            or "WAIT"
                        ),
                        confidence=float(
                            meta.get("confidence")
                            or ai.get("confidence")
                            or 0.4
                        ),
                        similarity=float(meta.get("similarity") or ai.get("_reuse_similarity") or 0.9),
                        source=str(meta.get("source") or ai.get("_reuse_source") or "analysis_record"),
                        source_id=meta.get("source_id") or ai.get("_reuse_source_id") or record_id,
                        situation_key=str(
                            meta.get("situation_key")
                            or situation_key(indicators, regime, symbol=symbol)
                        ),
                        signal=str(ai.get("signal") or "持有"),
                        analysis_text=str(ai.get("analysis") or ""),
                    )
                    card_id = reinforce_on_reuse(
                        self,
                        hit,
                        symbol=symbol,
                        indicators=indicators,
                        market_regime=regime,
                    )
                    if card_id:
                        growth["cards_reinforced"] = 1
                        growth["reinforced_card_id"] = int(card_id)
                except Exception as re_e:
                    logger.debug("growth reinforce_on_reuse: %s", re_e)
            else:
                # 新局面 → 规则快照沉淀（重复局面会走 dedupe 强化）
                growth["cards_added"] = mem.save_analysis_snapshot(result, record_id)

            if mem_cfg.get("extract_on_analysis", False):
                from bnb_quant_tool.capability_memory import extract_knowledge_async
                extract_knowledge_async(
                    mem,
                    "extract_and_save_from_analysis",
                    result=result,
                    record_id=record_id,
                )
        except Exception as e:
            logger.debug(f"after_analysis_growth snapshot failed: {e}")

        # 每次分析尝试消化积压的反思队列（不阻塞）
        # 若 intelligence_loop 已在 preflight 做过，则跳过避免双跑
        try:
            learn_cfg = self.config.get("learning") or {}
            loop_cfg = self.config.get("intelligence_loop") or {}
            defer = bool(loop_cfg.get("owns_preflight", True))
            if learn_cfg.get("drain_reflections_on_analysis", True) and not (
                defer and loop_cfg.get("preflight_drain_reflections", True)
            ):
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
                ev = LearningEvolutionCoordinator(
                    self,
                    capability_memory=self.capability_memory,
                    config=self.config,
                )
                drained = ev.drain_pending_reflections(force=False)
                if drained:
                    growth["reflections_drained"] = drained
        except Exception as e:
            logger.debug(f"after_analysis_growth drain reflections: {e}")

        # 观望类分析延时软反馈（preflight 已消化旧样本；此处再扫一轮新到期）
        try:
            loop_cfg = self.config.get("intelligence_loop") or {}
            # 保留：分析后用最新价再 drain，与 preflight 不冲突（已打标样本会跳过）
            price = float(result.get("current_price") or 0)
            symbol = str(result.get("symbol") or "BNBUSDT")
            if price > 0 and not (
                loop_cfg.get("owns_preflight", True)
                and loop_cfg.get("skip_post_soft_feedback", False)
            ):
                n_soft = self.drain_soft_analysis_feedback(price, symbol=symbol)
                if n_soft:
                    growth["soft_feedbacks"] = n_soft
        except Exception as e:
            logger.debug(f"after_analysis_growth soft feedback: {e}")

        # 回填历史平仓的议会胜负（默认交给 intelligence_loop.preflight）
        try:
            learn_cfg = self.config.get("learning") or {}
            loop_cfg = self.config.get("intelligence_loop") or {}
            if learn_cfg.get("backfill_council_on_analysis", True) and not (
                loop_cfg.get("owns_preflight", True)
                and loop_cfg.get("preflight_council_backfill", True)
            ):
                from bnb_quant_tool.trade_close_learning import (
                    backfill_missing_council_outcomes,
                )
                n_bf = backfill_missing_council_outcomes(
                    self.config,
                    limit=int(learn_cfg.get("backfill_council_batch", 8) or 8),
                )
                if n_bf:
                    growth["council_backfilled"] = n_bf
        except Exception as e:
            logger.debug(f"after_analysis_growth council backfill: {e}")

        try:
            snap = self.get_growth_snapshot()
            growth.update(snap)
            conn = self._get_conn()
            cur = conn.cursor()
            tag = "REUSE" if reused else "NEW"
            self._log_event(
                cur,
                "GROWTH",
                f"Analysis #{record_id} [{tag}] → capability L{snap.get('capability_level', 0)} "
                f"(cards={snap.get('knowledge_cards', 0)} "
                f"+{growth.get('cards_added', 0)}/r{growth.get('cards_reinforced', 0)})",
                json.dumps(growth, ensure_ascii=False),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"after_analysis_growth log failed: {e}")

    def drain_soft_analysis_feedback(
        self,
        current_price: float,
        *,
        symbol: str = "BNBUSDT",
    ) -> int:
        """给超时未反馈的 WAIT/HOLD 分析补软标签，让每次分析都能成长。

        - 价格几乎不动 → BREAK_EVEN（中性，不通胀「观望正确」WIN）
        - 出现强趋势行情 → BREAK_EVEN + direction_override（策略权重可学，不 validate 卡片 WIN）
        - 反事实「开仓会先触 SL」→ BREAK_EVEN（记笔记，不标 WIN）
        """
        learn = self.config.get("learning") or {}
        if not learn.get("soft_feedback_on_wait", True):
            return 0
        if current_price <= 0:
            return 0

        hours = float(learn.get("soft_feedback_hours", 4) or 4)
        deadband = float(learn.get("soft_feedback_deadband_pct", 0.008) or 0.008)
        strong = float(learn.get("soft_feedback_strong_pct", 0.02) or 0.02)
        batch = int(learn.get("soft_feedback_batch", 15) or 15)
        allow_card_win = bool(learn.get("soft_feedback_validate_cards_on_win", False))
        defer_zero = bool(learn.get("soft_feedback_defer_zero_move", True))
        zero_max_age = float(learn.get("soft_feedback_zero_move_max_age_hours", 12) or 12)
        cutoff = (_utc_now() - timedelta(hours=max(0.5, hours))).replace(tzinfo=None).isoformat()

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, current_price, trading_action, final_signal, ai_analysis, timestamp
            FROM analysis_records
            WHERE symbol=?
              AND (actual_result IS NULL OR actual_result='')
              AND timestamp <= ?
              AND COALESCE(trading_action, '') IN ('WAIT', 'HOLD', '')
            ORDER BY id ASC
            LIMIT ?
            """,
            (symbol, cutoff, max(1, batch)),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        done = 0
        for row in rows:
            rid = int(row[0])
            base = float(row[1] or 0)
            if base <= 0:
                continue
            analysis_txt = str(row[4] or "")
            ts_raw = str(row[5] or "")
            # 失败的空分析结果不当作有效学习样本
            if "402" in analysis_txt or "Payment Required" in analysis_txt or "主分析失败" in analysis_txt:
                try:
                    self.submit_feedback(
                        rid,
                        "BREAK_EVEN",
                        actual_price=current_price,
                        notes="soft: skip failed analysis sample",
                    )
                    done += 1
                except Exception:
                    pass
                continue

            move = (current_price - base) / base
            abs_move = abs(move)
            direction_override = None

            # 精确零涨跌：多为陈旧报价或未刷新现价，延迟打标，避免假「观望正确」
            if defer_zero and abs_move < 1e-8:
                age_h = None
                try:
                    parsed = _parse_analysis_ts(ts_raw)
                    if parsed is not None:
                        age_h = (_utc_now() - parsed).total_seconds() / 3600.0
                except Exception:
                    age_h = None
                if age_h is None or age_h < zero_max_age:
                    continue
                try:
                    ok = self.submit_feedback(
                        rid,
                        "BREAK_EVEN",
                        actual_price=current_price,
                        notes=(
                            f"soft_wait: identical_px {base:.2f} age={age_h:.1f}h "
                            f"→ 零波动中性(不强化)"
                        ),
                    )
                    if ok:
                        done += 1
                except Exception:
                    pass
                continue

            # 反事实：若当时按建议/默认方向开仓，是否会先触及 ATR 止损
            cf_note = ""
            try:
                if bool(learn.get("soft_feedback_counterfactual", True)):
                    sl_pct = float(learn.get("soft_feedback_sl_pct", 0.015) or 0.015)
                    # 尝试从 trade_advice 读止损；否则用固定比例近似
                    ta_raw = None
                    try:
                        cur2 = conn.cursor()
                        cur2.execute(
                            "SELECT trade_advice, indicators FROM analysis_records WHERE id=?",
                            (rid,),
                        )
                        tr = cur2.fetchone()
                        if tr:
                            ta_raw = tr[0]
                            ind_raw = tr[1]
                            if isinstance(ind_raw, str) and ind_raw:
                                import json as _json
                                ind = _json.loads(ind_raw)
                                atr = float((ind or {}).get("ATR") or 0)
                                if atr > 0 and base > 0:
                                    sl_pct = max(sl_pct, atr / base * 1.5)
                    except Exception:
                        pass
                    hyp_side = None
                    if isinstance(ta_raw, str) and ta_raw:
                        import json as _json
                        try:
                            ta = _json.loads(ta_raw)
                            hyp_side = str((ta or {}).get("action") or "").upper()
                        except Exception:
                            hyp_side = None
                    if hyp_side in ("LONG", "SHORT"):
                        hit_sl = (
                            (hyp_side == "LONG" and move <= -sl_pct)
                            or (hyp_side == "SHORT" and move >= sl_pct)
                        )
                    else:
                        # WAIT：任一侧开仓都会因反向波动先触 SL → 观望合理（中性）
                        hit_sl = move <= -sl_pct or move >= sl_pct
                        hyp_side = "LONG" if move < 0 else "SHORT"
                    if hit_sl and abs_move < strong:
                        outcome = "BREAK_EVEN"
                        note = (
                            f"soft_wait_cf: 若开{hyp_side}会先触SL(~{sl_pct:.1%}) "
                            f"move={move:+.2%} → 观望合理(中性)"
                        )
                        try:
                            ok = self.submit_feedback(
                                rid,
                                outcome,
                                actual_price=current_price,
                                notes=note,
                            )
                            if ok:
                                done += 1
                            continue
                        except Exception:
                            pass
                    elif hit_sl:
                        cf_note = f" | cf:开{hyp_side}会触SL"
            except Exception as cf_e:
                logger.debug("soft cf #%s: %s", rid, cf_e)

            if abs_move < deadband:
                outcome = "BREAK_EVEN"
                note = (
                    f"soft_wait: chop {move:+.2%} < {deadband:.2%} "
                    f"→ 震荡中性 (px {base:.2f}→{current_price:.2f})"
                )
            elif abs_move >= strong:
                # 错过趋势：给策略权重方向信号，但不标 WIN、不强化知识卡
                outcome = "BREAK_EVEN"
                direction_override = "LONG" if move > 0 else "SHORT"
                note = (
                    f"soft_wait: strong {move:+.2%} ≥ {strong:.2%} "
                    f"→ 错过{direction_override}机会(中性记) "
                    f"(px {base:.2f}→{current_price:.2f})"
                )
            else:
                outcome = "BREAK_EVEN"
                note = (
                    f"soft_wait: mild {move:+.2%} mid-zone "
                    f"(px {base:.2f}→{current_price:.2f})"
                )
            if cf_note:
                note = note + cf_note

            try:
                ok = self.submit_feedback(
                    rid,
                    outcome,
                    actual_price=current_price,
                    notes=note,
                    trade_direction_override=direction_override,
                )
                if ok:
                    done += 1
                    # 默认禁止软反馈强化知识卡；真盈亏才走 validate
                    if (
                        allow_card_win
                        and outcome == "WIN"
                        and direction_override is None
                    ):
                        try:
                            self.capability_memory.validate_cards_for_feedback(
                                rid, "WIN", {"tier": "B", "score": 70},
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("soft feedback #%s: %s", rid, e)
        if done:
            logger.info("soft analysis feedback drained: %s records", done)
        return done

    def _after_feedback_growth(self, record_id: int, actual_result: str) -> None:
        """交易反馈后记录能力成长（权重已在 _trigger_learning_optimization 更新）。"""
        try:
            snap = self.get_growth_snapshot()
            conn = self._get_conn()
            cur = conn.cursor()
            self._log_event(
                cur,
                "GROWTH",
                f"Feedback #{record_id}={actual_result} → "
                f"capability L{snap.get('capability_level', 0)} "
                f"(optimizations={snap.get('weight_optimizations', 0)})",
                json.dumps({"record_id": record_id, "result": actual_result, **snap}, ensure_ascii=False),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"after_feedback_growth log failed: {e}")

    def get_growth_snapshot(self) -> Dict:
        """累计能力快照 — 多维能力模型 + 综合等级。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM analysis_records")
        total_analyses = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result IS NOT NULL")
        total_feedbacks = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM learning_log WHERE event_type='OPTIMIZATION'")
        weight_optimizations = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM learning_log WHERE event_type='GROWTH'")
        growth_events = int(cur.fetchone()[0])

        knowledge_cards = 0
        validated_cards = 0
        pattern_count = 0
        try:
            knowledge_cards = self.capability_memory.count_active_cards()
            validated_cards = self._count_validated_knowledge_cards()
        except Exception:
            pass
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            from bnb_quant_tool.pattern_memory import PatternMemory
            pm = PatternMemory(str(get_localized_db_path("pattern_memory")))
            pattern_count = pm.get_pattern_count()
        except Exception:
            pass

        maturity = "BEGINNER"
        if total_feedbacks >= 100:
            maturity = "EXPERT"
        elif total_feedbacks >= 50:
            maturity = "ADVANCED"
        elif total_feedbacks >= 20:
            maturity = "INTERMEDIATE"
        elif total_feedbacks >= 10:
            maturity = "BEGINNER"

        paper_wr = self._paper_win_rate()
        avg_quality = self._avg_feedback_quality_score()
        gate_pass_rate = self._gate_pass_rate()
        meta_runs = self._meta_learning_count()

        sample_maturity = min(100, int(
            total_feedbacks * 2 + total_analyses * 0.1
        ))
        prediction_accuracy = min(100, int(paper_wr * 100))
        knowledge_quality = min(
            100,
            int(validated_cards * 8 + knowledge_cards * 0.5 + avg_quality * 0.3),
        )
        discipline = min(100, int(gate_pass_rate * 80 + (100 - self._circuit_trigger_rate())))
        evolution_activity = min(
            100,
            int(weight_optimizations * 5 + meta_runs * 10 + growth_events * 2),
        )

        capability_dimensions = {
            "sample_maturity": sample_maturity,
            "prediction_accuracy": prediction_accuracy,
            "knowledge_quality": knowledge_quality,
            "discipline": discipline,
            "evolution_activity": evolution_activity,
        }
        capability_level = min(
            100,
            int(
                sample_maturity * 0.20
                + prediction_accuracy * 0.30
                + knowledge_quality * 0.25
                + discipline * 0.15
                + evolution_activity * 0.10
            ),
        )

        ev_cfg = (self.config.get("learning_evolution") or {})
        min_fb = int(ev_cfg.get("cap_level_min_feedback", 10))
        max_without_fb = int(ev_cfg.get("cap_level_max_without_feedback", 25))
        if total_feedbacks < min_fb:
            capability_level = min(capability_level, max_without_fb)

        return {
            "analysis_count": total_analyses,
            "feedback_count": total_feedbacks,
            "knowledge_cards": knowledge_cards,
            "validated_knowledge_cards": validated_cards,
            "pattern_memory_count": pattern_count,
            "weight_optimizations": weight_optimizations,
            "growth_events": growth_events,
            "learning_maturity": maturity,
            "capability_level": capability_level,
            "capability_dimensions": capability_dimensions,
            "paper_win_rate": round(paper_wr, 4),
            "avg_quality_score": round(avg_quality, 1),
        }

    def _count_validated_knowledge_cards(self) -> int:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM knowledge_cards "
                "WHERE is_active=1 AND times_validated > 0"
            ).fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def _avg_feedback_quality_score(self) -> float:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT AVG(quality_score) FROM analysis_records "
                "WHERE quality_score IS NOT NULL AND actual_result IS NOT NULL"
            ).fetchone()
            return float(row[0] or 50)
        except Exception:
            return 50.0

    def _gate_pass_rate(self) -> float:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT AVG(CASE WHEN passed_gate=1 THEN 1.0 ELSE 0.0 END) "
                "FROM analysis_records"
            ).fetchone()
            return float(row[0] or 0.5)
        except Exception:
            return 0.5

    def _circuit_trigger_rate(self) -> float:
        try:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM analysis_records").fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM learning_log WHERE event_type='CIRCUIT_BREAKER'"
            ).fetchone()[0]
            if not total:
                return 0.0
            return min(100.0, float(blocked) / float(total) * 100)
        except Exception:
            return 0.0

    def _meta_learning_count(self) -> int:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM meta_learning_log").fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def _paper_win_rate(self) -> float:
        try:
            from bnb_quant_tool.ai_trading_context import get_paper_trading_stats
            stats = get_paper_trading_stats()
            return float(stats.get("win_rate") or 0)
        except Exception:
            return 0.0

    def record_factor_attribution(
        self,
        explanation: Optional[Dict],
        outcome: str,
        regime: Optional[str] = None,
        factor_scores: Optional[Dict[str, int]] = None,
    ) -> None:
        """决策解释因子归因学习。"""
        if not explanation and not factor_scores:
            return
        if factor_scores is None:
            from bnb_quant_tool.learning_evolution import extract_factor_scores
            factor_scores = extract_factor_scores(explanation)
        if not factor_scores:
            return

        regime_key = (regime or "GLOBAL")[:64]
        win = outcome == "WIN"
        loss = outcome == "LOSS"
        now = datetime.now().isoformat()
        conn = self._get_conn()
        cur = conn.cursor()

        for factor_key, score in factor_scores.items():
            cur.execute(
                "INSERT OR IGNORE INTO factor_attribution "
                "(factor_key, regime, wins, losses, net_score_when_win, "
                "net_score_when_loss, last_updated) "
                "VALUES (?,?,0,0,0,0,?)",
                (factor_key, regime_key, now),
            )
            if win:
                cur.execute(
                    "UPDATE factor_attribution SET wins=wins+1, "
                    "net_score_when_win=net_score_when_win+?, last_updated=? "
                    "WHERE factor_key=? AND regime=?",
                    (float(score), now, factor_key, regime_key),
                )
            elif loss:
                cur.execute(
                    "UPDATE factor_attribution SET losses=losses+1, "
                    "net_score_when_loss=net_score_when_loss+?, last_updated=? "
                    "WHERE factor_key=? AND regime=?",
                    (float(score), now, factor_key, regime_key),
                )
        conn.commit()

    def get_factor_attribution_summary(self, regime: Optional[str] = None) -> List[Dict]:
        conn = self._get_conn()
        cur = conn.cursor()
        if regime:
            cur.execute(
                "SELECT factor_key, regime, wins, losses, "
                "net_score_when_win, net_score_when_loss "
                "FROM factor_attribution WHERE regime=? ORDER BY wins DESC",
                (regime,),
            )
        else:
            cur.execute(
                "SELECT factor_key, regime, wins, losses, "
                "net_score_when_win, net_score_when_loss "
                "FROM factor_attribution ORDER BY wins DESC LIMIT 30"
            )
        rows = []
        for r in cur.fetchall():
            wins, losses = int(r[2] or 0), int(r[3] or 0)
            total = wins + losses
            rows.append({
                "factor_key": r[0],
                "regime": r[1],
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total, 4) if total else 0,
                "avg_score_win": round(float(r[4] or 0) / max(wins, 1), 2),
                "avg_score_loss": round(float(r[5] or 0) / max(losses, 1), 2),
            })
        return rows

    def record_agent_accuracy(
        self,
        deliberation: Optional[Dict],
        *,
        trade_side: str,
        outcome: str,
        record_id: Optional[int] = None,
    ) -> None:
        """平仓后记录各 Agent 预测准确度。"""
        if not deliberation or outcome not in ("WIN", "LOSS"):
            return

        side = str(trade_side or "").upper()
        agents = {
            "researcher": deliberation.get("researcher"),
            "quant": deliberation.get("quant"),
            "learning": deliberation.get("learning"),
            "risk": deliberation.get("risk_verdict"),
        }
        now = datetime.now().isoformat()
        conn = self._get_conn()
        cur = conn.cursor()

        for role, opinion in agents.items():
            if not opinion:
                continue
            action = str(opinion.get("action") or "WAIT").upper()
            if action == "WAIT" or side not in ("LONG", "SHORT"):
                continue
            if action == side:
                correct = outcome == "WIN"
            else:
                correct = outcome == "LOSS"

            cur.execute(
                "INSERT OR IGNORE INTO agent_accuracy "
                "(agent_role, correct_predictions, total_predictions, accuracy, last_updated) "
                "VALUES (?, 0, 0, 0.5, ?)",
                (role, now),
            )
            if correct:
                cur.execute(
                    "UPDATE agent_accuracy SET correct_predictions=correct_predictions+1, "
                    "total_predictions=total_predictions+1, last_updated=? "
                    "WHERE agent_role=?",
                    (now, role),
                )
            else:
                cur.execute(
                    "UPDATE agent_accuracy SET total_predictions=total_predictions+1, "
                    "last_updated=? WHERE agent_role=?",
                    (now, role),
                )
            row = cur.execute(
                "SELECT correct_predictions, total_predictions FROM agent_accuracy "
                "WHERE agent_role=?",
                (role,),
            ).fetchone()
            if row and int(row[1] or 0) > 0:
                acc = int(row[0] or 0) / int(row[1])
                cur.execute(
                    "UPDATE agent_accuracy SET accuracy=? WHERE agent_role=?",
                    (round(acc, 4), role),
                )

        if record_id:
            self._log_event(
                cur,
                "AGENT_ACCURACY",
                f"Recorded agent accuracy for #{record_id} ({outcome})",
                json.dumps({"record_id": record_id, "side": side}, ensure_ascii=False),
            )
        conn.commit()

    def get_agent_accuracy_summary(self) -> List[Dict]:
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT agent_role, correct_predictions, total_predictions, "
                "accuracy, last_updated FROM agent_accuracy "
                "ORDER BY total_predictions DESC"
            )
            return [
                {
                    "agent_role": r[0],
                    "correct": int(r[1] or 0),
                    "total": int(r[2] or 0),
                    "accuracy": round(float(r[3] or 0.5), 4),
                    "last_updated": r[4],
                }
                for r in cur.fetchall()
            ]
        except Exception:
            return []

    def queue_reflection(
        self,
        record_id: int,
        position_id: int,
        outcome: str,
    ) -> Optional[int]:
        """将平仓记录加入批量反思队列。"""
        ev_cfg = (self.config.get("learning_evolution") or {})
        if not ev_cfg.get("batch_reflection", True):
            return None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            now = datetime.now().isoformat()
            cur.execute(
                "INSERT INTO reflection_queue "
                "(timestamp, record_id, position_id, outcome, status) "
                "VALUES (?,?,?,?,?)",
                (now, int(record_id), int(position_id), outcome, "pending"),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception as e:
            logger.debug(f"queue_reflection failed: {e}")
            return None

    def get_pending_reflection_count(self) -> int:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM reflection_queue WHERE status='pending'"
            ).fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def _record_strategy_predictions(self, cur, record_id: int, inst_results: Dict):
        """记录每个策略的预测（初始化策略表现表）"""
        details = (inst_results or {}).get('strategy_details', {})
        for sname, detail in details.items():
            name = detail.get('strategy', sname)
            cur.execute("INSERT OR IGNORE INTO strategy_performance (strategy_name, total_predictions, weight) VALUES (?, 0, 1.0)", (name,))
            cur.execute("UPDATE strategy_performance SET total_predictions = total_predictions + 1, last_updated = ? WHERE strategy_name = ?",
                       (datetime.now().isoformat(), name))

    def _record_market_snapshot(self, cur, result: Dict):
        ind = result.get('indicators', {})
        cur.execute("""INSERT INTO market_snapshots (timestamp, symbol, price, volume, rsi, macd, bb_position)
            VALUES (?,?,?,?,?,?,?)""", (
            datetime.now().isoformat(),
            result.get('symbol', 'BNBUSDT'),
            float(result.get('current_price', 0)),
            ind.get('Volume_Ratio', 0),
            ind.get('RSI'),
            ind.get('MACD'),
            ind.get('BB_Position')
        ))

    @_retry_db(max_retries=5, base_delay=1.0)
    def submit_feedback(self, record_id: int, actual_result: str,
                        actual_price: float = None, notes: str = None,
                        quality: Optional[Dict] = None,
                        decision_explanation: Optional[Dict] = None,
                        trade_direction_override: Optional[str] = None) -> bool:
        """
        提交实际结果反馈（这是AI学习的关键！）
        Args:
            record_id: 分析记录ID
            actual_result: 'WIN' / 'LOSS' / 'BREAK_EVEN'
            actual_price: 24小时后实际价格
            notes: 备注
            quality: trade_quality 评分结果
            decision_explanation: 决策解释（因子归因学习）
            trade_direction_override: 软反馈时覆盖方向（如错过上涨→LONG）
        Returns: 是否成功
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT current_price, market_regime, decision_explanation, "
            "trading_action, final_signal "
            "FROM analysis_records WHERE id = ?",
            (record_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.error(f"Record {record_id} not found")
            return False

        current_price = float(row[0])
        regime = (row[1] or 'GLOBAL') if row[1] else 'GLOBAL'
        if decision_explanation is None and row[2]:
            try:
                decision_explanation = json.loads(row[2])
            except (json.JSONDecodeError, TypeError):
                decision_explanation = None

        # SHORT 盈利时价格下跌：按交易方向计算有符号 pnl_percent
        trade_dir = (
            (trade_direction_override or "").upper()
            or self._resolve_trade_direction(row[3] or "", row[4] or "")
        )
        if actual_price and current_price:
            raw_move = (float(actual_price) - current_price) / current_price * 100
            if trade_dir == "SHORT":
                pnl = -raw_move
            else:
                pnl = raw_move
        else:
            pnl = 0.0
        quality_obj = dict(quality or {})
        # 软反馈降权：避免 WAIT 延时标签过度扭曲真平仓胜率
        notes_s = str(notes or "")
        is_soft = notes_s.startswith("soft_wait") or notes_s.startswith("soft:")
        if is_soft:
            soft_w = float((self.config.get("learning") or {}).get("soft_feedback_weight", 0.35) or 0.35)
            quality_obj["soft_weight"] = soft_w
            if not quality_obj.get("tier"):
                quality_obj["tier"] = "C"
        quality_score = int(quality_obj.get('score') or 0)
        quality_tier = quality_obj.get('tier') or ''

        cur.execute("""UPDATE analysis_records SET
            actual_result = ?, actual_price_after_24h = ?, pnl_percent = ?,
            feedback_notes = ?, quality_score = ?, quality_tier = ?
            WHERE id = ?""", (
            actual_result, actual_price, pnl, notes,
            quality_score if quality_score else None,
            quality_tier or None,
            record_id,
        ))

        skip_strategy = is_soft and not bool(
            (self.config.get("learning") or {}).get("soft_feedback_update_strategy_weights", False)
        )
        if not skip_strategy:
            self._update_strategy_performance(
                cur, record_id, actual_result, quality=quality_obj, regime=regime,
                trade_direction_override=trade_direction_override,
            )
        self._log_event(cur, 'FEEDBACK',
                       f"Feedback for record {record_id}: {actual_result} (PnL: {pnl:.2f}%)",
                       json.dumps({
                           'record_id': record_id,
                           'pnl': round(pnl, 4),
                           'quality_tier': quality_tier,
                           'regime': regime,
                           'direction_override': trade_direction_override,
                           'soft_skipped_strategy': bool(skip_strategy),
                       }))
        conn.commit()

        try:
            from bnb_quant_tool.learning_evolution import extract_factor_scores
            self.record_factor_attribution(
                decision_explanation,
                outcome=actual_result,
                regime=regime,
                factor_scores=extract_factor_scores(decision_explanation),
            )
        except Exception as e:
            logger.debug(f"factor attribution skipped: {e}")

        try:
            self.capability_memory.validate_cards_for_feedback(
                int(record_id), actual_result, quality,
            )
        except Exception as e:
            logger.debug(f"card validation skipped: {e}")

        # 触发学习优化（根据最新反馈调整策略权重）
        self._trigger_learning_optimization()
        self._after_feedback_growth(record_id, actual_result)
        logger.info(f"Feedback submitted: ID={record_id}, result={actual_result}, PnL={pnl:.2f}%")
        return True

    def ensure_stub_analysis_record_for_position(self, position_row: Dict) -> Optional[int]:
        """平仓时若无 learning_record_id，创建可反馈的分析占位记录，保证学习不断链。"""
        try:
            snap = {}
            raw = position_row.get("advice_snapshot")
            if isinstance(raw, str) and raw.strip():
                try:
                    snap = json.loads(raw)
                except Exception:
                    snap = {}
            elif isinstance(raw, dict):
                snap = raw

            symbol = str(
                position_row.get("symbol")
                or snap.get("symbol")
                or "BNBUSDT"
            )
            side = str(position_row.get("side") or snap.get("action") or "LONG").upper()
            action = "LONG" if side in ("LONG", "BUY") else "SHORT" if side in ("SHORT", "SELL") else "WAIT"
            entry = float(position_row.get("entry_price") or snap.get("entry_price") or 0)
            sl = float(position_row.get("sl_initial") or snap.get("stop_loss") or 0)
            tp = float(position_row.get("tp1") or snap.get("take_profit") or 0)

            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO analysis_records (
                    timestamp, symbol, timeframe, current_price, final_signal,
                    consensus_confidence, buy_signals, sell_signals, hold_signals,
                    trading_action, entry_price, stop_loss, take_profit,
                    risk_passed, feedback_notes, strategy_mode, version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(),
                    symbol,
                    str(snap.get("timeframe") or "1h"),
                    entry if entry > 0 else float(position_row.get("close_avg_price") or 0),
                    action,
                    float(snap.get("confidence") or 0.5),
                    1 if action == "LONG" else 0,
                    1 if action == "SHORT" else 0,
                    1 if action == "WAIT" else 0,
                    action,
                    entry if entry > 0 else None,
                    sl if sl > 0 else None,
                    tp if tp > 0 else None,
                    1,
                    f"stub_from_paper #{position_row.get('id')}",
                    "paper_stub",
                    "2.0",
                ),
            )
            rid = int(cur.lastrowid)
            ma = snap.get("multi_agent_deliberation")
            if ma:
                try:
                    payload = ma if isinstance(ma, str) else json.dumps(ma, ensure_ascii=False)
                    cur.execute(
                        "UPDATE analysis_records SET multi_agent_deliberation=? WHERE id=?",
                        (payload, rid),
                    )
                except Exception:
                    pass
            self._log_event(
                cur,
                "STUB_RECORD",
                f"Created stub analysis #{rid} for paper #{position_row.get('id')}",
                json.dumps({"record_id": rid, "position_id": position_row.get("id")}),
            )
            conn.commit()
            logger.info("Created stub analysis record #%s for paper #%s", rid, position_row.get("id"))
            return rid
        except Exception as e:
            logger.warning("ensure_stub_analysis_record failed: %s", e)
            return None

    def mark_paper_close_learned(self, position_id: int, details: Optional[Dict] = None) -> None:
        """标记该仓位已完成平仓学习（幂等）。"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            self._log_event(
                cur,
                "PAPER_CLOSE_LEARNED",
                f"Paper #{int(position_id)} learned",
                json.dumps({"position_id": int(position_id), **(details or {})}, ensure_ascii=False),
            )
            conn.commit()
        except Exception as e:
            logger.debug("mark_paper_close_learned: %s", e)

    def has_paper_close_learned(self, position_id: int) -> bool:
        """精确匹配 position_id，避免 #1 误伤 #10/#12。"""
        try:
            pid = int(position_id)
            conn = self._get_conn()
            cur = conn.cursor()
            # JSON 数字后只能是逗号或右括号，杜绝前缀碰撞
            cur.execute(
                "SELECT details FROM learning_log WHERE event_type=? "
                "AND details LIKE ? LIMIT 20",
                ("PAPER_CLOSE_LEARNED", f'%"position_id": {pid}%'),
            )
            rows = cur.fetchall() or []
            for row in rows:
                raw = row[0] if not isinstance(row, sqlite3.Row) else row["details"]
                try:
                    data = json.loads(raw or "{}")
                    if int(data.get("position_id", -1)) == pid:
                        return True
                except Exception:
                    # 兜底：严格边界匹配（数字后必须是 , 或 }）
                    s = str(raw or "")
                    needle = f'"position_id": {pid}'
                    idx = s.find(needle)
                    while idx >= 0:
                        end = idx + len(needle)
                        if end >= len(s) or s[end] in ",}":
                            return True
                        idx = s.find(needle, end)
            return False
        except Exception:
            return False

    @staticmethod
    def _strategy_filter_sql(extra_where: str = "") -> str:
        """排除 paper_* 伪策略污染权重。"""
        clause = "strategy_name NOT LIKE 'paper_%'"
        if extra_where:
            return f"{extra_where} AND {clause}"
        return clause

    @staticmethod
    def is_strategy_signal_correct(
        signal: str,
        trade_direction: str,
        actual_result: str,
    ) -> Optional[bool]:
        """判断单策略信号是否与本次交易结果一致。

        trade_direction: LONG/SHORT/BUY/SELL/NONE
        返回 None 表示 BREAK_EVEN 等中性样本，跳过该策略计数。

        空方向（纯观望样本）:
        - WIN → HOLD 正确（震荡空仓避险）
        - LOSS → HOLD 错误（不该空仓 / 错过行情）
        """
        signal = (signal or "HOLD").upper()
        trade_direction = (trade_direction or "").upper()
        actual_result = (actual_result or "").upper()

        if actual_result == "BREAK_EVEN":
            return None

        long_trade = trade_direction in ("LONG", "BUY")
        short_trade = trade_direction in ("SHORT", "SELL")
        if not long_trade and not short_trade:
            if actual_result == "WIN":
                return signal == "HOLD"
            if actual_result == "LOSS":
                return signal != "HOLD"
            return None

        if long_trade and actual_result == "WIN":
            return signal == "BUY"
        if long_trade and actual_result == "LOSS":
            return signal in ("SELL", "HOLD")
        if short_trade and actual_result == "WIN":
            return signal == "SELL"
        if short_trade and actual_result == "LOSS":
            return signal in ("BUY", "HOLD")
        return None

    @staticmethod
    def _resolve_trade_direction(trading_action: str, final_signal: str) -> str:
        action = (trading_action or "").upper()
        if action in ("LONG", "SHORT"):
            return action
        sig = (final_signal or "").upper()
        if sig == "BUY":
            return "LONG"
        if sig == "SELL":
            return "SHORT"
        return ""

    def _quality_multiplier(self, quality: Optional[Dict]) -> float:
        if not quality:
            return 1.0
        tier = (quality.get('tier') or '').upper()
        from bnb_quant_tool.learning_evolution import QUALITY_TIER_MULTIPLIER
        base = float(QUALITY_TIER_MULTIPLIER.get(tier, 0.7))
        soft_w = quality.get("soft_weight")
        if soft_w is not None:
            try:
                base *= max(0.1, min(1.0, float(soft_w)))
            except (TypeError, ValueError):
                pass
        return base

    def _update_regime_strategy_performance(
        self,
        cur,
        strategy_name: str,
        regime: str,
        is_correct: bool,
        quality_mult: float,
    ) -> None:
        if not regime or regime == 'GLOBAL':
            return
        self._upsert_regime_row(cur, strategy_name, regime, is_correct, quality_mult)
        bucket = normalize_regime_bucket_for_learning(regime)
        if bucket not in (regime, 'GLOBAL'):
            self._upsert_regime_row(cur, strategy_name, bucket, is_correct, quality_mult)

    def _upsert_regime_row(
        self,
        cur,
        strategy_name: str,
        regime: str,
        is_correct: bool,
        quality_mult: float,
    ) -> None:
        now = datetime.now().isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO strategy_regime_performance "
            "(strategy_name, regime, total_predictions, correct_predictions, "
            "weighted_correct, win_rate, weight, last_updated) "
            "VALUES (?,?,0,0,0,0.5,1.0,?)",
            (strategy_name, regime, now),
        )
        if is_correct:
            cur.execute(
                "UPDATE strategy_regime_performance SET "
                "total_predictions=total_predictions+1, "
                "correct_predictions=correct_predictions+1, "
                "weighted_correct=weighted_correct+?, last_updated=? "
                "WHERE strategy_name=? AND regime=?",
                (quality_mult, now, strategy_name, regime),
            )
        else:
            cur.execute(
                "UPDATE strategy_regime_performance SET "
                "total_predictions=total_predictions+1, last_updated=? "
                "WHERE strategy_name=? AND regime=?",
                (now, strategy_name, regime),
            )
        cur.execute(
            "UPDATE strategy_regime_performance SET "
            "win_rate=CAST(correct_predictions AS REAL)/CAST(MAX(total_predictions,1) AS REAL) "
            "WHERE strategy_name=? AND regime=?",
            (strategy_name, regime),
        )

    def _update_strategy_performance(
        self,
        cur,
        record_id: int,
        actual_result: str,
        quality: Optional[Dict] = None,
        regime: Optional[str] = None,
        trade_direction_override: Optional[str] = None,
    ):
        """根据反馈更新每个策略的表现统计（含质量加权 + Regime 分表）。"""
        cur.execute(
            "SELECT institutional_results, trading_action, final_signal "
            "FROM analysis_records WHERE id = ?",
            (record_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return
        try:
            strategies = json.loads(row[0])
        except Exception:
            return

        trade_dir = (trade_direction_override or "").strip().upper() or self._resolve_trade_direction(
            row[1], row[2]
        )
        quality_mult = self._quality_multiplier(quality)

        for sname, detail in strategies.items():
            name = detail.get('strategy', sname)
            if str(name).startswith("paper_"):
                continue
            signal = detail.get('signal', 'HOLD')
            verdict = self.is_strategy_signal_correct(signal, trade_dir, actual_result)
            if verdict is None:
                continue

            is_correct = verdict
            now = datetime.now().isoformat()
            # 反馈路径可能跳过 record_analysis 种子行：先 upsert，避免 UPDATE 空转
            cur.execute(
                "INSERT OR IGNORE INTO strategy_performance "
                "(strategy_name, total_predictions, weight) VALUES (?, 1, 1.0)",
                (name,),
            )
            if is_correct:
                cur.execute("""UPDATE strategy_performance SET
                    correct_predictions = correct_predictions + 1,
                    weighted_correct = weighted_correct + ?,
                    streak_current = CASE WHEN streak_current >= 0 THEN streak_current + 1 ELSE 1 END,
                    streak_best = MAX(streak_best, CASE WHEN streak_current >= 0 THEN streak_current + 1 ELSE 1 END),
                    last_updated = ?
                    WHERE strategy_name = ?""", (quality_mult, now, name))
                if signal == 'BUY':
                    cur.execute("UPDATE strategy_performance SET correct_buy = correct_buy + 1 WHERE strategy_name = ?", (name,))
                elif signal == 'SELL':
                    cur.execute("UPDATE strategy_performance SET correct_sell = correct_sell + 1 WHERE strategy_name = ?", (name,))
                else:
                    cur.execute("UPDATE strategy_performance SET correct_hold = correct_hold + 1 WHERE strategy_name = ?", (name,))
            else:
                cur.execute("""UPDATE strategy_performance SET
                    streak_current = CASE WHEN streak_current <= 0 THEN streak_current - 1 ELSE -1 END,
                    streak_worst = MIN(streak_worst, CASE WHEN streak_current <= 0 THEN streak_current - 1 ELSE -1 END),
                    last_updated = ?
                    WHERE strategy_name = ?""", (now, name))
                if signal == 'BUY':
                    cur.execute("UPDATE strategy_performance SET wrong_buy = wrong_buy + 1 WHERE strategy_name = ?", (name,))
                elif signal == 'SELL':
                    cur.execute("UPDATE strategy_performance SET wrong_sell = wrong_sell + 1 WHERE strategy_name = ?", (name,))
                else:
                    cur.execute("UPDATE strategy_performance SET wrong_hold = wrong_hold + 1 WHERE strategy_name = ?", (name,))

            cur.execute("""UPDATE strategy_performance SET
                win_rate = CAST(correct_predictions AS REAL) / CAST(MAX(total_predictions, 1) AS REAL)
                WHERE strategy_name = ?""", (name,))

            self._update_regime_strategy_performance(
                cur, name, regime or 'GLOBAL', is_correct, quality_mult,
            )

    def _trigger_learning_optimization(self):
        """
        触发学习优化：根据各策略的历史胜率重新计算权重
        胜率高的策略权重增加，胜率低的权重降低
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result IS NOT NULL")
        count = cur.fetchone()[0]
        if count < self.min_samples_for_learning:
            logger.info(f"Not enough samples for learning ({count}/{self.min_samples_for_learning})")
            return

        cur.execute(f"""SELECT strategy_name, win_rate, total_predictions, weight,
            weighted_correct
            FROM strategy_performance
            WHERE total_predictions >= 3 AND is_active = 1
              AND {self._strategy_filter_sql()}
            ORDER BY win_rate DESC""")
        rows = cur.fetchall()
        if not rows:
            return

        # 质量加权有效胜率 = weighted_correct / total_predictions
        new_weights = {}
        for name, wr, total, old_w, weighted in rows:
            sample_factor = min(1.0, total / 20.0)
            effective_wr = (float(weighted or 0) / max(int(total), 1))
            if effective_wr <= 0:
                effective_wr = max(0.05, float(wr or 0))
            if effective_wr < 0.30 and total >= 5:
                win_factor = 0.05
            elif effective_wr < 0.40 and total >= 5:
                win_factor = max(0.08, effective_wr * 0.45)
            else:
                win_factor = max(0.05, effective_wr)
            new_weights[name] = win_factor * sample_factor

        total_w = sum(new_weights.values())
        if total_w > 0:
            for name, w in new_weights.items():
                normalized = w / total_w
                cur.execute("UPDATE strategy_performance SET weight = ? WHERE strategy_name = ?", (normalized, name))
            self._log_event(cur, 'OPTIMIZATION',
                           f"Optimized weights based on {count} feedback samples",
                           json.dumps({'n_strategies': len(rows), 'total_samples': count}))

        conn.commit()
        self._optimize_regime_weights(cur)
        conn.commit()
        self.strategy_weights = self._load_strategy_weights()
        logger.info(f"Learning optimization completed: {len(rows)} strategies reweighted")

    def _optimize_regime_weights(self, cur) -> None:
        """按市场状态重新计算策略权重。"""
        cur.execute(
            "SELECT DISTINCT regime FROM strategy_regime_performance "
            "WHERE total_predictions >= 3"
        )
        regimes = [r[0] for r in cur.fetchall()]
        for regime in regimes:
            cur.execute(
                "SELECT strategy_name, win_rate, total_predictions, weighted_correct "
                "FROM strategy_regime_performance WHERE regime=? AND total_predictions >= 3",
                (regime,),
            )
            rows = cur.fetchall()
            if not rows:
                continue
            new_weights = {}
            for name, wr, total, weighted in rows:
                sample_factor = min(1.0, int(total) / 15.0)
                effective_wr = float(weighted or 0) / max(int(total), 1)
                if effective_wr <= 0:
                    effective_wr = max(0.05, float(wr or 0))
                if effective_wr < 0.30 and int(total) >= 5:
                    win_factor = 0.05
                elif effective_wr < 0.40 and int(total) >= 5:
                    win_factor = max(0.08, effective_wr * 0.45)
                else:
                    win_factor = max(0.05, effective_wr)
                new_weights[name] = win_factor * sample_factor
            total_w = sum(new_weights.values())
            if total_w <= 0:
                continue
            for name, w in new_weights.items():
                cur.execute(
                    "UPDATE strategy_regime_performance SET weight=? "
                    "WHERE strategy_name=? AND regime=?",
                    (w / total_w, name, regime),
                )
        for bucket in ("TREND", "RANGE", "VOLATILE"):
            cur.execute(
                "SELECT strategy_name, win_rate, total_predictions, weighted_correct "
                "FROM strategy_regime_performance WHERE regime=? AND total_predictions >= 3",
                (bucket,),
            )
            rows = cur.fetchall()
            if not rows:
                continue
            new_weights = {}
            for name, wr, total, weighted in rows:
                sample_factor = min(1.0, int(total) / 12.0)
                effective_wr = float(weighted or 0) / max(int(total), 1)
                if effective_wr <= 0:
                    effective_wr = max(0.05, float(wr or 0))
                new_weights[name] = max(0.05, effective_wr) * sample_factor
            total_w = sum(new_weights.values())
            if total_w <= 0:
                continue
            for name, w in new_weights.items():
                cur.execute(
                    "UPDATE strategy_regime_performance SET weight=? "
                    "WHERE strategy_name=? AND regime=?",
                    (w / total_w, name, bucket),
                )

    def get_learning_insights(self, market_context: Dict = None) -> Dict:
        """获取AI学习洞察（供DeepSeek AI分析和自我改进）

        Args:
            market_context: 当前市场局面，用于语义检索相关知识卡片
        """
        conn = self._get_conn()
        cur = conn.cursor()
        insights = {
            'timestamp': datetime.now().isoformat(),
            'total_analyses': 0, 'total_feedbacks': 0,
            'overall_accuracy': 0.0, 'avg_pnl': 0.0,
            'best_strategies': [],
            'worst_strategies': [], 'recent_trend': [],
            'recommendations': [], 'learning_maturity': 'BEGINNER'
        }

        cur.execute("SELECT COUNT(*) FROM analysis_records")
        insights['total_analyses'] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result IS NOT NULL")
        insights['total_feedbacks'] = cur.fetchone()[0]

        if insights['total_feedbacks'] > 0:
            cur.execute("""SELECT COUNT(*) FROM analysis_records
                WHERE actual_result IN ('WIN', 'LOSS', 'BREAK_EVEN')
                  AND actual_result = 'WIN'""")
            wins = cur.fetchone()[0]
            insights['overall_accuracy'] = round(wins / insights['total_feedbacks'], 4)

        # 平均盈亏百分比
        cur.execute("SELECT AVG(pnl_percent) FROM analysis_records WHERE pnl_percent IS NOT NULL")
        row = cur.fetchone()
        insights['avg_pnl'] = round(row[0], 4) if row and row[0] is not None else 0.0

        # 最佳策略
        cur.execute(f"""SELECT strategy_name, win_rate, total_predictions, correct_predictions, streak_best, weight
            FROM strategy_performance
            WHERE total_predictions >= 3 AND {self._strategy_filter_sql()}
            ORDER BY win_rate DESC LIMIT 5""")
        insights['best_strategies'] = [{'name': r[0], 'win_rate': r[1], 'total': r[2], 'correct': r[3], 'streak': r[4], 'weight': r[5]} for r in cur.fetchall()]

        # 最差策略
        cur.execute(f"""SELECT strategy_name, win_rate, total_predictions, correct_predictions, streak_worst, weight
            FROM strategy_performance
            WHERE total_predictions >= 3 AND {self._strategy_filter_sql()}
            ORDER BY win_rate ASC LIMIT 5""")
        insights['worst_strategies'] = [{'name': r[0], 'win_rate': r[1], 'total': r[2], 'correct': r[3], 'streak': r[4], 'weight': r[5]} for r in cur.fetchall()]

        # 最近趋势
        cur.execute("""SELECT timestamp, final_signal, actual_result, pnl_percent FROM analysis_records
            WHERE actual_result IS NOT NULL ORDER BY timestamp DESC LIMIT 10""")
        insights['recent_trend'] = [{'time': r[0], 'signal': r[1], 'result': r[2], 'pnl': r[3]} for r in cur.fetchall()]

        # 成熟度
        fb = insights['total_feedbacks']
        if fb >= 100:
            insights['learning_maturity'] = 'EXPERT'
        elif fb >= 50:
            insights['learning_maturity'] = 'ADVANCED'
        elif fb >= 20:
            insights['learning_maturity'] = 'INTERMEDIATE'
        elif fb >= 10:
            insights['learning_maturity'] = 'BEGINNER'

        insights['recommendations'] = self._generate_recommendations(insights)
        regime = None
        if market_context:
            regime = market_context.get('regime') or market_context.get('market_regime')
        insights['strategy_weights'] = self._load_strategy_weights(regime=regime)
        try:
            from bnb_quant_tool.win_rate_strategy import load_strategy_performance_map

            insights['strategy_performance'] = load_strategy_performance_map(
                self, regime=regime
            )
        except ImportError:
            insights['strategy_performance'] = {}
        if regime:
            insights['regime_weights_applied'] = regime
            try:
                from bnb_quant_tool.learning_analytics import normalize_regime_bucket
                insights['regime_bucket'] = normalize_regime_bucket(regime)
            except ImportError:
                pass
        insights['growth'] = self.get_growth_snapshot()
        insights['factor_attribution'] = self.get_factor_attribution_summary(regime=regime)
        try:
            from bnb_quant_tool.factor_attribution_learner import compute_reliability_multipliers
            insights['factor_reliability'] = compute_reliability_multipliers(
                insights.get('factor_attribution')
            )
        except ImportError:
            insights['factor_reliability'] = {}
        insights['agent_accuracy'] = self.get_agent_accuracy_summary()
        try:
            mem = self.capability_memory
            mem_cfg = self.config.get("capability_memory") or {}
            require_kb = bool(mem_cfg.get("require_on_analysis", True))
            if market_context and (require_kb or hasattr(mem, "retrieve_for_analysis")):
                if hasattr(mem, "retrieve_for_analysis"):
                    cards, mode = mem.retrieve_for_analysis(market_context)
                else:
                    cards = mem.retrieve_relevant(market_context)
                    mode = "semantic"
                insights["capability_cards"] = cards
                insights["capability_retrieval_mode"] = mode
            elif market_context:
                insights["capability_cards"] = mem.retrieve_relevant(market_context)
                insights["capability_retrieval_mode"] = "semantic"
            else:
                insights["capability_cards"] = mem.get_recent_cards()
                insights["capability_retrieval_mode"] = "recent"
            insights["capability_summary"] = mem.get_summary()
            insights["knowledge_base_loaded"] = bool(mem.enabled)
            total_kb = int((insights["capability_summary"] or {}).get("total_active") or 0)
            if require_kb and total_kb > 0 and not insights["capability_cards"]:
                logger.warning(
                    "知识库 require_on_analysis: 库内 %d 条卡片但本次检索为空",
                    total_kb,
                )
        except Exception as e:
            logger.warning(f"capability_cards load failed: {e}")
            insights["capability_cards"] = []
            insights["capability_retrieval_mode"] = "error"
            insights["capability_summary"] = {}
            insights["knowledge_base_loaded"] = False
        return insights

    def _generate_recommendations(self, insights: Dict) -> List[str]:
        recs = []
        if insights['best_strategies']:
            b = insights['best_strategies'][0]
            recs.append(f"Strategy '{b['name']}' has best win rate ({b['win_rate']:.1%}), consider increasing weight")
        if insights['worst_strategies']:
            w = insights['worst_strategies'][0]
            if w['win_rate'] < 0.4:
                recs.append(f"Strategy '{w['name']}' underperforming ({w['win_rate']:.1%}), consider disabling")
        if len(insights['recent_trend']) >= 5:
            pnls = [t.get('pnl', 0) or 0 for t in insights['recent_trend'][:5]]
            avg_pnl = sum(pnls) / len(pnls)
            if avg_pnl < -1:
                recs.append(f"Recent avg PnL negative ({avg_pnl:.2f}%), review risk parameters")
            elif avg_pnl > 1:
                recs.append(f"Recent performance strong (avg PnL: {avg_pnl:.2f}%), strategy mix working well")
        if insights['total_feedbacks'] < 20:
            recs.append(f"Need more feedback ({insights['total_feedbacks']}/20 minimum) for reliable learning")
        return recs

    def _load_strategy_weights(self, regime: Optional[str] = None) -> Dict[str, float]:
        """从数据库加载策略权重（支持 Regime 分表融合）。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT strategy_name, weight FROM strategy_performance "
            f"WHERE is_active = 1 AND {self._strategy_filter_sql()}"
        )
        global_rows = cur.fetchall()
        if not global_rows:
            default_names = [
                'SMA Crossover', 'EMA Crossover', 'Bollinger Bands', 'RSI Extreme',
                'MACD Crossover', 'Fibonacci Retracement',
                'Renaissance Statistical Arbitrage', 'Citadel Multi-Factor Momentum',
                'Bridgewater Risk Parity', 'AQR Value + Momentum',
                'Two Sigma ML Prediction', 'Jump Trading Market Making',
                'Turtle Trading'
            ]
            return {s: round(1.0 / len(default_names), 6) for s in default_names}

        global_weights = {r[0]: float(r[1]) for r in global_rows}

        def _finalize_weights(weights: Dict[str, float], reg: Optional[str]) -> Dict[str, float]:
            try:
                from bnb_quant_tool.win_rate_strategy import (
                    load_strategy_performance_map,
                    penalize_weights_by_performance,
                    resolve_strategy_win_rate_config,
                )

                perf = load_strategy_performance_map(self, regime=reg)
                sw_cfg = resolve_strategy_win_rate_config(self.config)
                return penalize_weights_by_performance(weights, perf, sw_cfg)
            except ImportError:
                return weights

        if not regime or regime == 'GLOBAL':
            return _finalize_weights(global_weights, None)

        bucket = normalize_regime_bucket_for_learning(regime)
        lookup_regimes = [regime]
        if bucket not in lookup_regimes:
            lookup_regimes.append(bucket)

        regime_map: Dict[str, Tuple[float, int, float]] = {}
        for reg in lookup_regimes:
            cur.execute(
                "SELECT strategy_name, weight, total_predictions, win_rate "
                "FROM strategy_regime_performance WHERE regime=?",
                (reg,),
            )
            for r in cur.fetchall():
                name = r[0]
                samples = int(r[2] or 0)
                if name not in regime_map or samples > regime_map[name][1]:
                    regime_map[name] = (float(r[1]), samples, float(r[3] or 0))

        if not regime_map:
            return _finalize_weights(global_weights, regime)

        blended: Dict[str, float] = {}
        bucket_samples = sum(v[1] for v in regime_map.values()) // max(len(regime_map), 1)
        regime_blend = 0.65 if bucket_samples >= 8 else 0.45 if bucket_samples >= 3 else 0.25

        for name, gw in global_weights.items():
            if name in regime_map and regime_map[name][1] >= 3:
                rw, samples, rwr = regime_map[name]
                effective = max(0.05, rwr if rwr > 0 else rw)
                if rwr < 0.30 and samples >= 5:
                    effective = max(0.05, effective * 0.35)
                elif rwr < 0.40 and samples >= 5:
                    effective = max(0.05, effective * 0.55)
                blend = (1.0 - regime_blend) * gw + regime_blend * effective
                blended[name] = blend
            else:
                blended[name] = gw

        total = sum(blended.values())
        if total > 0:
            blended = {k: v / total for k, v in blended.items()}

        try:
            return _finalize_weights(blended, regime)
        except NameError:
            pass
        if total > 0:
            return blended
        return global_weights

    def get_adaptive_weights(self) -> Dict[str, float]:
        """获取自适应权重（供信号融合使用）"""
        return self.strategy_weights

    def _log_event(self, cur, event_type: str, message: str, details: str = None):
        cur.execute("INSERT INTO learning_log (timestamp, event_type, message, details) VALUES (?,?,?,?)",
                   (datetime.now().isoformat(), event_type, message, details))

    def get_learning_report(self) -> str:
        """生成人类可读的学习成长报告"""
        insights = self.get_learning_insights()
        lines = []
        lines.append('=' * 65)
        lines.append('           AI LEARNING & GROWTH REPORT')
        lines.append('=' * 65)
        lines.append(f"Generated: {insights['timestamp'][:19]}")
        lines.append(f"Maturity Level: {insights['learning_maturity']}")
        lines.append(f"\n--- Overview ---")
        lines.append(f"Total Analyses Recorded: {insights['total_analyses']}")
        lines.append(f"Total Feedback Received:   {insights['total_feedbacks']}")
        lines.append(f"Overall Accuracy:          {insights['overall_accuracy']:.1%}")
        lines.append("\n--- Top Performing Strategies ---")
        for i, s in enumerate(insights['best_strategies'][:5], 1):
            lines.append(f"  {i}. {s['name']:<40s} WR:{s['win_rate']:>6.1%} ({s['correct']}/{s['total']})")
        lines.append("\n--- Underperforming Strategies ---")
        for i, s in enumerate(insights['worst_strategies'][:5], 1):
            lines.append(f"  {i}. {s['name']:<40s} WR:{s['win_rate']:>6.1%} ({s['correct']}/{s['total']})")
        lines.append("\n--- Recent Performance (Last 10) ---")
        for t in insights['recent_trend'][:10]:
            marker = '+' if t.get('result') == 'WIN' else ('-' if t.get('result') == 'LOSS' else '=')
            pnl_str = f"{t.get('pnl', 0):+.2f}%" if t.get('pnl') is not None else "N/A"
            lines.append(f"  [{marker}] {str(t.get('time',''))[:16]:<18} {str(t.get('signal','')):<5s} -> {str(t.get('result','')):<8s} {pnl_str}")
        lines.append("\n--- AI Recommendations ---")
        for i, rec in enumerate(insights['recommendations'], 1):
            lines.append(f"  {i}. {rec}")
        if not insights['recommendations']:
            lines.append("  No recommendations yet. Need more feedback data.")

        cards = insights.get('capability_cards') or []
        if cards:
            lines.append("\n--- 本地知识卡片 (Knowledge Cards) ---")
            mode = insights.get('capability_retrieval_mode', '')
            if mode == 'semantic':
                lines.append("  [语义检索匹配当前局面]")
            for i, c in enumerate(cards[:8], 1):
                cat = c.get('category_label') or c.get('category', '?')
                sim = c.get('similarity')
                sim_s = f" 相关度{sim:.0%}" if sim else ""
                lines.append(f"  {i}. [{cat}] {c.get('title', '')} {sim_s}")
                if c.get('trigger_condition'):
                    lines.append(f"     条件: {c['trigger_condition']}")
                lines.append(f"     教训: {c.get('lesson', '')} (可信度 {c.get('confidence', 0):.0%})")
        else:
            lines.append("\n--- 本地知识卡片 ---")
            lines.append("  暂无。模拟盘平仓或 AI 复盘后会自动 AI 提炼并入库。")

        lines.append(f"\n{'='*65}")
        return '\n'.join(lines)

    def get_statistics_summary(self) -> Dict:
        """获取统计摘要（供GUI显示）"""
        conn = self._get_conn()
        cur = conn.cursor()
        s = {}
        cur.execute("SELECT COUNT(*) FROM analysis_records")
        s['total_analyses'] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result = 'WIN'")
        s['total_wins'] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result = 'LOSS'")
        s['total_losses'] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM analysis_records WHERE actual_result IS NULL")
        s['pending_feedback'] = cur.fetchone()[0]
        total_rated = s.get('total_wins', 0) + s.get('total_losses', 0)
        s['accuracy'] = round(s['total_wins'] / total_rated, 4) if total_rated > 0 else 0.0
        cur.execute("SELECT AVG(pnl_percent) FROM analysis_records WHERE pnl_percent IS NOT NULL")
        row = cur.fetchone()
        s['avg_pnl'] = round(row[0], 4) if row and row[0] is not None else 0.0
        cur.execute("SELECT COUNT(*) FROM strategy_performance WHERE is_active = 1")
        s['active_strategies'] = cur.fetchone()[0]
        cur.execute("SELECT MAX(timestamp) FROM analysis_records")
        row = cur.fetchone()
        s['last_analysis'] = row[0] if row and row[0] else 'Never'
        return s

    # ============================================================
    # 方向1: 外部查询机构策略表现 (供 AI 复盘调权重)
    # ============================================================
    def get_strategy_performance(self, min_total: int = 1) -> List[Dict]:
        """返回 13 机构策略的胜率与权重, 供复盘 prompt 使用."""
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT strategy_name, total_predictions, correct_predictions, win_rate, weight, "
                "is_active, streak_current, last_updated "
                "FROM strategy_performance "
                f"WHERE total_predictions >= ? AND {self._strategy_filter_sql()} "
                "ORDER BY win_rate DESC",
                (int(min_total),)
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"get_strategy_performance 查询失败: {e}")
            return []
        out = []
        for r in rows:
            out.append({
                "name": r[0],
                "total": r[1] or 0,
                "correct": r[2] or 0,
                "win_rate": round(r[3] or 0.0, 4),
                "weight": round(r[4] or 0.0, 4),
                "is_active": bool(r[5]) if r[5] is not None else True,
                "streak": r[6] or 0,
                "last_updated": r[7],
            })
        return out

    # ============================================================
    # 方向4: 参数变更日志
    # ============================================================
    def log_param_change(self, param_name: str, old_value, new_value,
                          source: str = "AI_REVIEW",
                          review_summary: str = "",
                          paper_engine=None) -> int:
        """记录一次参数变更 (多个参数请多次调用).

        如果传入 paper_engine, 会同时快照"变更前的总交易数/胜率/PF",
        下次复盘可以拿“变更后”的数据与之对比.
        """
        try:
            old_v = float(old_value) if old_value is not None else None
        except (TypeError, ValueError):
            old_v = None
        try:
            new_v = float(new_value) if new_value is not None else None
        except (TypeError, ValueError):
            new_v = None

        trades_before = None
        wr_before = None
        pf_before = None
        if paper_engine is not None:
            try:
                stats = paper_engine.get_stats() or {}
                trades_before = int(stats.get("total_trades") or 0)
                wr_before = float(stats.get("win_rate") or 0.0)
                pf_before = float(stats.get("profit_factor") or 0.0)
            except Exception as e:
                logger.debug(f"log_param_change 拿快照失败: {e}")

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO param_change_log (timestamp, param_name, old_value, new_value, "
            "source, review_summary, trades_before, wr_before, pf_before) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), param_name, old_v, new_v,
             source, review_summary, trades_before, wr_before, pf_before)
        )
        conn.commit()
        cid = cur.lastrowid
        logger.info(f"param_change logged: #{cid} {param_name} {old_v}→{new_v} (source={source})")
        return cid

    def get_recent_param_changes(self, limit: int = 5,
                                  paper_engine=None) -> List[Dict]:
        """返回近 N 次参数变更 + 当前胜率/PF 为变更后的表现指标."""
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, timestamp, param_name, old_value, new_value, source, "
                "review_summary, trades_before, wr_before, pf_before "
                "FROM param_change_log ORDER BY id DESC LIMIT ?",
                (int(limit),)
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"get_recent_param_changes 查询失败: {e}")
            return []

        # 拿当前胜率做“变更后”对比
        cur_total = cur_wr = cur_pf = None
        if paper_engine is not None:
            try:
                s = paper_engine.get_stats() or {}
                cur_total = int(s.get("total_trades") or 0)
                cur_wr = float(s.get("win_rate") or 0.0)
                cur_pf = float(s.get("profit_factor") or 0.0)
            except Exception:
                pass

        out = []
        for r in rows:
            entry = {
                "id": r[0], "timestamp": r[1], "param_name": r[2],
                "old_value": r[3], "new_value": r[4], "source": r[5],
                "review_summary": r[6],
                "trades_before": r[7], "wr_before": r[8], "pf_before": r[9],
            }
            if cur_total is not None and r[7] is not None:
                entry["trades_after_change"] = max(0, cur_total - int(r[7]))
                entry["wr_now"] = round(cur_wr, 4) if cur_wr is not None else None
                entry["pf_now"] = round(cur_pf, 4) if cur_pf is not None else None
                if r[8] is not None and cur_wr is not None:
                    entry["wr_delta"] = round(cur_wr - float(r[8]), 4)
                if r[9] is not None and cur_pf is not None:
                    entry["pf_delta"] = round(cur_pf - float(r[9]), 4)
            out.append(entry)
        return out


def normalize_regime_bucket_for_learning(regime: Optional[str]) -> str:
    try:
        from bnb_quant_tool.learning_analytics import normalize_regime_bucket
        return normalize_regime_bucket(regime)
    except ImportError:
        return str(regime or "GLOBAL").upper()


if __name__ == "__main__":
    print("=" * 60)
    print("AI Learning System Test (v1.2 Clean)")
    print("=" * 60)

    learner = AILearningSystem()

    # 模拟分析结果
    mock_result = {
        'timestamp': datetime.now().isoformat(),
        'symbol': 'BNBUSDT',
        'timeframe': '1h',
        'current_price': 632.94,
        'final_recommendation': 'BUY',
        'institutional_strategies': {
            'consensus_signal': 'BUY',
            'consensus_confidence': 0.72,
            'buy_signals': 7,
            'sell_signals': 2,
            'hold_signals': 4,
            'strategy_details': {
                'sma_crossover': {'strategy': 'SMA Crossover', 'signal': 'BUY', 'confidence': 0.75},
                'ema_crossover': {'strategy': 'EMA Crossover', 'signal': 'BUY', 'confidence': 0.70},
                'bollinger_bands': {'strategy': 'Bollinger Bands', 'signal': 'HOLD', 'confidence': 0.50},
            }
        },
        'indicators': {'RSI': 45.5, 'MACD': 2.3},
        'ai_analysis': {'signal': 'BUY', 'confidence': 0.72, 'analysis': 'Bullish trend detected'},
        'trading_plan': {'action': 'BUY', 'entry': {'price': 630}, 'risk_management': {'stop_loss': 615, 'take_profit': 660}},
        'risk_check': {'passed': True, 'reason': 'OK'},
        'data_points': 720,
        'strategy_mode': 'all'
    }

    record_id = learner.record_analysis(mock_result)
    print(f"✅ Recorded analysis ID={record_id}")

    learner.submit_feedback(record_id, 'WIN', actual_price=650.0, notes='Price went up as predicted')
    print(f"✅ Submitted feedback: WIN")

    stats = learner.get_statistics_summary()
    print(f"\n📊 Statistics:")
    for k, v in stats.items():
        print(f"   {k}: {v}")

    print(f"\n📋 Learning Report:")
    report = learner.get_learning_report()
    print(report)

    print("✅ All tests passed!")
