"""
知识卡片确定性门控 — 将高置信历史经验转为可执行拦截/收紧，不仅依赖 LLM 读 prompt。

匹配规则必须有明确触发条件；禁止 validated 次数通配永久 WAIT。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

MIN_CONFIDENCE = 0.72
MIN_VALIDATIONS = 1

# category → 默认门控行为
_CATEGORY_ACTIONS = {
    "error_lesson": "tighten",
    "stop_loss_rule": "tighten",
    "market_review": "tighten",
    "trading_logic": "tighten",
    "counterfactual": "tighten",
}

_HOLD_BAN_MARKERS = (
    "严禁在HOLD",
    "拒绝低质量与HOLD",
    "无视劣质信号与HOLD",
    "HOLD信号及低质量",
    "低质量评分下开仓",
    "强制空仓观望",
)


def _rule_implies_wait(rule: str) -> bool:
    r = (rule or "").lower()
    keywords = (
        "wait", "观望", "不开", "不做", "暂停", "拦截", "禁止",
        "降低置信", "提高门槛", "降级", "不宜", "避免开",
    )
    return any(k in r for k in keywords)


def _is_hold_ban_wildcard(card: Dict) -> bool:
    blob = " ".join(
        str(card.get(k) or "")
        for k in ("title", "action_rule", "lesson", "trigger_condition")
    )
    return any(m in blob for m in _HOLD_BAN_MARKERS)


def _rule_implies_reduce_long(rule: str) -> bool:
    r = (rule or "").lower()
    return any(k in r for k in ("不做多", "拦截做多", "禁止做多", "降多", "减多", "rsi>65", "rsi>70"))


def _rule_implies_reduce_short(rule: str) -> bool:
    r = (rule or "").lower()
    return any(k in r for k in ("不做空", "拦截做空", "禁止做空", "降空"))


def _matches_trigger(card: Dict, context: Dict) -> bool:
    trigger = (card.get("trigger_condition") or "").strip()
    # 空触发条件禁止通配（否则任意 error_lesson 都会拦开仓）
    if not trigger:
        return False

    # HOLD 禁开通配卡：不再进入确定性拦截（由 knowledge_hygiene 停用）
    if _is_hold_ban_wildcard(card):
        return False

    # 复用合成卡只作提示，不进确定性拦截
    tags = card.get("tags") or []
    if "knowledge_reuse" in tags or str(card.get("source") or "") == "reuse":
        return False

    regime = str(context.get("regime") or "").lower()
    action = str(context.get("action") or "").upper()
    rsi = context.get("rsi")
    lesson = (card.get("lesson") or "").lower()

    t = trigger.lower()
    hit = False
    if regime and regime in t:
        hit = True
    if action and action.lower() in t:
        hit = True
    if rsi is not None:
        m = re.search(r"rsi\s*[><=]+\s*(\d+)", t)
        if m:
            try:
                th = float(m.group(1))
                if ">" in t and float(rsi) > th:
                    hit = True
                if "<" in t and float(rsi) < th:
                    hit = True
            except (TypeError, ValueError):
                pass
    if "震荡" in t and "range" in regime:
        hit = True
    if "趋势" in t and "trend" in regime:
        hit = True
    # 明确写了「通用/类似」才允许弱匹配；禁止 validated≥2 空通配
    if not hit and ("通用" in trigger or "类似" in lesson):
        # 仍要求至少与当前方向或 regime 有一词相关
        if action and action.lower() in (lesson + t):
            hit = True
        elif regime and any(x in t for x in ("range", "trend", "震荡", "趋势")):
            hit = True
    return hit


def apply_knowledge_gates(
    action: str,
    learning_insights: Optional[Dict],
    *,
    ai_confidence: float = 0.0,
    market_regime: Optional[Dict] = None,
    indicators: Optional[Dict] = None,
) -> Tuple[str, List[str], float]:
    """
    根据注入的知识卡片对方向/门控做确定性调整。

    Returns:
        (可能修改后的 action, 门控原因列表, 额外 gate_tightening)
    """
    reasons: List[str] = []
    tightening = 0.0
    if action not in ("LONG", "SHORT"):
        return action, reasons, tightening

    cards = (learning_insights or {}).get("capability_cards") or []
    if not cards:
        return action, reasons, tightening

    regime_name = ""
    if market_regime:
        regime_name = str(market_regime.get("regime") or market_regime.get("label") or "")
    rsi = None
    if indicators:
        rsi = indicators.get("RSI") or indicators.get("rsi")

    ctx = {
        "action": action,
        "regime": regime_name,
        "rsi": rsi,
        "ai_confidence": ai_confidence,
    }

    applied = 0
    for card in cards:
        conf = float(card.get("confidence") or 0)
        validated = int(card.get("times_validated") or 0)
        if conf < MIN_CONFIDENCE and validated < MIN_VALIDATIONS:
            continue
        if not _matches_trigger(card, ctx):
            continue

        rule = str(card.get("action_rule") or "")
        title = str(card.get("title") or card.get("category") or "知识规则")
        cat = str(card.get("category") or "")

        # 硬 WAIT：必须规则明确暗示观望，且置信够高；不再用 validated≥2 单独硬拦
        if _rule_implies_wait(rule) and conf >= 0.78:
            reasons.append(f"知识门控: [{title}] {rule[:60] or '历史亏损教训'}")
            return "WAIT", reasons, tightening + 0.05

        if cat == "error_lesson" and conf >= 0.8:
            # 仅收紧，不因 category 单独改 WAIT
            reasons.append(f"知识门控: [{title}] {rule[:60] or '历史亏损教训'}")
            tightening += 0.03
            applied += 1

        if action == "LONG" and _rule_implies_reduce_long(rule):
            reasons.append(f"知识门控(做多): {title}")
            tightening += 0.04
            applied += 1
        elif action == "SHORT" and _rule_implies_reduce_short(rule):
            reasons.append(f"知识门控(做空): {title}")
            tightening += 0.04
            applied += 1
        elif _CATEGORY_ACTIONS.get(cat) == "tighten":
            tightening += min(0.03, 0.01 + conf * 0.02)
            applied += 1

    tightening = min(0.15, tightening)
    if applied >= 2 and action != "WAIT":
        reasons.append(f"知识库 {applied} 条规则同时收紧门槛")
    return action, reasons, tightening
