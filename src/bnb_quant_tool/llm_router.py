"""LLM 调用路由：平静期降本，异动/事件期三家全开。"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def assess_market_stress(
    *,
    atr_ratio: float = 1.0,
    market_regime: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """判断当前是否需要压力模式（三家 LLM）。"""
    llm = (config or {}).get("llm") or {}
    route = llm.get("routing") or {}
    calm_max = _f(route.get("calm_atr_ratio_max"), 1.25)
    reasons: List[str] = []
    stress = False

    if atr_ratio >= calm_max:
        stress = True
        reasons.append(f"ATR比={atr_ratio:.2f}>={calm_max}")

    regime = str((market_regime or {}).get("regime") or "").upper()
    if regime in ("HIGH_VOLATILITY", "PANIC", "EUPHORIA", "NEWS_DRIVEN"):
        stress = True
        reasons.append(f"regime={regime}")

    bf = bnb_factors or {}
    swan = (bf.get("risk_sentry") or {}).get("black_swan") or bf.get("black_swan") or {}
    if swan.get("triggered") or swan.get("emergency_liquidate"):
        stress = True
        reasons.append("黑天鹅哨兵")

    events = bf.get("event_calendar") or bf.get("events") or {}
    if isinstance(events, dict) and (
        events.get("block_trading") or events.get("high_impact") or events.get("imminent")
    ):
        stress = True
        reasons.append("高影响事件")

    news = news_summary or {}
    polarity = str(news.get("polarity") or "neutral").lower()
    nconf = _f(news.get("confidence"), 0)
    if polarity in ("bullish", "bearish") and nconf >= _f(route.get("news_stress_confidence"), 0.7):
        stress = True
        reasons.append(f"强新闻 {polarity}@{nconf:.0%}")
    if str(news.get("regime") or "").upper() in ("PANIC", "NEWS_DRIVEN"):
        stress = True
        reasons.append(f"新闻regime={news.get('regime')}")

    mode = "stress" if stress else "calm"
    return {
        "mode": mode,
        "stress": stress,
        "atr_ratio": round(atr_ratio, 3),
        "reasons": reasons,
    }


def resolve_analyzer_providers_for_route(
    config: Optional[Dict] = None,
    *,
    atr_ratio: float = 1.0,
    market_regime: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    force_stress: bool = False,
) -> Dict[str, Any]:
    """返回本轮应使用的 analyzer provider 列表与路由元数据。"""
    from bnb_quant_tool.llm_provider import _providers_with_keys, list_analyzer_providers

    cfg = config or {}
    llm = cfg.get("llm") or {}
    route = llm.get("routing") or {}
    assessment = assess_market_stress(
        atr_ratio=atr_ratio,
        market_regime=market_regime,
        bnb_factors=bnb_factors,
        news_summary=news_summary,
        config=cfg,
    )
    if force_stress:
        assessment = {
            **assessment,
            "mode": "stress",
            "stress": True,
            "reasons": list(assessment.get("reasons") or []) + ["force_full_ai"],
        }

    if not bool(route.get("enabled", True)):
        providers = list_analyzer_providers(cfg)
        return {
            **assessment,
            "providers": providers,
            "routing_applied": False,
            "note": "路由未启用，使用默认 analyzer_providers",
        }

    if assessment["stress"]:
        raw = list(route.get("stress_providers") or llm.get("analyzer_providers") or [])
        note = "异动/事件 → 三家（或配置的压力队）"
        if force_stress:
            note = "强制全量 AI → 三家"
    else:
        raw = list(route.get("calm_providers") or ["deepseek", "qianwen"])
        note = "平静期 → 双模型保底"

    providers = _providers_with_keys(cfg, raw) if raw else []
    if not providers:
        providers = list_analyzer_providers(cfg)
        note += "；回退默认列表"

    return {
        **assessment,
        "providers": providers,
        "routing_applied": True,
        "note": note + (f"；原因: {', '.join(assessment['reasons'])}" if assessment["reasons"] else ""),
    }


def apply_route_to_config(
    config: Optional[Dict],
    *,
    atr_ratio: float = 1.0,
    market_regime: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    force_stress: bool = False,
) -> Dict[str, Any]:
    """返回带临时 analyzer_providers 覆盖的 config 副本 + 路由信息。"""
    cfg = copy.deepcopy(config or {})
    info = resolve_analyzer_providers_for_route(
        cfg,
        atr_ratio=atr_ratio,
        market_regime=market_regime,
        bnb_factors=bnb_factors,
        news_summary=news_summary,
        force_stress=force_stress,
    )
    providers: Sequence[str] = info.get("providers") or []
    llm = cfg.setdefault("llm", {})
    if info.get("routing_applied") and providers:
        llm["analyzer_providers"] = list(providers)
        # 单模型时不强制 consensus 综合
        if len(providers) == 1:
            llm["analyzer_provider"] = providers[0]
            llm["synthesis"] = False
        else:
            llm["analyzer_provider"] = "consensus"
            llm["synthesis"] = bool(llm.get("synthesis", True))
    cfg["_llm_route"] = {
        "mode": info.get("mode"),
        "providers": list(providers),
        "note": info.get("note"),
        "reasons": info.get("reasons") or [],
        "atr_ratio": info.get("atr_ratio"),
        "force_stress": bool(force_stress),
    }
    logger.info("[LLM路由] %s providers=%s", info.get("note"), providers)
    return cfg
