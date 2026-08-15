"""
BNB量化交易工具 - 多周期共振分析 (Multi-Timeframe Confluence)
================================================================
作用：同时分析 15m / 1h / 4h / 1d 多个时间框架，
只有"多周期方向一致"才视为强信号，避免单周期假信号。

为什么重要：
- 1h 看似上涨但 4h 在下降通道 → 大概率是反弹，买入容易被套
- 多周期共振时，胜率显著提升

输出：
- timeframe_signals: 每个周期的方向 + 强度
- confluence: 共振等级（强/中/弱/分歧）
- recommended_action: 综合推荐方向
- direction_bias: 偏多/偏空打分
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import logging

import pandas as pd

from .technical_indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


class MultiTimeframeAnalyzer:
    """多周期共振分析器"""

    # 默认权重：周期越长权重越高（大周期决定方向）
    DEFAULT_WEIGHTS = {
        "15m": 0.10,
        "1h": 0.20,
        "4h": 0.30,
        "1d": 0.40,
    }

    def __init__(self, fetcher=None, weights: Optional[Dict[str, float]] = None):
        """
        fetcher: 一个具有 get_historical_klines(symbol, interval, start_str) 方法的对象
                 （即 BinanceDataFetcher）
        """
        self.fetcher = fetcher
        self.weights = weights or self.DEFAULT_WEIGHTS

    # ============================================================
    # 主入口
    # ============================================================
    def analyze(
        self,
        symbol: str = "BNBUSDT",
        timeframes: Optional[List[str]] = None,
        lookback_days_map: Optional[Dict[str, int]] = None,
        prefetched: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Dict:
        """
        执行多周期分析。

        Args:
            timeframes: 要分析的周期列表，默认 ["15m","1h","4h","1d"]
            lookback_days_map: 每个周期回溯天数，默认根据周期合理设置
            prefetched: 已经获取好的 K 线数据 (跳过 fetcher 调用)

        Returns:
            完整的多周期分析结果
        """
        timeframes = timeframes or ["15m", "1h", "4h", "1d"]
        lookback_days_map = lookback_days_map or {
            "15m": 5, "1h": 20, "4h": 60, "1d": 180
        }

        per_tf: Dict[str, Dict] = {}
        prefetched = prefetched or {}

        def _load_tf(tf: str) -> tuple[str, Optional[pd.DataFrame], Optional[str]]:
            if tf in prefetched:
                return tf, prefetched[tf], None
            if self.fetcher is None:
                return tf, None, "无 fetcher"
            try:
                df = self.fetcher.get_historical_klines(
                    symbol=symbol,
                    interval=tf,
                    start_str=f"{lookback_days_map.get(tf, 30)} days ago",
                )
                return tf, df, None
            except Exception as e:
                logger.warning(f"获取 {tf} K线失败: {e}")
                return tf, None, str(e)

        tf_frames: Dict[str, Optional[pd.DataFrame]] = {}
        tf_errors: Dict[str, str] = {}

        # 已有 prefetched 的直接处理，其余并行拉取
        pending: List[str] = []
        for tf in timeframes:
            if tf in prefetched:
                tf_frames[tf] = prefetched[tf]
            else:
                pending.append(tf)

        if pending and self.fetcher is not None:
            max_workers = min(4, len(pending))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_load_tf, tf): tf for tf in pending}
                for fut in as_completed(futures):
                    tf, df, err = fut.result()
                    if err:
                        tf_errors[tf] = err
                    tf_frames[tf] = df
        elif pending:
            for tf in pending:
                tf_errors[tf] = "无 fetcher"

        for tf in timeframes:
            df = tf_frames.get(tf)
            if tf in tf_errors:
                per_tf[tf] = {"error": tf_errors[tf], "direction": "UNKNOWN", "score": 0.0}
                continue

            if df is None or len(df) < 30:
                per_tf[tf] = {"error": "数据不足", "direction": "UNKNOWN", "score": 0.0}
                continue

            per_tf[tf] = self._analyze_single(df)

        return self._summarize(symbol, per_tf)

    # ============================================================
    # 单周期分析
    # ============================================================
    def _analyze_single(self, df: pd.DataFrame) -> Dict:
        """对单个周期 K 线打分，返回方向 + 分数 + 关键指标"""
        try:
            ind = TechnicalIndicators.calculate_all_indicators(df.tail(200))
        except Exception as e:
            return {"error": str(e), "direction": "UNKNOWN", "score": 0.0}

        close = float(df["close"].iloc[-1])
        score = 0.0
        signals: List[str] = []

        rsi = ind.get("RSI")
        if rsi is not None:
            if rsi < 30:
                score += 1.5
                signals.append(f"RSI超卖({rsi:.1f})")
            elif rsi > 70:
                score -= 1.5
                signals.append(f"RSI超买({rsi:.1f})")
            elif rsi > 55:
                score += 0.3
            elif rsi < 45:
                score -= 0.3

        macd = ind.get("MACD")
        macd_sig = ind.get("MACD_Signal")
        if macd is not None and macd_sig is not None:
            if macd > macd_sig and macd > 0:
                score += 1.2
                signals.append("MACD多头")
            elif macd > macd_sig:
                score += 0.6
            elif macd < macd_sig and macd < 0:
                score -= 1.2
                signals.append("MACD空头")
            elif macd < macd_sig:
                score -= 0.6

        ma20 = ind.get("MA_20")
        ma50 = ind.get("MA_50")
        if ma20 and ma50:
            if close > ma20 > ma50:
                score += 1.0
                signals.append("均线多头排列")
            elif close < ma20 < ma50:
                score -= 1.0
                signals.append("均线空头排列")

        bb_low = ind.get("BB_Lower")
        bb_up = ind.get("BB_Upper")
        if bb_low and close <= bb_low * 1.005:
            score += 0.8
            signals.append("触及布林下轨")
        if bb_up and close >= bb_up * 0.995:
            score -= 0.8
            signals.append("触及布林上轨")

        # 趋势：close 相对 20根前的变化
        if len(df) >= 20:
            ret_20 = (close - float(df["close"].iloc[-20])) / float(df["close"].iloc[-20])
            if ret_20 > 0.05:
                score += 0.5
            elif ret_20 < -0.05:
                score -= 0.5

        # 限幅
        score = max(-4.0, min(4.0, score))

        # 方向判断
        if score >= 1.5:
            direction = "LONG"
        elif score <= -1.5:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "score": round(score, 2),
            "close": round(close, 4),
            "RSI": round(rsi, 2) if rsi is not None else None,
            "MACD": round(macd, 4) if macd is not None else None,
            "signals": signals,
        }

    # ============================================================
    # 汇总
    # ============================================================
    def _summarize(self, symbol: str, per_tf: Dict[str, Dict]) -> Dict:
        weighted_score = 0.0
        total_weight = 0.0
        long_count = 0
        short_count = 0
        neutral_count = 0
        valid_count = 0

        for tf, info in per_tf.items():
            if "error" in info:
                continue
            valid_count += 1
            w = self.weights.get(tf, 0.25)
            weighted_score += info["score"] * w
            total_weight += w
            d = info["direction"]
            if d == "LONG":
                long_count += 1
            elif d == "SHORT":
                short_count += 1
            else:
                neutral_count += 1

        if total_weight > 0:
            normalized = weighted_score / total_weight  # 范围约 [-4, 4]
        else:
            normalized = 0.0

        # 共振等级
        if valid_count >= 3 and (long_count >= 3 or short_count >= 3):
            confluence = "强共振"
        elif valid_count >= 2 and (long_count >= 2 or short_count >= 2):
            confluence = "中等共振"
        elif long_count > 0 and short_count > 0:
            confluence = "方向分歧"
        else:
            confluence = "弱信号"

        # 推荐方向
        if normalized >= 1.0 and long_count >= short_count:
            action = "LONG"
        elif normalized <= -1.0 and short_count >= long_count:
            action = "SHORT"
        else:
            action = "WAIT"

        # 共振分 -100..100（weighted_score ≈ -4..4 → ×25）
        resonance_score = max(-100.0, min(100.0, normalized * 25.0))

        return {
            "symbol": symbol,
            "timeframe_signals": per_tf,
            "long_count": long_count,
            "short_count": short_count,
            "neutral_count": neutral_count,
            "weighted_score": round(normalized, 3),
            "resonance_score": round(resonance_score, 1),
            "confluence": confluence,
            "recommended_action": action,
            "direction_bias": "偏多" if normalized > 0.3 else ("偏空" if normalized < -0.3 else "中性"),
        }

    # ============================================================
    # 文本报告
    # ============================================================
    @staticmethod
    def format_report(result: Dict) -> str:
        sep = "=" * 60
        lines = [sep, f"  多周期共振分析  -  {result.get('symbol', 'N/A')}", sep]
        lines.append(f"加权得分    : {result['weighted_score']}  (范围约 -4~+4)")
        if result.get("resonance_score") is not None:
            lines.append(f"共振分数    : {result['resonance_score']}  (范围 -100~+100)")
        lines.append(f"方向分布    : 多头 {result['long_count']} | 空头 {result['short_count']} | 中性 {result['neutral_count']}")
        lines.append(f"共振等级    : {result['confluence']}")
        lines.append(f"方向偏向    : {result['direction_bias']}")
        lines.append(f"推荐操作    : {result['recommended_action']}")
        lines.append("")
        lines.append("各周期详情:")
        for tf, info in result["timeframe_signals"].items():
            if "error" in info:
                lines.append(f"  [{tf:>3s}]  ⚠ {info['error']}")
                continue
            d = info["direction"]
            mark = "🟢" if d == "LONG" else ("🔴" if d == "SHORT" else "⚪")
            lines.append(
                f"  [{tf:>3s}]  {mark} {d:<7s} score={info['score']:+.2f}  "
                f"RSI={info.get('RSI')}  close={info.get('close')}"
            )
            if info.get("signals"):
                lines.append(f"        信号: {', '.join(info['signals'])}")
        lines.append(sep)
        return "\n".join(lines)
