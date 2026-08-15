"""跨模态冲突硬门：链上 / 新闻 / 宏观 vs 开仓方向。

当反向信号总权重 ≥ 同向总权重 × conflict_ratio（默认 3）时强制 WAIT，
禁止在强冲突场景下开仓（不论 MTF 多高）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _onchain_bias(onchain: Optional[Dict]) -> Tuple[float, str]:
    """返回 (signed_weight, detail)。正=偏多。"""
    oc = onchain if isinstance(onchain, dict) else {}
    if not oc:
        return 0.0, ""
    score = oc.get("onchain_score")
    if score is None:
        ll = oc.get("lead_lag") or {}
        if isinstance(ll, dict) and ll.get("score") is not None:
            # lead_lag -100..100 → -1..1
            score = _f(ll.get("score")) / 100.0
        else:
            score = 0.0
    w = max(-1.0, min(1.0, _f(score)))
    # 放大有效权重
    weight = abs(w) * 1.0
    signed = weight if w >= 0 else -weight
    return signed, f"onchain={w:+.2f}"


def _news_bias(news: Optional[Dict]) -> Tuple[float, str]:
    ns = news if isinstance(news, dict) else {}
    if not ns:
        return 0.0, ""
    pol = str(ns.get("polarity") or ns.get("sentiment") or "neutral").lower()
    conf = _f(ns.get("confidence") or ns.get("cred_score") or ns.get("credibility"), 0.5)
    conf = max(0.2, min(1.0, conf if conf <= 1 else conf / 100.0))
    if pol in ("bullish", "positive", "看涨", "利多"):
        return conf, f"news=bull@{conf:.0%}"
    if pol in ("bearish", "negative", "看跌", "利空"):
        return -conf, f"news=bear@{conf:.0%}"
    return 0.0, "news=neutral"


def _macro_bias(macro: Optional[Dict], sentiment: Optional[Dict] = None) -> Tuple[float, str]:
    m = macro if isinstance(macro, dict) else {}
    if not m and isinstance(sentiment, dict):
        m = sentiment.get("macro") if isinstance(sentiment.get("macro"), dict) else {}
    if not m:
        return 0.0, ""

    # 常见字段
    regime = str(m.get("risk_regime") or m.get("regime") or m.get("stance") or "").lower()
    score = m.get("macro_score") or m.get("risk_score") or m.get("score")
    if score is not None:
        s = max(-1.0, min(1.0, _f(score)))
        return s, f"macro={s:+.2f}"

    if any(k in regime for k in ("risk-on", "risk_on", "riskon", "宽松", "偏多")):
        return 0.7, "macro=risk-on"
    if any(k in regime for k in ("risk-off", "risk_off", "riskoff", "紧缩", "偏空", "避险")):
        return -0.7, "macro=risk-off"

    # VIX / 美元走强等启发式
    vix = m.get("vix") or (m.get("assets") or {}).get("vix")
    if vix is not None:
        v = _f(vix)
        if v >= 28:
            return -0.8, f"macro=vix{v:.0f}"
        if v <= 14:
            return 0.4, f"macro=vix{v:.0f}"
    return 0.0, "macro=neutral"


def score_cross_modal(
    action: str,
    *,
    onchain: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    macro: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """计算同向/反向权重。"""
    wcfg = weights or {}
    w_on = float(wcfg.get("onchain", 1.0) or 1.0)
    w_news = float(wcfg.get("news", 1.0) or 1.0)
    w_macro = float(wcfg.get("macro", 1.0) or 1.0)

    parts: List[Tuple[float, str]] = []
    s, d = _onchain_bias(onchain)
    if d:
        parts.append((s * w_on, d))
    s, d = _news_bias(news_summary)
    if d and "neutral" not in d:
        parts.append((s * w_news, d))
    s, d = _macro_bias(macro, sentiment)
    if d and "neutral" not in d:
        parts.append((s * w_macro, d))

    side = (action or "").upper()
    want_long = side in ("LONG", "BUY")
    same = 0.0
    reverse = 0.0
    same_details: List[str] = []
    rev_details: List[str] = []
    for signed, detail in parts:
        if abs(signed) < 1e-9:
            continue
        is_longish = signed > 0
        aligns = (want_long and is_longish) or ((not want_long) and (not is_longish))
        if aligns:
            same += abs(signed)
            same_details.append(detail)
        else:
            reverse += abs(signed)
            rev_details.append(detail)

    return {
        "same_weight": round(same, 4),
        "reverse_weight": round(reverse, 4),
        "same_details": same_details,
        "reverse_details": rev_details,
        "n_signals": len(parts),
    }


def apply_cross_modal_conflict_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict] = None,
    onchain: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    macro: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    learning_context: Optional[Dict] = None,
) -> Dict[str, Any]:
    cfg = (config or {}).get("cross_modal_conflict") or {}
    if cfg.get("enabled", True) is False:
        return advice

    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice

    lc = learning_context or {}
    oc = onchain or lc.get("onchain") or advice.get("onchain")
    news = news_summary or lc.get("news_summary") or advice.get("news_summary")
    mac = macro or lc.get("macro") or advice.get("macro")
    sent = sentiment or lc.get("sentiment")

    ratio_thr = float(cfg.get("conflict_ratio", 3.0) or 3.0)
    min_reverse = float(cfg.get("min_reverse_weight", 0.6) or 0.6)
    scored = score_cross_modal(
        action,
        onchain=oc if isinstance(oc, dict) else None,
        news_summary=news if isinstance(news, dict) else None,
        macro=mac if isinstance(mac, dict) else None,
        sentiment=sent if isinstance(sent, dict) else None,
        weights=cfg.get("weights") if isinstance(cfg.get("weights"), dict) else None,
    )

    out = dict(advice)
    out["cross_modal_scores"] = scored
    same = float(scored["same_weight"])
    rev = float(scored["reverse_weight"])

    # 无有效跨模态信号则放行
    if scored["n_signals"] <= 0 or rev < min_reverse:
        return out

    conflict = False
    if same <= 1e-9:
        conflict = rev >= min_reverse
    else:
        conflict = rev >= same * ratio_thr

    if not conflict:
        return out

    out["action"] = "WAIT"
    out["passed_gate"] = False
    reasons = list(out.get("gate_reasons") or [])
    msg = (
        f"跨模态冲突硬门: 反向权重 {rev:.2f} ≥ 同向 {same:.2f}×{ratio_thr:.0f} "
        f"(反:{','.join(scored['reverse_details']) or '-'} / "
        f"同:{','.join(scored['same_details']) or '-'})，禁止开仓"
    )
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    out["cross_modal_blocked"] = True
    return out
