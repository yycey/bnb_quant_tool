"""
因子归因学习 — 将历史 WIN/LOSS 因子表现反哺决策解释与门控。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from bnb_quant_tool.learning_evolution import FACTOR_NAME_TO_KEY

# 决策解释器展示名 → 归因键（扩展）
EXPLAINER_FACTOR_KEYS: Dict[str, str] = {
    **FACTOR_NAME_TO_KEY,
    "机构共识一致": "institutional_consensus",
    "机构共识相反": "institutional_consensus",
    "机构无共识": "institutional_consensus",
    "策略投票倾斜": "institutional_vote_ratio",
    "策略投票反对": "institutional_vote_ratio",
    "RSI 超卖": "rsi_signal",
    "RSI 超买": "rsi_signal",
    "MACD 金叉": "macd_signal",
    "MACD 死叉": "macd_signal",
    "多周期共振": "multi_timeframe",
    "多周期冲突": "multi_timeframe",
    "新闻利好": "news_sentiment",
    "新闻利空": "news_sentiment",
    "AI 高置信": "ai_confidence",
    "AI 中置信": "ai_confidence",
    "AI 低置信": "ai_confidence",
    "RR 优秀": "risk_reward",
    "RR 合格": "risk_reward",
    "RR 过低": "risk_reward",
    "波动率偏高": "volatility",
}


def compute_reliability_multipliers(
    attribution_summary: Optional[List[Dict]],
    *,
    min_samples: int = 3,
) -> Dict[str, float]:
    """根据归因统计计算因子可靠度系数 0.5~1.5。"""
    multipliers: Dict[str, float] = {}
    if not attribution_summary:
        return multipliers

    for row in attribution_summary:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        total = wins + losses
        if total < min_samples:
            continue
        wr = wins / total
        key = str(row.get("factor_key") or "")
        if wr >= 0.65:
            mult = min(1.5, 1.0 + (wr - 0.5) * 0.8)
        elif wr <= 0.35:
            mult = max(0.5, 1.0 - (0.5 - wr) * 0.8)
        else:
            mult = 1.0
        multipliers[key] = round(mult, 3)
    return multipliers


def gate_tightening_from_attribution(learning_insights: Optional[Dict]) -> float:
    """不可靠因子过多时提高门控门槛。"""
    summary = (learning_insights or {}).get("factor_attribution") or []
    unreliable = 0
    for row in summary:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        if wins + losses >= 5 and wins / max(wins + losses, 1) < 0.35:
            unreliable += 1
    return min(0.12, unreliable * 0.03)


def apply_factor_attribution_gate(
    advice: Dict[str, Any],
    learning_insights: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """主导因子历史胜率过低且置信不足时拦截开仓。"""
    cfg = (config or {}).get("factor_attribution_gate") or {}
    if cfg.get("enabled") is False:
        return advice

    action = advice.get("action")
    if action not in ("LONG", "SHORT"):
        return advice

    conf = float(advice.get("confidence") or 0)
    block_below = float(cfg.get("block_below_confidence", 0.62))
    min_unreliable = int(cfg.get("min_unreliable_factors", 3))

    summary = (learning_insights or {}).get("factor_attribution") or []
    unreliable = 0
    caution_keys: List[str] = []
    for row in summary:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        total = wins + losses
        if total < 5:
            continue
        wr = wins / total
        if wr < 0.35:
            unreliable += 1
            caution_keys.append(str(row.get("factor_key") or "?"))

    if unreliable < min_unreliable or conf >= block_below:
        return advice

    out = dict(advice)
    out["action"] = "WAIT"
    out["passed_gate"] = False
    reasons = list(out.get("gate_reasons") or [])
    keys_preview = ", ".join(caution_keys[:3])
    reasons.append(
        f"因子归因门控: {unreliable} 个因子历史胜率<35% ({keys_preview})，"
        f"置信 {conf:.0%} < {block_below:.0%}，改 WAIT"
    )
    out["gate_reasons"] = reasons
    out["attribution_blocked"] = True
    return out


def apply_reliability_to_factors(
    factors: List[Dict],
    multipliers: Dict[str, float],
) -> List[Dict]:
    """对决策解释因子分应用可靠度系数。"""
    if not multipliers:
        return factors
    out = []
    for f in factors:
        name = str(f.get("name") or "")
        key = EXPLAINER_FACTOR_KEYS.get(name)
        if not key:
            for prefix, k in EXPLAINER_FACTOR_KEYS.items():
                if name.startswith(prefix.split(" ")[0]):
                    key = k
                    break
        mult = multipliers.get(key or "", 1.0)
        score = int(round(float(f.get("score") or 0) * mult))
        detail = f.get("detail") or ""
        if mult != 1.0 and key:
            detail = f"{detail} [归因×{mult:.2f}]"
        out.append({**f, "score": score, "detail": detail, "reliability": mult})
    return out


def format_attribution_for_prompt(learning_insights: Optional[Dict]) -> str:
    """格式化因子归因摘要供 DeepSeek 注入。"""
    summary = (learning_insights or {}).get("factor_attribution") or []
    regime = (learning_insights or {}).get("regime_weights_applied") or "GLOBAL"
    if not summary:
        return ""

    lines = [
        "",
        f"【因子归因学习 — {regime} 市场下历史因子可靠度，请参考】",
        "=" * 48,
    ]
    for row in summary[:8]:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        total = wins + losses
        if total < 2:
            continue
        wr = float(row.get("win_rate") or (wins / total))
        key = row.get("factor_key", "?")
        tag = "可靠" if wr >= 0.6 else ("慎用" if wr < 0.4 else "中性")
        lines.append(
            f"  · {key}: 胜率 {wr:.0%} ({wins}W/{losses}L) → {tag}"
        )
    lines.append("规则: 标记「慎用」的因子应降低权重；「可靠」因子可加强。")
    lines.append("=" * 48)
    return "\n".join(lines)
