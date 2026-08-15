"""
BNB量化交易工具 - 主动信号扫描器 (Signal Scanner)
================================================================

设计目标：从"定时等信号"进化到"主动盯盘"。

与 fullauto 的区别：
- fullauto / 定时: 平静期每 2–4 小时做一轮完整 AI 分析
- scanner: 持续轻量盯价；仅大波动/急涨急跌时立刻触发完整分析

扫描条件（记录信号；仅大波动类触发 fullauto）：
┌─────────────────────────────────┬────────────────────────────────────────┐
│ 条件                            │ 是否触发完整分析                        │
├─────────────────────────────────┼────────────────────────────────────────┤
│ PRICE_SHOCK 价格急变            │ 是（主触发）                            │
│ ATR 急涨急跌 / 放量 / 突破      │ 是（大波动）                            │
│ RSI / 布林 / 多周期等           │ 默认只记录，不刷分析                    │
└─────────────────────────────────┴────────────────────────────────────────┘

防噪机制：
- 同一条件冷却期
- 大波动触发后 min_trigger_interval 内不重复拉全量分析
- 持仓保证金耗尽时不触发开仓向分析
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# 信号数据类
# ============================================================
@dataclass
class ScanSignal:
    """扫描信号"""
    signal_type: str          # RSI_CROSS / BB_TOUCH / VOLUME_SPIKE / BREAKOUT / MTF_CONFLUENCE / ATR_SPIKE / PRICE_SHOCK
    direction: str            # BULLISH / BEARISH / NEUTRAL
    strength: float           # 0.0 ~ 1.0
    symbol: str
    price: float
    detail: str               # 人类可读描述
    indicators: Dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ============================================================
# 触发条件冷却管理
# ============================================================
class CooldownManager:
    """管理每个触发条件的冷却期"""

    def __init__(self, default_cooldown: float = 300.0):
        self._cooldowns: Dict[str, float] = {}  # key → 上次触发时间戳
        self._default = default_cooldown

    def is_cooled(self, key: str) -> bool:
        """检查是否在冷却期外（可以触发）"""
        last = self._cooldowns.get(key, 0)
        return (time.time() - last) >= self._default

    def mark_fired(self, key: str):
        """标记已触发"""
        self._cooldowns[key] = time.time()

    def reset(self):
        self._cooldowns.clear()


# 默认可触发完整分析的大波动信号类型
DEFAULT_BIG_MOVE_TYPES = frozenset({
    "PRICE_SHOCK",
    "ATR_SPIKE",
    "VOLUME_SPIKE",
    "BREAKOUT",
})


# ============================================================
# 信号扫描器核心
# ============================================================
class SignalScanner:
    """主动行情信号扫描器"""

    SIGNAL_TYPES = [
        "RSI_CROSS", "BB_TOUCH", "VOLUME_SPIKE",
        "BREAKOUT", "MTF_CONFLUENCE", "ATR_SPIKE", "PRICE_SHOCK",
        "GOLDEN_CROSS", "DEATH_CROSS",
    ]

    def __init__(
        self,
        fetcher,                          # BinanceDataFetcher 实例
        db_path: str = "data/paper_trading.db",
        config: Optional[Dict] = None,
        on_signal: Optional[Callable[[ScanSignal], None]] = None,
    ):
        cfg = config or {}
        self._fetcher = fetcher
        from bnb_quant_tool.data_localization import resolve_db_path
        self._db_path = resolve_db_path(db_path, "paper_trading")
        self._on_signal = on_signal
        self._last_trigger_payload: Optional[Dict] = None
        self._config = cfg

        # 扫描参数：盯价要勤，分析要省
        self.scan_interval: int = int(cfg.get("scan_interval", 20))
        self.rsi_oversold: float = float(cfg.get("rsi_oversold", 30))
        self.rsi_overbought: float = float(cfg.get("rsi_overbought", 70))
        self.volume_spike_mult: float = float(cfg.get("volume_spike_mult", 2.5))
        self.atr_spike_mult: float = float(cfg.get("atr_spike_mult", 2.0))
        self.breakout_lookback: int = int(cfg.get("breakout_lookback", 20))
        self.cooldown_seconds: float = float(cfg.get("cooldown_seconds", 300))
        self.min_trigger_interval: float = float(cfg.get("min_trigger_interval", 300))
        self._principal_usdt: float = float(
            cfg.get("account_balance") or cfg.get("principal_usdt") or 0
        )
        self.mtf_timeframes: List[str] = list(cfg.get("mtf_timeframes", ["15m", "1h", "4h"]))
        self.mtf_cache_seconds: int = int(cfg.get("mtf_cache_seconds", 300))
        self.min_strength: float = float(cfg.get("min_strength", 0.65))

        # 大波动才拉完整分析；平静期交给 autopilot 2–4h 定时
        self.trigger_on_big_move_only: bool = bool(cfg.get("trigger_on_big_move_only", True))
        raw_types = cfg.get("trigger_signal_types") or list(DEFAULT_BIG_MOVE_TYPES)
        self.trigger_signal_types = {
            str(t).strip().upper() for t in raw_types if str(t).strip()
        } or set(DEFAULT_BIG_MOVE_TYPES)
        self.big_move_pct: float = float(cfg.get("big_move_pct", 0.012))  # 1.2%
        self.big_move_lookback_bars: int = int(cfg.get("big_move_lookback_bars", 4))
        self.big_move_min_strength: float = float(
            cfg.get("big_move_min_strength", 0.55)
        )

        # 扫描品种（主品种 + 关联品种）
        self.symbols: List[str] = cfg.get("symbols", ["BNBUSDT"])

        # 冷却管理
        self._cooldown = CooldownManager(default_cooldown=self.cooldown_seconds)
        self._last_trigger_time: float = 0.0

        # 状态
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._scan_count: int = 0
        self._signal_count: int = 0
        self._last_scan_time: str = ""
        self._last_signals: List[ScanSignal] = []

        # 线程本地 DB 连接
        self._local = threading.local()

        # 上一次的 RSI 值（用于检测穿越）
        self._prev_rsi: Dict[str, float] = {}
        self._mtf_cache: Dict[str, Tuple[float, Dict]] = {}
        self._last_price: Dict[str, float] = {}
        from .multi_timeframe import MultiTimeframeAnalyzer
        self._mtf_analyzer = MultiTimeframeAnalyzer(fetcher=self._fetcher)

        # 初始化 DB
        self._ensure_table()

    def _is_big_move_signal(self, sig: ScanSignal) -> bool:
        """是否允许触发完整 AI 分析。"""
        if not self.trigger_on_big_move_only:
            return True
        return str(sig.signal_type or "").upper() in self.trigger_signal_types

    # ── DB ──────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self._db_path, timeout=60.0)
        return self._local.conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接（导入/外部写入后刷新用）。"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _margin_exhausted(self) -> bool:
        """保证金模式：可用保证金耗尽则不再触发开单。"""
        if self._principal_usdt <= 0:
            return False
        try:
            from bnb_quant_tool.config_access import get_margin_state_from_db
            state = get_margin_state_from_db(self._db_path, self._principal_usdt)
            return state["available_margin"] <= 0.01
        except Exception:
            return False

    def _ensure_table(self):
        from bnb_quant_tool.sqlite_util import run_db

        def _op():
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strength REAL,
                    symbol TEXT,
                    price REAL,
                    detail TEXT,
                    indicators_json TEXT,
                    triggered_fullauto INTEGER DEFAULT 0,
                    created_at TEXT
                )
            """)
            if conn.isolation_level is not None:
                conn.commit()

        run_db(_op, label="scan_ensure_table", on_locked=self.reset_connection)

    def _save_signal(self, sig: ScanSignal, triggered_fullauto: bool = False):
        from bnb_quant_tool.sqlite_util import begin_immediate, run_db

        payload = (
            sig.signal_type, sig.direction, sig.strength, sig.symbol, sig.price,
            sig.detail, json.dumps(sig.indicators, default=str),
            1 if triggered_fullauto else 0, sig.timestamp,
        )

        def _op():
            conn = self._get_conn()
            try:
                begin_immediate(conn)
                conn.execute(
                    "INSERT INTO scan_signals "
                    "(signal_type, direction, strength, symbol, price, detail, indicators_json, triggered_fullauto, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                self.reset_connection()
                raise

        try:
            run_db(_op, label="scan_save_signal", on_locked=self.reset_connection)
        except Exception as e:
            logger.debug(f"信号保存失败（非致命）: {e}")

    # ── 扫描线程控制 ────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"SignalScanner 启动，盯盘间隔 {self.scan_interval}s，"
            f"大波动触发={sorted(self.trigger_signal_types)}，"
            f"阈值±{self.big_move_pct:.2%}，品种 {self.symbols}"
        )

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("SignalScanner 已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> Dict:
        return {
            "scan_count": self._scan_count,
            "signal_count": self._signal_count,
            "last_scan_time": self._last_scan_time,
            "last_signals": [asdict(s) for s in self._last_signals[-10:]],
            "running": self._running,
        }

    def get_recent_signals(self, limit: int = 50) -> List[Dict]:
        """获取最近的扫描信号"""
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT * FROM scan_signals ORDER BY id DESC LIMIT ?", (limit,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def delete_signals(self, signal_ids) -> int:
        """按 id 批量删除扫描信号，返回删除行数。"""
        ids = [int(x) for x in (signal_ids or []) if x is not None]
        if not ids:
            return 0
        try:
            conn = self._get_conn()
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"DELETE FROM scan_signals WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
            return int(cur.rowcount or 0)
        except Exception as e:
            logger.debug("delete_signals 失败: %s", e)
            return 0

    def clear_signals(self) -> int:
        """清空全部扫描信号，返回删除行数。"""
        try:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM scan_signals")
            conn.commit()
            return int(cur.rowcount or 0)
        except Exception as e:
            logger.debug("clear_signals 失败: %s", e)
            return 0

    # ── 扫描主循环 ──────────────────────────────────────
    def _scan_loop(self):
        while self._running:
            self._scan_count += 1
            self._last_scan_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            signals_this_round: List[ScanSignal] = []

            for symbol in self.symbols:
                try:
                    signals = self._scan_symbol(symbol)
                    signals_this_round.extend(signals)
                except Exception as e:
                    logger.debug(f"扫描 {symbol} 异常: {e}")

            # 去重 + 冷却过滤
            filtered = []
            for sig in signals_this_round:
                cooldown_key = f"{sig.signal_type}_{sig.symbol}_{sig.direction}"
                if self._cooldown.is_cooled(cooldown_key):
                    self._cooldown.mark_fired(cooldown_key)
                    filtered.append(sig)

            # 记录信号
            self._last_signals = filtered
            for sig in filtered:
                self._signal_count += 1
                is_big = self._is_big_move_signal(sig)
                need_strength = (
                    self.big_move_min_strength if is_big else self.min_strength
                )
                # 仅大波动类信号触发完整分析；其余只盯盘记录
                can_trigger = (
                    is_big
                    and not self._margin_exhausted()
                    and (time.time() - self._last_trigger_time) >= self.min_trigger_interval
                    and sig.strength >= need_strength
                )
                self._save_signal(sig, triggered_fullauto=can_trigger)

                if can_trigger:
                    self._last_trigger_time = time.time()
                    trade_dir = (
                        "LONG" if sig.direction == "BULLISH"
                        else "SHORT" if sig.direction == "BEARISH"
                        else "WAIT"
                    )
                    self._last_trigger_payload = {
                        "signal_type": sig.signal_type,
                        "direction": sig.direction,
                        "trade_direction": trade_dir,
                        "strength": sig.strength,
                        "symbol": sig.symbol,
                        "price": sig.price,
                        "detail": sig.detail,
                        "timestamp": sig.timestamp,
                    }
                    # 持久化供 headless/GUI 注入验证探针（跨线程）
                    try:
                        self._persist_pending_scanner(self._last_trigger_payload)
                    except Exception:
                        pass
                    logger.info(
                        f"🔥 大波动触发分析: {sig.signal_type} "
                        f"{sig.direction} {sig.symbol} @ {sig.price} "
                        f"强度={sig.strength:.2f} | {sig.detail}"
                    )
                    # 漏斗只记「触发」；过门/开仓由分析结束后 headless/gui 回填
                    try:
                        from bnb_quant_tool.trading_profile import record_decision_funnel
                        record_decision_funnel(
                            analysis_triggered=True,
                            gate_passed=False,
                            opened=False,
                            source="scanner",
                            symbol=sig.symbol,
                        )
                    except Exception:
                        pass
                    if self._on_signal:
                        try:
                            self._on_signal(sig)
                        except Exception as cb_err:
                            logger.debug(f"信号回调异常: {cb_err}")
                else:
                    why = (
                        "非大波动仅记录"
                        if not is_big
                        else (
                            "冷却中"
                            if (time.time() - self._last_trigger_time) < self.min_trigger_interval
                            else "强度不足"
                        )
                    )
                    logger.info(
                        f"📡 盯盘信号（{why}）: {sig.signal_type} "
                        f"{sig.direction} {sig.symbol} @ {sig.price} "
                        f"强度={sig.strength:.2f} | {sig.detail}"
                    )

            # 等待下一轮
            for _ in range(self.scan_interval):
                if not self._running:
                    break
                time.sleep(1)

    # ── 单品种扫描 ──────────────────────────────────────
    def _scan_symbol(self, symbol: str) -> List[ScanSignal]:
        """扫描单个品种，返回触发的信号列表"""
        signals: List[ScanSignal] = []

        # 1. 拉取 15m K线数据（最近 100 根）
        try:
            df = self._fetcher.get_klines(symbol=symbol, interval="15m", limit=100)
            if df is None or len(df) < 30:
                return signals
        except Exception as e:
            logger.debug(f"获取 {symbol} K线失败: {e}")
            return signals

        close = float(df['close'].iloc[-1])
        high = float(df['high'].iloc[-1])
        low = float(df['low'].iloc[-1])
        volume = float(df['volume'].iloc[-1])

        # 急变/信号价优先用实时 ticker，避免归档过期收盘触发误报
        try:
            resolve = getattr(self._fetcher, "resolve_current_price", None)
            if callable(resolve):
                live = float(resolve(symbol, df) or 0)
                if live > 0:
                    close = live
        except Exception as e:
            logger.debug("scanner live price %s: %s", symbol, e)

        # 2. 计算指标快照
        from .technical_indicators import TechnicalIndicators
        indicators = TechnicalIndicators.calculate_all_indicators(df)

        # ── 价格急变（相对 N 根 K 线前 + 相对上次扫描）──
        shock = self._detect_price_shock(symbol, df, close)
        if shock is not None:
            signals.append(shock)
        self._last_price[symbol] = close

        rsi = indicators.get('RSI', 50)
        if isinstance(rsi, pd.Series):
            rsi = float(rsi.iloc[-1])
        elif rsi is None:
            rsi = 50.0
        else:
            rsi = float(rsi)

        bb_upper = indicators.get('BB_Upper')
        bb_lower = indicators.get('BB_Lower')
        bb_mid = indicators.get('BB_Middle')
        if isinstance(bb_upper, pd.Series):
            bb_upper = float(bb_upper.iloc[-1])
            bb_lower = float(bb_lower.iloc[-1])
            bb_mid = float(bb_mid.iloc[-1]) if bb_mid is not None else None
        elif bb_upper is not None:
            bb_upper = float(bb_upper)
            bb_lower = float(bb_lower)
            bb_mid = float(bb_mid) if bb_mid is not None else None
        else:
            bb_upper = bb_lower = bb_mid = None

        atr = indicators.get('ATR')
        if isinstance(atr, pd.Series):
            atr = float(atr.iloc[-1])
        elif atr is not None:
            atr = float(atr)
        else:
            atr = close * 0.02  # fallback

        macd = indicators.get('MACD')
        macd_signal = indicators.get('MACD_Signal')
        if isinstance(macd, pd.Series):
            macd = float(macd.iloc[-1])
            macd_signal = float(macd_signal.iloc[-1])
        elif macd is not None:
            macd = float(macd)
            macd_signal = float(macd_signal) if macd_signal is not None else 0
        else:
            macd = macd_signal = 0

        # 成交量均值
        vol_ma = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else volume

        # 3. 逐一检查触发条件

        # ── RSI 穿越 ──
        prev_rsi = self._prev_rsi.get(symbol)
        self._prev_rsi[symbol] = rsi

        if prev_rsi is not None:
            if prev_rsi <= self.rsi_oversold and rsi > self.rsi_oversold:
                signals.append(ScanSignal(
                    signal_type="RSI_CROSS",
                    direction="BULLISH",
                    strength=min(1.0, (self.rsi_oversold - prev_rsi + 5) / 20),
                    symbol=symbol, price=close,
                    detail=f"RSI 从超卖区上穿: {prev_rsi:.1f} → {rsi:.1f}",
                    indicators={"rsi": rsi, "prev_rsi": prev_rsi},
                ))
            elif prev_rsi >= self.rsi_overbought and rsi < self.rsi_overbought:
                signals.append(ScanSignal(
                    signal_type="RSI_CROSS",
                    direction="BEARISH",
                    strength=min(1.0, (prev_rsi - self.rsi_overbought + 5) / 20),
                    symbol=symbol, price=close,
                    detail=f"RSI 从超买区下穿: {prev_rsi:.1f} → {rsi:.1f}",
                    indicators={"rsi": rsi, "prev_rsi": prev_rsi},
                ))

        # ── 布林带触边 ──
        if bb_upper is not None and bb_lower is not None:
            bb_width = bb_upper - bb_lower
            if bb_width > 0:
                bb_pos = (close - bb_lower) / bb_width  # 0=下轨, 1=上轨
                if close >= bb_upper:
                    signals.append(ScanSignal(
                        signal_type="BB_TOUCH",
                        direction="BEARISH",
                        strength=min(1.0, bb_pos - 0.9 + 0.2),
                        symbol=symbol, price=close,
                        detail=f"价格触及布林带上轨: {close:.2f} >= {bb_upper:.2f}",
                        indicators={"bb_pos": round(bb_pos, 3), "bb_upper": bb_upper},
                    ))
                elif close <= bb_lower:
                    signals.append(ScanSignal(
                        signal_type="BB_TOUCH",
                        direction="BULLISH",
                        strength=min(1.0, 0.1 - bb_pos + 0.2),
                        symbol=symbol, price=close,
                        detail=f"价格触及布林带下轨: {close:.2f} <= {bb_lower:.2f}",
                        indicators={"bb_pos": round(bb_pos, 3), "bb_lower": bb_lower},
                    ))

        # ── 成交量突增 ──
        if vol_ma > 0 and volume > vol_ma * self.volume_spike_mult:
            vol_ratio = volume / vol_ma
            signals.append(ScanSignal(
                signal_type="VOLUME_SPIKE",
                direction="NEUTRAL",
                strength=min(1.0, (vol_ratio - self.volume_spike_mult) / 3 + 0.5),
                symbol=symbol, price=close,
                detail=f"成交量突增: 当前={volume:.0f}, 均值={vol_ma:.0f}, 倍数={vol_ratio:.1f}x",
                indicators={"vol_ratio": round(vol_ratio, 2), "volume": volume, "vol_ma": vol_ma},
            ))

        # ── 价格突破 ──
        if len(df) >= self.breakout_lookback:
            recent_high = float(df['high'].iloc[-self.breakout_lookback:-1].max())
            recent_low = float(df['low'].iloc[-self.breakout_lookback:-1].min())
            if high > recent_high:
                breakout_pct = (high - recent_high) / recent_high
                signals.append(ScanSignal(
                    signal_type="BREAKOUT",
                    direction="BULLISH",
                    strength=min(1.0, breakout_pct * 50 + 0.5),
                    symbol=symbol, price=close,
                    detail=f"突破 {self.breakout_lookback}K 高点: {high:.2f} > {recent_high:.2f} (+{breakout_pct:.2%})",
                    indicators={"breakout_pct": round(breakout_pct, 4), "recent_high": recent_high},
                ))
            elif low < recent_low:
                breakdown_pct = (recent_low - low) / recent_low
                signals.append(ScanSignal(
                    signal_type="BREAKOUT",
                    direction="BEARISH",
                    strength=min(1.0, breakdown_pct * 50 + 0.5),
                    symbol=symbol, price=close,
                    detail=f"跌破 {self.breakout_lookback}K 低点: {low:.2f} < {recent_low:.2f} (-{breakdown_pct:.2%})",
                    indicators={"breakdown_pct": round(breakdown_pct, 4), "recent_low": recent_low},
                ))

        # ── 黄金/死亡交叉 + ADX 确认 ──
        ma50 = indicators.get('MA_50')
        ma200 = indicators.get('MA_200')
        adx = indicators.get('ADX')
        if ma50 is not None and ma200 is not None and len(df) >= 205:
            try:
                ma_prev = TechnicalIndicators.calculate_moving_averages(df.iloc[:-1], periods=[50, 200])
                ma50_prev = float(ma_prev['MA_50'].iloc[-1])
                ma200_prev = float(ma_prev['MA_200'].iloc[-1])
                ma50_now = float(ma50.iloc[-1]) if isinstance(ma50, pd.Series) else float(ma50)
                ma200_now = float(ma200.iloc[-1]) if isinstance(ma200, pd.Series) else float(ma200)
                adx_now = float(adx.iloc[-1]) if isinstance(adx, pd.Series) else float(adx or 0)
                if ma50_prev <= ma200_prev and ma50_now > ma200_now:
                    signals.append(ScanSignal(
                        signal_type="GOLDEN_CROSS",
                        direction="BULLISH",
                        strength=min(1.0, 0.55 + max(0.0, adx_now - 20.0) / 25.0),
                        symbol=symbol, price=close,
                        detail=f"SMA50 上穿 SMA200，ADX={adx_now:.1f}",
                        indicators={"adx": round(adx_now, 2), "ma50": ma50_now, "ma200": ma200_now},
                    ))
                elif ma50_prev >= ma200_prev and ma50_now < ma200_now:
                    signals.append(ScanSignal(
                        signal_type="DEATH_CROSS",
                        direction="BEARISH",
                        strength=min(1.0, 0.55 + max(0.0, adx_now - 20.0) / 25.0),
                        symbol=symbol, price=close,
                        detail=f"SMA50 下穿 SMA200，ADX={adx_now:.1f}",
                        indicators={"adx": round(adx_now, 2), "ma50": ma50_now, "ma200": ma200_now},
                    ))
            except Exception as e:
                logger.debug(f"golden/death cross scan skipped {symbol}: {e}")

        # ── 急跌/急涨 (ATR Spike) ──
        if atr > 0:
            candle_range = high - low
            atr_ratio = candle_range / atr
            if atr_ratio >= self.atr_spike_mult:
                candle_dir = "BULLISH" if close > (high + low) / 2 else "BEARISH"
                change_pct = (close - float(df['close'].iloc[-2])) / float(df['close'].iloc[-2]) if len(df) >= 2 else 0
                if abs(change_pct) > 0.003:  # 至少 0.3% 变化
                    signals.append(ScanSignal(
                        signal_type="ATR_SPIKE",
                        direction=candle_dir,
                        strength=min(1.0, (atr_ratio - self.atr_spike_mult) / 2 + 0.5),
                        symbol=symbol, price=close,
                        detail=f"急{'涨' if candle_dir == 'BULLISH' else '跌'}: "
                               f"振幅={candle_range:.2f}, {atr_ratio:.1f}x ATR, 变化={change_pct:.2%}",
                        indicators={"atr_ratio": round(atr_ratio, 2), "change_pct": round(change_pct, 4)},
                    ))

        # ── 多周期共振（15m/1h/4h 方向一致）──
        mtf_sig = self._check_mtf_confluence(symbol, df)
        if mtf_sig is not None:
            signals.append(mtf_sig)

        return signals

    def _detect_price_shock(
        self,
        symbol: str,
        df: pd.DataFrame,
        close: float,
    ) -> Optional[ScanSignal]:
        """相对 lookback 根 K 线前的涨跌幅达到阈值 → 立即分析。"""
        if close <= 0:
            return None
        move_pct = 0.0
        ref_price = 0.0
        source = ""

        lb = max(1, int(self.big_move_lookback_bars))
        if len(df) > lb:
            ref_price = float(df["close"].iloc[-1 - lb])
            if ref_price > 0:
                move_pct = (close - ref_price) / ref_price
                source = f"{lb}x15m"

        # 与上次扫描价比较，捕捉更快的瞬时跳变
        prev = self._last_price.get(symbol)
        if prev and prev > 0:
            scan_move = (close - prev) / prev
            if abs(scan_move) > abs(move_pct):
                move_pct = scan_move
                ref_price = prev
                source = "scan"

        if abs(move_pct) < self.big_move_pct:
            return None

        direction = "BULLISH" if move_pct > 0 else "BEARISH"
        # 超过阈值越多强度越高
        over = abs(move_pct) / max(self.big_move_pct, 1e-6)
        strength = min(1.0, 0.55 + (over - 1.0) * 0.25)
        return ScanSignal(
            signal_type="PRICE_SHOCK",
            direction=direction,
            strength=round(strength, 3),
            symbol=symbol,
            price=close,
            detail=(
                f"价格急变 {move_pct:+.2%} "
                f"(ref={ref_price:.2f}→{close:.2f}, {source}, "
                f"阈值±{self.big_move_pct:.2%})"
            ),
            indicators={
                "move_pct": round(move_pct, 5),
                "ref_price": ref_price,
                "source": source,
                "threshold": self.big_move_pct,
            },
        )

    def _check_mtf_confluence(self, symbol: str, df_15m: pd.DataFrame) -> Optional[ScanSignal]:
        """多周期方向共振：复用 15m 数据，1h/4h 带缓存拉取。"""
        now = time.time()
        cached = self._mtf_cache.get(symbol)
        if cached and (now - cached[0]) < self.mtf_cache_seconds:
            result = cached[1]
        else:
            try:
                prefetched = {"15m": df_15m}
                if "1h" in self.mtf_timeframes:
                    df_1h = self._fetcher.get_klines(symbol=symbol, interval="1h", limit=100)
                    if df_1h is not None and len(df_1h) >= 30:
                        prefetched["1h"] = df_1h
                if "4h" in self.mtf_timeframes:
                    df_4h = self._fetcher.get_klines(symbol=symbol, interval="4h", limit=100)
                    if df_4h is not None and len(df_4h) >= 30:
                        prefetched["4h"] = df_4h
                result = self._mtf_analyzer.analyze(
                    symbol=symbol,
                    timeframes=[tf for tf in self.mtf_timeframes if tf in prefetched],
                    prefetched=prefetched,
                )
                self._mtf_cache[symbol] = (now, result)
            except Exception as e:
                logger.debug(f"MTF 分析失败 {symbol}: {e}")
                return None

        action = result.get("recommended_action")
        confluence = result.get("confluence", "")
        long_c = int(result.get("long_count", 0))
        short_c = int(result.get("short_count", 0))
        valid_tfs = [
            tf for tf, info in (result.get("timeframe_signals") or {}).items()
            if "error" not in info
        ]

        if action not in ("LONG", "SHORT") or len(valid_tfs) < 2:
            return None
        if confluence == "方向分歧":
            return None

        direction = "BULLISH" if action == "LONG" else "BEARISH"
        aligned = long_c if action == "LONG" else short_c
        strength = min(1.0, 0.55 + aligned * 0.12 + (0.1 if confluence == "强共振" else 0))

        tf_detail = ", ".join(
            f"{tf}:{info.get('direction', '?')}"
            for tf, info in (result.get("timeframe_signals") or {}).items()
            if "error" not in info
        )
        bar_close = float(df_15m["close"].iloc[-1])
        live_price = bar_close
        try:
            resolve = getattr(self._fetcher, "resolve_current_price", None)
            if callable(resolve):
                live = float(resolve(symbol, df_15m) or 0)
                if live > 0:
                    live_price = live
        except Exception:
            pass
        return ScanSignal(
            signal_type="MTF_CONFLUENCE",
            direction=direction,
            strength=round(strength, 2),
            symbol=symbol,
            price=live_price,
            detail=f"多周期{confluence}: {tf_detail} → {action}",
            indicators={
                "confluence": confluence,
                "weighted_score": result.get("weighted_score"),
                "long_count": long_c,
                "short_count": short_c,
            },
        )

    def _pending_path(self) -> Path:
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
        except Exception:
            root = Path(__file__).resolve().parents[2]
        return root / "data" / "pending_scanner_signal.json"

    def _persist_pending_scanner(self, payload: Dict) -> None:
        path = self._pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def consume_pending_scanner_signal(
    *,
    symbol: Optional[str] = None,
    max_age_sec: float = 900.0,
) -> Optional[Dict]:
    """读取并消费最近扫盘大波动（供验证探针注入方向）。过期则丢弃。"""
    try:
        from bnb_quant_tool.data_localization import get_localization_manager
        root = Path(get_localization_manager().workspace)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    path = root / "data" / "pending_scanner_signal.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    if not isinstance(payload, dict):
        return None
    if symbol and payload.get("symbol") and str(payload.get("symbol")).upper() != str(symbol).upper():
        return None
    ts = str(payload.get("timestamp") or "")
    if ts:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = (datetime.now(dt.tzinfo) - dt).total_seconds() if dt.tzinfo else 0
            if age > max_age_sec:
                return None
        except Exception:
            pass
    return payload


def inject_scanner_into_advice(trade_advice: Dict, *, symbol: Optional[str] = None) -> Dict:
    """把待处理扫盘方向写入 advice（原地修改并返回）。"""
    if not isinstance(trade_advice, dict):
        return trade_advice
    scan = consume_pending_scanner_signal(symbol=symbol or trade_advice.get("symbol"))
    if not scan:
        return trade_advice
    trade_advice["scanner_signal"] = scan
    trade_advice["scanner_direction"] = scan.get("trade_direction")
    votes = dict(trade_advice.get("votes") or {})
    if scan.get("trade_direction") in ("LONG", "SHORT"):
        votes["scanner_direction"] = scan["trade_direction"]
    trade_advice["votes"] = votes
    return trade_advice
