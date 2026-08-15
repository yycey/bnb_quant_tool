"""
知识卡消毒 — 停用「严禁 HOLD / 低质量开仓」通配 WAIT 卡，切断自锁环路。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 标题/规则命中即视为「HOLD 禁开通配卡」
_HOLD_BAN_PATTERNS = (
    "严禁在HOLD",
    "严禁在HOLD信号",
    "严禁 HOLD",
    "拒绝低质量与HOLD",
    "无视劣质信号与HOLD",
    "当信号为HOLD或质量",
    "HOLD信号及低质量",
    "低质量评分下开仓",
    "强制空仓观望",
    "严禁在低质量信号下",
)


def is_hold_ban_wildcard_card(card: Dict[str, Any]) -> bool:
    blob = " ".join(
        str(card.get(k) or "")
        for k in ("title", "action_rule", "lesson", "trigger_condition")
    )
    return any(p in blob for p in _HOLD_BAN_PATTERNS)


def sanitize_hold_ban_cards(
    db_path: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """将 HOLD 禁开通配卡 is_active=0。可重复执行。"""
    path = db_path
    if not path:
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            path = str(get_localized_db_path("ai_learning"))
        except Exception:
            root = Path(__file__).resolve().parents[2]
            path = str(root / "data" / "ai_learning.db")
    if not path or not Path(path).is_file():
        return {"status": "skip", "reason": "db_missing", "deactivated": []}

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, action_rule, lesson, trigger_condition, is_active,
               times_validated, confidence
        FROM knowledge_cards
        WHERE is_active = 1
        """
    ).fetchall()

    to_deactivate: List[int] = []
    samples: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if is_hold_ban_wildcard_card(d):
            to_deactivate.append(int(d["id"]))
            samples.append({
                "id": d["id"],
                "title": (d.get("title") or "")[:60],
                "validated": d.get("times_validated"),
            })

    if dry_run:
        conn.close()
        return {
            "status": "dry_run",
            "count": len(to_deactivate),
            "deactivated": samples,
        }

    if to_deactivate:
        conn.executemany(
            "UPDATE knowledge_cards SET is_active = 0 WHERE id = ?",
            [(i,) for i in to_deactivate],
        )
        conn.commit()
        logger.info(
            "knowledge hygiene: deactivated %d HOLD-ban wildcard cards",
            len(to_deactivate),
        )
    conn.close()
    return {
        "status": "ok",
        "count": len(to_deactivate),
        "deactivated": samples,
    }
