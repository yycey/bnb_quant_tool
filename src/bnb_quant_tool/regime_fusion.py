"""
市场状态多信号融合 — 趋势 / 波动 / Funding / 恐惧贪婪 独立投票后融合。

机构实践：单指标 regime 易误判；要求 3+ 独立信号一致才切换状态（降低 whipsaw）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from bnb_quant_tool.market_regime import (
    EUPHORIA,
    HIGH_VOLATILITY,
    LOW_VOLATILITY,
    PANIC,
    RANGING,
    TRENDING,
    MarketRegimeDetector,
)

# 融合桶 → 细粒度 regime
BUCKET_TREND = "TREND"
BUCKET_RANGE = "RANGE"
BUCKET_VOLATILE = "VOLATILE"
BUCKET_PANIC = "PANIC"
BUCKET_EUPHORIA = "EUPHORIA"


def _bucket_to_regime(bucket: str, trend_dir: int = 0) -> str:
    if bucket == BUCKET_TREND:
        return TRENDING
    if bucket == BUCKET_RANGE:
        return RANGING
    if bucket == BUCKET_VOLATILE:
        return HIGH_VOLATILITY
    if bucket == BUCKET_PANIC:
        return PANIC
    if bucket == BUCKET_EUPHORIA:
        return EUPHORIA
    return RANGING


def _signal_trend(df: pd.DataFrame, indicators: Dict, cfg: Dict) -> Tuple[str, str]:
    """MA 斜率 + 价格相对 MA20。"""
    slope_th = float(cfg.get("trend_slope_threshold", 0.0015))
    slope = MarketRegimeDetector._ma_slope(df)
    close = float(df["close"].iloc[-1]) if df is not None and len(df) else 0
    ma20 = float(indicators.get("MA_20") or indicators.get("SMA_20") or 0)
    above_ma = close > ma20 if ma20 > 0 else slope > 0

    if slope >= slope_th and above_ma:
        return BUCKET_TREND, f"趋势向上 (斜率 {slope:.4f}, 价>MA20)"
    if slope <= -slope_th and not above_ma:
        return BUCKET_PANIC if slope <= -slope_th * 2 else BUCKET_TREND, f"趋势向下 (斜率 {slope:.4f})"
    return BUCKET_RANGE, f"横盘 (斜率 {slope:.4f})"


def _signal_volatility(df: pd.DataFrame, indicators: Dict, cfg: Dict) -> Tuple[str, str]:
    atr = float(indicators.get("ATR") or 0)
    avg_atr = MarketRegimeDetector._avg_atr(df)
    hi = float(cfg.get("high_vol_atr_ratio", 1.35))
    lo = float(cfg.get("low_vol_atr_ratio", 0.75))
    if atr > 0 and avg_atr > 0:
        ratio = atr / avg_atr
        if ratio >= hi:
            return BUCKET_VOLATILE, f"高波动 ATR比={ratio:.2f}"
        if ratio <= lo:
            return BUCKET_RANGE, f"低波动 ATR比={ratio:.2f}"
        return BUCKET_TREND, f"波动正常 ATR比={ratio:.2f}"
    return BUCKET_RANGE, "波动数据不足"


def _signal_funding(sentiment: Optional[Dict], bnb_factors: Optional[Dict]) -> Tuple[str, str]:
    rate = None
    bnb = bnb_factors or {}
    rs = bnb.get("risk_sentry") or {}
    fr = rs.get("funding_extreme") or {}
    if fr.get("rate") is not None:
        rate = float(fr["rate"])
    elif sentiment:
        fr2 = sentiment.get("funding_rate") or {}
        if isinstance(fr2, dict) and fr2.get("rate") is not None:
            rate = float(fr2["rate"])

    if rate is None:
        return BUCKET_RANGE, "Funding 无数据"

    if rate >= 0.0008:
        return BUCKET_EUPHORIA, f"Funding 极正 {rate:+.4%} (多头拥挤→易回调)"
    if rate >= 0.0003:
        return BUCKET_TREND, f"Funding 偏正 {rate:+.4%}"
    if rate <= -0.0008:
        return BUCKET_PANIC, f"Funding 极负 {rate:+.4%} (空头拥挤→易反弹)"
    if rate <= -0.0003:
        return BUCKET_RANGE, f"Funding 偏负 {rate:+.4%}"
    return BUCKET_RANGE, f"Funding 中性 {rate:+.4%}"


def _signal_fear_greed(sentiment: Optional[Dict], cfg: Dict) -> Tuple[str, str]:
    fng = (sentiment or {}).get("fear_greed") or {}
    if fng.get("error"):
        return BUCKET_RANGE, "恐惧贪婪无数据"
    val = int(fng.get("value") or 50)
    panic_th = int(cfg.get("fng_panic", 22))
    euph_th = int(cfg.get("fng_euphoria", 78))
    if val <= panic_th:
        return BUCKET_PANIC, f"恐惧贪婪 {val} 极度恐惧"
    if val >= euph_th:
        return BUCKET_EUPHORIA, f"恐惧贪婪 {val} 极度贪婪"
    if val <= 40:
        return BUCKET_RANGE, f"恐惧贪婪 {val} 偏恐惧"
    if val >= 60:
        return BUCKET_TREND, f"恐惧贪婪 {val} 偏贪婪"
    return BUCKET_RANGE, f"恐惧贪婪 {val} 中性"


def fuse_regime(
    df: pd.DataFrame,
    indicators: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    config: Optional[Dict] = None,
    legacy_regime: Optional[str] = None,
) -> Dict[str, Any]:
    """四路独立信号投票 → 融合 regime + 置信度。"""
    indicators = indicators or {}
    cfg = config or {}
    min_votes = int(cfg.get("fusion_min_agreement", 2))

    votes: List[Dict[str, str]] = []
    buckets: List[str] = []

    b1, d1 = _signal_trend(df, indicators, cfg)
    votes.append({"signal": "trend", "bucket": b1, "detail": d1})
    buckets.append(b1)

    b2, d2 = _signal_volatility(df, indicators, cfg)
    votes.append({"signal": "volatility", "bucket": b2, "detail": d2})
    buckets.append(b2)

    b3, d3 = _signal_funding(sentiment, bnb_factors)
    votes.append({"signal": "funding", "bucket": b3, "detail": d3})
    buckets.append(b3)

    b4, d4 = _signal_fear_greed(sentiment, cfg)
    votes.append({"signal": "fear_greed", "bucket": b4, "detail": d4})
    buckets.append(b4)

    # 多数票
    from collections import Counter
    counts = Counter(buckets)
    winner, count = counts.most_common(1)[0]
    confidence = count / len(buckets)

    # 冲突检测：趋势 vs funding 相反
    conflicts: List[str] = []
    trend_b = buckets[0]
    fund_b = buckets[2]
    if trend_b == BUCKET_TREND and fund_b in (BUCKET_EUPHORIA, BUCKET_PANIC):
        conflicts.append("趋势偏多但 Funding 显示拥挤/极端 — 慎追多")
    if trend_b == BUCKET_PANIC and fund_b == BUCKET_RANGE:
        conflicts.append("趋势偏空但 Funding 未确认 — 可能假跌破")

    # 不足共识 → 震荡
    if count < min_votes:
        regime = RANGING
        desc = f"信号分裂 ({count}/{len(buckets)} 一致) — 按震荡处理"
    else:
        regime = _bucket_to_regime(winner)
        desc = f"多信号融合 → {regime} ({count}/{len(buckets)} 票)"

    if legacy_regime and legacy_regime != regime and count < len(buckets) - 1:
        conflicts.append(f"融合={regime} vs 规则={legacy_regime}")

    return {
        "regime": regime,
        "fusion_confidence": round(confidence, 3),
        "fusion_winner": winner,
        "regime_votes": votes,
        "regime_conflicts": conflicts,
        "description": desc,
        "multi_signal_fusion": True,
    }


def merge_fusion_into_regime(
    base: Dict[str, Any],
    fusion: Dict[str, Any],
    prefer_fusion: bool = True,
) -> Dict[str, Any]:
    """将融合结果合并进 market_regime 字典。"""
    out = dict(base)
    if not fusion:
        return out
    out["regime_votes"] = fusion.get("regime_votes")
    out["regime_conflicts"] = fusion.get("regime_conflicts")
    out["fusion_confidence"] = fusion.get("fusion_confidence")
    out["legacy_regime"] = base.get("regime")
    if prefer_fusion and fusion.get("regime"):
        out["regime"] = fusion["regime"]
        out["description"] = fusion.get("description") or out.get("description")
        reasons = list(out.get("reasons") or [])
        for v in fusion.get("regime_votes") or []:
            reasons.append(f"{v['signal']}: {v['detail']}")
        out["reasons"] = reasons[:8]
    return out
