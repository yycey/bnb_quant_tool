"""
ValidationTrading — 持续开平验证模式。

核心观念：没有开平仓就没有收益，也无法验证策略/技术/想法对错。
- 软门控拦下时：允许小仓 validation_probe 开仓（硬拦仍生效）
- 每笔记录 hypothesis（方向、regime、置信、关键因子）
- 平仓后标记 validated_correct / validated_wrong + 写入本地日志

与 learning_phase 区别：不依赖「攒够 30 笔就关」；可持续跑验证流。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _workspace(config: Optional[Dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager
        return Path(get_localization_manager().workspace)
    except Exception:
        return Path(__file__).resolve().parents[2]


def validation_log_path(config: Optional[Dict] = None) -> Path:
    cfg = (config or {}).get("validation_trading") or {}
    if cfg.get("log_path"):
        return Path(cfg["log_path"])
    return _workspace(config) / "data" / "validation_log.jsonl"


def is_validation_trading_enabled(config: Optional[Dict] = None) -> bool:
    """是否处于持续验证开平模式。"""
    cfg = config or {}
    if str(cfg.get("_trading_profile_applied") or "").lower() == "validation":
        return True
    vt = cfg.get("validation_trading") or {}
    if vt.get("enabled") is True:
        return True
    ai = cfg.get("ai_trading") or {}
    return bool(ai.get("validation_trading", False))


def validation_cfg(config: Optional[Dict] = None) -> Dict[str, Any]:
    cfg = dict((config or {}).get("validation_trading") or {})
    ai = (config or {}).get("ai_trading") or {}
    # ai_trading 可覆盖部分验证参数
    for k in (
        "probe_position_scale",
        "probe_confidence_floor",
        "min_open_confidence",
        "min_net_rr",
        "open_follow_cooldown_minutes",
    ):
        if ai.get(k) is not None and k not in cfg:
            cfg[k] = ai[k]
    return cfg


def record_validation_open(
    *,
    position_id: int,
    advice: Dict[str, Any],
    record_id: Optional[int] = None,
    config: Optional[Dict] = None,
) -> None:
    """开仓时记录待验证假设。"""
    if not is_validation_trading_enabled(config):
        return
    vt = validation_cfg(config)
    if vt.get("record_hypothesis", True) is False:
        return

    votes = advice.get("votes") or {}
    regime = (advice.get("market_regime") or {})
    if isinstance(regime, str):
        regime = {"regime": regime}
    hypothesis = {
        "event": "open",
        "position_id": int(position_id),
        "record_id": record_id,
        "symbol": advice.get("symbol") or "BNBUSDT",
        "side": advice.get("action") or advice.get("raw_action"),
        "confidence": advice.get("calibrated_confidence") or advice.get("confidence"),
        "regime": regime.get("regime") if isinstance(regime, dict) else regime,
        "ai_direction": votes.get("ai_direction"),
        "institutional": votes.get("institutional_consensus"),
        "orderflow_score": (advice.get("orderflow") or {}).get("orderflow_score"),
        "gate_reasons": (advice.get("gate_reasons") or [])[:5],
        "validation_probe": bool(
            advice.get("validation_probe") or advice.get("learning_phase_probe")
        ),
        "thesis": _extract_thesis(advice),
        "at": _now(),
    }
    _append_log(hypothesis, config)


def record_validation_close(
    *,
    position_id: int,
    trade_row: Dict[str, Any],
    outcome: str,
    pnl: float,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """平仓时标记假设验证结果。"""
    if not is_validation_trading_enabled(config):
        return {}
    side = str(trade_row.get("side") or "").upper()
    correct = _hypothesis_correct(side, outcome, pnl)
    entry = {
        "event": "close",
        "position_id": int(position_id),
        "side": side,
        "outcome": outcome,
        "pnl_usdt": round(float(pnl), 4),
        "r_multiple": trade_row.get("r_multiple"),
        "mfe_r": trade_row.get("mfe_r"),
        "close_reason": trade_row.get("close_reason"),
        "validated_correct": correct,
        "verdict": "正确" if correct else "错误",
        "at": _now(),
    }
    _append_log(entry, config)
    stats = get_validation_stats(config)
    logger.info(
        "[Validation] #%s %s → %s (%s) | 累计验证 %s 正确率 %.0f%%",
        position_id,
        side,
        outcome,
        entry["verdict"],
        stats.get("closed", 0),
        float(stats.get("accuracy_pct") or 0),
    )
    return entry


def get_validation_stats(config: Optional[Dict] = None) -> Dict[str, Any]:
    """从本地 jsonl 汇总验证统计。"""
    path = validation_log_path(config)
    if not path.is_file():
        return {"closed": 0, "correct": 0, "accuracy_pct": 0.0, "opens": 0}

    opens = 0
    closed = 0
    correct = 0
    recent: List[Dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("event") == "open":
                opens += 1
            elif row.get("event") == "close":
                closed += 1
                if row.get("validated_correct"):
                    correct += 1
                recent.append(row)
    except Exception as e:
        logger.debug("validation stats: %s", e)
        return {"closed": 0, "correct": 0, "accuracy_pct": 0.0, "opens": 0}

    acc = (correct / closed * 100.0) if closed else 0.0
    return {
        "opens": opens,
        "closed": closed,
        "correct": correct,
        "wrong": closed - correct,
        "accuracy_pct": round(acc, 1),
        "recent": recent[-5:],
        "log_path": str(path),
    }


def count_opens_in_lookback(
    paper_db_path: Optional[str] = None,
    lookback_days: int = 7,
) -> int:
    """统计近 N 天开仓笔数（opened_at）。"""
    if not paper_db_path:
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            paper_db_path = str(get_localized_db_path("paper_trading"))
        except Exception:
            return 0
    path = Path(paper_db_path)
    if not path.is_file():
        return 0
    try:
        import sqlite3
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))).isoformat()
        conn = sqlite3.connect(str(path), timeout=10)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE opened_at >= ?",
                (cutoff,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def open_density_per_day(
    paper_stats: Optional[Dict] = None,
    lookback_days: int = 7,
    paper_db_path: Optional[str] = None,
) -> float:
    """近 lookback 天的日均开仓密度（不是终身笔数/天数）。"""
    lookback_days = max(int(lookback_days or 7), 1)
    stats = paper_stats or {}
    if "opens_last_days" in stats:
        n = int(stats.get("opens_last_days") or 0)
    elif "recent_opens" in stats:
        n = int(stats.get("recent_opens") or 0)
    else:
        n = count_opens_in_lookback(paper_db_path, lookback_days)
    return n / float(lookback_days)


def should_encourage_validation_opens(
    config: Optional[Dict] = None,
    paper_stats: Optional[Dict] = None,
    funnel: Optional[Dict] = None,
    paper_db_path: Optional[str] = None,
) -> bool:
    """开仓密度过低 → 应鼓励验证单。"""
    if not is_validation_trading_enabled(config):
        return False
    vt = validation_cfg(config)
    target = float(vt.get("target_opens_per_day", 0.4))
    lookback = int(vt.get("density_lookback_days", 7))
    if not paper_db_path:
        paper_db_path = ((config or {}).get("paper_trading") or {}).get("db_path")
    density = open_density_per_day(
        paper_stats, lookback, paper_db_path=paper_db_path
    )
    if density < target:
        return True
    triggered = int((funnel or {}).get("analysis_triggered") or 0)
    opened = int((funnel or {}).get("opened") or 0)
    if triggered >= 10 and opened / max(triggered, 1) < 0.05:
        return True
    return False


def _hypothesis_correct(side: str, outcome: str, pnl: float) -> bool:
    if outcome == "WIN":
        return True
    if outcome == "LOSS":
        return False
    # BREAK_EVEN：小盈算对，小亏算错
    return pnl > 0.01


def _extract_thesis(advice: Dict[str, Any]) -> str:
    parts: List[str] = []
    expl = advice.get("decision_explanation") or advice.get("explanation") or {}
    if isinstance(expl, dict):
        for f in (expl.get("factors") or expl.get("key_factors") or [])[:3]:
            if isinstance(f, dict):
                parts.append(f"{f.get('name', '?')}: {f.get('detail', f.get('score', ''))}")
            else:
                parts.append(str(f))
    if not parts:
        votes = advice.get("votes") or {}
        parts.append(f"AI={votes.get('ai_direction')} inst={votes.get('institutional_consensus')}")
    return " | ".join(parts)[:300]


def _append_log(entry: Dict[str, Any], config: Optional[Dict] = None) -> None:
    path = validation_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
