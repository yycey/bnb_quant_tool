"""
统一学习门控 — 模式记忆 / 反事实 / Funding / 胜率学习 / TA Playbook 后处理。

供 analysis_pipeline、GUI、Headless 共用，避免门控逻辑分散在多处。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def apply_long_strict_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """LONG 加严：历史 LONG 弱于 SHORT，要求更高置信度与净 RR。"""
    cfg = (config or {}).get("ai_trading") or {}
    if cfg.get("long_strict_gate_enabled", True) is False:
        return advice
    action = str(advice.get("action") or "").upper()
    if action != "LONG":
        return advice

    conf = float(
        advice.get("calibrated_confidence")
        or advice.get("confidence")
        or 0.0
    )
    min_conf = float(cfg.get("long_min_confidence", 0.72) or 0.72)
    min_rr = float(cfg.get("long_min_net_rr", 2.0) or 2.0)
    net_rr = 0.0
    detail = advice.get("net_rr") or {}
    if isinstance(detail, dict):
        for key in ("net_rr", "ratio", "net_reward_risk", "rr"):
            if detail.get(key) is not None:
                try:
                    net_rr = float(detail[key])
                    break
                except (TypeError, ValueError):
                    continue
    if net_rr <= 0:
        try:
            net_rr = float(advice.get("net_rr_value") or 0)
        except (TypeError, ValueError):
            net_rr = 0.0

    reasons_fail: list = []
    if conf < min_conf:
        reasons_fail.append(f"LONG置信度 {conf:.0%} < {min_conf:.0%}")
    if net_rr > 0 and net_rr < min_rr:
        reasons_fail.append(f"LONG净RR {net_rr:.2f} < {min_rr:.2f}")
    # 无净 RR 明细时仍用置信度硬拦；有明细则两项都要过
    if not reasons_fail:
        return advice
    if net_rr <= 0 and conf >= min_conf:
        return advice

    out = dict(advice)
    out["action"] = "WAIT"
    out["passed_gate"] = False
    out["long_strict_blocked"] = True
    reasons = list(out.get("gate_reasons") or [])
    msg = "LONG加严拦截: " + "; ".join(reasons_fail)
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    return out


def apply_follow_cooldown_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    learning_context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """同 symbol+side 跟单冷却，防止连开堆单。"""
    cfg = (config or {}).get("ai_trading") or {}
    cooldown_min = float(cfg.get("open_follow_cooldown_minutes", 30) or 0)
    if cooldown_min <= 0:
        return advice
    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice

    lc = learning_context or {}
    paper = lc.get("_paper_engine") or lc.get("paper_engine")
    if paper is None:
        return advice
    symbol = str(advice.get("symbol") or "BNBUSDT")
    try:
        mins = paper.minutes_since_last_open(symbol, action)
    except Exception as e:
        logger.debug("follow_cooldown check failed: %s", e)
        return advice
    if mins is None:
        return advice
    if mins >= cooldown_min:
        return advice

    out = dict(advice)
    out["action"] = "WAIT"
    out["passed_gate"] = False
    out["follow_cooldown_blocked"] = True
    reasons = list(out.get("gate_reasons") or [])
    msg = (
        f"跟单冷却: {symbol} {action} 距上次开仓 {mins:.0f} 分钟 "
        f"< {cooldown_min:.0f} 分钟"
    )
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    return out


def apply_win_rate_gate(
    advice: Dict[str, Any],
    learning_insights: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """胜率学习：regime+方向反复亏损时拦截（与 trade_advisor 内部门控互补）。"""
    cfg = (config or {}).get("win_rate_optimizer") or {}
    if cfg.get("enabled") is False:
        return advice

    from bnb_quant_tool.learning_analytics import apply_direction_blocks

    wrc = (learning_insights or {}).get("win_rate_context") or {}
    action = advice.get("action")
    if action not in ("LONG", "SHORT"):
        return advice

    new_action, reason = apply_direction_blocks(action, wrc)
    if new_action == action or not reason:
        return advice

    out = dict(advice)
    out["action"] = new_action
    out["passed_gate"] = False
    reasons = list(out.get("gate_reasons") or [])
    reasons.append(reason)
    out["gate_reasons"] = reasons
    out["win_rate_blocked"] = True
    return out


def apply_confidence_hard_gate(
    advice: Dict[str, Any],
    *,
    ai_analysis: Optional[Dict] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """综合置信度硬约束：使用校准后置信度卡阈值 / 试探仓。"""
    cfg = (config or {}).get("ai_trading") or {}
    if cfg.get("confidence_hard_gate", True) is False:
        return advice

    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice

    min_conf = float(cfg.get("min_open_confidence", 0.70) or 0.70)
    probe_floor = float(cfg.get("probe_confidence_floor", 0.55) or 0.55)
    probe_scale = float(cfg.get("probe_position_scale", 0.35) or 0.35)
    try:
        from bnb_quant_tool.ai_trading_context import is_learning_phase

        if is_learning_phase(config):
            lp = float(cfg.get("learning_phase_min_open_confidence", min_conf) or min_conf)
            min_conf = min(min_conf, lp)
            probe_floor = min(probe_floor, max(0.45, min_conf - 0.10))
    except Exception:
        pass

    # 优先用校准置信度；否则取 advice/AI 较低者
    if advice.get("calibrated_confidence") is not None:
        effective = float(advice.get("calibrated_confidence") or 0)
    else:
        conf = float(advice.get("confidence") or 0)
        if conf > 1.0:
            conf = conf / 100.0
        ai = ai_analysis if isinstance(ai_analysis, dict) else {}
        ai_conf = float(ai.get("confidence") or 0)
        if ai_conf > 1.0:
            ai_conf = ai_conf / 100.0
        effective = min(conf, ai_conf) if ai_conf > 0 else conf
    if effective > 1.0:
        effective = effective / 100.0

    out = dict(advice)
    reasons = list(out.get("gate_reasons") or [])
    raw = out.get("raw_confidence")
    cal_tag = (
        f"（原始 {float(raw):.0%}→校准 {effective:.0%}）"
        if raw is not None else ""
    )

    if effective < probe_floor:
        out["action"] = "WAIT"
        out["passed_gate"] = False
        msg = (
            f"置信度硬门控: {effective:.0%} < {probe_floor:.0%}{cal_tag}，"
            f"强制观望（杜绝强行开仓）"
        )
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        out["confidence_hard_blocked"] = True
        return out

    if effective < min_conf:
        pos = out.get("position")
        if isinstance(pos, dict):
            pos = dict(pos)
            for k in ("quantity", "usdt_amount", "margin_required", "risk_amount"):
                if pos.get(k) is not None:
                    try:
                        pos[k] = round(float(pos[k]) * probe_scale, 6)
                    except (TypeError, ValueError):
                        pass
            out["position"] = pos
        msg = (
            f"置信度偏弱 {effective:.0%} < {min_conf:.0%}{cal_tag}，"
            f"试探小仓×{probe_scale:.0%}（非满仓）"
        )
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        out["probe_position"] = True
        out["confidence_hard_probe"] = True
        return out

    return out


def apply_net_rr_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    learning_context: Optional[Dict] = None,
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
) -> Dict[str, Any]:
    """净盈亏比硬门槛：扣全链路成本后仍 ≥ min_net_rr。"""
    cfg = (config or {}).get("ai_trading") or {}
    if cfg.get("net_rr_gate_enabled", True) is False:
        return advice
    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice

    prices = advice.get("prices") or {}
    try:
        from bnb_quant_tool.ai_trading_context import is_learning_phase
        learning = is_learning_phase(config)
    except Exception:
        learning = bool(cfg.get("learning_phase"))
    min_rr = float(cfg.get("min_net_rr", 1.5) or 1.5)
    if learning:
        min_rr = float(cfg.get("learning_phase_min_net_rr", min_rr) or min_rr)
    lc = learning_context or {}
    paper = lc.get("_paper_engine") or lc.get("paper_engine")

    # 无有效价格时 RR 会变成 0；学习期不硬杀，留给试探补价
    entry = float(prices.get("entry_mid") or 0)
    sl = float(prices.get("stop_loss") or 0)
    tp = float(prices.get("tp2") or prices.get("tp1") or 0)
    if entry <= 0 or sl <= 0 or tp <= 0:
        out = dict(advice)
        reasons = list(out.get("gate_reasons") or [])
        msg = "净盈亏比跳过：入场/止损/止盈不完整（避免 RR=0 误杀）"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        if learning:
            return out
        # 非学习期：缺价视为不过
        out["action"] = "WAIT"
        out["passed_gate"] = False
        out["net_rr_blocked"] = True
        return out

    try:
        from bnb_quant_tool.trade_cost_model import build_full_cost_inputs
        from bnb_quant_tool.kelly_sizing import net_reward_risk_ratio

        costs = build_full_cost_inputs(
            advice,
            config=config,
            paper_engine=paper,
            sentiment=sentiment,
            bnb_factors=bnb_factors,
        )
        # funding_pct 已是整段持仓成本 → hold_periods=1
        ratio, detail = net_reward_risk_ratio(
            prices,
            fee_rate=float(costs["fee_rate"]),
            slippage_pct=float(costs["slippage_pct"]),
            funding_pct=float(costs["funding_pct"]),
            hold_periods=1,
            liquidity_premium_pct=float(costs["liquidity_premium_pct"]),
        )
        detail = dict(detail)
        detail["hold_hours"] = costs["hold_hours"]
        detail["slippage_meta"] = costs["slippage_meta"]
        detail["funding_meta"] = costs["funding_meta"]
        detail["cost_model"] = "full_pipeline"
        detail["min_rr_applied"] = min_rr
        detail["learning_phase"] = learning
    except Exception as e:
        logger.warning("net_rr full-cost gate failed, skip: %s", e)
        return advice

    out = dict(advice)
    out["net_rr"] = detail
    if ratio <= 0 and learning:
        reasons = list(out.get("gate_reasons") or [])
        msg = f"净盈亏比 {ratio:.2f} 无效，学习期跳过硬拦"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        return out
    if ratio < min_rr:
        out["action"] = "WAIT"
        out["passed_gate"] = False
        reasons = list(out.get("gate_reasons") or [])
        msg = (
            f"净盈亏比 {ratio:.2f} < {min_rr:.1f}"
            f"（含手续费/实测滑点/资金费/流动性溢价），禁止开仓"
        )
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        out["net_rr_blocked"] = True
    return out


def apply_anti_memory_gate(
    advice: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    capability_memory=None,
    situation_key: str = "",
) -> Dict[str, Any]:
    """反记忆：相似踩坑局面直接否决开仓。"""
    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice
    key = situation_key or str(advice.get("situation_key") or "")
    try:
        from bnb_quant_tool.memory_governance import anti_memory_block

        hit = anti_memory_block(
            key, capability_memory=capability_memory, config=config
        )
    except Exception:
        return advice
    if not hit.get("blocked"):
        return advice
    out = dict(advice)
    out["action"] = "WAIT"
    out["passed_gate"] = False
    reasons = list(out.get("gate_reasons") or [])
    msg = str(hit.get("reason") or "反记忆命中，禁止开仓")
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    out["anti_memory_blocked"] = True
    return out


def apply_mtf_resonance_gate(
    advice: Dict[str, Any],
    *,
    multi_timeframe: Optional[Dict] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """多周期共振分数硬门槛：开多需 > long_min，开空需 < short_max。"""
    cfg = (config or {}).get("multi_timeframe") or {}
    if cfg.get("resonance_gate_enabled", True) is False:
        return advice

    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice

    mtf = multi_timeframe or advice.get("multi_timeframe") or {}
    if not isinstance(mtf, dict) or not mtf:
        return advice

    # resonance_score 优先；否则由 weighted_score(-4~4) 映射到 -100~100
    score = mtf.get("resonance_score")
    if score is None:
        ws = float(mtf.get("weighted_score") or 0)
        score = max(-100.0, min(100.0, ws * 25.0))
    else:
        score = float(score)

    long_min = float(cfg.get("resonance_long_min", 50) or 50)
    short_max = float(cfg.get("resonance_short_max", -50) or -50)
    try:
        from bnb_quant_tool.ai_trading_context import is_learning_phase

        ai = (config or {}).get("ai_trading") or {}
        if is_learning_phase(config):
            if ai.get("learning_phase_resonance_long_min") is not None:
                long_min = float(ai["learning_phase_resonance_long_min"])
            if ai.get("learning_phase_resonance_short_max") is not None:
                short_max = float(ai["learning_phase_resonance_short_max"])
    except Exception:
        pass

    out = dict(advice)
    out["mtf_resonance_score"] = round(score, 1)
    reasons = list(out.get("gate_reasons") or [])

    if action == "LONG" and score < long_min:
        out["action"] = "WAIT"
        out["passed_gate"] = False
        msg = f"多周期共振分 {score:.0f} < {long_min:.0f}，禁止开多"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        out["mtf_resonance_blocked"] = True
        return out

    if action == "SHORT" and score > short_max:
        out["action"] = "WAIT"
        out["passed_gate"] = False
        msg = f"多周期共振分 {score:.0f} > {short_max:.0f}，禁止开空"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        out["mtf_resonance_blocked"] = True
        return out

    return out


def apply_all_learning_post_gates(
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
    """应用全部学习相关后处理门控（分析流水线统一出口）。"""
    advice = dict(trade_advice or {})
    cfg = config or {}
    lc = dict(learning_context or {})
    # 门控依赖：校准需 learner；净 RR 需 paper；跨模态需 onchain/macro
    if onchain and not lc.get("onchain"):
        lc["onchain"] = onchain
    if macro and not lc.get("macro"):
        lc["macro"] = macro
    if news_summary and not lc.get("news_summary"):
        lc["news_summary"] = news_summary
    try:
        act = str(advice.get("action") or "").upper()
        if act in ("LONG", "SHORT"):
            from bnb_quant_tool.analysis_reuse import (
                apply_execution_template,
                lookup_execution_template,
            )

            tpl = lookup_execution_template(
                config=cfg,
                symbol=str((multi_agent_kwargs or {}).get("symbol") or "BNBUSDT"),
                action=act,
                indicators=indicators,
                market_regime=advice.get("market_regime") or lc.get("market_regime"),
                learner=None,
            )
            if tpl:
                advice = apply_execution_template(
                    advice, tpl, indicators=indicators
                )
    except Exception as e:
        logger.debug("post_gate execution_template: %s", e)

    try:
        from bnb_quant_tool.ai_trading_context import apply_pattern_memory_gate

        insight = pattern_insight or lc.get("pattern_memory") or {}
        if not insight.get("matched") and pattern_memory and indicators is not None:
            insight = pattern_memory.get_insight({
                "indicators": indicators,
                "ai_analysis": ai_analysis or {},
                "institutional_strategies": inst_results or {},
                "current_price": current_price,
                "news_summary": news_summary or {},
            })
        if insight.get("matched", 0) > 0:
            advice["pattern_insight"] = insight
            if advice.get("report_text") and insight.get("text"):
                advice["report_text"] += "\n\n" + insight["text"]
            advice = apply_pattern_memory_gate(advice, insight, cfg)
    except Exception as e:
        logger.debug("post_gate pattern_memory: %s", e)

    try:
        from bnb_quant_tool.ai_trading_context import apply_counterfactual_gate
        advice = apply_counterfactual_gate(advice, lc, cfg)
    except Exception as e:
        logger.debug("post_gate counterfactual: %s", e)

    try:
        advice = apply_win_rate_gate(advice, lc, cfg)
    except Exception as e:
        logger.debug("post_gate win_rate: %s", e)

    try:
        from bnb_quant_tool.factor_attribution_learner import apply_factor_attribution_gate
        advice = apply_factor_attribution_gate(advice, lc, cfg)
    except Exception as e:
        logger.debug("post_gate factor_attribution: %s", e)

    try:
        from bnb_quant_tool.ai_trading_context import apply_funding_direction_gate
        advice = apply_funding_direction_gate(
            advice,
            sentiment=sentiment,
            bnb_factors=bnb_factors,
            config=cfg,
        )
    except Exception as e:
        logger.debug("post_gate funding: %s", e)

    try:
        from bnb_quant_tool.ta_playbook_gate import apply_ta_playbook_post_gate
        advice = apply_ta_playbook_post_gate(advice, config=cfg)
    except Exception as e:
        logger.debug("post_gate ta_playbook: %s", e)

    # 置信度后验校准 → 硬门控 + 多周期共振 + 跨模态冲突 + 全成本净 RR
    try:
        from bnb_quant_tool.confidence_calibration import apply_confidence_calibration

        advice = apply_confidence_calibration(
            advice,
            ai_analysis=ai_analysis,
            learning_context=lc,
            config=cfg,
        )
    except Exception as e:
        logger.debug("post_gate confidence_calibration: %s", e)

    try:
        advice = apply_confidence_hard_gate(
            advice, ai_analysis=ai_analysis, config=cfg
        )
    except Exception as e:
        logger.debug("post_gate confidence_hard: %s", e)

    try:
        mtf = advice.get("multi_timeframe") or lc.get("multi_timeframe")
        advice = apply_mtf_resonance_gate(
            advice, multi_timeframe=mtf if isinstance(mtf, dict) else None, config=cfg
        )
    except Exception as e:
        logger.debug("post_gate mtf_resonance: %s", e)

    try:
        from bnb_quant_tool.cross_modal_conflict import apply_cross_modal_conflict_gate

        advice = apply_cross_modal_conflict_gate(
            advice,
            config=cfg,
            news_summary=news_summary,
            sentiment=sentiment,
            learning_context=lc,
            onchain=onchain or lc.get("onchain") or (bnb_factors or {}).get("onchain"),
            macro=macro or lc.get("macro"),
        )
    except Exception as e:
        logger.debug("post_gate cross_modal: %s", e)

    try:
        advice = apply_net_rr_gate(
            advice,
            config=cfg,
            learning_context=lc,
            sentiment=sentiment,
            bnb_factors=bnb_factors,
        )
    except Exception as e:
        logger.debug("post_gate net_rr: %s", e)

    try:
        advice = apply_long_strict_gate(advice, config=cfg)
    except Exception as e:
        logger.debug("post_gate long_strict: %s", e)

    try:
        advice = apply_follow_cooldown_gate(
            advice, config=cfg, learning_context=lc
        )
    except Exception as e:
        logger.debug("post_gate follow_cooldown: %s", e)

    try:
        advice = apply_anti_memory_gate(
            advice,
            config=cfg,
            capability_memory=lc.get("capability_memory"),
            situation_key=str(
                (ai_analysis or {}).get("situation_key")
                or advice.get("situation_key")
                or ""
            ),
        )
    except Exception as e:
        logger.debug("post_gate anti_memory: %s", e)

    try:
        from bnb_quant_tool.position_reeval import apply_position_reeval_gate

        advice = apply_position_reeval_gate(
            advice, learning_context=lc, config=cfg
        )
    except Exception as e:
        logger.debug("post_gate position_reeval: %s", e)

    # 知识复用命中：跳过交易员议会（最贵的 token 消耗）
    reused = bool(
        (ai_analysis or {}).get("_reused")
        or advice.get("_reused")
        or (ai_analysis or {}).get("_provider") == "knowledge_reuse"
    )
    allow_skip = bool(
        ((config or {}).get("capability_memory") or {}).get("skip_council_on_reuse", True)
    )
    skip_council = reused and allow_skip
    # 已被硬门控改成 WAIT 也可跳过议会省 token（覆盖全部硬 WAIT 旗标）
    hard_wait_flags = (
        "confidence_hard_blocked",
        "mtf_resonance_blocked",
        "net_rr_blocked",
        "cross_modal_blocked",
        "anti_memory_blocked",
        "pattern_blocked",
        "win_rate_blocked",
        "counterfactual_blocked",
        "funding_blocked",
        "ta_playbook_blocked",
        "factor_attribution_blocked",
        "long_strict_blocked",
        "follow_cooldown_blocked",
    )
    if advice.get("action") == "WAIT" and any(advice.get(f) for f in hard_wait_flags):
        skip_council = True
    if multi_agent_fn and not skip_council:
        try:
            kwargs = dict(multi_agent_kwargs or {})
            advice = multi_agent_fn(advice, **kwargs)
        except Exception as e:
            logger.debug("post_gate multi_agent: %s", e)
    elif multi_agent_fn and skip_council:
        reasons = list(advice.get("gate_reasons") or [])
        if reused and allow_skip:
            reason = (ai_analysis or {}).get("_reuse_reason") or "知识复用：跳过议会 LLM"
        else:
            reason = "硬门控已观望：跳过议会 LLM"
        if reason not in reasons:
            reasons.append(reason)
        advice["gate_reasons"] = reasons
        if reused and allow_skip:
            advice["_skipped_council_reuse"] = True
            logger.info("skip multi-agent council due to knowledge reuse")

    try:
        from bnb_quant_tool.ai_trading_context import (
            apply_learning_phase_probe,
            enrich_advice_execution_metadata,
        )

        advice = apply_learning_phase_probe(advice, cfg)
        advice = enrich_advice_execution_metadata(advice, cfg)
    except Exception as e:
        logger.debug("post_gate learning_probe/execution_metadata: %s", e)

    wrc = lc.get("win_rate_context")
    if wrc:
        advice["win_rate_context"] = wrc

    try:
        from bnb_quant_tool.decision_state import attach_decision_state
        advice = attach_decision_state(advice)
    except Exception as e:
        logger.debug("post_gate decision_state: %s", e)

    return advice
