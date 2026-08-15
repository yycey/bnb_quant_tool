"""全链路交易成本估计：滑点均值 / 手续费 / 资金费 / 流动性溢价。"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def estimate_slippage_from_paper(
    paper_engine=None,
    *,
    lookback: int = 100,
    fallback_pct: float = 0.002,
    config: Optional[dict] = None,
) -> Tuple[float, Dict[str, Any]]:
    """最近 N 笔：用 advice_snapshot.entry_mid 与实际 entry_price 估单边滑点均值。"""
    pt = (config or {}).get("paper_trading") or {}
    lo = _f(pt.get("slippage_min_pct"), 0.001)
    hi = _f(pt.get("slippage_max_pct"), 0.005)
    cfg_mid = (lo + hi) / 2.0 if hi >= lo else fallback_pct

    if paper_engine is None:
        return float(fallback_pct or cfg_mid), {
            "source": "fallback_config",
            "slippage_pct": float(fallback_pct or cfg_mid),
            "n": 0,
        }

    db = getattr(paper_engine, "db_path", None)
    if not db:
        return float(fallback_pct or cfg_mid), {"source": "fallback_no_db", "n": 0}

    slips = []
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT entry_price, advice_snapshot FROM paper_positions
                WHERE entry_price IS NOT NULL AND entry_price > 0
                ORDER BY id DESC LIMIT ?
                """,
                (int(lookback),),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            entry = _f(row["entry_price"])
            raw = row["advice_snapshot"]
            if not raw or entry <= 0:
                continue
            try:
                snap = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(snap, dict):
                continue
            prices = snap.get("prices") or {}
            mid = _f(prices.get("entry_mid") or prices.get("entry") or snap.get("entry_price"))
            if mid <= 0:
                continue
            slips.append(abs(entry - mid) / mid)
    except Exception as e:
        logger.debug("estimate slippage: %s", e)

    if len(slips) < 5:
        # 样本不足：配置中位与 fallback 混合
        use = float(fallback_pct or cfg_mid)
        return use, {
            "source": "fallback_sparse",
            "slippage_pct": use,
            "n": len(slips),
            "cfg_mid": round(cfg_mid, 6),
        }

    mean_slip = sum(slips) / len(slips)
    # 合理夹紧
    mean_slip = max(0.0002, min(0.02, mean_slip))
    return mean_slip, {
        "source": "paper_realized",
        "slippage_pct": round(mean_slip, 6),
        "n": len(slips),
        "p50": round(sorted(slips)[len(slips) // 2], 6),
    }


def extract_funding_rate(
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
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
        if fr2 is not None and not isinstance(fr2, dict):
            try:
                return float(fr2)
            except (TypeError, ValueError):
                pass
    return None


def expected_funding_cost_pct(
    *,
    action: str,
    funding_rate: Optional[float],
    hold_hours: float = 24.0,
    config: Optional[dict] = None,
) -> Tuple[float, Dict[str, Any]]:
    """持仓周期预期资金费（占名义比例，作成本项）。

    资金费每 8h 结算一次。做多付正费率、做空付负费率的绝对值成本；
    若费率对己有利则为 0（不把 rebate 算进「风险分母」的补贴）。
    """
    cfg = (config or {}).get("ai_trading") or {}
    period_h = float(cfg.get("funding_period_hours", 8) or 8)
    hold = max(1.0, float(hold_hours or 24))
    periods = hold / period_h
    if funding_rate is None:
        # 无行情时用配置默认预期
        default_abs = float(cfg.get("default_funding_abs_rate", 0.0001) or 0.0001)
        cost = abs(default_abs) * periods
        return cost, {
            "funding_rate": None,
            "periods": round(periods, 3),
            "cost_pct": round(cost, 6),
            "source": "default",
        }

    rate = float(funding_rate)
    side = (action or "").upper()
    # LONG 在 rate>0 时付费；SHORT 在 rate<0 时付费
    if side == "LONG" and rate > 0:
        cost = rate * periods
    elif side == "SHORT" and rate < 0:
        cost = abs(rate) * periods
    else:
        cost = 0.0
    return cost, {
        "funding_rate": rate,
        "periods": round(periods, 3),
        "cost_pct": round(cost, 6),
        "source": "live",
    }


def resolve_hold_hours(advice: Optional[Dict] = None) -> float:
    adv = advice or {}
    for key in ("hold_hours", "max_hold_hours"):
        if adv.get(key) is not None:
            return max(1.0, _f(adv.get(key), 24))
    prices = adv.get("prices") or {}
    if isinstance(prices, dict) and prices.get("hold_hours") is not None:
        return max(1.0, _f(prices.get("hold_hours"), 24))
    ai = adv.get("ai_analysis") or {}
    if isinstance(ai, dict) and ai.get("hold_hours") is not None:
        return max(1.0, _f(ai.get("hold_hours"), 24))
    return 24.0


def build_full_cost_inputs(
    advice: Dict[str, Any],
    *,
    config: Optional[dict] = None,
    paper_engine=None,
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
) -> Dict[str, Any]:
    """汇总净 RR 所需的全链路成本参数。"""
    cfg = (config or {}).get("ai_trading") or {}
    fee = _f(
        cfg.get("fee_rate")
        or (config or {}).get("backtest", {}).get("fee_rate"),
        0.0004,
    )
    fallback_slip = _f(
        cfg.get("slippage_pct")
        or (config or {}).get("backtest", {}).get("slippage_pct"),
        0.002,
    )
    lookback = int(cfg.get("slippage_lookback_trades", 100) or 100)
    slip, slip_meta = estimate_slippage_from_paper(
        paper_engine,
        lookback=lookback,
        fallback_pct=fallback_slip,
        config=config,
    )
    liq = _f(cfg.get("liquidity_premium_pct"), 0.0005)
    hold_h = resolve_hold_hours(advice)
    fr = extract_funding_rate(sentiment, bnb_factors)
    fund_cost, fund_meta = expected_funding_cost_pct(
        action=str(advice.get("action") or ""),
        funding_rate=fr,
        hold_hours=hold_h,
        config=config,
    )
    return {
        "fee_rate": fee,
        "slippage_pct": slip,
        "funding_pct": fund_cost,
        "liquidity_premium_pct": liq,
        "hold_hours": hold_h,
        "slippage_meta": slip_meta,
        "funding_meta": fund_meta,
    }
