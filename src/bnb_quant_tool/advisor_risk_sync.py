"""TradeAdvisor 风控参数同步 — GUI / watcher 共用。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from bnb_quant_tool.bnb_symbol import is_bnb_trading_pair
from bnb_quant_tool.config_access import (
    build_trade_advisor_config,
    get_atr_sl_mult,
    get_max_position_pct,
)
from bnb_quant_tool.trade_advisor import TradeAdvisor


def apply_event_cycle_risk(
    cfg: Dict,
    advisor: TradeAdvisor,
    calendar,
    symbol: Optional[str] = None,
) -> Dict:
    """刷新 BNB 事件周期并应用到 TradeAdvisor（仅 BNB 交易对）。"""
    if not calendar:
        return {}
    sym = symbol or (cfg.get("trading") or {}).get("symbol", "BNBUSDT")
    if not is_bnb_trading_pair(sym):
        advisor._event_cycle = {}
        if hasattr(advisor, "_event_block_long"):
            advisor._event_block_long = False
        if hasattr(advisor, "_event_position_factor"):
            advisor._event_position_factor = 1.0
        return {"skipped": True, "skip_reason": "non_bnb_pair", "symbol": sym}

    cycle = calendar.analyze()
    calendar.apply_to_trade_advisor(advisor, cycle)
    advisor._event_cycle = cycle

    policy = cycle.get("policy") or {}
    base_max_pct = get_max_position_pct(cfg)
    scale = float(policy.get("paper_max_position_pct_scale", 1.0) or 1.0)
    advisor.max_position_pct = base_max_pct * scale

    ta_cfg = build_trade_advisor_config(cfg)
    base_min_conf = float(ta_cfg.get("min_confidence", 0.6))
    if cycle.get("phase") == "unlock_dump":
        advisor.min_confidence = max(0.55, base_min_conf + 0.05)
    else:
        advisor.min_confidence = base_min_conf
    return cycle


def apply_risk_sentry(
    cfg: Dict,
    advisor: TradeAdvisor,
    sentry,
    symbol: str = "BNBUSDT",
) -> Dict:
    """刷新 BNB 风控哨兵并缩放仓位/止损（仅 BNB 交易对）。"""
    if not sentry:
        return {}
    if not is_bnb_trading_pair(symbol):
        advisor._risk_sentry = {"enabled": False, "skipped": True}
        return {"skipped": True, "skip_reason": "non_bnb_pair", "symbol": symbol}

    rs = sentry.fetch_all(symbol=symbol)
    advisor._risk_sentry = rs

    base_max = get_max_position_pct(cfg)
    event_scale = 1.0
    if getattr(advisor, "_event_cycle", None):
        policy = (advisor._event_cycle.get("policy") or {})
        event_scale = float(policy.get("paper_max_position_pct_scale", 1.0) or 1.0)
    sentry_scale = float(rs.get("position_scale") or 1.0)
    advisor.max_position_pct = base_max * event_scale * sentry_scale

    sl_factor = float(rs.get("sl_tighten_factor") or 1.0)
    if sl_factor < 1.0:
        advisor.atr_sl_mult = max(
            advisor.atr_sl_mult_low_vol,
            get_atr_sl_mult(cfg) * sl_factor,
        )
    return rs


def sync_advisor_risk_context(
    cfg: Dict,
    advisor: TradeAdvisor,
    *,
    calendar=None,
    sentry=None,
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """启动时一次性同步事件周期 + 风控哨兵。"""
    sym = symbol or (cfg.get("trading") or {}).get("symbol", "BNBUSDT")
    out: Dict[str, Any] = {}
    if calendar:
        out["event_cycle"] = apply_event_cycle_risk(cfg, advisor, calendar, symbol=sym)
    if sentry:
        out["risk_sentry"] = apply_risk_sentry(cfg, advisor, sentry, sym)
    return out
