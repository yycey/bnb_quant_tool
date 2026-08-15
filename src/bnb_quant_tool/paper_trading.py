"""
模拟交易引擎 (Paper Trading Engine)
============================================================

功能:
  1. 接收 TradeAdvisor 的开单建议 (advice) 自动开仓
  2. 后台轮询最新价格, 自动检查 SL/TP, 触发分批止盈/止损
  3. SQLite 持久化所有持仓和成交流水
  4. 提供统计 (胜率/盈亏比/累计收益/最大回撤/最大连亏)
  5. 提供 AI 复盘所需的交易历史汇总数据

分批止盈规则:
  - 触及 TP1: 平 40% 仓位, 移动 SL 到 entry (保本)（默认 tp_split）
  - 触及 TP2: 平 35% 仓位, 移动 SL 到 TP1 (锁部分利润)
  - 触及 TP3: 平掉剩余 25% 仓位
  - 触及 SL : 全平剩余仓位
  - 反向信号: 由调用方手动 close_manual (可选)

手续费: 双边 0.04% (币安期货标准吃单费率)
"""
import json
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 兼容旧 import：tests / 外部仍可 from paper_trading import _parse_opened_at
from bnb_quant_tool.position_exit_policy import (  # noqa: E402
    parse_opened_at as _parse_opened_at,
)


logger = logging.getLogger(__name__)

# 双边手续费 (开 + 平)
FEE_RATE = 0.0004

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_CANCELLED = "CANCELLED"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


@dataclass
class PaperPosition:
    id: Optional[int]
    symbol: str
    side: str  # LONG / SHORT
    status: str  # OPEN / CLOSED / CANCELLED
    opened_at: str
    closed_at: Optional[str]
    entry_price: float
    qty_total: float          # 开仓时数量
    qty_remaining: float      # 当前未平数量
    leverage: int
    sl: float                 # 当前止损 (会随分批止盈移动)
    sl_initial: float         # 初始止损 (用于 R 倍计算)
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    realized_pnl_usdt: float = 0.0   # 累计已实现盈亏
    close_avg_price: Optional[float] = None
    close_reason: Optional[str] = None
    advice_snapshot: Optional[str] = None  # JSON 字符串
    learning_record_id: Optional[int] = None
    notes: str = ""           # 备注 / 标签
    # ----- MFE / MAE / R-multiple 追踪 (方向3：止盈止损质量诊断) -----
    mfe_price: Optional[float] = None    # 开仓后最有利价格
    mae_price: Optional[float] = None    # 开仓后最不利价格
    mfe_pct: float = 0.0                  # MFE 占入场价百分比
    mae_pct: float = 0.0                  # MAE 占入场价百分比 (负数)
    mfe_r: float = 0.0                    # MFE / 初始风险距离
    mae_r: float = 0.0                    # MAE / 初始风险距离 (负数)
    r_multiple: Optional[float] = None    # 最终 PnL / 初始风险

    def to_dict(self) -> Dict:
        return asdict(self)

    def realized_pnl_pct(self) -> float:
        """以入场名义本金为基准的收益率"""
        notional = self.entry_price * self.qty_total
        if notional <= 0:
            return 0.0
        return self.realized_pnl_usdt / notional


