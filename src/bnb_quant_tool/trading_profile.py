"""交易剖面 / 统计漏斗工具。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_funnel_lock = threading.Lock()


def apply_trading_profile(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按 trading_profile 合并互斥剖面到 config（浅合并二级节）。"""
    cfg = dict(config or {})
    name = str(cfg.get("trading_profile") or "production").strip().lower()
    profiles = cfg.get("trading_profiles") or {}
    overlay = profiles.get(name) or profiles.get("production") or {}
    if not isinstance(overlay, dict):
        return cfg
    for section, values in overlay.items():
        if not isinstance(values, dict):
            cfg[section] = values
            continue
        base = dict(cfg.get(section) or {})
        base.update(values)
        cfg[section] = base
    cfg["_trading_profile_applied"] = name
    return cfg


def _funnel_path(config: Optional[Dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager
        root = Path(get_localization_manager().workspace)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    return root / "data" / "decision_funnel.json"


def record_decision_funnel(
    *,
    analysis_triggered: bool = True,
    gate_passed: bool = False,
    opened: bool = False,
    source: str = "analysis",
    symbol: str = "BNBUSDT",
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """三段漏斗：触发分析 / 过门 / 开仓。"""
    path = _funnel_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _funnel_lock:
        data: Dict[str, Any] = {
            "analysis_triggered": 0,
            "gate_passed": 0,
            "opened": 0,
            "by_source": {},
            "updated_at": "",
        }
        if path.is_file():
            try:
                data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        if analysis_triggered:
            data["analysis_triggered"] = int(data.get("analysis_triggered") or 0) + 1
        if gate_passed:
            data["gate_passed"] = int(data.get("gate_passed") or 0) + 1
        if opened:
            data["opened"] = int(data.get("opened") or 0) + 1
        by_src = dict(data.get("by_source") or {})
        src = by_src.get(source) or {"analysis_triggered": 0, "gate_passed": 0, "opened": 0}
        if analysis_triggered:
            src["analysis_triggered"] = int(src.get("analysis_triggered") or 0) + 1
        if gate_passed:
            src["gate_passed"] = int(src.get("gate_passed") or 0) + 1
        if opened:
            src["opened"] = int(src.get("opened") or 0) + 1
        by_src[source] = src
        data["by_source"] = by_src
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["last_symbol"] = symbol
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("funnel write: %s", e)
        return data


def get_decision_funnel(config: Optional[Dict] = None) -> Dict[str, Any]:
    path = _funnel_path(config)
    if not path.is_file():
        return {
            "analysis_triggered": 0,
            "gate_passed": 0,
            "opened": 0,
            "by_source": {},
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"analysis_triggered": 0, "gate_passed": 0, "opened": 0}


def auto_expectancy_stats(
    paper_db: Optional[str] = None,
    *,
    lookback_days: int = 14,
    auto_only: bool = True,
) -> Dict[str, Any]:
    """自动单滚动期望与开仓密度（用于改参守门）。

    opened_at 存 UTC ISO；cutoff 用 Python UTC，避免 SQLite local now 与 UTC 混比。
    """
    from datetime import timedelta

    path = paper_db
    if not path:
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            path = str(get_localized_db_path("paper_trading"))
        except Exception:
            path = str(Path(__file__).resolve().parents[2] / "data" / "paper_trading.db")
    if not path or not Path(path).is_file():
        return {"n": 0, "expectancy_r": 0.0, "opens_per_day": 0.0}

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    ).isoformat()
    where = "status='CLOSED' AND r_multiple IS NOT NULL AND opened_at >= ?"
    params: list = [cutoff]
    if auto_only:
        where += " AND (close_reason IS NULL OR close_reason NOT LIKE 'MANUAL%')"
    try:
        conn = sqlite3.connect(path, timeout=5)
        rows = conn.execute(
            f"SELECT r_multiple, opened_at FROM paper_positions WHERE {where}",
            params,
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("auto_expectancy_stats: %s", e)
        return {"n": 0, "expectancy_r": 0.0, "opens_per_day": 0.0}

    n = len(rows)
    if n == 0:
        return {"n": 0, "expectancy_r": 0.0, "opens_per_day": 0.0, "auto_only": auto_only}
    avg_r = sum(float(r[0] or 0) for r in rows) / n
    days = max(1.0, float(lookback_days))
    return {
        "n": n,
        "expectancy_r": round(avg_r, 4),
        "opens_per_day": round(n / days, 4),
        "auto_only": auto_only,
        "lookback_days": lookback_days,
    }
