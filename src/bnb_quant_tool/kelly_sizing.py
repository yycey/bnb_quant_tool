"""凯利仓位变体 + 净盈亏比工具。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def half_kelly_fraction(
    win_rate: float,
    payoff_ratio: float,
    *,
    confidence: float = 0.5,
    fraction: float = 0.5,
    max_f: float = 0.20,
    min_f: float = 0.0,
) -> float:
    """半凯利：f* = p - (1-p)/b，再 ×fraction，并用置信度缩放。

    win_rate: 历史胜率 0~1
    payoff_ratio: 盈亏比 b = 平均盈利/平均亏损（>0）
    """
    p = max(0.01, min(0.99, float(win_rate or 0.5)))
    b = max(0.05, float(payoff_ratio or 1.0))
    edge = p - (1.0 - p) / b
    if edge <= 0:
        return 0.0
    conf = max(0.0, min(1.0, float(confidence or 0.5)))
    # 置信度 <0.55 时进一步打折
    conf_scale = 0.35 + 0.65 * conf
    f = edge * float(fraction) * conf_scale
    return max(float(min_f), min(float(max_f), f))


def estimate_payoff_from_prices(prices: Optional[Dict]) -> float:
    """用止损/止盈距离估算盈亏比。"""
    if not isinstance(prices, dict):
        return 1.5
    entry = float(prices.get("entry_mid") or prices.get("entry") or 0)
    sl = float(prices.get("stop_loss") or 0)
    tp = float(prices.get("tp2") or prices.get("tp1") or 0)
    if entry <= 0 or sl <= 0 or tp <= 0:
        return 1.5
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 1e-12:
        return 1.5
    return max(0.2, reward / risk)


def net_reward_risk_ratio(
    prices: Optional[Dict],
    *,
    fee_rate: float = 0.0004,
    slippage_pct: float = 0.002,
    funding_pct: float = 0.0,
    hold_periods: int = 1,
    liquidity_premium_pct: float = 0.0,
) -> Tuple[float, Dict[str, float]]:
    """预期净盈亏比 = 预期盈利 / (预期亏损 + 双边手续费 + 滑点 + 资金费 + 流动性溢价)。"""
    prices = prices or {}
    entry = float(prices.get("entry_mid") or prices.get("entry") or 0)
    sl = float(prices.get("stop_loss") or 0)
    tp = float(prices.get("tp2") or prices.get("tp1") or 0)
    if entry <= 0 or sl <= 0 or tp <= 0:
        return 0.0, {"reason": "missing_prices"}

    reward = abs(tp - entry) / entry
    risk = abs(entry - sl) / entry
    roundtrip_fee = 2.0 * float(fee_rate)
    slip = 2.0 * float(slippage_pct)
    funding = abs(float(funding_pct)) * max(1, int(hold_periods))
    # funding_pct 若已是「整段持仓总成本」则 hold_periods 应传 1
    liq = abs(float(liquidity_premium_pct))
    denom = risk + roundtrip_fee + slip + funding + liq
    if denom <= 1e-12:
        return 0.0, {"reason": "zero_denom"}
    ratio = reward / denom
    detail = {
        "reward_pct": round(reward, 6),
        "risk_pct": round(risk, 6),
        "fee_pct": round(roundtrip_fee, 6),
        "slip_pct": round(slip, 6),
        "funding_pct": round(funding, 6),
        "liquidity_premium_pct": round(liq, 6),
        "net_rr": round(ratio, 4),
    }
    return ratio, detail


def kelly_position_scale(
    *,
    win_rate: float,
    prices: Optional[Dict],
    confidence: float,
    config: Optional[Dict[str, Any]] = None,
    skip_confidence_probe: bool = False,
) -> Dict[str, Any]:
    """返回相对基础仓位的缩放系数（相对 max_position 的凯利占比归一化）。"""
    cfg = (config or {}).get("kelly") or (config or {})
    if cfg.get("enabled", True) is False:
        return {"factor": 1.0, "kelly_f": 0.0, "enabled": False}

    payoff = estimate_payoff_from_prices(prices)
    f = half_kelly_fraction(
        win_rate,
        payoff,
        confidence=confidence,
        fraction=float(cfg.get("fraction", 0.5) or 0.5),
        max_f=float(cfg.get("max_fraction", cfg.get("max_f", 0.20)) or 0.20),
        min_f=float(cfg.get("min_fraction", 0.0) or 0.0),
    )
    # 以「满仓上限」为 1.0：f/max_f
    max_f = float(cfg.get("max_fraction", cfg.get("max_f", 0.20)) or 0.20)
    scale = (f / max_f) if max_f > 0 else 0.0
    # 置信度不足 → 小仓试探；若 confidence_hard 已 probe 则跳过，避免 ×0.35²
    if not skip_confidence_probe:
        probe = float(cfg.get("low_conf_probe_scale", 0.35) or 0.35)
        # 与 ai_trading.min_open_confidence 对齐（可读外层 config）
        full_min = float(cfg.get("full_size_min_confidence", 0.70) or 0.70)
        parent = config or {}
        ai = parent.get("ai_trading") or {}
        if ai.get("min_open_confidence") is not None:
            full_min = float(ai.get("min_open_confidence") or full_min)
        if ai.get("probe_position_scale") is not None:
            probe = float(ai.get("probe_position_scale") or probe)
        if confidence < full_min:
            scale = min(scale, probe)
    scale = max(float(cfg.get("min_scale", 0.15) or 0.15), min(1.0, scale))
    return {
        "factor": round(scale, 4),
        "kelly_f": round(f, 4),
        "payoff_ratio": round(payoff, 3),
        "win_rate": round(float(win_rate or 0), 3),
        "enabled": True,
        "probe_skipped": bool(skip_confidence_probe),
    }