class PaperTradingEngine:
    """模拟交易引擎"""

    # 硬超时默认 48h；软超时（未触 TP1）默认 6h（期望窗口 2–10h，偏快周转学习）
    DEFAULT_MAX_POSITION_AGE_SEC = 48 * 3600
    DEFAULT_SOFT_EXIT_HOURS = 6.0
    DEFAULT_SOFT_EXIT_MIN_HOURS = 2.0
    DEFAULT_ADMIT_WRONG_MIN_AGE_MIN = 30.0
    DEFAULT_ADMIT_WRONG_ADVERSE_R = 0.35

    def __init__(self, db_path: str = None,
                 fee_rate: float = FEE_RATE,
                 config: Dict = None,
                 ai_review_engine=None):
        # 本地化：所有数据保存在工作空间 data/ 目录
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                db_path = str(get_localized_db_path('paper_trading'))
            except Exception:
                # 回退：优先项目根目录/data/，其次模块同级目录
                base_dir = Path(__file__).parent.parent.parent / "data"
                if not base_dir.exists():
                    base_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(base_dir / "paper_trading.db")
        elif not Path(db_path).is_absolute():
            # 相对路径转为绝对路径（相对于项目根目录）
            project_root = Path(__file__).parent.parent.parent
            db_path = str(project_root / db_path)
        
        # 确保目录存在
        db_dir = Path(db_path).parent
        if not db_dir.exists():
            db_dir.mkdir(parents=True, exist_ok=True)
        
        self.db_path = str(Path(db_path).resolve())
        logger.info(f"[PaperTrading] 数据库路径: {self.db_path}")
        self.fee_rate = fee_rate
        self.config = config or {}
        pt_cfg = (self.config.get("paper_trading") or {})
        self.slippage_enabled = bool(pt_cfg.get("slippage_enabled", True))
        self.slippage_min_pct = float(pt_cfg.get("slippage_min_pct", 0.001) or 0.001)
        self.slippage_max_pct = float(pt_cfg.get("slippage_max_pct", 0.005) or 0.005)
        self.slippage_atr_link = bool(pt_cfg.get("slippage_atr_link", True))
        self._atr_ratio: float = 1.0
        self.pin_filter_enabled = bool(pt_cfg.get("pin_filter_enabled", True))
        self.pin_confirm_seconds = float(pt_cfg.get("pin_confirm_seconds", 60) or 60)
        self._pending_triggers: Dict[str, float] = {}
        # MFE 阶梯锁盈
        mfe_cfg = pt_cfg.get("mfe_lock") or {}
        self.mfe_lock_enabled = bool(mfe_cfg.get("enabled", True))
        self.mfe_lock_fee_buffer_pct = float(mfe_cfg.get("fee_buffer_pct", 0.0015) or 0.0015)
        raw_tiers = mfe_cfg.get("tiers") or [
            {"mfe_r": 0.6, "lock_r": 0.0},
            {"mfe_r": 1.0, "lock_r": 0.3},
            {"mfe_r": 1.5, "lock_r": 0.7},
        ]
        self.mfe_lock_tiers: List[Tuple[float, float]] = []
        for t in raw_tiers:
            try:
                self.mfe_lock_tiers.append(
                    (float(t.get("mfe_r", 0)), float(t.get("lock_r", 0)))
                )
            except (TypeError, ValueError, AttributeError):
                continue
        self.mfe_lock_tiers.sort(key=lambda x: x[0])
        self.reject_same_side_stack = bool(pt_cfg.get("reject_same_side_stack", True))
        self.stats_auto_only_default = bool(
            pt_cfg.get(
                "stats_auto_only_default",
                (self.config.get("ai_trading") or {}).get("stats_auto_only_default", True),
            )
        )
        self._local = threading.local()
        self.db_recovery_info = self._ensure_db_healthy()
        if not self.db_recovery_info.get("ok"):
            raise RuntimeError(
                self.db_recovery_info.get("message")
                or "paper_trading.db 损坏且无法自动修复，请关闭其他实例后重试。"
            )
        self._init_db()

        # AI复盘引擎
        self.ai_review_engine = ai_review_engine
        self._pending_review_check = False

        # v2.0: 连亏状态 + trade_advisor 引用
        self._consec_losses = 0
        self._trade_advisor = None

        # v2.0: TP 分批比例（从 trade_advisor 配置读取，默认 40/35/25）
        ta_cfg = (config or {}).get('trade_advisor', {})
        tp_split_cfg = ta_cfg.get('tp_split', {'tp1': '40%', 'tp2': '35%', 'tp3': '25%'})
        self._tp1_pct = self._parse_pct(tp_split_cfg.get('tp1', '40%'), 0.40)
        self._tp2_pct = self._parse_pct(tp_split_cfg.get('tp2', '35%'), 0.35)
        self._tp3_pct = 1.0 - self._tp1_pct - self._tp2_pct  # 剩余全部给 TP3

        # watcher 后台线程
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_running = False
        self._watcher_interval = 15.0
        self._price_provider: Optional[Callable[[str], float]] = None
        self._on_event: Optional[Callable[[str, Dict], None]] = None
        self._lock = threading.Lock()
        self._heartbeat_path = Path(self.db_path).parent / "watcher.heartbeat"
        self._price_fail_streak: Dict[str, int] = {}
        self._last_price_error: str = ""

    @staticmethod
    def calc_unrealized_pnl(row: Dict, price: float, fee_rate: float = FEE_RATE) -> float:
        """估算持仓浮动盈亏（含预估平仓手续费）"""
        entry = float(row.get("entry_price") or 0)
        qty = float(row.get("qty_remaining") or 0)
        side = (row.get("side") or "LONG").upper()
        if entry <= 0 or qty <= 0 or price <= 0:
            return 0.0
        fee = price * qty * fee_rate
        if side == SIDE_LONG:
            return (price - entry) * qty - fee
        return (entry - price) * qty - fee

    def set_atr_ratio(self, atr_ratio: float) -> None:
        """供分析/watcher 注入 ATR/均值，驱动滑点随波动率浮动。"""
        try:
            self._atr_ratio = max(0.3, min(4.0, float(atr_ratio or 1.0)))
        except (TypeError, ValueError):
            self._atr_ratio = 1.0

    def _apply_slippage(self, price: float, side: str, *, is_open: bool) -> float:
        """按波动区间随机滑点 0.1%~0.5%；ATR 高时偏向更大滑点。"""
        if not self.slippage_enabled or price <= 0:
            return price
        lo = float(self.slippage_min_pct)
        hi = float(self.slippage_max_pct)
        if hi < lo:
            lo, hi = hi, lo
        if self.slippage_atr_link:
            # atr_ratio 0.8→偏小滑点，2.0→偏大
            t = (float(getattr(self, "_atr_ratio", 1.0)) - 0.8) / 1.2
            t = max(0.0, min(1.0, t))
            mid = lo + t * (hi - lo)
            # 在 mid 附近抖动，仍落在 [lo, hi]
            span = max(1e-6, (hi - lo) * 0.35)
            slip = random.uniform(max(lo, mid - span), min(hi, mid + span))
        else:
            slip = random.uniform(lo, hi)
        side_u = (side or "LONG").upper()
        adverse_up = (is_open and side_u == "LONG") or ((not is_open) and side_u == "SHORT")
        return round(price * (1 + slip) if adverse_up else price * (1 - slip), 6)

    def _pin_confirmed(self, key: str, touched: bool) -> bool:
        """插针过滤：首次触及后需持续 pin_confirm_seconds 仍触及才触发。"""
        if not self.pin_filter_enabled:
            return touched
        now = time.time()
        if not touched:
            self._pending_triggers.pop(key, None)
            return False
        first = self._pending_triggers.get(key)
        if first is None:
            self._pending_triggers[key] = now
            return False
        if (now - first) >= self.pin_confirm_seconds:
            self._pending_triggers.pop(key, None)
            return True
        return False

    @staticmethod
    def calc_position_margin(row: Dict) -> float:
        """单笔占用保证金 = 名义价值 / 杠杆。"""
        from bnb_quant_tool.config_access import calc_position_margin
        return calc_position_margin(row)

    def get_total_realized_pnl(self) -> float:
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usdt), 0) FROM paper_positions"
        ).fetchone()
        return float(row[0] if row else 0)

    def get_margin_state(self, principal_usdt: float) -> Dict:
        """权益 = 本金 + 累计已实现盈亏；可用 = 权益 - 已占用保证金。"""
        from bnb_quant_tool.config_access import get_margin_state
        return get_margin_state(
            principal_usdt,
            self.get_open_positions(),
            total_realized_pnl=self.get_total_realized_pnl(),
        )

    def can_allocate_margin(self, margin_required: float, principal_usdt: float) -> Tuple[bool, str]:
        state = self.get_margin_state(principal_usdt)
        avail = state["available_margin"]
        if margin_required <= avail + 0.01:
            return True, ""
        return False, (
            f"保证金不足 (本次需 {margin_required:.2f} USDT，"
            f"可用 {avail:.2f} / 权益 {state['equity']:.2f}，"
            f"已占用 {state['used_margin']:.2f})"
        )

    def _touch_heartbeat(self):
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            fails = {k: v for k, v in self._price_fail_streak.items() if v > 0}
            payload = {
                "ts": _utc_now_iso(),
                "watching": bool(self._watcher_running),
                "price_fail_streak": fails,
                "last_price_error": self._last_price_error or "",
            }
            self._heartbeat_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------
    # DB
    # ------------------------------------------------------------
    def _ensure_db_healthy(self) -> Dict:
        try:
            from bnb_quant_tool.sqlite_recovery import ensure_sqlite_db_healthy
            return ensure_sqlite_db_healthy(self.db_path, label="paper_trading")
        except Exception as e:
            logger.error("模拟盘数据库健康检查失败: %s", e)
            return {"ok": False, "action": "none", "backup": None, "message": str(e)}

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            try:
                self._local.conn = sqlite3.connect(self.db_path, timeout=60.0)
                self._local.conn.row_factory = sqlite3.Row
                self._local.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as e:
                if "malformed" in str(e).lower() or "disk image" in str(e).lower():
                    self.db_recovery_info = self._ensure_db_healthy()
                    self._local.conn = sqlite3.connect(self.db_path, timeout=60.0)
                    self._local.conn.row_factory = sqlite3.Row
                    self._local.conn.execute("PRAGMA journal_mode=WAL")
                else:
                    raise
            # 写锁等待加长：Web/学习管道并发时避免 tick/平仓直接失败
            self._local.conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA temp_store=MEMORY")
            # 关闭隐式事务，改由 BEGIN IMMEDIATE / commit 显式控制，避免半开事务占锁
            self._local.conn.isolation_level = None
        return self._local.conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接（导入/外部写入后刷新用）。"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    @staticmethod
    def _is_db_locked(exc: BaseException) -> bool:
        if not isinstance(exc, sqlite3.OperationalError):
            return False
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg

    def _safe_rollback(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception:
            pass

    def _run_db(self, op, *, label: str = "db", max_retries: int = 8):
        """执行 DB 操作；遇 database is locked 则 rollback/复位连接并退避重试。"""
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries):
            try:
                return op()
            except sqlite3.OperationalError as e:
                last_exc = e
                self._safe_rollback()
                if not self._is_db_locked(e):
                    raise
                # 半开事务会持续占锁；每次 locked 都丢弃当前连接再重试
                self.reset_connection()
                delay = min(2.5, 0.08 * (2 ** attempt)) + random.uniform(0.0, 0.06)
                logger.warning(
                    "[PaperTrading] %s database locked (尝试 %d/%d)，%.2fs 后重试",
                    label,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def checkpoint_wal(self) -> None:
        try:
            def _op():
                conn = self._conn()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.commit()

            self._run_db(_op, label="wal_checkpoint", max_retries=3)
        except Exception as e:
            logger.debug("WAL checkpoint skipped: %s", e)

    def _init_db(self):
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                entry_price REAL NOT NULL,
                qty_total REAL NOT NULL,
                qty_remaining REAL NOT NULL,
                leverage INTEGER DEFAULT 1,
                sl REAL NOT NULL,
                sl_initial REAL NOT NULL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                realized_pnl_usdt REAL DEFAULT 0,
                close_avg_price REAL,
                close_reason TEXT,
                advice_snapshot TEXT,
                learning_record_id INTEGER,
                notes TEXT,
                mfe_price REAL,
                mae_price REAL,
                mfe_pct REAL DEFAULT 0,
                mae_pct REAL DEFAULT 0,
                mfe_r REAL DEFAULT 0,
                mae_r REAL DEFAULT 0,
                r_multiple REAL
            )
            """
        )
        # 自动迁移：旧库补齐新增列
        for col, ddl in [
            ("mfe_price", "REAL"),
            ("mae_price", "REAL"),
            ("mfe_pct", "REAL DEFAULT 0"),
            ("mae_pct", "REAL DEFAULT 0"),
            ("mfe_r", "REAL DEFAULT 0"),
            ("mae_r", "REAL DEFAULT 0"),
            ("r_multiple", "REAL"),
            ("signal_tracking_id", "INTEGER"),
        ]:
            try:
                cur.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        try:
            from bnb_quant_tool.position_reeval import ensure_reeval_tables

            ensure_reeval_tables(conn)
        except Exception as e:
            logger.debug("position_reeval tables: %s", e)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                fill_type TEXT NOT NULL,        -- OPEN / TP1 / TP2 / TP3 / SL / MANUAL
                price REAL NOT NULL,
                qty REAL NOT NULL,
                fee REAL NOT NULL,
                pnl REAL NOT NULL,
                FOREIGN KEY(position_id) REFERENCES paper_positions(id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pp_status ON paper_positions(status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pf_pid ON paper_fills(position_id)"
        )
        # 信号追踪表：记录 AI 输出的每个信号，供用户手动回填实际交易结果
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL,
                stop_loss REAL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                confidence REAL,
                strength TEXT,
                market_regime TEXT,
                advice_snapshot TEXT,
                -- 用户手动回填字段
                followed INTEGER DEFAULT 0,
                followed_at TEXT,
                actual_entry REAL,
                actual_exit REAL,
                actual_pnl_usdt REAL,
                actual_pnl_pct REAL,
                exit_reason TEXT,
                feedback_note TEXT,
                feedback_at TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_st_followed ON signal_tracking(followed)"
        )
        conn.commit()

    # ------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------
    def open_from_advice(self, advice: Dict,
                         equity_usdt: float = 5000.0,
                         learning_record_id: Optional[int] = None,
                         notes: str = "",
                         relaxed: bool = False) -> Optional[int]:
        """
        根据 TradeAdvisor 的 advice 字典开仓.
        - 简化为以 entry_mid 直接成交 (不模拟挂单未成交)
        - 数量优先取 advice.position.quantity, 否则取 usdt_amount；皆无则拒开
        - 启用 slippage_enabled 时对入场价施加不利滑点
        - relaxed=True: 即使 action=WAIT, 也会用 raw_action 开仓 (用于收集模拟样本)
        返回 position_id; 若不可开仓返回 None
        """
        action = (advice.get("action") or "").upper()
        # 宽松模式: 如果 action=WAIT 但 raw_action 是 LONG/SHORT, 则用 raw_action
        if relaxed and action == "WAIT":
            raw = (advice.get("raw_action") or "").upper()
            if raw in (SIDE_LONG, SIDE_SHORT):
                action = raw
        if action not in (SIDE_LONG, SIDE_SHORT):
            return None

        prices = advice.get("prices") or {}
        entry = self._safe_float(prices.get("entry_mid"))
        sl = self._safe_float(prices.get("stop_loss"))
        tp1 = self._safe_float(prices.get("tp1"))
        tp2 = self._safe_float(prices.get("tp2"))
        tp3 = self._safe_float(prices.get("tp3"))

        # 价格兼底: relaxed 模式下用 current_price + ATR 估算 (避免 advice 由于 action=WAIT 导致 prices 全为 None)
        if entry <= 0:
            entry = self._safe_float(advice.get("current_price"))
        if entry <= 0:
            logger.warning("open_from_advice: 无法确定入场价, 取消开仓")
            return None

        # 开仓滑点（对己不利方向）
        entry = self._apply_slippage(entry, action, is_open=True)

        if sl <= 0:
            # ATR 估算底底止损 (1% 价格)
            atr_pct = 0.01
            if action == SIDE_LONG:
                sl = round(entry * (1 - atr_pct * 1.5), 4)
            else:
                sl = round(entry * (1 + atr_pct * 1.5), 4)
        if tp1 <= 0:
            tp1_pct = 0.015
            tp1 = round(entry * (1 + tp1_pct), 4) if action == SIDE_LONG else round(entry * (1 - tp1_pct), 4)
        if tp2 <= 0:
            tp2_pct = 0.03
            tp2 = round(entry * (1 + tp2_pct), 4) if action == SIDE_LONG else round(entry * (1 - tp2_pct), 4)
        if tp3 <= 0:
            tp3_pct = 0.05
            tp3 = round(entry * (1 + tp3_pct), 4) if action == SIDE_LONG else round(entry * (1 - tp3_pct), 4)

        # SL/TP 有效性：距离过近或方向错误（探针翻边残留）一律重算
        sl_wrong_side = (
            (action == SIDE_LONG and sl >= entry)
            or (action == SIDE_SHORT and sl <= entry)
        )
        if sl_wrong_side or abs(sl - entry) / entry < 0.001:
            if action == SIDE_LONG:
                sl = round(entry * (1 - 0.015), 4)
            else:
                sl = round(entry * (1 + 0.015), 4)
            logger.warning(
                f"[PaperTrading] SL 无效(错边或距入场价 <0.1%)，已自动修正为 {sl}"
            )
        tp1_wrong = (
            (action == SIDE_LONG and tp1 <= entry)
            or (action == SIDE_SHORT and tp1 >= entry)
        )
        if tp1_wrong:
            if action == SIDE_LONG:
                tp1 = round(entry * 1.015, 4)
                tp2 = round(entry * 1.03, 4) if tp2 <= entry else tp2
                tp3 = round(entry * 1.05, 4) if tp3 <= entry else tp3
            else:
                tp1 = round(entry * 0.985, 4)
                tp2 = round(entry * 0.97, 4) if tp2 >= entry else tp2
                tp3 = round(entry * 0.95, 4) if tp3 >= entry else tp3
            logger.warning(
                f"[PaperTrading] TP 与方向不符，已自动修正 tp1={tp1}"
            )

        position = advice.get("position") or {}
        qty = self._safe_float(position.get("quantity"))
        if qty <= 0:
            usdt = self._safe_float(position.get("usdt_amount"))
            if usdt <= 0:
                # 禁止静默用 30% 权益开仓；仓位字段缺失则拒开
                logger.warning(
                    "open_from_advice: 缺少 quantity/usdt_amount，拒绝开仓"
                )
                return None
            qty = round(usdt / entry, 6)
        leverage = int(position.get("leverage_suggest") or 1)

        margin_required = self._safe_float(position.get("margin_required"))
        if margin_required <= 0:
            margin_required = entry * qty / max(1, leverage)

        ok_margin, margin_reason = self.can_allocate_margin(margin_required, equity_usdt)
        if not ok_margin:
            logger.warning("open_from_advice: %s", margin_reason)
            return None

        symbol = advice.get("symbol", "BNBUSDT")
        side = action

        # 持仓上限 / 同向叠仓拒开
        opens = self.get_open_positions()
        try:
            from bnb_quant_tool.config_access import (
                get_max_open_positions,
                is_position_limit_reached,
            )
            max_open = get_max_open_positions(self.config, default=0)
            if is_position_limit_reached(len(opens), max_open):
                logger.warning(
                    "open_from_advice: 已达最大持仓数 %s，拒开 %s %s",
                    max_open, symbol, side,
                )
                return None
        except Exception as e:
            logger.debug("max_open_positions check skipped: %s", e)

        if self.reject_same_side_stack:
            for op in opens:
                if (
                    str(op.get("symbol") or "").upper() == str(symbol).upper()
                    and str(op.get("side") or "").upper() == side
                ):
                    logger.warning(
                        "open_from_advice: 同向叠仓拒开 %s %s (已有 OPEN #%s)",
                        symbol, side, op.get("id"),
                    )
                    return None
            # 异向也拒开（本期不做翻仓）— 同 symbol 已有任意 OPEN 则拒
            for op in opens:
                if str(op.get("symbol") or "").upper() == str(symbol).upper():
                    logger.warning(
                        "open_from_advice: 同品种已有持仓 #%s %s，拒开 %s",
                        op.get("id"), op.get("side"), side,
                    )
                    return None

        # 入场手续费
        open_fee = entry * qty * self.fee_rate

        signal_tracking_id = advice.get('_signal_tracking_id')

        def _insert():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    now = _utc_now_iso()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        """
                        INSERT INTO paper_positions
                        (symbol, side, status, opened_at, entry_price, qty_total, qty_remaining,
                         leverage, sl, sl_initial, tp1, tp2, tp3,
                         realized_pnl_usdt, advice_snapshot, learning_record_id, notes, signal_tracking_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (symbol, side, STATUS_OPEN, now, entry, qty, qty, leverage,
                         sl, sl, tp1, tp2, tp3,
                         -open_fee, json.dumps(advice, ensure_ascii=False, default=str),
                         learning_record_id, notes, signal_tracking_id)
                    )
                    new_pid = cur.lastrowid
                    cur.execute(
                        """
                        INSERT INTO paper_fills
                        (position_id, ts, fill_type, price, qty, fee, pnl)
                        VALUES (?, ?, 'OPEN', ?, ?, ?, ?)
                        """,
                        (new_pid, now, entry, qty, open_fee, -open_fee)
                    )
                    conn.commit()
                    return new_pid
                except Exception:
                    self._safe_rollback()
                    raise

        pid = self._run_db(_insert, label=f"open:{symbol}:{side}")

        logger.info(
            f"[PaperTrading] OPEN #{pid} {symbol} {side} entry={entry} "
            f"qty={qty} SL={sl} TP1={tp1}"
        )
        try:
            from bnb_quant_tool.validation_trading import record_validation_open
            record_validation_open(
                position_id=int(pid),
                advice=advice,
                record_id=learning_record_id,
                config=self.config,
            )
        except Exception as ve:
            logger.debug("validation open record: %s", ve)
        if self._on_event:
            try:
                self._on_event("OPEN", {
                    "id": pid, "symbol": symbol, "side": side,
                    "entry": entry, "qty": qty, "sl": sl, "tp1": tp1
                })
            except Exception:
                pass
        return pid

    # ------------------------------------------------------------
    # 行情驱动: 检查 SL/TP
    # ------------------------------------------------------------
    def tick(self, symbol: str, price: float) -> List[Dict]:
        """
        喂入最新价格, 检查所有 OPEN 仓位是否触发 SL/TP.
        返回本次发生的事件列表.
        """
        events: List[Dict] = []
        if price <= 0:
            return events

        def _load_open():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM paper_positions WHERE status=? AND symbol=?",
                    (STATUS_OPEN, symbol)
                )
                return [dict(r) for r in cur.fetchall()]

        try:
            rows = self._run_db(_load_open, label=f"tick_load:{symbol}")
        except sqlite3.OperationalError as e:
            if self._is_db_locked(e):
                raise
            raise

        for row in rows:
            evs = self._evaluate_position(row, price)
            events.extend(evs)
        return events

    def _evaluate_position(self, row: Dict, price: float) -> List[Dict]:
        events: List[Dict] = []
        pid = row["id"]
        side = row["side"]
        sl = row["sl"]
        tp1, tp2, tp3 = row["tp1"], row["tp2"], row["tp3"]

        # 0. 实时更新 MFE / MAE（方向3）
        try:
            self._update_mfe_mae(row, price)
        except Exception as e:
            logger.debug(f"_update_mfe_mae 异常: {e}")

        # 0.5 MFE 阶梯锁盈（TP1 之前即可保本/锁利润）
        try:
            lock_ev = self._apply_mfe_lock(row)
            if lock_ev:
                events.append(lock_ev)
                row = self._get_position_row(pid) or row
                sl = row.get("sl", sl)
        except Exception as e:
            logger.debug(f"_apply_mfe_lock 异常: {e}")

        # 1. 先检查 SL (最重要, 优先于 TP)；插针过滤需持续触及才确认
        sl_hit = self._sl_touched(side, price, sl)
        if self._pin_confirmed(f"{pid}:SL", sl_hit):
            fill_px = self._apply_slippage(price, side, is_open=False)
            if self._do_full_close_atomic(pid, fill_px, "SL", "STOP_LOSS"):
                events.append({"type": "SL", "id": pid, "price": fill_px})
                if self._on_event:
                    try:
                        self._on_event("SL", {"id": pid, "price": fill_px})
                    except Exception:
                        pass
            return events

        # 2. 分批止盈（同样过插针确认）
        if tp1 and not row["tp1_hit"]:
            tp1_hit = self._tp_touched(side, price, tp1)
            if self._pin_confirmed(f"{pid}:TP1", tp1_hit):
                qty_part = round(row["qty_total"] * self._tp1_pct, 6)
                qty_part = min(qty_part, row["qty_remaining"])
                fill_px = self._apply_slippage(tp1, side, is_open=False)
                if self._do_tp_partial_atomic(
                    pid, qty_part, fill_px, "TP1",
                    new_sl=row["entry_price"], flag="tp1_hit",
                ):
                    events.append({"type": "TP1", "id": pid, "price": fill_px})
                    if self._on_event:
                        try:
                            self._on_event("TP1", {"id": pid, "price": fill_px})
                        except Exception:
                            pass
            elif not tp1_hit:
                pass  # _pin_confirmed 已清 pending

        # 重新读 row (qty_remaining/sl 已变)
        row = self._get_position_row(pid)
        if row is None or row["status"] != STATUS_OPEN:
            return events

        if tp2 and not row["tp2_hit"]:
            tp2_hit = self._tp_touched(side, price, tp2)
            if self._pin_confirmed(f"{pid}:TP2", tp2_hit):
                qty_part = round(row["qty_total"] * self._tp2_pct, 6)
                qty_part = min(qty_part, row["qty_remaining"])
                fill_px = self._apply_slippage(tp2, side, is_open=False)
                new_sl = tp1 if tp1 else row["entry_price"]
                if self._do_tp_partial_atomic(
                    pid, qty_part, fill_px, "TP2",
                    new_sl=new_sl, flag="tp2_hit",
                ):
                    events.append({"type": "TP2", "id": pid, "price": fill_px})
                    if self._on_event:
                        try:
                            self._on_event("TP2", {"id": pid, "price": fill_px})
                        except Exception:
                            pass

        row = self._get_position_row(pid)
        if row is None or row["status"] != STATUS_OPEN:
            return events

        if tp3 and not row["tp3_hit"]:
            tp3_hit = self._tp_touched(side, price, tp3)
            if self._pin_confirmed(f"{pid}:TP3", tp3_hit):
                fill_px = self._apply_slippage(tp3, side, is_open=False)
                if self._do_full_close_atomic(
                    pid, fill_px, "TP3", "TAKE_PROFIT_FULL", flag="tp3_hit",
                ):
                    events.append({"type": "TP3", "id": pid, "price": fill_px})
                    if self._on_event:
                        try:
                            self._on_event("TP3", {"id": pid, "price": fill_px})
                        except Exception:
                            pass

        return events

    def _mfe_lock_target_sl(self, row: Dict, mfe_r: float) -> Optional[float]:
        """按 MFE(R) 阶梯计算目标止损；只返回「应移到」的价，调用方负责只收紧。"""
        if not self.mfe_lock_enabled or not self.mfe_lock_tiers:
            return None
        entry = float(row.get("entry_price") or 0)
        sl0 = float(row.get("sl_initial") or 0)
        side = str(row.get("side") or "").upper()
        if entry <= 0 or side not in (SIDE_LONG, SIDE_SHORT):
            return None
        risk = abs(entry - sl0) if sl0 > 0 else 0.0
        if risk <= 0:
            return None

        best_lock_r: Optional[float] = None
        for thr, lock_r in self.mfe_lock_tiers:
            if mfe_r + 1e-9 >= thr:
                best_lock_r = lock_r
        if best_lock_r is None:
            return None

        fee_buf = max(0.0, float(self.mfe_lock_fee_buffer_pct))
        if side == SIDE_LONG:
            # lock_r=0 → 入场上方小缓冲（覆盖双边费）
            base = entry * (1.0 + fee_buf) if best_lock_r <= 0 else entry + best_lock_r * risk
            return round(base, 6)
        base = entry * (1.0 - fee_buf) if best_lock_r <= 0 else entry - best_lock_r * risk
        return round(base, 6)

    def _apply_mfe_lock(self, row: Dict) -> Optional[Dict]:
        """根据当前 mfe_r 收紧 SL；只朝有利方向移动。"""
        if not self.mfe_lock_enabled:
            return None
        pid = row["id"]
        side = str(row.get("side") or "").upper()
        mfe_r = float(row.get("mfe_r") or 0)
        target = self._mfe_lock_target_sl(row, mfe_r)
        if target is None:
            return None
        cur_sl = float(row.get("sl") or 0)
        if cur_sl <= 0:
            return None
        # 只收紧
        if side == SIDE_LONG:
            if target <= cur_sl + 1e-9:
                return None
        else:
            if target >= cur_sl - 1e-9:
                return None
        self._update_sl_only(pid, target)
        row["sl"] = target
        logger.info(
            "[PaperTrading] MFE_LOCK #%s mfe_r=%.2f SL %.4f → %.4f",
            pid, mfe_r, cur_sl, target,
        )
        ev = {
            "type": "MFE_LOCK",
            "id": pid,
            "mfe_r": round(mfe_r, 3),
            "old_sl": cur_sl,
            "new_sl": target,
        }
        if self._on_event:
            try:
                self._on_event("MFE_LOCK", ev)
            except Exception:
                pass
        return ev

    @staticmethod
    def _sl_touched(side: str, price: float, sl: float) -> bool:
        if side == SIDE_LONG:
            return price <= sl
        return price >= sl

    @staticmethod
    def _tp_touched(side: str, price: float, tp: float) -> bool:
        if side == SIDE_LONG:
            return price >= tp
        return price <= tp

    # ------------------------------------------------------------
    # 平仓 / 修改
    # ------------------------------------------------------------
    def _do_partial_close(self, pid: int, qty: float, price: float, fill_type: str):
        if qty <= 0:
            return

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "SELECT * FROM paper_positions WHERE id=? AND status=?",
                        (pid, STATUS_OPEN),
                    )
                    r = cur.fetchone()
                    if r is None:
                        conn.rollback()
                        return
                    r = dict(r)
                    remain = float(r["qty_remaining"] or 0)
                    use_qty = min(float(qty), remain)
                    if use_qty <= 0:
                        conn.rollback()
                        return
                    entry = r["entry_price"]
                    side = r["side"]
                    fee = price * use_qty * self.fee_rate
                    if side == SIDE_LONG:
                        pnl = (price - entry) * use_qty - fee
                    else:
                        pnl = (entry - price) * use_qty - fee

                    new_remaining = max(0.0, remain - use_qty)
                    new_realized = r["realized_pnl_usdt"] + pnl

                    now = _utc_now_iso()
                    cur.execute(
                        """
                        UPDATE paper_positions
                        SET qty_remaining=?, realized_pnl_usdt=?
                        WHERE id=? AND status=? AND qty_remaining>=?
                        """,
                        (new_remaining, new_realized, pid, STATUS_OPEN, use_qty),
                    )
                    if cur.rowcount <= 0:
                        conn.rollback()
                        return
                    cur.execute(
                        """
                        INSERT INTO paper_fills
                        (position_id, ts, fill_type, price, qty, fee, pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (pid, now, fill_type, price, use_qty, fee, pnl)
                    )
                    conn.commit()
                    logger.info(
                        f"[PaperTrading] {fill_type} #{pid} qty={use_qty} price={price} "
                        f"pnl={pnl:.4f} remaining={new_remaining}"
                    )
                except Exception:
                    self._safe_rollback()
                    raise

        self._run_db(_op, label=f"partial_close#{pid}:{fill_type}")

    def _do_tp_partial_atomic(
        self,
        pid: int,
        qty: float,
        price: float,
        fill_type: str,
        new_sl: float,
        flag: str,
    ) -> bool:
        """TP1/TP2：减仓 + 填单 + 移 SL + 置旗标，同一事务，避免重复止盈。"""
        if qty <= 0 or flag not in ("tp1_hit", "tp2_hit", "tp3_hit"):
            return False

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "SELECT * FROM paper_positions WHERE id=? AND status=?",
                        (pid, STATUS_OPEN),
                    )
                    r = cur.fetchone()
                    if r is None:
                        conn.rollback()
                        return False
                    r = dict(r)
                    if int(r.get(flag) or 0):
                        conn.rollback()
                        return False
                    remain = float(r["qty_remaining"] or 0)
                    use_qty = min(float(qty), remain)
                    if use_qty <= 0:
                        conn.rollback()
                        return False
                    entry = r["entry_price"]
                    side = r["side"]
                    fee = price * use_qty * self.fee_rate
                    if side == SIDE_LONG:
                        pnl = (price - entry) * use_qty - fee
                    else:
                        pnl = (entry - price) * use_qty - fee
                    new_remaining = max(0.0, remain - use_qty)
                    new_realized = float(r["realized_pnl_usdt"] or 0) + pnl
                    now = _utc_now_iso()
                    cur.execute(
                        """
                        UPDATE paper_positions
                        SET qty_remaining=?, realized_pnl_usdt=?, sl=?, {flag}=1
                        WHERE id=? AND status=? AND qty_remaining>=? AND COALESCE({flag},0)=0
                        """.format(flag=flag),
                        (new_remaining, new_realized, new_sl, pid, STATUS_OPEN, use_qty),
                    )
                    if cur.rowcount <= 0:
                        conn.rollback()
                        return False
                    cur.execute(
                        """
                        INSERT INTO paper_fills
                        (position_id, ts, fill_type, price, qty, fee, pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (pid, now, fill_type, price, use_qty, fee, pnl),
                    )
                    conn.commit()
                    logger.info(
                        f"[PaperTrading] {fill_type} #{pid} qty={use_qty} price={price} "
                        f"pnl={pnl:.4f} remaining={new_remaining} sl→{new_sl}"
                    )
                    return True
                except Exception:
                    self._safe_rollback()
                    raise

        return bool(self._run_db(_op, label=f"tp_atomic#{pid}:{fill_type}"))

    def _do_full_close_atomic(
        self,
        pid: int,
        price: float,
        fill_type: str,
        close_reason: str,
        *,
        flag: Optional[str] = None,
    ) -> bool:
        """全平剩余 + CLOSED 标记（可选 TP3 旗标）同一事务。"""

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "SELECT * FROM paper_positions WHERE id=? AND status=?",
                        (pid, STATUS_OPEN),
                    )
                    r = cur.fetchone()
                    if r is None:
                        conn.rollback()
                        return False
                    r = dict(r)
                    use_qty = float(r["qty_remaining"] or 0)
                    if use_qty <= 0:
                        conn.rollback()
                        return False
                    entry = float(r["entry_price"] or 0)
                    side = r["side"]
                    fee = price * use_qty * self.fee_rate
                    if side == SIDE_LONG:
                        pnl = (price - entry) * use_qty - fee
                    else:
                        pnl = (entry - price) * use_qty - fee
                    new_realized = float(r["realized_pnl_usdt"] or 0) + pnl
                    now = _utc_now_iso()
                    r_mult = None
                    try:
                        sl0 = float(r.get("sl_initial") or 0)
                        qty_total = float(r.get("qty_total") or 0)
                        risk_total = abs(entry - sl0) * qty_total
                        if risk_total > 0:
                            r_mult = round(new_realized / risk_total, 3)
                    except Exception:
                        pass
                    if flag and flag in ("tp1_hit", "tp2_hit", "tp3_hit"):
                        cur.execute(
                            f"""
                            UPDATE paper_positions
                            SET qty_remaining=0, realized_pnl_usdt=?, status=?,
                                closed_at=?, close_avg_price=?, close_reason=?,
                                r_multiple=?, {flag}=1
                            WHERE id=? AND status=?
                            """,
                            (
                                new_realized, STATUS_CLOSED, now, price, close_reason,
                                r_mult, pid, STATUS_OPEN,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE paper_positions
                            SET qty_remaining=0, realized_pnl_usdt=?, status=?,
                                closed_at=?, close_avg_price=?, close_reason=?,
                                r_multiple=?
                            WHERE id=? AND status=?
                            """,
                            (
                                new_realized, STATUS_CLOSED, now, price, close_reason,
                                r_mult, pid, STATUS_OPEN,
                            ),
                        )
                    if cur.rowcount <= 0:
                        conn.rollback()
                        return False
                    cur.execute(
                        """
                        INSERT INTO paper_fills
                        (position_id, ts, fill_type, price, qty, fee, pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (pid, now, fill_type, price, use_qty, fee, pnl),
                    )
                    conn.commit()
                    logger.info(
                        f"[PaperTrading] {fill_type}/{close_reason} #{pid} "
                        f"qty={use_qty} price={price} pnl={pnl:.4f} CLOSED"
                    )
                    return True
                except Exception:
                    self._safe_rollback()
                    raise

        ok = bool(self._run_db(_op, label=f"full_close#{pid}:{close_reason}"))
        if not ok:
            return False

        def _post_close_hooks():
            try:
                self._update_consec_losses(pid)
                self._run_close_learning_pipeline(pid)
                self._auto_fill_signal_result(pid, price, close_reason)
                self._check_and_trigger_review()
            except Exception as e:
                logger.warning(f"post-close hooks error: {e}")

        threading.Thread(target=_post_close_hooks, daemon=True).start()
        return True

    def _update_sl_and_flag(self, pid: int, new_sl: float, flag: str):
        if flag not in ("tp1_hit", "tp2_hit", "tp3_hit"):
            return

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        f"UPDATE paper_positions SET sl=?, {flag}=1 WHERE id=?",
                        (new_sl, pid)
                    )
                    conn.commit()
                except Exception:
                    self._safe_rollback()
                    raise

        self._run_db(_op, label=f"update_sl_flag#{pid}:{flag}")

    def _update_sl_only(self, pid: int, new_sl: float) -> None:
        """仅移动止损（MFE 锁盈），不改 TP 旗标。"""

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "UPDATE paper_positions SET sl=? WHERE id=? AND status=?",
                        (new_sl, pid, STATUS_OPEN),
                    )
                    conn.commit()
                except Exception:
                    self._safe_rollback()
                    raise

        self._run_db(_op, label=f"update_sl#{pid}")

    def _mark_closed(self, pid: int, close_price: float, reason: str):
        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    now = _utc_now_iso()
                    # 计算 r_multiple = 实现PnL / 初始风险 (以名义本金为基准)
                    r_mult = None
                    try:
                        cur.execute(
                            "SELECT entry_price, qty_total, sl_initial, side, realized_pnl_usdt "
                            "FROM paper_positions WHERE id=?", (pid,)
                        )
                        r = cur.fetchone()
                        if r is not None:
                            entry, qty, sl0, side, pnl = r[0], r[1], r[2], r[3], r[4]
                            risk_per_unit = abs(entry - sl0)
                            risk_total = risk_per_unit * qty
                            if risk_total > 0:
                                r_mult = round(pnl / risk_total, 3)
                    except Exception as e:
                        logger.debug(f"r_multiple 计算异常: {e}")
                    cur.execute(
                        """
                        UPDATE paper_positions
                        SET status=?, closed_at=?, close_avg_price=?, close_reason=?, r_multiple=?
                        WHERE id=? AND status=?
                        """,
                        (STATUS_CLOSED, now, close_price, reason, r_mult, pid, STATUS_OPEN)
                    )
                    closed_ok = cur.rowcount > 0
                    conn.commit()
                    return closed_ok
                except Exception:
                    self._safe_rollback()
                    raise

        closed_ok = bool(self._run_db(_op, label=f"mark_closed#{pid}:{reason}"))

        if not closed_ok:
            logger.debug("_mark_closed skipped (already closed): #%s", pid)
            return

        def _post_close_hooks():
            try:
                self._update_consec_losses(pid)
                # 始终跑统一学习管道（process_trade_close 内部幂等，避免漏学）
                self._run_close_learning_pipeline(pid)
                self._auto_fill_signal_result(pid, close_price, reason)
                self._check_and_trigger_review()
            except Exception as e:
                logger.warning(f"post-close hooks error: {e}")

        threading.Thread(target=_post_close_hooks, daemon=True).start()

    def _update_consec_losses(self, closed_pid: int):
        """统计当前连亏笔数（仅用于绩效展示；与熔断 ignore 列表对齐）。"""
        try:
            from bnb_quant_tool.circuit_breaker import (
                DEFAULT_CONSEC_IGNORE_REASONS,
                is_ignored_consec_close_reason,
            )
            ignore = set(DEFAULT_CONSEC_IGNORE_REASONS)
            cb = (self.config or {}).get("circuit_breaker") or {}
            if cb.get("consec_ignore_reasons") is not None:
                ignore = {
                    str(x).strip().upper()
                    for x in (cb.get("consec_ignore_reasons") or [])
                    if str(x).strip()
                }
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT realized_pnl_usdt, close_reason FROM paper_positions "
                "WHERE status=? ORDER BY closed_at DESC LIMIT 20",
                (STATUS_CLOSED,)
            )
            rows = cur.fetchall()
            consec = 0
            for r in rows:
                if is_ignored_consec_close_reason(r[1], ignore):
                    continue
                if r[0] is not None and float(r[0]) < 0:
                    consec += 1
                else:
                    break
            self._consec_losses = consec
            logger.debug(f"连亏统计: {consec} 笔")
        except Exception as e:
            logger.debug(f"_update_consec_losses error: {e}")

    def set_trade_advisor(self, advisor):
        """v2.0: 设置 trade_advisor 引用。"""
        self._trade_advisor = advisor

    def set_learner(self, learner):
        """v2.2: 设置 AI 学习系统引用，用于平仓自动反馈闭环。"""
        self._learner = learner

    def set_learning_pipeline_deps(
        self,
        counterfactual=None,
        pattern_memory=None,
        evolution=None,
        on_status=None,
    ):
        """完整学习管道依赖（GUI / watcher 注入）。"""
        self._counterfactual = counterfactual
        self._pattern_memory = pattern_memory
        self._learning_evolution = evolution
        self._learning_status_cb = on_status

    def _run_close_learning_pipeline(self, closed_pid: int):
        """统一平仓学习管道 — GUI / 无头模式共用。"""
        learner = getattr(self, "_learner", None)
        if learner is None:
            return
        try:
            from bnb_quant_tool.trade_close_learning import (
                TradeCloseLearningDeps,
                build_evolution_coordinator,
                process_trade_close,
            )

            evolution = getattr(self, "_learning_evolution", None)
            if evolution is None:
                evolution = build_evolution_coordinator(
                    learner,
                    self.config,
                    counterfactual=getattr(self, "_counterfactual", None),
                )
                self._learning_evolution = evolution

            trader_memory = getattr(self, "_trader_memory", None)

            deps = TradeCloseLearningDeps(
                learner=learner,
                config=self.config,
                get_position_row=self._get_position_row,
                counterfactual=getattr(self, "_counterfactual", None),
                pattern_memory=getattr(self, "_pattern_memory", None),
                evolution=evolution,
                trader_memory=trader_memory,
                paper_engine=self,
                on_status=getattr(self, "_learning_status_cb", None),
            )
            result = process_trade_close(int(closed_pid), deps)
            if result.errors and not result.skipped_duplicate:
                logger.warning(
                    "close learning #%s errors: %s", closed_pid, result.errors
                )
        except Exception as e:
            logger.warning("close learning pipeline: %s", e)

    def set_trader_memory(self, trader_memory) -> None:
        """注入议会记忆，平仓后回写各交易员胜负权重。"""
        self._trader_memory = trader_memory

    def _auto_feedback_to_learner(self, closed_pid: int):
        """兼容旧入口 — 转发到完整学习管道。"""
        self._run_close_learning_pipeline(closed_pid)

    def _auto_fill_signal_result(self, closed_pid: int, close_price: float, reason: str):
        """v2.3: 平仓后自动回填关联的信号追踪记录"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT signal_tracking_id, realized_pnl_usdt, entry_price, qty_total "
                "FROM paper_positions WHERE id=?",
                (closed_pid,)
            )
            r = cur.fetchone()
            if r is None or r[0] is None:
                return
            sig_id = r[0]
            pnl = r[1] or 0
            entry = r[2] or 0
            qty = r[3] or 0
            pnl_pct = (pnl / (entry * qty) * 100) if entry > 0 and qty > 0 else None
            # fill_signal_result 内部已加锁，此处不可再嵌套 with self._lock
            self.fill_signal_result(
                signal_id=sig_id,
                actual_exit=close_price,
                actual_pnl_usdt=pnl,
                actual_pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
                exit_reason=reason,
            )
            logger.debug(f"信号追踪 #{sig_id} 自动回填: exit={close_price} pnl={pnl:+.2f}")
        except Exception as e:
            logger.debug(f"_auto_fill_signal_result 异常: {e}")

    def _check_and_trigger_review(self):
        """检查是否应该触发AI复盘
        
        注意：GUI 模式下由 gui._maybe_trigger_auto_review 统一触发，
        此方法仅在非 GUI 模式下工作（无头模式）。
        """
        if not self.ai_review_engine:
            return

        # 如果有事件回调，说明有 GUI 在监听，由 GUI 侧触发
        if self._on_event:
            return

        try:
            should, reason = self.ai_review_engine.should_trigger_review(self)
            if should:
                if not self.ai_review_engine.try_begin_review():
                    logger.info("AI复盘跳过: 上一轮未结束")
                    return
                logger.info(f"触发AI复盘: {reason}")
                import threading
                def _run_review():
                    try:
                        payload = None
                        try:
                            payload = self.build_review_payload(max_trades=50)
                        except Exception:
                            payload = None
                        if payload:
                            symbol = (
                                (getattr(self, "_symbol", None) or "BNBUSDT")
                                .replace("USDT", "")
                            )
                            result = self.ai_review_engine.run_enriched_review(
                                payload, symbol=symbol
                            )
                        else:
                            result = self.ai_review_engine.run_review()
                        if result.get("status") == "success":
                            trades_n = result.get('trades_analyzed', 0)
                            logger.info(f"AI复盘完成: 分析了{trades_n}笔交易")

                            stats = self.get_stats()
                            closed_n = stats.get('total_closed_trades', 0)
                            self.ai_review_engine.mark_review_triggered(
                                total_closed=closed_n,
                                streak=("连续" in str(reason)),
                            )

                            # 自动学习（参数写回）
                            if self.ai_review_engine.auto_apply:
                                apply_result = self.ai_review_engine.auto_apply_after_review(result)
                                applied = apply_result.get('applied', [])
                                if applied:
                                    logger.info(f"🧠 AI自学习: 自动应用{len(applied)}项参数优化")
                                    for a in applied:
                                        logger.info(f"  ✅ {a['param']} = {a['new_value']} ({a['reason']})")

                            # 统一训练节拍：Lab 晋升 + 策略变异
                            try:
                                from bnb_quant_tool.training_loop import after_successful_review

                                tl_out = after_successful_review(
                                    paper_engine=self,
                                    learner=getattr(self, "_learner", None),
                                    evolution=getattr(self, "_learning_evolution", None),
                                    review_result=result,
                                    config=getattr(self, "config", None) or {},
                                )
                                promo = (tl_out or {}).get("promote") or {}
                                mutate = (tl_out or {}).get("mutate") or {}
                                if promo.get("status"):
                                    logger.info(
                                        "训练循环晋升: %s (%s)",
                                        promo.get("status"),
                                        len(promo.get("promoted") or []),
                                    )
                                if mutate.get("added"):
                                    logger.info(
                                        "训练循环变异: 入队 %s",
                                        mutate.get("added"),
                                    )
                            except Exception as te:
                                logger.warning("training_loop after review: %s", te)
                    except Exception as e:
                        logger.error(f"AI复盘执行失败: {e}")
                    finally:
                        try:
                            self.ai_review_engine.end_review()
                        except Exception:
                            pass

                threading.Thread(target=_run_review, daemon=True).start()
        except Exception as e:
            logger.error(f"_check_and_trigger_review error: {e}")
            try:
                self.ai_review_engine.end_review()
            except Exception:
                pass

    def _update_mfe_mae(self, row: Dict, price: float):
        """实时更新一笔仓位的最大有利/不利偏移 (Max Favorable/Adverse Excursion).

        - mfe_pct/mae_pct: 以入场价为基准的百分比 (多头、空头都转换为"顺势为正")
        - mfe_r/mae_r: 以 |entry - sl_initial| 为 1R 进行归一
        过滤估价过低/进价错乱的负面样本。
        """
        pid = row["id"]
        entry = float(row.get("entry_price") or 0)
        sl0 = float(row.get("sl_initial") or 0)
        side = row.get("side")
        if entry <= 0 or price <= 0:
            return
        # "顺势偏移"、"逆势偏移"
        if side == SIDE_LONG:
            fav = price - entry
        else:
            fav = entry - price
        adv = -fav  # 与 fav 反号

        cur_mfe = float(row.get("mfe_pct") or 0)
        cur_mae = float(row.get("mae_pct") or 0)
        new_mfe_pct = cur_mfe
        new_mae_pct = cur_mae
        fav_pct = fav / entry * 100.0 if entry > 0 else 0.0
        adv_pct = adv / entry * 100.0 if entry > 0 else 0.0
        if fav_pct > new_mfe_pct:
            new_mfe_pct = fav_pct
        if adv_pct > 0 and -adv_pct < new_mae_pct:
            new_mae_pct = -adv_pct  # 负数

        if abs(new_mfe_pct - cur_mfe) < 1e-6 and abs(new_mae_pct - cur_mae) < 1e-6:
            # 仍同步 row，供同 tick 的 MFE lock 读取
            row["mfe_r"] = float(row.get("mfe_r") or 0)
            row["mae_r"] = float(row.get("mae_r") or 0)
            return  # 未更新

        risk_per_unit = abs(entry - sl0) if sl0 > 0 else 0.0
        new_mfe_r = (new_mfe_pct / 100.0 * entry / risk_per_unit) if risk_per_unit > 0 else 0.0
        new_mae_r = (new_mae_pct / 100.0 * entry / risk_per_unit) if risk_per_unit > 0 else 0.0
        new_mfe_price = entry * (1 + new_mfe_pct / 100.0) if side == SIDE_LONG else entry * (1 - new_mfe_pct / 100.0)
        new_mae_price = entry * (1 + new_mae_pct / 100.0) if side == SIDE_LONG else entry * (1 - new_mae_pct / 100.0)

        new_mfe_price_r = round(new_mfe_price, 6)
        new_mae_price_r = round(new_mae_price, 6)
        new_mfe_pct_r = round(new_mfe_pct, 4)
        new_mae_pct_r = round(new_mae_pct, 4)
        new_mfe_r_r = round(new_mfe_r, 3)
        new_mae_r_r = round(new_mae_r, 3)

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        """
                        UPDATE paper_positions
                        SET mfe_price=?, mae_price=?, mfe_pct=?, mae_pct=?, mfe_r=?, mae_r=?
                        WHERE id=?
                        """,
                        (new_mfe_price_r, new_mae_price_r,
                         new_mfe_pct_r, new_mae_pct_r,
                         new_mfe_r_r, new_mae_r_r, pid)
                    )
                    conn.commit()
                except Exception:
                    self._safe_rollback()
                    raise

        self._run_db(_op, label=f"mfe_mae#{pid}")

        # 同步内存行，供本 tick MFE_LOCK / 后续逻辑使用
        row["mfe_price"] = new_mfe_price_r
        row["mae_price"] = new_mae_price_r
        row["mfe_pct"] = new_mfe_pct_r
        row["mae_pct"] = new_mae_pct_r
        row["mfe_r"] = new_mfe_r_r
        row["mae_r"] = new_mae_r_r

    def _get_position_row(self, pid: int) -> Optional[Dict]:
        def _op():
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM paper_positions WHERE id=?", (pid,))
            r = cur.fetchone()
            return dict(r) if r else None

        return self._run_db(_op, label=f"get_position#{pid}")

    def close_manual(self, pid: int, price: float,
                     reason: str = "MANUAL") -> bool:
        """手动平掉指定仓位 (剩余全部)."""
        try:
            row = self._get_position_row(pid)
            if row is None or row["status"] != STATUS_OPEN:
                return False
            side = row.get("side") or SIDE_LONG
            fill_px = self._apply_slippage(price, side, is_open=False)
            ok = self._do_full_close_atomic(pid, fill_px, reason, reason)
            if ok and self._on_event:
                try:
                    self._on_event("MANUAL_CLOSE", {"id": pid, "price": fill_px})
                except Exception:
                    pass
            return ok
        except sqlite3.OperationalError as e:
            if self._is_db_locked(e):
                self.reset_connection()
                logger.error(
                    "[PaperTrading] close_manual #%s database locked，本轮放弃，下轮重试",
                    pid,
                )
                return False
            raise

    def cancel(self, pid: int) -> bool:
        """取消未触发的仓位 (一般用不到, 因为我们立即成交)."""

        def _op():
            with self._lock:
                conn = self._conn()
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "UPDATE paper_positions SET status=? WHERE id=? AND status=?",
                        (STATUS_CANCELLED, pid, STATUS_OPEN)
                    )
                    ok = cur.rowcount > 0
                    conn.commit()
                    return ok
                except Exception:
                    self._safe_rollback()
                    raise

        return bool(self._run_db(_op, label=f"cancel#{pid}"))

    # ------------------------------------------------------------
    # 查询 / 统计
    # ------------------------------------------------------------
    def get_open_positions(self) -> List[Dict]:
        def _op():
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM paper_positions WHERE status=? ORDER BY id DESC",
                (STATUS_OPEN,)
            )
            return [dict(r) for r in cur.fetchall()]

        return list(self._run_db(_op, label="get_open_positions") or [])

    def get_closed_positions(self, limit: int = 200) -> List[Dict]:
        def _op():
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM paper_positions WHERE status=? "
                "ORDER BY id DESC LIMIT ?",
                (STATUS_CLOSED, limit)
            )
            return [dict(r) for r in cur.fetchall()]

        return list(self._run_db(_op, label="get_closed_positions") or [])

    def get_recent_trades(self, limit: int = 5) -> List[Dict]:
        """返回最近N笔已平仓交易（简表），供 AIReviewEngine 连亏检测用。
        每条包含: id, side, pnl (=realized_pnl_usdt), close_reason, closed_at
        """
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, side, realized_pnl_usdt AS pnl, close_reason, closed_at "
            "FROM paper_positions WHERE status=? "
            "ORDER BY closed_at DESC LIMIT ?",
            (STATUS_CLOSED, limit)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_fills(self, position_id: int) -> List[Dict]:
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_fills WHERE position_id=? ORDER BY id ASC",
            (position_id,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_stats(self, opens: Optional[List[Dict]] = None,
                  closed: Optional[List[Dict]] = None,
                  *,
                  auto_only: Optional[bool] = None) -> Dict:
        """汇总统计: 胜率/盈亏比/累计盈亏/最大回撤/最大连亏 + E[R]/自动单口径。

        auto_only=True 时排除 close_reason 以 MANUAL 开头的成交（默认跟随配置）。
        """
        if closed is None:
            closed = self.get_closed_positions(limit=10000)
        if opens is None:
            opens = self.get_open_positions()
        if auto_only is None:
            auto_only = bool(self.stats_auto_only_default)

        all_closed = list(closed)
        if auto_only:
            closed = [
                c for c in all_closed
                if not str(c.get("close_reason") or "").upper().startswith("MANUAL")
            ]

        n = len(closed)
        empty = {
            "total_trades": 0,
            "open_count": len(opens),
            "win_rate": 0.0,
            "avg_win_usdt": 0.0,
            "avg_loss_usdt": 0.0,
            "profit_factor": 0.0,
            "total_realized_pnl": 0.0,
            "max_drawdown_usdt": 0.0,
            "max_consec_losses": 0,
            "best_trade_usdt": 0.0,
            "worst_trade_usdt": 0.0,
            "expectancy_r": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "avg_r": 0.0,
            "auto_only": bool(auto_only),
            "manual_excluded": len(all_closed) - n if auto_only else 0,
            "all_trades_including_manual": len(all_closed),
            "gave_back_count": 0,
        }
        if n == 0:
            return empty

        # 胜率分母排除近似保本（与 circuit_breaker 口径一致：|pnl| 极小不算输赢）
        wins = [c for c in closed if c["realized_pnl_usdt"] > 0.01]
        losses = [c for c in closed if c["realized_pnl_usdt"] < -0.01]
        decided = len(wins) + len(losses)
        total_pnl = sum(c["realized_pnl_usdt"] for c in closed)
        avg_win = sum(c["realized_pnl_usdt"] for c in wins) / len(wins) if wins else 0.0
        avg_loss = sum(c["realized_pnl_usdt"] for c in losses) / len(losses) if losses else 0.0
        gross_win = sum(c["realized_pnl_usdt"] for c in wins) if wins else 0.0
        gross_loss = abs(sum(c["realized_pnl_usdt"] for c in losses)) if losses else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0.0)

        r_vals = [
            float(c["r_multiple"])
            for c in closed
            if c.get("r_multiple") is not None
        ]
        win_rs = [
            float(c["r_multiple"])
            for c in wins
            if c.get("r_multiple") is not None
        ]
        loss_rs = [
            float(c["r_multiple"])
            for c in losses
            if c.get("r_multiple") is not None
        ]
        avg_r = sum(r_vals) / len(r_vals) if r_vals else 0.0
        avg_win_r = sum(win_rs) / len(win_rs) if win_rs else 0.0
        avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
        # E[R] = 有 R 的样本均值（含小保本）
        expectancy_r = avg_r

        gave_back = sum(
            1
            for c in closed
            if float(c.get("mfe_r") or 0) >= 0.5
            and float(c.get("realized_pnl_usdt") or 0) < -0.01
        )

        # 最大回撤 (按倒序时间序列累计)
        chrono = sorted(closed, key=lambda x: x.get("closed_at") or "")
        equity_curve = []
        cum = 0.0
        for c in chrono:
            cum += c["realized_pnl_usdt"]
            equity_curve.append(cum)
        peak = -1e18
        max_dd = 0.0
        for v in equity_curve:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd

        # 最大连亏
        consec = 0
        max_consec = 0
        for c in chrono:
            if c["realized_pnl_usdt"] < -0.01:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0

        best = max((c["realized_pnl_usdt"] for c in closed), default=0.0)
        worst = min((c["realized_pnl_usdt"] for c in closed), default=0.0)

        return {
            "total_trades": n,
            "open_count": len(opens),
            "win_rate": (len(wins) / decided) if decided else 0.0,
            "avg_win_usdt": round(avg_win, 4),
            "avg_loss_usdt": round(avg_loss, 4),
            "profit_factor": round(pf, 3),
            "total_realized_pnl": round(total_pnl, 4),
            "max_drawdown_usdt": round(max_dd, 4),
            "max_consec_losses": max_consec,
            "best_trade_usdt": round(best, 4),
            "worst_trade_usdt": round(worst, 4),
            "expectancy_r": round(expectancy_r, 4),
            "avg_win_r": round(avg_win_r, 4),
            "avg_loss_r": round(avg_loss_r, 4),
            "avg_r": round(avg_r, 4),
            "auto_only": bool(auto_only),
            "manual_excluded": len(all_closed) - n if auto_only else 0,
            "all_trades_including_manual": len(all_closed),
            "gave_back_count": gave_back,
        }

    def build_review_payload(self, max_trades: int = 30,
                              strategy_perf: Optional[List[Dict]] = None,
                              recent_param_changes: Optional[List[Dict]] = None,
                              pattern_insight: Optional[Dict] = None,
                              counterfactual_stats: Optional[Dict] = None) -> Dict:
        """构造给 AI 复盘的精简数据 (避免 prompt 过长).

        v2 升级 (含分桶+MFE/MAE+多源顶层信号):
          - stats: 全量统计
          - trades: 最近 max_trades 笔明细 (含 mfe_r/mae_r/r_multiple)
          - breakdown: 按 side / close_reason / hour / confidence_bucket / mfe_mae_quadrant 分桶
          - strategy_perf: 13 机构策略表现 (供 AI 调权重)
          - recent_param_changes: 上 N 次参数调整及其后的实际效果 (用于 revert_suggestion)
          - pattern_insight / counterfactual_stats: Pattern Memory + 反事实输入
        """
        stats = self.get_stats()
        closed = self.get_closed_positions(limit=max_trades)
        trades_brief = []
        for c in closed:
            adv = {}
            try:
                adv = json.loads(c.get("advice_snapshot") or "{}")
            except Exception:
                adv = {}
            ai_ana = adv.get("ai_analysis") if isinstance(adv.get("ai_analysis"), dict) else {}
            news = adv.get("news_summary") if isinstance(adv.get("news_summary"), dict) else {}
            mtf = adv.get("multi_timeframe") if isinstance(adv.get("multi_timeframe"), dict) else {}
            ind = adv.get("indicators") if isinstance(adv.get("indicators"), dict) else {}
            # 推断 regime: 优先从 advice.indicators.regime / mtf.regime / ATR相对价估算
            regime = (
                ind.get("regime")
                or mtf.get("regime")
                or self._infer_regime_from_adv(adv)
            )
            # opened_at 提取小时
            opened_hour = None
            try:
                opened_hour = int(str(c["opened_at"])[11:13])
            except Exception:
                pass
            trades_brief.append({
                "id": c["id"],
                "symbol": c["symbol"],
                "side": c["side"],
                "opened_at": c["opened_at"],
                "closed_at": c["closed_at"],
                "opened_hour": opened_hour,
                "entry": c["entry_price"],
                "close": c["close_avg_price"],
                "qty": c["qty_total"],
                "pnl_usdt": round(c["realized_pnl_usdt"], 4),
                "close_reason": c["close_reason"],
                "tp1_hit": bool(c["tp1_hit"]),
                "tp2_hit": bool(c["tp2_hit"]),
                # 方向3：MFE / MAE / R
                "mfe_pct": c.get("mfe_pct") or 0,
                "mae_pct": c.get("mae_pct") or 0,
                "mfe_r": c.get("mfe_r") or 0,
                "mae_r": c.get("mae_r") or 0,
                "r_multiple": c.get("r_multiple"),
                # 上下文
                "confidence": adv.get("confidence"),
                "conservativeness": adv.get("conservativeness"),
                "ai_signal": ai_ana.get("signal"),
                "news_polarity": news.get("polarity"),
                "mtf_action": mtf.get("recommended_action"),
                "regime": regime,
            })

        breakdown = self._compute_breakdown(trades_brief)
        payload = {
            "stats": stats,
            "trades": trades_brief,
            "breakdown": breakdown,
        }
        # 可选顶层信号
        if strategy_perf:
            payload["strategy_perf"] = strategy_perf
        if recent_param_changes:
            payload["recent_param_changes"] = recent_param_changes
        if pattern_insight:
            payload["pattern_insight"] = pattern_insight
        if counterfactual_stats:
            payload["counterfactual_stats"] = counterfactual_stats
        return payload

    @staticmethod
    def _infer_regime_from_adv(adv: Dict) -> str:
        """从 advice 中粗估 regime: trend / range / high_vol / unknown"""
        ind = adv.get("indicators") or {}
        atr_pct = None
        try:
            atr = float(ind.get("ATR") or 0)
            cur = float(adv.get("current_price") or 0)
            if atr > 0 and cur > 0:
                atr_pct = atr / cur
        except Exception:
            atr_pct = None
        adx = None
        try:
            adx = float(ind.get("ADX") or 0)
        except Exception:
            adx = None
        if atr_pct is not None and atr_pct > 0.025:
            return "high_vol"
        if adx is not None and adx >= 25:
            return "trend"
        if adx is not None and adx < 18:
            return "range"
        return "unknown"

    @staticmethod
    def _compute_breakdown(trades: List[Dict]) -> Dict:
        """按多个维度分桶统计胜率/PF，让 AI 看起来不是平均数"""
        def _agg(rows: List[Dict]) -> Dict:
            n = len(rows)
            if n == 0:
                return {"n": 0, "win_rate": 0.0, "pf": 0.0, "avg_pnl": 0.0,
                        "avg_mfe_r": 0.0, "avg_mae_r": 0.0, "avg_r": 0.0}
            wins = [t for t in rows if (t.get("pnl_usdt") or 0) > 0]
            losses = [t for t in rows if (t.get("pnl_usdt") or 0) < 0]
            gw = sum((t.get("pnl_usdt") or 0) for t in wins)
            gl = abs(sum((t.get("pnl_usdt") or 0) for t in losses))
            pf = (gw / gl) if gl > 0 else (gw if gw > 0 else 0.0)
            avg_pnl = sum((t.get("pnl_usdt") or 0) for t in rows) / n
            avg_mfe_r = sum((t.get("mfe_r") or 0) for t in rows) / n
            avg_mae_r = sum((t.get("mae_r") or 0) for t in rows) / n
            r_list = [t.get("r_multiple") for t in rows if t.get("r_multiple") is not None]
            avg_r = (sum(r_list) / len(r_list)) if r_list else 0.0
            return {
                "n": n,
                "win_rate": round(len(wins) / n, 3),
                "pf": round(pf, 3),
                "avg_pnl": round(avg_pnl, 3),
                "avg_mfe_r": round(avg_mfe_r, 3),
                "avg_mae_r": round(avg_mae_r, 3),
                "avg_r": round(avg_r, 3),
            }

        # 1. 按 side
        by_side = {
            "LONG": _agg([t for t in trades if t.get("side") == "LONG"]),
            "SHORT": _agg([t for t in trades if t.get("side") == "SHORT"]),
        }
        # 2. 按 close_reason
        reasons = {}
        for t in trades:
            r = t.get("close_reason") or "UNKNOWN"
            reasons.setdefault(r, []).append(t)
        by_close_reason = {k: _agg(v) for k, v in reasons.items()}
        # 3. 按 hour bucket (00-08 / 08-16 / 16-24)
        def _bucket_hour(h):
            if h is None:
                return "unknown"
            if 0 <= h < 8:
                return "00-08"
            if 8 <= h < 16:
                return "08-16"
            return "16-24"
        hours = {}
        for t in trades:
            b = _bucket_hour(t.get("opened_hour"))
            hours.setdefault(b, []).append(t)
        by_hour = {k: _agg(v) for k, v in hours.items()}
        # 4. 按 confidence bucket
        def _bucket_conf(c):
            if c is None:
                return "unknown"
            try:
                c = float(c)
            except Exception:
                return "unknown"
            if c < 0.6:
                return "<0.6"
            if c < 0.7:
                return "0.6-0.7"
            if c < 0.8:
                return "0.7-0.8"
            return ">=0.8"
        confs = {}
        for t in trades:
            b = _bucket_conf(t.get("confidence"))
            confs.setdefault(b, []).append(t)
        by_confidence = {k: _agg(v) for k, v in confs.items()}
        # 5. 按 regime
        regs = {}
        for t in trades:
            r = t.get("regime") or "unknown"
            regs.setdefault(r, []).append(t)
        by_regime = {k: _agg(v) for k, v in regs.items()}
        # 6. MFE/MAE 象限诊断："赢单MFE走远但只吃到1R" / "输单MAE太深"
        big_mfe_small_r = [t for t in trades if (t.get("mfe_r") or 0) >= 2.0
                           and (t.get("r_multiple") or 0) <= 1.0]
        deep_mae_loss = [t for t in trades if (t.get("mae_r") or 0) <= -1.0
                         and (t.get("pnl_usdt") or 0) < 0]
        diagnostics = {
            "big_mfe_small_r_count": len(big_mfe_small_r),
            "big_mfe_small_r_hint": (
                "处理 TP1 太近: 赢单浮盈>=2R 但最终只含<=1R" if big_mfe_small_r else ""
            ),
            "deep_mae_loss_count": len(deep_mae_loss),
            "deep_mae_loss_hint": (
                "损单 MAE<=-1R 才止损: SL 实际滢于初始 1R" if deep_mae_loss else ""
            ),
        }
        return {
            "by_side": by_side,
            "by_close_reason": by_close_reason,
            "by_hour": by_hour,
            "by_confidence": by_confidence,
            "by_regime": by_regime,
            "diagnostics": diagnostics,
        }

    # ------------------------------------------------------------
    # Watcher 后台循环
    # ------------------------------------------------------------
    def set_price_provider(self, fn: Callable[[str], float]):
        """设置价格回调: provider(symbol) -> price"""
        self._price_provider = fn

    def set_event_callback(self, fn: Callable[[str, Dict], None]):
        """事件回调: (event_type, payload) -> None"""
        self._on_event = fn

    def start_watcher(self, interval: float = 15.0):
        if self._watcher_running:
            return
        if self._price_provider is None:
            raise RuntimeError("先调用 set_price_provider 设置价格源")
        self._watcher_interval = interval
        self._watcher_running = True
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()
        logger.info(f"[PaperTrading] watcher 已启动, interval={interval}s")

    def stop_watcher(self):
        self._watcher_running = False
        logger.info("[PaperTrading] watcher 已停止")

    def is_watching(self) -> bool:
        return self._watcher_running

    def _watch_loop(self):
        while self._watcher_running:
            try:
                opens = self.get_open_positions()
                symbols = sorted({p["symbol"] for p in opens})
                for sym in symbols:
                    try:
                        price = self._price_provider(sym)
                        if price and price > 0:
                            self.tick(sym, float(price))
                            self._price_fail_streak[sym] = 0
                        else:
                            streak = self._price_fail_streak.get(sym, 0) + 1
                            self._price_fail_streak[sym] = streak
                            self._last_price_error = f"{sym}: empty_price"
                            if streak == 1 or streak % 10 == 0:
                                logger.warning(
                                    "[PaperTrading] %s 取价失败连续 %d 次，跳过本轮 tick",
                                    sym,
                                    streak,
                                )
                    except Exception as e:
                        streak = self._price_fail_streak.get(sym, 0) + 1
                        self._price_fail_streak[sym] = streak
                        self._last_price_error = f"{sym}: {e}"
                        if self._is_db_locked(e):
                            self.reset_connection()
                        logger.warning(f"[PaperTrading] tick 失败 {sym}: {e}")

                # tick 可能已 SL/TP 平仓；刷新后再做超时/认错，避免脏快照
                opens = self.get_open_positions()

                # 超时平仓：软未止盈 + 硬上限
                self._check_position_timeout(opens)
                opens = self.get_open_positions()

                # 方向认错：逆向达到阈值立即平仓，把「错了」标清楚留给 AI 学
                self._check_admit_wrong(opens)
                opens = self.get_open_positions()

                # 持仓轻量复评（MTF+Regime，不调 LLM）→ 可自动认错/提前平
                try:
                    from bnb_quant_tool.position_reeval import maybe_reeval_open_positions

                    maybe_reeval_open_positions(self, opens)
                except Exception as e:
                    logger.debug("[PaperTrading] position_reeval: %s", e)

                self._touch_heartbeat()

            except Exception as e:
                if self._is_db_locked(e):
                    self.reset_connection()
                logger.error(f"[PaperTrading] watcher loop 错误: {e}")
            # sleep with frequent wakeup
            for _ in range(int(self._watcher_interval * 2)):
                if not self._watcher_running:
                    break
                time.sleep(0.5)

    def _resolve_timeout_policy(self) -> Dict[str, float]:
        from bnb_quant_tool.position_exit_policy import resolve_timeout_policy
        return resolve_timeout_policy(self.config)

    def _check_position_timeout(self, open_positions: List[Dict]):
        """软未止盈超时 + 硬上限超时 → 市价平。"""
        if not open_positions:
            return
        from bnb_quant_tool.position_exit_policy import collect_timeout_exits

        prices: Dict[str, float] = {}
        for pos in open_positions:
            sym = str(pos.get("symbol") or "")
            if not sym or sym in prices:
                continue
            try:
                px = self._price_provider(sym) if self._price_provider else None
                if px and float(px) > 0:
                    prices[sym] = float(px)
            except Exception:
                continue

        for pos, dec in collect_timeout_exits(
            open_positions, self.config, prices_by_symbol=prices
        ):
            try:
                sym = pos["symbol"]
                price = prices.get(sym)
                if not price or price <= 0:
                    price = self._price_provider(sym) if self._price_provider else None
                if not price or price <= 0:
                    continue
                pid = pos["id"]
                side = pos["side"]
                if dec.reason == "TIMEOUT":
                    logger.info(
                        "[PaperTrading] 硬超时平仓 #%s %s %s (%s)，市价平仓",
                        pid, side, sym, dec.detail,
                    )
                else:
                    logger.info(
                        "[PaperTrading] 软超时平仓 #%s %s %s (%s)，尽快平仓控风险",
                        pid, side, sym, dec.detail,
                    )
                self.close_manual(pid, float(price), reason=dec.reason)
            except Exception as e:
                logger.warning(f"[PaperTrading] 超时检查异常 #{pos.get('id')}: {e}")

    def _live_pnl_r(self, pos: Dict, price: float) -> Optional[float]:
        from bnb_quant_tool.position_exit_policy import live_pnl_r
        return live_pnl_r(pos, price)

    def _check_admit_wrong(self, open_positions: List[Dict]):
        """方向开错就认：逆向达到阈值尽快平仓。"""
        if not open_positions:
            return
        from bnb_quant_tool.position_exit_policy import evaluate_admit_wrong

        for pos in open_positions:
            try:
                sym = pos["symbol"]
                price = self._price_provider(sym) if self._price_provider else None
                if not price or price <= 0:
                    continue
                dec = evaluate_admit_wrong(pos, float(price), self.config)
                if not dec:
                    continue
                pid = pos["id"]
                side = pos["side"]
                logger.info(
                    "[PaperTrading] 认错平仓 #%s %s %s "
                    "(持仓 %.0fmin，%s) — 方向错了就认，留给 AI 学",
                    pid, side, sym, dec.age_minutes, dec.detail,
                )
                self.close_manual(pid, float(price), reason=dec.reason)
            except Exception as e:
                logger.warning(f"[PaperTrading] 认错检查异常 #{pos.get('id')}: {e}")

    # ------------------------------------------------------------
    # 信号追踪 + 手动回填
    # ------------------------------------------------------------
    def track_signal(self, advice: Dict, market_regime: str = "") -> int:
        """记录 AI 输出的信号到 signal_tracking 表，返回记录 ID"""
        p = advice.get("prices", {})
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            now = _utc_now_iso()
            cur.execute(
                """INSERT INTO signal_tracking
                (generated_at, symbol, timeframe, direction, entry_price,
                 stop_loss, tp1, tp2, tp3, confidence, strength,
                 market_regime, advice_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now,
                 advice.get('symbol', ''),
                 advice.get('timeframe', ''),
                 advice.get('action', 'WAIT'),
                 self._safe_float(p.get('entry_mid')),
                 self._safe_float(p.get('stop_loss')),
                 self._safe_float(p.get('tp1')),
                 self._safe_float(p.get('tp2')),
                 self._safe_float(p.get('tp3')),
                 self._safe_float(advice.get('confidence')),
                 advice.get('strength', ''),
                 market_regime,
                 json.dumps(advice, ensure_ascii=False, default=str))
            )
            sid = cur.lastrowid
            conn.commit()
        return sid

    def get_pending_signals(self, limit: int = 50) -> List[Dict]:
        """获取未回填的信号列表"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM signal_tracking WHERE followed=0 ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    def get_pending_reeval_alerts(self, limit: int = 50) -> List[Dict]:
        """持仓复评：待处理的提前平仓提示（不自动砍仓）。"""
        from bnb_quant_tool.position_reeval import list_pending_reeval

        return list_pending_reeval(str(self.db_path), limit=limit)

    def acknowledge_reeval_alert(self, queue_id: int, *, status: str = "acknowledged") -> bool:
        from bnb_quant_tool.position_reeval import acknowledge_reeval

        return acknowledge_reeval(str(self.db_path), int(queue_id), status=status)

    def get_all_signals(self, limit: int = 200) -> List[Dict]:
        """获取所有信号列表"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM signal_tracking ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    def mark_signal_followed(self, signal_id: int, actual_entry: float = None) -> bool:
        """标记信号已跟单"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            now = _utc_now_iso()
            cur.execute(
                "UPDATE signal_tracking SET followed=1, followed_at=?, actual_entry=? WHERE id=?",
                (now, actual_entry, signal_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def fill_signal_result(self, signal_id: int, actual_exit: float,
                           actual_pnl_usdt: float = None, actual_pnl_pct: float = None,
                           exit_reason: str = "", feedback_note: str = "") -> bool:
        """回填信号的实际交易结果"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            now = _utc_now_iso()
            cur.execute(
                """UPDATE signal_tracking
                SET actual_exit=?, actual_pnl_usdt=?, actual_pnl_pct=?,
                    exit_reason=?, feedback_note=?, feedback_at=?
                WHERE id=?""",
                (actual_exit, actual_pnl_usdt, actual_pnl_pct,
                 exit_reason, feedback_note, now, signal_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def get_signal_stats(self) -> Dict:
        """获取信号追踪统计（历史信号胜率面板）"""
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            # 总体统计
            cur.execute("SELECT COUNT(*) FROM signal_tracking")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM signal_tracking WHERE followed=1")
            followed = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM signal_tracking WHERE feedback_at IS NOT NULL")
            feedbacked = cur.fetchone()[0]
            # 已反馈的胜率
            cur.execute(
                "SELECT direction, COUNT(*) as cnt, "
                "SUM(CASE WHEN actual_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(actual_pnl_usdt) as total_pnl, "
                "AVG(actual_pnl_pct) as avg_pnl_pct "
                "FROM signal_tracking WHERE feedback_at IS NOT NULL "
                "GROUP BY direction"
            )
            by_direction = {}
            for r in cur.fetchall():
                by_direction[r[0]] = {
                    'count': r[1],
                    'wins': r[2],
                    'win_rate': r[2] / r[1] if r[1] > 0 else 0,
                    'total_pnl': r[3] or 0,
                    'avg_pnl_pct': r[4] or 0,
                }
            # 最近20个信号
            cur.execute(
                "SELECT id, generated_at, symbol, direction, confidence, "
                "followed, actual_pnl_usdt, feedback_at "
                "FROM signal_tracking ORDER BY id DESC LIMIT 20"
            )
            recent = [dict(r) for r in cur.fetchall()]
            return {
                'total_signals': total,
                'followed': followed,
                'feedbacked': feedbacked,
                'by_direction': by_direction,
                'recent': recent,
            }

    # ------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------
    @staticmethod
    def _safe_float(v, default: float = 0.0) -> float:
        try:
            if v is None:
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_pct(val, default: float = 0.33) -> float:
        """解析百分比字符串或浮点数 (如 '40%' → 0.40, 40 → 0.40, 0.4 → 0.4)"""
        if isinstance(val, (int, float)):
            if val > 1.0:
                return val / 100.0
            return float(val)
        s = str(val).strip().rstrip('%')
        try:
            f = float(s)
            if f > 1.0:
                return f / 100.0
            return f
        except (TypeError, ValueError):
            return default

    @staticmethod
    def format_stats_report(stats: Dict) -> str:
        if not stats or stats.get("total_trades", 0) == 0:
            scope = "自动单" if stats.get("auto_only") else "全部"
            return ("=" * 60 + "\n" +
                    f"模拟交易统计（{scope}）\n" +
                    "=" * 60 + "\n" +
                    f"  当前持仓: {stats.get('open_count', 0)} 笔\n" +
                    "  尚无已完成的交易, 跑一段时间再回来看.\n")
        scope = "自动单口径" if stats.get("auto_only") else "含手动"
        excl = stats.get("manual_excluded") or 0
        lines = [
            "=" * 60,
            f"模拟交易统计（{scope}）",
            "=" * 60,
            f"  总交易笔数:   {stats['total_trades']}"
            + (f"  (已排除手动 {excl})" if excl else ""),
            f"  当前持仓:     {stats['open_count']}",
            f"  期望值 E[R]:  {stats.get('expectancy_r', stats.get('avg_r', 0)):+.3f}",
            f"  均赢R/均亏R:  {stats.get('avg_win_r', 0):+.2f} / {stats.get('avg_loss_r', 0):+.2f}",
            f"  胜率:         {stats['win_rate']:.1%}  (次要)",
            f"  累计盈亏:     {stats['total_realized_pnl']:+.2f} USDT",
            f"  盈亏比 (PF):  {stats['profit_factor']:.2f}",
            f"  平均盈利:     {stats['avg_win_usdt']:+.2f}",
            f"  平均亏损:     {stats['avg_loss_usdt']:+.2f}",
            f"  回吐亏损笔:   {stats.get('gave_back_count', 0)}  (MFE≥0.5R 最终亏)",
            f"  最大单笔盈:   {stats['best_trade_usdt']:+.2f}",
            f"  最大单笔亏:   {stats['worst_trade_usdt']:+.2f}",
            f"  最大回撤:     {stats['max_drawdown_usdt']:.2f}",
            f"  最大连亏:     {stats['max_consec_losses']} 笔",
            "=" * 60,
        ]
        return "\n".join(lines)

    def minutes_since_last_open(
        self, symbol: str, side: str
    ) -> Optional[float]:
        """距上次同 symbol+side 开仓的分钟数；无记录返回 None。"""
        sym = str(symbol or "").upper()
        sd = str(side or "").upper()
        if not sym or sd not in (SIDE_LONG, SIDE_SHORT):
            return None
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT opened_at FROM paper_positions
            WHERE UPPER(symbol)=? AND UPPER(side)=?
            ORDER BY opened_at DESC LIMIT 1
            """,
            (sym, sd),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        raw = str(row[0]).replace("Z", "+00:00")
        try:
            opened = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - opened).total_seconds() / 60.0)
