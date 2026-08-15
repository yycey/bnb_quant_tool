"""
新闻影响衰减模型 — 不同类型新闻按时间衰减影响力。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 半衰期（小时）
DECAY_HALF_LIFE_HOURS: Dict[str, float] = {
    "regulatory": 168.0,      # 监管/调查 ~7天
    "etf_macro": 72.0,        # ETF/宏观 ~3天
    "exchange": 48.0,         # 交易所公告 ~2天
    "hack_security": 96.0,    # 安全事件 ~4天
    "listing": 24.0,          # 上币/合作 ~1天
    "kol_hype": 4.0,          # KOL喊单 ~4小时
    "general": 12.0,          # 一般新闻 ~12小时
}


def classify_news_type(title: str, summary: str = "") -> str:
    """根据标题/摘要分类新闻类型。"""
    text = f"{title} {summary}".lower()
    if re.search(r"sec|监管|调查|lawsuit|ban|禁止|合规", text, re.I):
        return "regulatory"
    if re.search(r"etf|fed|利率|宏观|treasury|通胀", text, re.I):
        return "etf_macro"
    if re.search(r"binance|币安|announcement|公告|上币|listing", text, re.I):
        return "exchange"
    if re.search(r"hack|被盗|exploit|漏洞|安全", text, re.I):
        return "hack_security"
    if re.search(r"合作|launch|上线|partnership", text, re.I):
        return "listing"
    if re.search(r"cz|何一|kol|喊单|twitter|tweet", text, re.I):
        return "kol_hype"
    return "general"


def decay_weight(hours_ago: float, news_type: str) -> float:
    """指数衰减权重 0~1。"""
    half_life = DECAY_HALF_LIFE_HOURS.get(news_type, 12.0)
    if hours_ago <= 0:
        return 1.0
    import math
    return max(0.05, math.exp(-0.693 * hours_ago / half_life))


def apply_decay_to_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为新闻列表附加 decay_score / effective_weight。"""
    now = datetime.now(timezone.utc)
    out = []
    for item in items or []:
        row = dict(item)
        ts = row.get("published_ts") or row.get("timestamp")
        hours_ago = 24.0
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                hours_ago = max(0.0, (now - dt).total_seconds() / 3600.0)
            except (ValueError, TypeError, OSError):
                pass
        ntype = classify_news_type(
            str(row.get("title") or ""),
            str(row.get("summary") or ""),
        )
        dw = decay_weight(hours_ago, ntype)
        row["news_type"] = ntype
        row["hours_ago"] = round(hours_ago, 1)
        row["decay_score"] = round(dw, 4)
        out.append(row)
    out.sort(key=lambda x: float(x.get("decay_score") or 0), reverse=True)
    return out


def adjust_news_summary(
    news_summary: Optional[Dict[str, Any]],
    news_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """用衰减加权调整新闻摘要的有效置信度。"""
    summary = dict(news_summary or {})
    items = apply_decay_to_items(news_items or [])
    if not items:
        return summary

    weighted_conf = 0.0
    weighted_polarity = 0.0
    total_w = 0.0
    for it in items[:15]:
        w = float(it.get("decay_score") or 0)
        if w < 0.1:
            continue
        pol = str(it.get("polarity") or summary.get("polarity") or "neutral").lower()
        pol_score = {"bullish": 1.0, "bearish": -1.0}.get(pol, 0.0)
        conf = float(it.get("confidence") or summary.get("confidence") or 0.5)
        weighted_conf += conf * w
        weighted_polarity += pol_score * w
        total_w += w

    if total_w > 0:
        eff_conf = weighted_conf / total_w
        summary["confidence"] = round(min(1.0, eff_conf), 4)
        summary["decay_adjusted"] = True
        summary["avg_decay_score"] = round(
            sum(float(i.get("decay_score") or 0) for i in items[:10]) / min(10, len(items)),
            4,
        )
        if weighted_polarity / total_w > 0.25:
            summary["polarity"] = "bullish"
        elif weighted_polarity / total_w < -0.25:
            summary["polarity"] = "bearish"
        else:
            summary["polarity"] = summary.get("polarity") or "neutral"
    return summary
