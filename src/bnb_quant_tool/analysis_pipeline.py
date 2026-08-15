"""
统一分析后处理管道 — 模式记忆 / 反事实 / Funding 门控 / 快照序列化。

供 GUI 手动分析、AI 全自动、Autopilot 共用，避免三条链路漂移。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def attach_ta_playbook(
    payload: Dict[str, Any],
    *,
    config: Optional[Dict] = None,
    account_balance: Optional[float] = None,
) -> Dict[str, Any]:
    """为 analysis_result / record 附加 ta_playbook 快照。"""
    out = dict(payload or {})
    try:
        from bnb_quant_tool.crypto_ta_playbook import build_ta_analysis_bundle

        mr = out.get("market_regime") or {}
        regime = mr.get("regime") if isinstance(mr, dict) else None
        balance = account_balance
        if balance is None:
            balance = (out.get("trade_advice") or {}).get("account_balance")
        out["ta_playbook"] = build_ta_analysis_bundle(
            regime=regime,
            indicators=out.get("indicators"),
            inst_results=out.get("institutional_strategies"),
            config=config,
            account_balance=balance,
            symbol=str(out.get("symbol") or "BNBUSDT"),
        )
    except Exception as e:
        logger.debug("attach_ta_playbook: %s", e)
        out["ta_playbook"] = {"enabled": False, "error": str(e)}
    return out


def apply_post_advice_gates(
    trade_advice: Dict[str, Any],
    *,
    learning_context: Optional[Dict] = None,
    config: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    pattern_memory=None,
    pattern_insight: Optional[Dict] = None,
    indicators: Optional[Dict] = None,
    ai_analysis: Optional[Dict] = None,
    inst_results: Optional[Dict] = None,
    current_price: float = 0.0,
    news_summary: Optional[Dict] = None,
    onchain: Optional[Dict] = None,
    macro: Optional[Dict] = None,
    multi_agent_fn: Optional[Callable] = None,
    multi_agent_kwargs: Optional[Dict] = None,
) -> Dict[str, Any]:
    """应用模式记忆、反事实、Funding、胜率学习、TA 门控；可选多智能体。"""
    from bnb_quant_tool.learning_gates import apply_all_learning_post_gates

    return apply_all_learning_post_gates(
        trade_advice,
        learning_context=learning_context,
        config=config,
        sentiment=sentiment,
        bnb_factors=bnb_factors,
        pattern_memory=pattern_memory,
        pattern_insight=pattern_insight,
        indicators=indicators,
        ai_analysis=ai_analysis,
        inst_results=inst_results,
        current_price=current_price,
        news_summary=news_summary,
        onchain=onchain,
        macro=macro,
        multi_agent_fn=multi_agent_fn,
        multi_agent_kwargs=multi_agent_kwargs,
    )


def attach_decision_explanation(
    trade_advice: Dict[str, Any],
    explainer,
    *,
    indicators: Dict,
    ai_analysis: Dict,
    institutional: Dict,
    learning_insights: Optional[Dict] = None,
    multi_timeframe: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    factor_reliability: Optional[Dict] = None,
    append_to_report: bool = True,
) -> Dict[str, Any]:
    """在门控/动态仓位之后生成决策分解，与最终 action 一致。"""
    advice = dict(trade_advice or {})
    try:
        explanation = explainer.explain(
            action=advice.get("action") or advice.get("raw_action") or "WAIT",
            indicators=indicators,
            ai_analysis=ai_analysis,
            institutional=institutional,
            learning_insights=learning_insights or {},
            multi_timeframe=multi_timeframe,
            sentiment=sentiment,
            news_summary=news_summary,
            bnb_factors=bnb_factors,
            prices=advice.get("prices"),
            risk_reward_ratio=advice.get("risk_reward_ratio"),
            factor_reliability=factor_reliability,
            votes=advice.get("votes"),
            dl_signal=advice.get("dl_signal"),
            explorer_signal=advice.get("explorer_signal"),
            gate_reasons=advice.get("gate_reasons"),
        )
        advice["explanation"] = explanation
        if append_to_report and explanation.get("text") and advice.get("report_text"):
            marker = "[决策分解]"
            if marker not in advice["report_text"]:
                advice["report_text"] = advice["report_text"] + "\n\n" + explanation["text"]
    except Exception as e:
        logger.debug("attach_decision_explanation: %s", e)
    return advice


def apply_dynamic_position(
    trade_advice: Dict[str, Any],
    *,
    position_sizer,
    regime_detector,
    df,
    indicators: Dict,
    market_regime: Optional[Dict] = None,
    multi_timeframe: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """门控与多智能体之后应用动态仓位（最终 action/confidence）。"""
    advice = dict(trade_advice or {})
    if advice.get("action") == "WAIT" or not advice.get("position"):
        return advice
    try:
        avg_atr = regime_detector._avg_atr(df)
        wrc = advice.get("win_rate_context") or {}
        wr = float(
            wrc.get("paper_win_rate")
            or wrc.get("win_rate")
            or wrc.get("strategy_win_rate")
            or 0.5
        )
        if wr > 1.0:
            wr = wr / 100.0
        council_sf = advice.get("council_size_factor")
        if council_sf is None:
            ma = advice.get("multi_agent_deliberation") or {}
            council = ma.get("council") if isinstance(ma, dict) else {}
            if isinstance(council, dict) and council.get("size_factor") is not None:
                council_sf = council.get("size_factor")

        dyn = position_sizer.compute_factor(
            confidence=float(advice.get("confidence", 0.5) or 0.5),
            conservativeness=float(advice.get("conservativeness", 1.0) or 1.0),
            atr_ratio=(
                float(indicators.get("ATR", 0) or 0) / avg_atr
                if avg_atr and indicators.get("ATR") else 1.0
            ),
            multi_timeframe=multi_timeframe,
            news_summary=news_summary,
            market_regime=market_regime,
            win_rate=wr,
            prices=advice.get("prices") if isinstance(advice.get("prices"), dict) else None,
            council_size_factor=float(council_sf) if council_sf is not None else None,
            already_probed=bool(advice.get("confidence_hard_probe") or advice.get("probe_position")),
        )
        advice["position"] = position_sizer.apply_to_position(
            advice["position"], dyn["factor"]
        )
        advice["dynamic_position"] = dyn
        # 单笔名义硬顶（默认账户 20%）
        advice = _clamp_position_notional(advice, config=config)
        if dyn.get("note") and advice.get("report_text"):
            advice["report_text"] += f"\n\n{dyn['note']}"
    except Exception as e:
        logger.debug("apply_dynamic_position: %s", e)
    return advice


def _clamp_position_notional(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """单笔 usdt_amount ≤ 账户余额 × max_position_pct（与 Advisor 同源）。"""
    cfg = config or {}
    try:
        from bnb_quant_tool.config_access import get_max_position_pct

        max_pct = float(get_max_position_pct(cfg, default=0.20))
    except Exception:
        risk = cfg.get("risk_management") or {}
        kelly_cfg = cfg.get("kelly") or {}
        ai_cfg = cfg.get("ai_trading") or {}
        max_pct = float(
            risk.get("max_position_pct")
            or kelly_cfg.get("max_position_pct")
            or ai_cfg.get("max_position_pct")
            or 0.20
        )
    trading = cfg.get("trading") or {}
    balance = float(
        trading.get("account_balance")
        or (cfg.get("kelly") or {}).get("account_balance")
        or 5000.0
    )
    pos = advice.get("position")
    if not isinstance(pos, dict) or max_pct <= 0 or balance <= 0:
        return advice
    cap = balance * max_pct
    usdt = float(pos.get("usdt_amount") or 0)
    if usdt <= 0 or usdt <= cap:
        return advice
    scale = cap / usdt
    pos = dict(pos)
    for k in ("usdt_amount", "quantity", "margin_required", "risk_amount"):
        if pos.get(k) is not None:
            try:
                pos[k] = round(float(pos[k]) * scale, 6)
            except (TypeError, ValueError):
                pass
    pos["notional_capped"] = True
    pos["max_position_pct"] = max_pct
    advice = dict(advice)
    advice["position"] = pos
    note = f"仓位硬顶 {max_pct:.0%} 账户（名义≤{cap:.0f} USDT）"
    reasons = list(advice.get("gate_reasons") or [])
    if note not in reasons:
        reasons.append(note)
    advice["gate_reasons"] = reasons
    return advice


def build_analysis_record_payload(
    *,
    symbol: str,
    timeframe: str,
    current_price: float,
    data_points: int,
    strategy_mode: str,
    indicators: Dict,
    inst_results: Dict,
    ai_analysis: Dict,
    trade_advice: Dict,
    market_regime: Dict,
    learning_context: Optional[Dict] = None,
    news_summary: Optional[Dict] = None,
    news_items: Optional[list] = None,
    trading_plan: Optional[Dict] = None,
    risk_check: Optional[Dict] = None,
    final_recommendation: Optional[str] = None,
    bg_extra: Optional[Dict] = None,
) -> Dict[str, Any]:
    """统一 manual / fullauto 的 record_analysis 结构。"""
    ta_action = trade_advice.get("action", "WAIT")
    final = final_recommendation or (
        "BUY" if ta_action == "LONG" else ("SELL" if ta_action == "SHORT" else "HOLD")
    )
    payload = {
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "data_points": data_points,
        "current_price": current_price,
        "strategy_mode": strategy_mode,
        "indicators": indicators,
        "institutional_strategies": inst_results,
        "ai_analysis": ai_analysis,
        "trading_plan": trading_plan or {},
        "risk_check": risk_check or {"passed": trade_advice.get("passed_gate")},
        "final_recommendation": final,
        "trade_advice": trade_advice,
        "market_regime": market_regime,
        "learning_context": learning_context or {},
        "news_summary": news_summary or {},
        "news_items": news_items or [],
    }
    if bg_extra:
        payload.update(bg_extra)
    return payload


def build_trade_advice_snapshot(
    trade_advice: Dict[str, Any],
    *,
    market_regime: Optional[Dict] = None,
    learning_context: Optional[Dict] = None,
    ai_analysis: Optional[Dict] = None,
    ai_analyses: Optional[Dict] = None,
    ai_analysis_note: str = "",
    ai_primary_provider: str = "",
) -> str:
    """序列化 Web/GUI 驾驶舱所需字段。"""
    ta = trade_advice or {}
    mr = market_regime or {}
    lc = learning_context or {}
    ai = ai_analysis if isinstance(ai_analysis, dict) else {}
    analyses_in = ai_analyses if isinstance(ai_analyses, dict) else {}
    conv = ta.get("institutional_conviction") or lc.get("institutional_conviction") or {}

    # 压缩各家信号，供 Web 三家看板（避免塞进完整长文）
    analyses_compact: Dict[str, Any] = {}
    for prov, pack in analyses_in.items():
        if not isinstance(pack, dict):
            continue
        analyses_compact[str(prov)] = {
            "signal": pack.get("signal") or pack.get("trade_suggestion"),
            "confidence": pack.get("confidence"),
            "_degraded": bool(pack.get("_degraded") or pack.get("_error")),
        }

    reused = bool(
        ta.get("_reused")
        or ai.get("_reused")
        or ai.get("_provider") == "knowledge_reuse"
    )
    payload = {
        "action": ta.get("action"),
        "confidence": ta.get("confidence"),
        "passed_gate": ta.get("passed_gate"),
        "gate_reasons": ta.get("gate_reasons"),
        "circuit_breaker": ta.get("circuit_breaker"),
        "institutional_conviction": {
            "conviction": conv.get("conviction"),
            "score": conv.get("conviction") if conv.get("conviction") is not None else conv.get("score"),
            "direction": conv.get("direction"),
            "strength": conv.get("strength"),
            "summary": conv.get("summary"),
            "factors": (conv.get("factors") or [])[:8],
            "conflicts": (conv.get("conflicts") or [])[:4],
            "strategy_family": conv.get("strategy_family"),
        },
        "market_regime": {
            "regime": mr.get("regime") or ta.get("market_regime", {}).get("regime") if isinstance(ta.get("market_regime"), dict) else mr.get("regime"),
            "description": mr.get("description"),
            "fusion_confidence": mr.get("fusion_confidence"),
            "regime_votes": mr.get("regime_votes"),
            "regime_conflicts": mr.get("regime_conflicts"),
            "hmm_regime": mr.get("hmm_regime"),
            "hmm_confidence": mr.get("hmm_confidence"),
            "hmm_detail": mr.get("hmm_detail"),
            "hmm_agreement": mr.get("hmm_agreement"),
        },
        "structural_vote": ta.get("structural_vote"),
        "analysis_mode": ta.get("analysis_mode"),
        "votes": ta.get("votes"),
        "raw_action": ta.get("raw_action"),
        "execution_context": ta.get("execution_context"),
        "effective_direction": ta.get("effective_direction"),
        "multi_agent_deliberation": ta.get("multi_agent_deliberation"),
        "ta_playbook": ta.get("ta_playbook") or lc.get("ta_playbook"),
        "win_rate_context": ta.get("win_rate_context") or lc.get("win_rate_context"),
        # Web 闭环展示
        "_reused": reused,
        "_reuse_reason": ta.get("_reuse_reason") or ai.get("_reuse_reason") or "",
        "_skipped_council_reuse": bool(ta.get("_skipped_council_reuse")),
        "ai_primary_provider": ai_primary_provider
        or ta.get("ai_primary_provider")
        or ai.get("_provider")
        or "",
        "ai_analysis_note": ai_analysis_note or ta.get("ai_analysis_note") or "",
        "ai_analyses": analyses_compact,
        "ai_analysis": {
            "_reused": reused,
            "_provider": ai.get("_provider"),
            "_reuse_reason": ai.get("_reuse_reason"),
            "signal": ai.get("signal"),
            "confidence": ai.get("confidence"),
            "trade_suggestion": ai.get("trade_suggestion"),
        },
    }
    return json.dumps(payload, ensure_ascii=False)
