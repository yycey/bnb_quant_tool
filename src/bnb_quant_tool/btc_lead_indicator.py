"""
BTC 领先 / BNB 滞后指标 — BTC 结构领先 BNB，检测跟随与背离。

机构逻辑：BTC 是 crypto beta 锚；BNB 常滞后 1–2 根周期。
- BTC 趋势向上 + BNB 未跟上 → 潜在补涨（偏多 BNB）
- BTC 趋势向下 + BNB 相对抗跌 → 假强势（慎多）
- 斜率背离 → 写入 conviction conflicts
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from bnb_quant_tool.market_regime import MarketRegimeDetector


def _pct_change(closes: np.ndarray, bars: int) -> float:
    if len(closes) <= bars:
        return 0.0
    start = float(closes[-bars - 1])
    end = float(closes[-1])
    if start <= 0:
        return 0.0
    return (end - start) / start


def _ma_slope(df: pd.DataFrame) -> float:
    if df is None or len(df) < 25:
        return 0.0
    return MarketRegimeDetector._ma_slope(df)


def compute_btc_lead_indicator(
    bnb_df: Optional[pd.DataFrame] = None,
    btc_df: Optional[pd.DataFrame] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """比较 BTC vs BNB 结构与动量，输出领先分数与背离标记。"""
    cfg = config or {}
    lookback = int(cfg.get("lookback_bars", 24))
    slope_th = float(cfg.get("slope_lag_ratio", 0.45))
    div_th = float(cfg.get("divergence_pct", 0.008))

    if bnb_df is None or btc_df is None:
        return {"available": False, "reason": "缺少 BNB/BTC K线"}
    if len(bnb_df) < 30 or len(btc_df) < 30:
        return {"available": False, "reason": "K线长度不足"}

    bnb_close = bnb_df["close"].astype(float).values
    btc_close = btc_df["close"].astype(float).values

    bnb_ret = _pct_change(bnb_close, lookback)
    btc_ret = _pct_change(btc_close, lookback)
    bnb_slope = _ma_slope(bnb_df)
    btc_slope = _ma_slope(btc_df)

    score = 0.0
    signals: list[str] = []
    divergence: Optional[str] = None

    # BTC 领先：BTC 强趋势，BNB 斜率明显落后
    if btc_slope > 0.001 and bnb_slope < btc_slope * slope_th:
        lag = btc_slope - bnb_slope
        boost = min(0.55, lag * 80)
        score += boost
        signals.append(f"BTC 领先上涨 (BTC斜率 {btc_slope:.4f} > BNB {bnb_slope:.4f}) — BNB 可能补涨")
    elif btc_slope < -0.001 and bnb_slope > btc_slope * slope_th:
        lag = bnb_slope - btc_slope
        penalty = min(0.55, lag * 80)
        score -= penalty
        signals.append(f"BTC 领先下跌但 BNB 抗跌 (BNB斜率 {bnb_slope:.4f}) — 假强势风险")

    # 收益率背离
    ret_diff = bnb_ret - btc_ret
    if btc_ret >= div_th and bnb_ret <= 0:
        divergence = "bullish_catch_up"
        score += 0.35
        signals.append(f"BTC +{btc_ret:.2%} 而 BNB {bnb_ret:+.2%} — 补涨窗口")
    elif btc_ret <= -div_th and bnb_ret >= -div_th * 0.3:
        divergence = "bearish_fake_strength"
        score -= 0.4
        signals.append(f"BTC {btc_ret:+.2%} 但 BNB 仅 {bnb_ret:+.2%} — 相对强势不可持续")
    elif btc_ret <= -div_th and bnb_ret < btc_ret - div_th:
        divergence = "bearish_lag"
        score -= 0.25
        signals.append(f"BNB 跌幅大于 BTC — 弱势跟随")

    score = float(max(-1.0, min(1.0, score)))
    follow = "aligned"
    if abs(btc_slope) > 0.0008:
        if btc_slope > 0 and bnb_slope > 0:
            follow = "both_up"
        elif btc_slope < 0 and bnb_slope < 0:
            follow = "both_down"
        elif btc_slope > 0 > bnb_slope:
            follow = "bnb_lagging"
        elif btc_slope < 0 < bnb_slope:
            follow = "bnb_resilient"

    summary = signals[0] if signals else (
        f"BTC/BNB 同步 (BTC {btc_ret:+.2%}, BNB {bnb_ret:+.2%})"
    )

    return {
        "available": True,
        "lead_score": round(score, 3),
        "btc_return_pct": round(btc_ret * 100, 3),
        "bnb_return_pct": round(bnb_ret * 100, 3),
        "btc_slope": round(btc_slope, 5),
        "bnb_slope": round(bnb_slope, 5),
        "follow_mode": follow,
        "divergence": divergence,
        "signals": signals,
        "summary": summary,
    }


def btc_lead_conviction_score(btc_lead: Optional[Dict]) -> Tuple[Optional[float], str]:
    """供 institutional_conviction 使用的分数与文案。"""
    if not btc_lead or not btc_lead.get("available"):
        return None, ""
    score = float(btc_lead.get("lead_score") or 0)
    text = btc_lead.get("summary") or ""
    div = btc_lead.get("divergence")
    if div == "bullish_catch_up":
        text += " — 机构视角：beta 滞后，可布局 BNB 跟随"
    elif div == "bearish_fake_strength":
        text += " — 机构视角：相对强势多为假突破"
    return score, text
