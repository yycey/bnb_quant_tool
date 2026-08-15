"""LLM 超时 / 失败时的规则引擎降级。

方向：MTF 共振 + 均线排列；止损：固定 1.5%（ATR 可用时取 max(1.5%价, 1.5×ATR)）。
数据不足时强制 WAIT 并写告警。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_STOP_PCT = 0.015
_DEFAULT_ATR_MULT = 1.5
_DEFAULT_RR = 1.5


def _f(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _resolve_price(indicators: Dict[str, Any], df=None) -> Optional[float]:
    for key in ("close", "price", "last", "current_price"):
        p = _f(indicators.get(key))
        if p and p > 0:
            return p
    if df is not None and len(df) > 0:
        try:
            return float(df.iloc[-1]["close"])
        except Exception:
            return None
    return None


def _ma_alignment(indicators: Dict[str, Any], price: float) -> Tuple[str, str]:
    """返回 (bias, detail)：bias ∈ long|short|neutral。"""
    ma20 = _f(indicators.get("MA_20") or indicators.get("SMA_20"))
    ma50 = _f(indicators.get("MA_50") or indicators.get("SMA_50"))
    ma200 = _f(indicators.get("MA_200") or indicators.get("SMA_200"))
    parts: List[str] = []
    if ma20:
        parts.append(f"MA20={ma20:.4f}")
    if ma50:
        parts.append(f"MA50={ma50:.4f}")
    if ma200:
        parts.append(f"MA200={ma200:.4f}")
    detail = " ".join(parts) or "均线缺失"

    if ma20 is None or ma50 is None:
        return "neutral", detail

    if price > ma20 > ma50:
        if ma200 is None or ma50 >= ma200 * 0.995:
            return "long", detail
        return "long" if price > ma20 else "neutral", detail
    if price < ma20 < ma50:
        if ma200 is None or ma50 <= ma200 * 1.005:
            return "short", detail
        return "short" if price < ma20 else "neutral", detail
    return "neutral", detail


def _mtf_bias(multi_timeframe: Optional[Dict[str, Any]]) -> Tuple[str, str, Optional[float]]:
    """返回 (bias, detail, resonance)。"""
    mtf = multi_timeframe if isinstance(multi_timeframe, dict) else {}
    if not mtf:
        return "neutral", "MTF缺失", None
    action = str(mtf.get("recommended_action") or "").upper()
    score = _f(mtf.get("resonance_score"))
    conf = str(mtf.get("confluence") or "")
    detail = f"action={action or '?'} resonance={score} confluence={conf}"
    if action in ("LONG", "BUY"):
        return "long", detail, score
    if action in ("SHORT", "SELL"):
        return "short", detail, score
    if score is not None:
        if score >= 50:
            return "long", detail, score
        if score <= -50:
            return "short", detail, score
        if score > 15:
            return "long", detail, score
        if score < -15:
            return "short", detail, score
    return "neutral", detail, score


def _stop_distance(price: float, indicators: Dict[str, Any], config: Optional[dict]) -> float:
    llm = (config or {}).get("llm") or {}
    fb = llm.get("rule_fallback") if isinstance(llm.get("rule_fallback"), dict) else {}
    stop_pct = float(fb.get("stop_pct", _DEFAULT_STOP_PCT) or _DEFAULT_STOP_PCT)
    atr_mult = float(fb.get("atr_mult", _DEFAULT_ATR_MULT) or _DEFAULT_ATR_MULT)
    pct_dist = price * stop_pct
    atr = _f(indicators.get("ATR") or indicators.get("ATR_14") or indicators.get("atr"))
    if atr is not None and atr > 0:
        return max(pct_dist, atr * atr_mult)
    return pct_dist


def force_wait_analysis(*, reason: str, price: Optional[float] = None) -> Dict[str, Any]:
    """规则引擎也无法决策时的强制 WAIT。"""
    entry = float(price) if price and price > 0 else 0.0
    return {
        "trend": "震荡",
        "confidence": 0.2,
        "signal": "持有",
        "trade_suggestion": "WAIT",
        "entry_price": entry,
        "stop_loss": entry,
        "take_profit": entry,
        "risk_reward_ratio": 0.0,
        "analysis": f"【规则引擎兜底】{reason} → 强制 WAIT。",
        "self_reflection": "LLM 超时且规则数据不足，禁止开仓。",
        "risks": ["LLM超时", "规则数据不足", "强制观望"],
        "_provider": "rule_fallback",
        "_provider_label": "规则引擎",
        "_model": "mtf+ma",
        "_llm_timeout": True,
        "_rule_fallback": True,
        "_rule_forced_wait": True,
        "_degraded": True,
    }


def build_rule_engine_analysis(
    indicators: Dict[str, Any],
    *,
    multi_timeframe: Optional[Dict[str, Any]] = None,
    df=None,
    config: Optional[dict] = None,
    reason: str = "LLM超时",
) -> Dict[str, Any]:
    """用 MTF + 均线给出方向；失败则 WAIT。"""
    ind = indicators if isinstance(indicators, dict) else {}
    price = _resolve_price(ind, df)
    if not price or price <= 0:
        return force_wait_analysis(reason=f"{reason}；无有效价格", price=None)

    ma_bias, ma_detail = _ma_alignment(ind, price)
    mtf_bias, mtf_detail, resonance = _mtf_bias(multi_timeframe)

    # 同向优先；均线有方向且 MTF 不反向也可；仅 MTF 强共振（|score|≥50）可独立给方向
    if ma_bias == mtf_bias and ma_bias in ("long", "short"):
        direction = ma_bias
    elif ma_bias in ("long", "short") and mtf_bias == "neutral":
        direction = ma_bias
    elif mtf_bias in ("long", "short") and ma_bias == "neutral":
        if resonance is not None and abs(resonance) >= 50:
            direction = mtf_bias
        else:
            direction = "wait"
    else:
        direction = "wait"

    if direction == "wait":
        why = (
            f"均线与MTF均无明确方向（{ma_detail}；{mtf_detail}）"
            if ma_bias == "neutral" and mtf_bias == "neutral"
            else f"方向冲突或不足（MA={ma_bias}, MTF={mtf_bias}；{ma_detail}；{mtf_detail}）"
        )
        return force_wait_analysis(reason=f"{reason}；{why}", price=price)

    sl_dist = _stop_distance(price, ind, config)
    rr = float(
        (((config or {}).get("llm") or {}).get("rule_fallback") or {}).get("rr", _DEFAULT_RR)
        or _DEFAULT_RR
    )
    if direction == "long":
        stop = price - sl_dist
        tp = price + sl_dist * rr
        signal = "买入"
        trend = "看涨"
        suggestion = "LONG"
    else:
        stop = price + sl_dist
        tp = price - sl_dist * rr
        signal = "卖出"
        trend = "看跌"
        suggestion = "SHORT"

    # 双信号一致更高置信；仅均线略低
    if ma_bias == mtf_bias == direction:
        conf = 0.62
    elif ma_bias == direction:
        conf = 0.58
    else:
        conf = 0.55

    analysis = (
        f"【规则引擎降级｜{reason}】方向={suggestion}；"
        f"均线={ma_bias}({ma_detail})；MTF={mtf_bias}({mtf_detail})；"
        f"止损距={sl_dist:.4f}(1.5%价/1.5×ATR取大)，RR={rr}."
    )
    return {
        "trend": trend,
        "confidence": conf,
        "signal": signal,
        "trade_suggestion": suggestion,
        "entry_price": round(price, 6),
        "entry_zone": [round(price * 0.998, 6), round(price * 1.002, 6)],
        "stop_loss": round(stop, 6),
        "take_profit": round(tp, 6),
        "tp1": round(price + (tp - price) * 0.5 if direction == "long" else price + (tp - price) * 0.5, 6),
        "tp2": round(price + (tp - price) * 0.8 if direction == "long" else price + (tp - price) * 0.8, 6),
        "tp3": round(tp, 6),
        "risk_reward_ratio": round(rr, 3),
        "hold_hours": 10,
        "position_size_pct": 0.08,
        "analysis": analysis,
        "key_levels": [
            {"type": "支撑" if direction == "long" else "阻力", "price": round(stop, 6)},
            {"type": "阻力" if direction == "long" else "支撑", "price": round(tp, 6)},
        ],
        "risks": ["LLM超时降级", "规则引擎非LLM研判", "请人工复核"],
        "invalidation": "价格击破止损或均线/MTF 翻转",
        "self_reflection": "本次未调用有效 LLM，结论来自本地规则，勿过度信任。",
        "_provider": "rule_fallback",
        "_provider_label": "规则引擎",
        "_model": "mtf+ma+atr_stop",
        "_llm_timeout": True,
        "_rule_fallback": True,
        "_rule_detail": {
            "ma_bias": ma_bias,
            "mtf_bias": mtf_bias,
            "resonance": resonance,
            "stop_distance": sl_dist,
            "reason": reason,
        },
    }


def _events_path(config: Optional[dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager

        root = Path(get_localization_manager().workspace)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data / "llm_fallback_events.jsonl"


def append_fallback_event(event: Dict[str, Any], config: Optional[dict] = None) -> None:
    payload = {"ts": datetime.now().isoformat(), **event}
    path = _events_path(config)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.debug("append_fallback_event failed: %s", e)


def emit_llm_fallback_alert(
    message: str,
    *,
    config: Optional[dict] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """告警：日志 CRITICAL + 事件文件。"""
    logger.critical("[ALERT][LLM降级] %s | %s", message, details or {})
    append_fallback_event(
        {"type": "alert", "message": message, "details": details or {}},
        config=config,
    )


def record_llm_timeout_memory(
    config: Optional[dict],
    *,
    learner=None,
    reason: str = "LLM超时",
    providers_failed: Optional[Dict[str, str]] = None,
    rule_action: str = "WAIT",
    forced_wait: bool = False,
) -> None:
    """记录「LLM超时」到记忆库（知识卡 + 事件日志）。"""
    detail = {
        "reason": reason,
        "providers_failed": providers_failed or {},
        "rule_action": rule_action,
        "forced_wait": forced_wait,
    }
    append_fallback_event({"type": "llm_timeout", **detail}, config=config)

    card = {
        "category": "error_lesson",
        "title": "LLM超时",
        "lesson": (
            f"主分析 LLM 超时/失败已降级规则引擎："
            f"失败={providers_failed or {}}；规则动作={rule_action}；"
            f"{'强制WAIT并告警' if forced_wait else '规则给出方向'}。"
        ),
        "confidence": 0.7,
        "tags": ["LLM超时", "rule_fallback", "degradation"],
        "trigger_condition": "llm_timeout_or_api_failure",
        "action_taken": rule_action,
    }
    mem = None
    if learner is not None:
        mem = getattr(learner, "capability_memory", None)
    if mem is not None:
        try:
            mem.save_knowledge_card(card, source="llm_timeout")
        except Exception as e:
            logger.warning("record LLM超时记忆失败: %s", e)

    if forced_wait:
        emit_llm_fallback_alert(
            "规则引擎数据不足，强制 WAIT",
            config=config,
            details=detail,
        )
    else:
        logger.warning("[LLM超时] 已降级规则引擎 → %s | %s", rule_action, providers_failed)
