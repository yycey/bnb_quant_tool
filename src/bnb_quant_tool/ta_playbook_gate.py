"""
技术分析 Playbook 门控 — 经典 TA 与方向/Regime 对齐度参与风控。

规则来源：FinLab（弱证据观望）、IG/Moomoo（趋势需 ADX 确认、震荡用均值回归）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

ACTION_LONG = "LONG"
ACTION_SHORT = "SHORT"
ACTION_WAIT = "WAIT"

TREND_REGIMES = frozenset({"TRENDING", "EUPHORIA", "HIGH_VOLATILITY", "NEWS_DRIVEN"})
RANGE_REGIMES = frozenset({"RANGING", "LOW_VOLATILITY"})


def apply_ta_playbook_gates(
    action: str,
    ta_bundle: Optional[Dict[str, Any]],
    *,
    indicators: Optional[Dict[str, Any]] = None,
    market_regime: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str], float, float]:
    """
    根据 Playbook 对齐度调整方向与门控松紧。

    Returns:
        (action, block_reasons, gate_tightening, gate_relaxation)
    """
    reasons: List[str] = []
    tightening = 0.0
    relaxation = 0.0
    if action not in (ACTION_LONG, ACTION_SHORT):
        return action, reasons, tightening, relaxation

    bundle = ta_bundle or {}
    if bundle.get("enabled") is False:
        return action, reasons, tightening, relaxation

    cfg = dict(config or {})
    gate = cfg.get("gate") or cfg
    if gate.get("enabled") is False:
        return action, reasons, tightening, relaxation

    min_conflict_votes = int(gate.get("min_conflict_votes", 2))
    block_on_conflict = gate.get("block_on_classic_conflict", True) is not False
    relax_on_align = gate.get("relax_on_alignment", True) is not False
    relax_amount = float(gate.get("alignment_relaxation", 0.03))
    adx_low = float(gate.get("adx_low_threshold", 20))
    adx_tighten = float(gate.get("adx_low_tightening", 0.05))
    ranging_trend_penalty = float(gate.get("ranging_trend_penalty", 0.04))

    classic_bias = str(bundle.get("classic_ta_bias") or "HOLD").upper()
    votes = bundle.get("classic_ta_votes") or {}
    buy_v = int(votes.get("BUY") or 0)
    sell_v = int(votes.get("SELL") or 0)

    regime = str(
        (market_regime or {}).get("regime")
        or bundle.get("regime")
        or "UNKNOWN"
    ).upper()

    ind = indicators or bundle.get("indicator_snapshot") or {}
    adx = _f(ind.get("ADX"))

    # 1. 经典 TA 与拟开仓方向硬冲突
    if block_on_conflict:
        if action == ACTION_LONG and classic_bias == "SELL" and sell_v >= min_conflict_votes:
            reasons.append(
                f"TA Playbook: 经典TA偏空({sell_v}票) 与做多冲突，转观望"
            )
            return ACTION_WAIT, reasons, tightening, relaxation
        if action == ACTION_SHORT and classic_bias == "BUY" and buy_v >= min_conflict_votes:
            reasons.append(
                f"TA Playbook: 经典TA偏多({buy_v}票) 与做空冲突，转观望"
            )
            return ACTION_WAIT, reasons, tightening, relaxation

    # 2. 机构共识与经典 TA 双空/双多但 action 反向（强冲突）
    consensus = str(bundle.get("institutional_consensus") or "HOLD").upper()
    if block_on_conflict:
        if action == ACTION_LONG and consensus == "SELL" and classic_bias == "SELL":
            reasons.append("TA Playbook: 机构共识+经典TA均看空，拦截做多")
            return ACTION_WAIT, reasons, tightening, relaxation
        if action == ACTION_SHORT and consensus == "BUY" and classic_bias == "BUY":
            reasons.append("TA Playbook: 机构共识+经典TA均看多，拦截做空")
            return ACTION_WAIT, reasons, tightening, relaxation

    # 3. ADX 偏低：趋势单收紧（FinLab/Moomoo 纪律）
    if adx is not None and adx < adx_low and regime in TREND_REGIMES:
        tightening += adx_tighten
        reasons.append(f"TA Playbook: ADX {adx:.1f} 偏低，趋势环境下降置信门槛")

    # 4. 震荡 Regime 做趋势追单 → 小幅收紧
    if regime in RANGE_REGIMES and classic_bias == "HOLD":
        tightening += ranging_trend_penalty

    # 5. 对齐放宽
    aligned = bundle.get("classic_ta_aligned_with_consensus")
    same_dir = (
        (action == ACTION_LONG and classic_bias == "BUY")
        or (action == ACTION_SHORT and classic_bias == "SELL")
    )
    if relax_on_align and same_dir and (aligned or buy_v >= 2 or sell_v >= 2):
        relaxation += relax_amount

    return action, reasons, tightening, relaxation


def apply_ta_playbook_post_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """后处理门控（post_advice_gates 链路）：最终 action 再校验一次。"""
    cfg = config or {}
    ta_cfg = (cfg.get("analysis") or {}).get("ta_playbook") or cfg.get("ta_playbook") or {}
    if ta_cfg.get("enabled") is False:
        return advice

    gate_cfg = ta_cfg.get("gate") or ta_cfg
    if gate_cfg.get("enabled") is False:
        return advice

    bundle = advice.get("ta_playbook")
    if not bundle:
        return advice

    action = advice.get("raw_action") or advice.get("action")
    new_action, reasons, tightening, relaxation = apply_ta_playbook_gates(
        str(action or ACTION_WAIT),
        bundle,
        indicators=advice.get("indicators"),
        market_regime=advice.get("market_regime"),
        config=ta_cfg,
    )

    if not reasons and tightening <= 0 and relaxation <= 0:
        return advice

    out = dict(advice)
    out_reasons = list(out.get("gate_reasons") or [])

    if new_action == ACTION_WAIT and action in (ACTION_LONG, ACTION_SHORT):
        out["action"] = ACTION_WAIT
        out["passed_gate"] = False
        out_reasons.extend(reasons)
        out["ta_playbook_blocked"] = True
    elif reasons:
        out_reasons.extend([r for r in reasons if "收紧" in r or "偏低" in r])

    if tightening > 0:
        out["ta_gate_tightening"] = round(
            float(out.get("ta_gate_tightening") or 0) + tightening, 4
        )
    if relaxation > 0:
        out["gate_relaxation"] = round(
            float(out.get("gate_relaxation") or 0) + relaxation, 4
        )

    out["gate_reasons"] = out_reasons
    return out


def _f(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None
