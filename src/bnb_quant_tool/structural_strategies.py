"""
结构性策略层 — Funding 拥挤 carry / Basis 价差（软投票，与硬门控互补）。

机构逻辑：Funding 不只 block long，在中等极端时参与方向投票。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

ACTION_LONG = "LONG"
ACTION_SHORT = "SHORT"
ACTION_WAIT = "WAIT"


def _extract_funding_rate(
    sentiment: Optional[Dict],
    bnb_factors: Optional[Dict],
) -> Optional[float]:
    bnb = bnb_factors or {}
    rs = bnb.get("risk_sentry") or {}
    fr = rs.get("funding_extreme") or {}
    if fr.get("rate") is not None:
        return float(fr["rate"])
    if sentiment:
        fr2 = sentiment.get("funding_rate") or {}
        if isinstance(fr2, dict) and fr2.get("rate") is not None:
            return float(fr2["rate"])
    return None


def compute_funding_carry_signal(
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Funding 拥挤 → 反向 carry 投票（delta-neutral 机构压拥挤侧）。"""
    cfg = (config or {}).get("funding_carry") or config or {}
    if cfg.get("enabled") is False:
        return {"enabled": False, "signal": "HOLD", "bias": 0.0}

    rate = _extract_funding_rate(sentiment, bnb_factors)
    if rate is None:
        return {"enabled": True, "signal": "HOLD", "bias": 0.0, "detail": "Funding 无数据"}

    ext_long = float(cfg.get("extreme_long_rate", 0.001))
    ext_short = float(cfg.get("extreme_short_rate", -0.001))
    mod_long = float(cfg.get("moderate_long_rate", 0.0005))
    mod_short = float(cfg.get("moderate_short_rate", -0.0005))
    vote_weight = float(cfg.get("vote_weight", 0.12))

    bias = 0.0
    signal = "HOLD"
    detail = f"Funding {rate:+.4%} 中性"

    if rate >= ext_long:
        bias = -0.85
        signal = "SELL"
        detail = f"Funding 极正 {rate:+.4%} — 多头拥挤，carry 偏空"
    elif rate >= mod_long:
        bias = -0.45
        signal = "SELL"
        detail = f"Funding 偏正 {rate:+.4%} — 轻度 crowding 偏空"
    elif rate <= ext_short:
        bias = 0.75
        signal = "BUY"
        detail = f"Funding 极负 {rate:+.4%} — 空头拥挤，carry 偏多"
    elif rate <= mod_short:
        bias = 0.35
        signal = "BUY"
        detail = f"Funding 偏负 {rate:+.4%} — 轻度 crowding 偏多"

    return {
        "enabled": True,
        "signal": signal,
        "bias": round(bias, 3),
        "vote_weight": vote_weight,
        "funding_rate": rate,
        "detail": detail,
        "strategy": "funding_carry_arb",
    }


def compute_basis_spread_signal(
    fetcher=None,
    symbol: str = "BNBUSDT",
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Perp-spot basis 均值回归（需 premiumIndex，默认关闭）。"""
    cfg = (config or {}).get("basis") or {}
    if cfg.get("enabled") is not True:
        return {"enabled": False, "signal": "HOLD", "bias": 0.0, "detail": "Basis 未启用"}
    # 预留：接入 Binance premiumIndex + spot 后实现
    return {"enabled": True, "signal": "HOLD", "bias": 0.0, "detail": "Basis 数据源待接入"}


def compute_structural_vote(
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """合并结构性信号，供 trade_advisor 投票层使用。"""
    cfg = config or {}
    carry = compute_funding_carry_signal(sentiment, bnb_factors, cfg)
    basis = compute_basis_spread_signal(config=cfg)

    long_score = 0.0
    short_score = 0.0
    parts = []

    for sig in (carry, basis):
        if not sig.get("enabled"):
            continue
        w = float(sig.get("vote_weight") or 0.1)
        bias = float(sig.get("bias") or 0)
        if bias > 0:
            long_score += w * bias
        elif bias < 0:
            short_score += w * abs(bias)
        if sig.get("detail"):
            parts.append(sig["detail"])

    direction = ACTION_WAIT
    if long_score > short_score + 0.02:
        direction = ACTION_LONG
    elif short_score > long_score + 0.02:
        direction = ACTION_SHORT

    return {
        "direction": direction,
        "long_score": round(long_score, 4),
        "short_score": round(short_score, 4),
        "signals": {"funding_carry": carry, "basis": basis},
        "summary": " | ".join(parts) if parts else "结构性信号中性",
    }
