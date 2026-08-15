"""
BNB量化交易工具 - 回测引擎 (Backtest Engine)
================================================
作用：用历史 K 线 + 技术指标 + 机构策略 来"重放"过去 N 个月的市场，
验证某套规则到底能不能赚钱、最大回撤多大、胜率多少。

设计原则：
- 不调用 AI（AI 太慢且耗费 token），只用本地的技术信号 + 机构策略共识
- 严格按时间顺序前向滚动，不偷看未来（no look-ahead bias）
- 每笔交易完整记录：入场、止损、止盈、退出原因、PnL
- 输出可读报告 + JSON 结果

核心指标：
- 总收益率 / 年化收益
- 胜率 / 盈亏比
- 最大回撤 / 夏普比率（简化）
- 交易次数 / 平均持仓时长
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Callable
import math

import numpy as np
import pandas as pd

from .technical_indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------
@dataclass
class Trade:
    """一笔完整交易"""
    direction: str  # 'LONG' / 'SHORT'
    entry_time: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # 'TP' / 'SL' / 'TIMEOUT' / 'REVERSE'
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    hold_hours: float = 0.0


@dataclass
class BacktestResult:
    """回测最终结果"""
    initial_balance: float
    final_balance: float
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    win_rate: float
    profit_factor: float  # 总盈利/总亏损
    avg_win_pct: float
    avg_loss_pct: float
    total_trades: int
    win_trades: int
    loss_trades: int
    avg_hold_hours: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)


# ------------------------------------------------------------
# 默认信号生成函数（基于已有技术指标 + 简单规则）
# ------------------------------------------------------------
def default_signal_func(window_df: pd.DataFrame, indicators: Dict) -> Tuple[str, float]:
    """
    返回 (signal, confidence)
    signal: 'LONG' / 'SHORT' / 'HOLD'

    规则（多因子打分）：
      +1: RSI < 30 (超卖)
      +1: MACD > MACD_Signal (金叉)
      +1: 收盘价 > MA_20 > MA_50 (多头排列)
      +1: 收盘价触及 BB_Lower (反弹)
      -1 / -1 / -1 / -1: 对应反向条件
    score >= 2 -> LONG, <= -2 -> SHORT
    """
    score = 0
    rsi = indicators.get("RSI")
    macd = indicators.get("MACD")
    macd_sig = indicators.get("MACD_Signal")
    ma20 = indicators.get("MA_20")
    ma50 = indicators.get("MA_50")
    bb_low = indicators.get("BB_Lower")
    bb_up = indicators.get("BB_Upper")

    close = float(window_df["close"].iloc[-1])

    if rsi is not None:
        if rsi < 30:
            score += 1
        elif rsi > 70:
            score -= 1

    if macd is not None and macd_sig is not None:
        if macd > macd_sig:
            score += 1
        elif macd < macd_sig:
            score -= 1

    if ma20 and ma50:
        if close > ma20 > ma50:
            score += 1
        elif close < ma20 < ma50:
            score -= 1

    if bb_low and close <= bb_low * 1.005:
        score += 1
    if bb_up and close >= bb_up * 0.995:
        score -= 1

    if score >= 2:
        return "LONG", min(1.0, score / 4.0)
    if score <= -2:
        return "SHORT", min(1.0, abs(score) / 4.0)
    return "HOLD", 0.0


# ------------------------------------------------------------
# 回测器
# ------------------------------------------------------------
class BacktestEngine:
    """简单稳健的事件驱动型回测器"""

    def __init__(
        self,
        initial_balance: float = 10000.0,
        risk_per_trade: float = 0.015,      # 单笔风险 1.5%
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
        fee_rate: float = 0.0004,           # 双边 0.04% 手续费(maker+taker 平均)
        slippage_pct: float = 0.0005,       # 滑点 0.05%
        max_hold_bars: int = 96,            # 单笔最长持仓 K 线数（1h周期=4天）
        min_indicator_bars: int = 60,       # 至少多少根 K 线才能开始回测
        signal_func: Optional[Callable[[pd.DataFrame, Dict], Tuple[str, float]]] = None,
        timeframe_hours: float = 1.0,
    ):
        self.initial_balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.fee_rate = fee_rate
        self.slippage_pct = slippage_pct
        self.max_hold_bars = max_hold_bars
        self.min_indicator_bars = min_indicator_bars
        self.signal_func = signal_func or default_signal_func
        self.timeframe_hours = timeframe_hours

    # ============================================================
    # 主入口
    # ============================================================
    def run(self, df: pd.DataFrame) -> BacktestResult:
        """对一段 K 线数据进行完整回测。

        df 需要包含: timestamp, open, high, low, close, volume 列
        """
        if df is None or len(df) < self.min_indicator_bars + 10:
            raise ValueError(f"数据不足，至少需要 {self.min_indicator_bars + 10} 根 K 线")

        df = df.copy()
        if "timestamp" not in df.columns and df.index.name == "timestamp":
            df = df.reset_index()

        balance = float(self.initial_balance)
        equity_curve: List[Tuple[str, float]] = []
        trades: List[Trade] = []
        open_trade: Optional[Trade] = None
        open_bar_index: int = -1

        n = len(df)
        for i in range(self.min_indicator_bars, n):
            row = df.iloc[i]
            ts = self._fmt_ts(row.get("timestamp"))
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            # ---------- 1) 处理已开仓的持仓退出 ----------
            if open_trade is not None:
                exit_price, exit_reason = self._check_exit(open_trade, high, low, close)
                if exit_reason or (i - open_bar_index) >= self.max_hold_bars:
                    if not exit_reason:
                        exit_reason = "TIMEOUT"
                        exit_price = close
                    self._close_trade(open_trade, exit_price, exit_reason, ts, i - open_bar_index)
                    pnl = open_trade.pnl_usdt
                    balance += pnl
                    trades.append(open_trade)
                    open_trade = None

            # 当前权益（含未平仓盈亏估算）
            float_equity = balance
            if open_trade is not None:
                float_equity = balance + self._floating_pnl(open_trade, close)
            equity_curve.append((ts, round(float_equity, 4)))

            # ---------- 2) 没持仓 → 看是否开仓 ----------
            if open_trade is None:
                window = df.iloc[: i + 1]
                # 用最近 200 根算指标（节省时间）
                lookback = window.tail(min(len(window), 200))
                try:
                    indicators = TechnicalIndicators.calculate_all_indicators(lookback)
                except Exception as e:
                    logger.debug(f"指标计算失败: {e}")
                    continue

                signal, confidence = self.signal_func(window, indicators)
                if signal in ("LONG", "SHORT") and confidence >= 0.4:
                    atr = float(indicators.get("ATR") or close * 0.01)
                    if atr <= 0:
                        continue
                    if signal == "LONG":
                        sl = close - atr * self.atr_sl_mult
                        tp = close + atr * self.atr_tp_mult
                    else:
                        sl = close + atr * self.atr_sl_mult
                        tp = close - atr * self.atr_tp_mult
                    risk_per_unit = abs(close - sl)
                    if risk_per_unit <= 0:
                        continue
                    risk_amount = balance * self.risk_per_trade
                    qty = risk_amount / risk_per_unit
                    # 不允许仓位超过账户 30%
                    cap = balance * 0.3 / max(close, 1e-6)
                    qty = min(qty, cap)
                    if qty * close < 10:  # 太小直接跳过
                        continue

                    # 加滑点 + 手续费扣减
                    entry_eff = close * (1 + self.slippage_pct) if signal == "LONG" else close * (1 - self.slippage_pct)
                    fee = entry_eff * qty * self.fee_rate
                    balance -= fee  # 入场手续费

                    open_trade = Trade(
                        direction=signal,
                        entry_time=ts,
                        entry_price=round(entry_eff, 6),
                        stop_loss=round(sl, 6),
                        take_profit=round(tp, 6),
                        quantity=round(qty, 8),
                    )
                    open_bar_index = i

        # 收尾：还有未平仓的强制平仓
        if open_trade is not None:
            last = df.iloc[-1]
            ts = self._fmt_ts(last.get("timestamp"))
            self._close_trade(open_trade, float(last["close"]), "FORCED", ts, n - 1 - open_bar_index)
            balance += open_trade.pnl_usdt
            trades.append(open_trade)

        return self._summarize(balance, trades, equity_curve)

    # ============================================================
    # 内部方法
    # ============================================================
    def _check_exit(self, t: Trade, high: float, low: float, close: float) -> Tuple[float, str]:
        """检查是否在本根 K 线触及止损/止盈"""
        if t.direction == "LONG":
            if low <= t.stop_loss:
                return t.stop_loss, "SL"
            if high >= t.take_profit:
                return t.take_profit, "TP"
        else:
            if high >= t.stop_loss:
                return t.stop_loss, "SL"
            if low <= t.take_profit:
                return t.take_profit, "TP"
        return 0.0, ""

    def _close_trade(self, t: Trade, exit_price: float, reason: str, exit_time: str, bars: int):
        """平仓并计算 PnL（含滑点、手续费）"""
        # 出场滑点
        if t.direction == "LONG":
            eff = exit_price * (1 - self.slippage_pct)
            pnl_per_unit = eff - t.entry_price
        else:
            eff = exit_price * (1 + self.slippage_pct)
            pnl_per_unit = t.entry_price - eff
        pnl_usdt = pnl_per_unit * t.quantity
        # 出场手续费
        pnl_usdt -= eff * t.quantity * self.fee_rate

        t.exit_time = exit_time
        t.exit_price = round(eff, 6)
        t.exit_reason = reason
        t.pnl_usdt = round(pnl_usdt, 4)
        t.pnl_pct = round(pnl_per_unit / max(t.entry_price, 1e-6) * 100, 4)
        t.hold_hours = round(bars * self.timeframe_hours, 2)

    def _floating_pnl(self, t: Trade, price: float) -> float:
        if t.direction == "LONG":
            return (price - t.entry_price) * t.quantity
        return (t.entry_price - price) * t.quantity

    @staticmethod
    def _fmt_ts(ts) -> str:
        try:
            if isinstance(ts, (pd.Timestamp, datetime)):
                return ts.strftime("%Y-%m-%d %H:%M")
            return str(ts)[:16]
        except Exception:
            return str(ts)[:16]

    # ============================================================
    # 汇总报告
    # ============================================================
    def _summarize(
        self, final_balance: float, trades: List[Trade],
        equity_curve: List[Tuple[str, float]]
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                initial_balance=self.initial_balance,
                final_balance=final_balance,
                total_return_pct=0.0, annual_return_pct=0.0,
                max_drawdown_pct=0.0, sharpe_ratio=0.0,
                win_rate=0.0, profit_factor=0.0,
                avg_win_pct=0.0, avg_loss_pct=0.0,
                total_trades=0, win_trades=0, loss_trades=0,
                avg_hold_hours=0.0, trades=[], equity_curve=equity_curve
            )

        wins = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        gross_win = sum(t.pnl_usdt for t in wins)
        gross_loss = abs(sum(t.pnl_usdt for t in losses))

        total_return = (final_balance - self.initial_balance) / self.initial_balance
        # 估算年化（按 equity_curve 时长）
        if len(equity_curve) >= 2:
            try:
                t0 = pd.to_datetime(equity_curve[0][0])
                t1 = pd.to_datetime(equity_curve[-1][0])
                days = max(1.0, (t1 - t0).total_seconds() / 86400.0)
            except Exception:
                days = max(1.0, len(equity_curve) * self.timeframe_hours / 24)
        else:
            days = 1.0
        annual = (1 + total_return) ** (365.0 / days) - 1 if (1 + total_return) > 0 else -1.0

        # 最大回撤
        max_dd = 0.0
        peak = -math.inf
        for _, eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (eq - peak) / peak
                max_dd = min(max_dd, dd)

        # 夏普（简化：按每根K线的权益变化）
        if len(equity_curve) >= 5:
            eq = np.array([e for _, e in equity_curve], dtype=float)
            rets = np.diff(eq) / eq[:-1]
            if rets.std() > 0:
                bars_per_year = 365.0 * 24.0 / max(self.timeframe_hours, 1e-6)
                sharpe = (rets.mean() / rets.std()) * math.sqrt(bars_per_year)
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        return BacktestResult(
            initial_balance=self.initial_balance,
            final_balance=round(final_balance, 4),
            total_return_pct=round(total_return * 100, 3),
            annual_return_pct=round(annual * 100, 3),
            max_drawdown_pct=round(max_dd * 100, 3),
            sharpe_ratio=round(sharpe, 3),
            win_rate=round(len(wins) / len(trades), 4),
            profit_factor=round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
            avg_win_pct=round(np.mean([t.pnl_pct for t in wins]) if wins else 0.0, 3),
            avg_loss_pct=round(np.mean([t.pnl_pct for t in losses]) if losses else 0.0, 3),
            total_trades=len(trades),
            win_trades=len(wins),
            loss_trades=len(losses),
            avg_hold_hours=round(np.mean([t.hold_hours for t in trades]), 2),
            trades=trades,
            equity_curve=equity_curve,
        )

    # ============================================================
    # 文本报告
    # ============================================================
    @staticmethod
    def format_report(r: BacktestResult, title: str = "BNB 回测报告") -> str:
        sep = "=" * 70
        lines = [sep, f"  {title}", sep]
        lines.append(f"初始资金     : {r.initial_balance:,.2f} USDT")
        lines.append(f"最终资金     : {r.final_balance:,.2f} USDT")
        lines.append(f"总收益率     : {r.total_return_pct:+.2f}%")
        lines.append(f"年化收益率   : {r.annual_return_pct:+.2f}%")
        lines.append(f"最大回撤     : {r.max_drawdown_pct:+.2f}%")
        lines.append(f"夏普比率     : {r.sharpe_ratio:.3f}")
        lines.append(f"胜率         : {r.win_rate:.1%}")
        lines.append(f"盈亏比       : {r.profit_factor:.2f}")
        lines.append(f"平均盈利     : {r.avg_win_pct:+.2f}%")
        lines.append(f"平均亏损     : {r.avg_loss_pct:+.2f}%")
        lines.append(f"总交易数     : {r.total_trades}  (盈 {r.win_trades} / 亏 {r.loss_trades})")
        lines.append(f"平均持仓时长 : {r.avg_hold_hours:.1f} 小时")
        lines.append(sep)

        # 评估
        verdict = []
        if r.total_return_pct > 0 and r.win_rate >= 0.45 and r.profit_factor >= 1.3:
            verdict.append("✅ 策略整体可用，可考虑小资金前瞻测试")
        elif r.total_return_pct > 0 and r.profit_factor >= 1.0:
            verdict.append("⚠ 策略勉强盈利，建议优化参数后再用")
        else:
            verdict.append("❌ 策略当前不能稳定盈利，请勿直接实盘")
        if r.max_drawdown_pct < -25:
            verdict.append("❌ 最大回撤超过 25%，风险过高")
        if r.total_trades < 10:
            verdict.append("⚠ 交易样本太少，统计结果不可靠")
        lines.append("评估:")
        for v in verdict:
            lines.append(f"  {v}")
        lines.append(sep)

        # 最近 5 笔
        if r.trades:
            lines.append("最近 5 笔交易:")
            for t in r.trades[-5:]:
                lines.append(
                    f"  [{t.direction}] {t.entry_time}@{t.entry_price} -> "
                    f"{t.exit_time}@{t.exit_price} ({t.exit_reason}) "
                    f"PnL {t.pnl_pct:+.2f}% / {t.pnl_usdt:+.2f}U"
                )
            lines.append(sep)
        return "\n".join(lines)

    @staticmethod
    def to_json(r: BacktestResult) -> Dict:
        d = asdict(r)
        d["trades"] = [asdict(t) for t in r.trades]
        return d


if __name__ == "__main__":
    # 自测：用 demo_main 风格生成的随机数据回测
    import numpy as np
    rng = np.random.default_rng(42)
    n = 1500
    prices = 600 + np.cumsum(rng.normal(0, 1.5, n))
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="1H"),
        "open": prices + rng.normal(0, 0.3, n),
        "high": prices + np.abs(rng.normal(0, 1, n)),
        "low": prices - np.abs(rng.normal(0, 1, n)),
        "close": prices,
        "volume": rng.uniform(800, 2000, n),
    })
    eng = BacktestEngine(initial_balance=5000)
    res = eng.run(df)
    print(BacktestEngine.format_report(res))
