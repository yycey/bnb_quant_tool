"""链上领先滞后：启发式 + 离线回测最优窗口（次日生效）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def lead_lag_score(
    onchain: Optional[Dict] = None,
    *,
    price_change_pct: float = 0.0,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """输出 -100~100 的链上前瞻分及方向建议。

    规则启发式（无历史回测库时的可运行近似）：
    - 交易所净流出 / 巨鲸囤积 → 偏多领先
    - 净流入交易所 / 巨鲸派发 → 偏空领先
    - 与近期价格同向时略降权（可能已定价）
    - 若存在离线回测配置（effective_date≤今天），按 sign/corr/horizon 校准
    """
    cfg = (config or {}).get("onchain_lead_lag") or {}
    if cfg.get("enabled", True) is False:
        return {"enabled": False, "score": 0.0}

    oc = onchain or {}
    score = 0.0
    reasons = []

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    net_flow = _f(
        oc.get("exchange_net_flow")
        or oc.get("net_flow")
        or (oc.get("exchange") or {}).get("net_flow")
        or ((oc.get("exchange_netflow") or {}) if isinstance(oc.get("exchange_netflow"), dict) else {}).get("latest")
    )
    whale = _f(
        oc.get("whale_net")
        or oc.get("whale_accumulation")
        or (oc.get("whale") or {}).get("score")
    )
    tvl_chg = _f(oc.get("tvl_change_pct") or (oc.get("tvl") or {}).get("change_pct"))
    active = _f(oc.get("active_addresses_change") or oc.get("active_change"))

    if net_flow < 0:
        score += min(35.0, abs(net_flow) * 5)
        reasons.append("交易所净流出偏多")
    elif net_flow > 0:
        score -= min(35.0, abs(net_flow) * 5)
        reasons.append("交易所净流入偏空")

    if whale > 0:
        score += min(25.0, whale * 10)
        reasons.append("巨鲸囤积")
    elif whale < 0:
        score -= min(25.0, abs(whale) * 10)
        reasons.append("巨鲸派发")

    if tvl_chg > 0:
        score += min(15.0, tvl_chg)
        reasons.append(f"TVL↑{tvl_chg:.1f}%")
    elif tvl_chg < 0:
        score -= min(15.0, abs(tvl_chg))
        reasons.append(f"TVL↓{tvl_chg:.1f}%")

    if active > 0:
        score += min(10.0, active)
    elif active < 0:
        score -= min(10.0, abs(active))

    if price_change_pct != 0 and score != 0:
        if (score > 0 and price_change_pct > 1.0) or (score < 0 and price_change_pct < -1.0):
            score *= 0.7
            reasons.append("价格已部分反应，降权")

    # 离线回测校准
    model_meta: Dict[str, Any] = {"applied": False}
    try:
        from bnb_quant_tool.onchain_lead_lag_backtest import load_active_params

        params = load_active_params(config, symbol=str(oc.get("symbol") or "BNBUSDT"))
        if params.get("enabled"):
            sign = int(params.get("sign") or 1)
            corr = abs(float(params.get("correlation") or 0))
            horizon = int(params.get("best_horizon_hours") or 4)
            # sign=-1：领先关系反向 → 翻转启发式分
            if sign < 0:
                score = -score
                reasons.append(f"回测sign=-1，翻转启发式(窗口{horizon}h)")
            # |corr| 弱则收缩，强则略放大（上限 1.25）
            scale = 0.55 + min(0.70, corr * 2.0)
            score *= scale
            model_meta = {
                "applied": True,
                "best_horizon_hours": horizon,
                "correlation": float(params.get("correlation") or 0),
                "hit_rate": float(params.get("hit_rate") or 0),
                "sign": sign,
                "effective_date": params.get("effective_date"),
                "scale": round(scale, 3),
                "from": params.get("from"),
            }
            reasons.append(
                f"回测窗口{horizon}h corr={params.get('correlation')} "
                f"hit={float(params.get('hit_rate') or 0):.0%}"
            )
    except Exception as e:
        logger.debug("lead_lag model params: %s", e)

    score = max(-100.0, min(100.0, score))
    action = "LONG" if score >= float(cfg.get("long_min", 25) or 25) else (
        "SHORT" if score <= float(cfg.get("short_max", -25) or -25) else "WAIT"
    )
    return {
        "enabled": True,
        "score": round(score, 1),
        "recommended_action": action,
        "reasons": reasons[:8],
        "as_factor_weight": float(cfg.get("decision_weight", 0.12) or 0.12),
        "model": model_meta,
    }
