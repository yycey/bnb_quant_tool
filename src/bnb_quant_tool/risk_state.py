# -*- coding: utf-8 -*-
"""熔断 / 黑天鹅冷却状态持久化（重启后仍生效）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    from bnb_quant_tool.data_localization import get_localized_db_path

    return get_localized_db_path("paper_trading").parent / "risk_state.json"


def load_risk_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 risk_state.json 失败: %s", e)
        return {}


def save_risk_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_circuit_breaker_stop_time() -> Optional[float]:
    val = load_risk_state().get("circuit_breaker_stop_ts")
    return float(val) if val is not None else None


def set_circuit_breaker_stop_time(ts: float) -> None:
    state = load_risk_state()
    state["circuit_breaker_stop_ts"] = ts
    save_risk_state(state)


def clear_circuit_breaker_stop_time() -> None:
    state = load_risk_state()
    state.pop("circuit_breaker_stop_ts", None)
    save_risk_state(state)


def get_black_swan_ts() -> Optional[float]:
    val = load_risk_state().get("black_swan_ts")
    return float(val) if val is not None else None


def set_black_swan_ts(ts: float) -> None:
    state = load_risk_state()
    state["black_swan_ts"] = ts
    save_risk_state(state)


def clear_black_swan_ts() -> None:
    state = load_risk_state()
    state.pop("black_swan_ts", None)
    save_risk_state(state)
