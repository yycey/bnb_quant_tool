"""
持仓出场策略 — 软/硬超时与认错判定（纯逻辑，不碰 DB/下单）。

由 PaperTradingEngine 在 watcher 中调用；保持 paper_trading 变薄。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SOFT_EXIT_HOURS = 6.0
DEFAULT_SOFT_EXIT_MIN_HOURS = 2.0
DEFAULT_HARD_EXIT_HOURS = 48.0
DEFAULT_ADMIT_WRONG_MIN_AGE_MIN = 30.0
DEFAULT_ADMIT_WRONG_ADVERSE_R = 0.35
# 软超时：浮盈达到该 R 时暂不平，留给 TP/硬超时（避免砍掉未触 TP1 的健康浮盈单）
DEFAULT_SOFT_EXIT_MAX_LIVE_R = 0.25


def parse_opened_at(opened_at_str: str) -> Optional[datetime]:
    """解析持仓 opened_at（UTC ISO）为 aware datetime，兼容历史 naive。"""
    if not opened_at_str:
        return None
    try:
        dt = datetime.fromisoformat(str(opened_at_str).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_timeout_policy(config: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """读取硬/软持仓超时策略。"""
    pt = (config or {}).get("paper_trading") or {}
    soft = pt.get("soft_exit") or {}
    if isinstance(soft, (int, float)):
        soft = {"enabled": True, "hours": float(soft)}
    hard_h = float(pt.get("max_position_age_hours", DEFAULT_HARD_EXIT_HOURS) or DEFAULT_HARD_EXIT_HOURS)
    soft_enabled = bool(soft.get("enabled", True))
    soft_h = float(soft.get("hours", DEFAULT_SOFT_EXIT_HOURS) or DEFAULT_SOFT_EXIT_HOURS)
    soft_min_h = float(
        soft.get("min_hours", DEFAULT_SOFT_EXIT_MIN_HOURS) or DEFAULT_SOFT_EXIT_MIN_HOURS
    )
    soft_h = max(soft_min_h, min(soft_h, hard_h))
    _raw_max_live = soft.get("max_live_r", DEFAULT_SOFT_EXIT_MAX_LIVE_R)
    soft_max_live_r = float(
        DEFAULT_SOFT_EXIT_MAX_LIVE_R if _raw_max_live is None else _raw_max_live
    )
    return {
        "hard_hours": hard_h,
        "soft_hours": soft_h,
        "soft_min_hours": soft_min_h,
        "soft_enabled": 1.0 if soft_enabled else 0.0,
        "require_tp1_not_hit": 1.0 if bool(soft.get("require_tp1_not_hit", True)) else 0.0,
        "soft_max_live_r": soft_max_live_r,
    }


def live_pnl_r(pos: Dict[str, Any], price: float) -> Optional[float]:
    """当前浮盈折合 R（相对初始风险）。"""
    try:
        entry = float(pos.get("entry_price") or 0)
        sl0 = float(pos.get("sl_initial") or pos.get("sl") or 0)
        if entry <= 0 or price <= 0:
            return None
        risk = abs(entry - sl0) if sl0 > 0 else 0.0
        if risk <= 0:
            return None
        side = str(pos.get("side") or "LONG").upper()
        if side == "SHORT":
            return (entry - float(price)) / risk
        return (float(price) - entry) / risk
    except Exception:
        return None


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    detail: str = ""
    age_hours: float = 0.0
    age_minutes: float = 0.0


def evaluate_timeout(
    pos: Dict[str, Any],
    policy: Dict[str, float],
    *,
    now: Optional[datetime] = None,
    price: Optional[float] = None,
) -> Optional[ExitDecision]:
    """判定是否应超时平仓。不取价、不执行。

    soft_max_live_r：软超时时若已知现价且浮盈 ≥ 该阈值，暂不平（硬超时仍强制）。
    """
    now = now or datetime.now(timezone.utc)
    opened_at = parse_opened_at(pos.get("opened_at") or "")
    if opened_at is None:
        return None
    age_sec = (now - opened_at).total_seconds()
    if age_sec <= 0:
        return None

    hard_sec = float(policy["hard_hours"]) * 3600.0
    soft_sec = float(policy["soft_hours"]) * 3600.0
    soft_on = bool(policy["soft_enabled"])
    require_no_tp1 = bool(policy["require_tp1_not_hit"])
    age_h = age_sec / 3600.0

    if age_sec > hard_sec:
        return ExitDecision(
            reason="TIMEOUT",
            detail=f"持仓 {age_h:.1f}h > {policy['hard_hours']:.0f}h",
            age_hours=age_h,
        )
    if soft_on and age_sec >= soft_sec:
        if require_no_tp1 and bool(pos.get("tp1_hit")):
            return None
        max_live = float(policy.get("soft_max_live_r", DEFAULT_SOFT_EXIT_MAX_LIVE_R))
        if price is not None and price > 0:
            lr = live_pnl_r(pos, float(price))
            if lr is not None and lr >= max_live:
                return None
        return ExitDecision(
            reason="TIMEOUT_NO_TP",
            detail=f"持仓 {age_h:.1f}h ≥ {policy['soft_hours']:.0f}h 未触 TP1",
            age_hours=age_h,
        )
    return None


def evaluate_admit_wrong(
    pos: Dict[str, Any],
    price: float,
    config: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[ExitDecision]:
    """判定是否应认错平仓。需要现价。"""
    pt = (config or {}).get("paper_trading") or {}
    aw = pt.get("admit_wrong") or {}
    if isinstance(aw, bool):
        aw = {"enabled": aw}
    if aw.get("enabled", True) is False:
        return None

    min_age_min = float(
        aw.get("min_age_minutes", DEFAULT_ADMIT_WRONG_MIN_AGE_MIN)
        or DEFAULT_ADMIT_WRONG_MIN_AGE_MIN
    )
    adverse_r = float(
        aw.get("adverse_r", DEFAULT_ADMIT_WRONG_ADVERSE_R)
        or DEFAULT_ADMIT_WRONG_ADVERSE_R
    )
    skip_if_tp1 = bool(aw.get("skip_if_tp1_hit", True))

    if skip_if_tp1 and bool(pos.get("tp1_hit")):
        return None

    now = now or datetime.now(timezone.utc)
    opened_at = parse_opened_at(pos.get("opened_at") or "")
    if opened_at is None:
        return None
    age_min = (now - opened_at).total_seconds() / 60.0
    if age_min < min_age_min:
        return None

    live_r = live_pnl_r(pos, float(price))
    if live_r is None:
        return None

    mae_r_abs = abs(float(pos.get("mae_r") or 0))
    detail = ""
    if live_r <= -adverse_r:
        detail = f"浮盈={live_r:.2f}R ≤ -{adverse_r:.2f}R"
    elif mae_r_abs >= adverse_r and live_r < 0:
        detail = (
            f"|MAE|={mae_r_abs:.2f}R ≥ {adverse_r:.2f}R "
            f"且仍亏 live={live_r:.2f}R"
        )
    else:
        return None

    return ExitDecision(
        reason="ADMIT_WRONG",
        detail=detail,
        age_minutes=age_min,
        age_hours=age_min / 60.0,
    )


def collect_timeout_exits(
    open_positions: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    prices_by_symbol: Optional[Dict[str, float]] = None,
) -> List[Tuple[Dict[str, Any], ExitDecision]]:
    policy = resolve_timeout_policy(config)
    now = now or datetime.now(timezone.utc)
    prices_by_symbol = prices_by_symbol or {}
    out: List[Tuple[Dict[str, Any], ExitDecision]] = []
    for pos in open_positions or []:
        sym = str(pos.get("symbol") or "")
        px = prices_by_symbol.get(sym)
        dec = evaluate_timeout(pos, policy, now=now, price=px)
        if dec:
            out.append((pos, dec))
    return out
