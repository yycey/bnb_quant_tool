"""记忆质量治理：衰减、反记忆、分级检索优先级。"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def time_decay_weight(
    created_at: Any,
    *,
    half_life_days: float = 30.0,
    now: Optional[float] = None,
) -> float:
    """指数衰减：半衰期 half_life_days。"""
    ts = _parse_ts(created_at)
    if ts is None or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, ((now or time.time()) - ts) / 86400.0)
    return max(0.05, math.exp(-0.693 * age_days / half_life_days))


def rank_memories(
    rows: List[Dict[str, Any]],
    *,
    half_life_days: float = 30.0,
    conf_key: str = "confidence",
    wr_key: str = "win_rate",
    time_key: str = "created_at",
) -> List[Dict[str, Any]]:
    """检索优先级：高置信 × 近期衰减 × 高胜率。"""
    now = time.time()
    scored: List[Tuple[float, Dict]] = []
    for r in rows:
        conf = float(r.get(conf_key) or 0.5)
        wr = float(r.get(wr_key) or r.get("accuracy") or 0.5)
        decay = time_decay_weight(r.get(time_key), half_life_days=half_life_days, now=now)
        # 反向验证折损
        fails = int(r.get("times_failed") or r.get("fail_count") or 0)
        fail_pen = max(0.2, 1.0 - 0.15 * fails)
        score = conf * (0.5 + 0.5 * wr) * decay * fail_pen
        item = dict(r)
        item["_memory_score"] = round(score, 4)
        item["_decay"] = round(decay, 4)
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored]


def is_stale_for_archive(
    row: Dict[str, Any],
    *,
    half_life_days: float = 30.0,
    min_weight: float = 0.12,
    max_age_days: float = 180.0,
) -> bool:
    decay = time_decay_weight(row.get("created_at") or row.get("updated_at"), half_life_days=half_life_days)
    conf = float(row.get("confidence") or 0)
    age_ok = True
    ts = _parse_ts(row.get("created_at") or row.get("updated_at"))
    if ts is not None:
        age_days = (time.time() - ts) / 86400.0
        age_ok = age_days >= max_age_days
    return age_ok and (decay * conf) < min_weight


def anti_memory_block(
    situation_key: str,
    *,
    capability_memory=None,
    trader_memory=None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """匹配高频踩坑局面 → 否决开仓。"""
    cfg = (config or {}).get("memory_governance") or (config or {})
    if cfg.get("anti_memory_enabled", True) is False:
        return {"blocked": False}
    key = (situation_key or "").strip()
    if not key:
        return {"blocked": False}

    min_hits = int(cfg.get("anti_memory_min_hits", 2) or 2)
    look_tags = ("anti_memory", "loss_lesson", "踩坑", "counterfactual")

    # capability knowledge cards
    if capability_memory is not None:
        try:
            cards = []
            if hasattr(capability_memory, "list_active_cards"):
                cards = capability_memory.list_active_cards(limit=80) or []
            elif hasattr(capability_memory, "get_top_cards"):
                cards = capability_memory.get_top_cards(limit=80) or []
            hits = []
            for c in cards:
                tags = c.get("tags") or []
                if isinstance(tags, str):
                    tags = [tags]
                tag_ok = any(t in look_tags or "anti" in str(t).lower() for t in tags)
                title = str(c.get("title") or c.get("summary") or "")
                body = str(c.get("content") or c.get("lesson") or "")
                sk = str(c.get("situation_key") or c.get("dedupe_key") or "")
                if key and (key in sk or key in title or key in body):
                    if tag_ok or "LOSS" in body.upper() or "亏损" in body:
                        hits.append(c)
                elif tag_ok and (key[:24] in sk or key[:24] in body):
                    hits.append(c)
            if len(hits) >= min_hits:
                return {
                    "blocked": True,
                    "reason": f"反记忆命中 {len(hits)} 条踩坑教训（局面 {key[:40]}）",
                    "hits": len(hits),
                }
        except Exception as e:
            logger.debug("anti_memory capability: %s", e)

    # trader lessons preview
    if trader_memory is not None:
        try:
            dash = trader_memory.dashboard([]) if hasattr(trader_memory, "dashboard") else {}
            text_blob = str(dash)
            if key[:20] and key[:20] in text_blob and ("亏损" in text_blob or "LOSS" in text_blob):
                return {
                    "blocked": True,
                    "reason": f"反记忆：议会教训库含相似踩坑局面",
                    "hits": 1,
                }
        except Exception as e:
            logger.debug("anti_memory trader: %s", e)

    return {"blocked": False}


def save_anti_memory_lesson(
    capability_memory,
    *,
    situation_key: str,
    side: str,
    pnl: float,
    note: str = "",
) -> int:
    """亏损平仓写入反记忆卡。"""
    if capability_memory is None or not situation_key:
        return 0
    try:
        title = f"反记忆踩坑 [{side}] {situation_key[:48]}"
        content = (
            f"局面 {situation_key} 方向 {side} 亏损 ${pnl:.2f}。"
            f"相似局面禁止重复开仓。{note}"
        )
        card = {
            "title": title,
            "lesson": content,
            "category": "anti_memory",
            "confidence": 0.72,
            "tags": ["anti_memory", "loss_lesson", str(side or "").upper()],
            "situation_key": situation_key,
            "trigger_condition": f"situation_key={situation_key}",
        }
        if hasattr(capability_memory, "save_knowledge_card"):
            cid = capability_memory.save_knowledge_card(
                card, source="trade_close_anti_memory"
            )
            return int(cid or 0)
        if hasattr(capability_memory, "save_counterfactual_lesson"):
            return int(
                capability_memory.save_counterfactual_lesson(
                    best="WAIT",
                    side=side,
                    note=content,
                    extra={"situation_key": situation_key, "anti_memory": True},
                )
                or 0
            )
    except Exception as e:
        logger.debug("save_anti_memory_lesson: %s", e)
    return 0
