"""
分析复用门控 — 相同/高度相似局面直接复用历史结论，跳过昂贵 LLM。

目标：
1. 学会了 → 下次相似局面不再全量分析
2. 重复局面强化知识置信度，而不是刷 token
3. 新局面或高不确定时才调用 LLM（真正的进步）
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class ReuseHit:
    """一次可复用的历史结论。"""

    reuse: bool
    reason: str
    action: str = "WAIT"
    confidence: float = 0.0
    similarity: float = 0.0
    source: str = ""  # analysis_record | knowledge_card
    source_id: Optional[int] = None
    situation_key: str = ""
    signal: str = "持有"
    analysis_text: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_ai_analysis(self) -> Dict[str, Any]:
        return {
            "signal": self.signal,
            "confidence": float(self.confidence),
            "trend": "震荡" if self.action == "WAIT" else (
                "上涨" if self.action == "LONG" else "下跌"
            ),
            "analysis": self.analysis_text or self.reason,
            "self_reflection": "知识复用：未调用 LLM，沿用历史相似局面结论",
            "trade_suggestion": self.action,
            "_reused": True,
            "_reuse_source": self.source,
            "_reuse_source_id": self.source_id,
            "_reuse_similarity": round(float(self.similarity), 4),
            "_reuse_reason": self.reason,
            "_provider": "knowledge_reuse",
            "_provider_label": "知识复用",
            "_model": "local-memory",
        }

    def to_ai_bundle(self) -> Dict[str, Any]:
        primary = self.to_ai_analysis()
        return {
            "primary": primary,
            "by_provider": {"knowledge_reuse": primary},
            "primary_provider": "knowledge_reuse",
            "providers": ["knowledge_reuse"],
            "note": self.reason,
            "errors": {},
            "reused": True,
        }


def _mem_cfg(config: Optional[Dict]) -> Dict[str, Any]:
    return dict((config or {}).get("capability_memory") or {})


def effective_reuse_actions(config: Optional[Dict]) -> set:
    """可跳过 LLM 的复用动作集合。

    生产默认禁止 WAIT：WAIT 复用会自强化成观望机。
    allow_wait_reuse_skip_llm=true 时才允许 WAIT 进入 skip-LLM 路径。
    """
    cfg = _mem_cfg(config)
    raw = cfg.get("reuse_actions")
    if raw is None:
        raw = ["LONG", "SHORT"]
    allowed = {_normalize_action(x) for x in (raw or [])}
    allowed.discard("")
    allow_wait = bool(cfg.get("allow_wait_reuse_skip_llm", False))
    if not allow_wait:
        allowed.discard("WAIT")
    # 学习期额外禁 WAIT（即使显式打开）
    ai = (config or {}).get("ai_trading") or {}
    disable_wait = bool(
        cfg.get("disable_reuse_in_learning_phase", True)
        or ai.get("learning_phase_disable_wait_reuse", True)
    )
    if disable_wait:
        try:
            from bnb_quant_tool.ai_trading_context import is_learning_phase
            if is_learning_phase(config):
                allowed.discard("WAIT")
        except Exception:
            pass
    return allowed


def reuse_enabled(config: Optional[Dict]) -> bool:
    cfg = _mem_cfg(config)
    if cfg.get("enabled") is False:
        return False
    if not bool(cfg.get("reuse_known_situation", True) and cfg.get("skip_llm_on_reuse", True)):
        return False
    # 无可复用动作时关闭整条 skip-LLM 路径
    if not effective_reuse_actions(config):
        return False
    # 学习期：若配置要求禁用复用，整段关闭
    ai = (config or {}).get("ai_trading") or {}
    disable = bool(cfg.get("disable_reuse_in_learning_phase", True))
    if disable:
        try:
            from bnb_quant_tool.ai_trading_context import is_learning_phase
            if is_learning_phase(config):
                # 学习期仅允许非 WAIT 复用（若仍有 LONG/SHORT）；默认空则关
                if not effective_reuse_actions(config):
                    return False
        except Exception:
            pass
    # 兼容旧旗标：学习期禁 WAIT 复用已在 effective_reuse_actions 处理
    _ = ai.get("learning_phase_disable_wait_reuse", True)
    return True


def _safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _bucket(v: Optional[float], step: float) -> str:
    if v is None or step <= 0:
        return "x"
    return str(int(math.floor(float(v) / step) * step))


def situation_key(
    indicators: Optional[Dict[str, Any]],
    regime: Any = None,
    *,
    symbol: str = "BNBUSDT",
) -> str:
    """离散局面键：相同桶 → 视为同一类局面。"""
    ind = indicators or {}
    regime_name = ""
    if isinstance(regime, dict):
        regime_name = str(regime.get("regime") or regime.get("label") or "")
    else:
        regime_name = str(regime or "")
    rsi = _safe_float(ind.get("RSI") if ind.get("RSI") is not None else ind.get("rsi"))
    bb = _safe_float(
        ind.get("BB_Position") if ind.get("BB_Position") is not None else ind.get("bb_position")
    )
    adx = _safe_float(ind.get("ADX") if ind.get("ADX") is not None else ind.get("adx"))
    macd_h = _safe_float(
        ind.get("MACD_Histogram")
        if ind.get("MACD_Histogram") is not None
        else ind.get("MACD_hist")
    )
    if macd_h is None:
        macd = _safe_float(ind.get("MACD"))
        sig = _safe_float(ind.get("MACD_Signal"))
        if macd is not None and sig is not None:
            macd_h = macd - sig
    macd_sign = "0"
    if macd_h is not None:
        macd_sign = "1" if macd_h > 0 else "-1"
    return "|".join([
        str(symbol or "BNBUSDT").upper(),
        regime_name.upper() or "UNKNOWN",
        f"rsi{_bucket(rsi, 5)}",
        f"bb{_bucket(bb, 5)}",
        f"adx{_bucket(adx, 5)}",
        f"m{macd_sign}",
    ])


def situation_vector(
    indicators: Optional[Dict[str, Any]],
    regime: Any = None,
) -> List[float]:
    """连续特征向量，用于近邻相似度。"""
    ind = indicators or {}
    rsi = _safe_float(ind.get("RSI"), 50.0) or 50.0
    bb = _safe_float(ind.get("BB_Position"), 50.0) or 50.0
    adx = _safe_float(ind.get("ADX"), 20.0) or 20.0
    stoch = _safe_float(ind.get("Stoch_K"), 50.0) or 50.0
    macd_h = _safe_float(ind.get("MACD_Histogram"), 0.0)
    if macd_h is None:
        macd = _safe_float(ind.get("MACD"), 0.0) or 0.0
        sig = _safe_float(ind.get("MACD_Signal"), 0.0) or 0.0
        macd_h = macd - sig
    vol = _safe_float(ind.get("Volume_Ratio"), 1.0) or 1.0
    atr = _safe_float(ind.get("ATR"), 0.0) or 0.0
    price = _safe_float(ind.get("close") or ind.get("price"), 0.0) or 0.0
    atr_pct = min((atr / price * 100.0) if price > 0 else 0.0, 5.0) / 5.0

    regime_name = ""
    if isinstance(regime, dict):
        regime_name = str(regime.get("regime") or "").upper()
    else:
        regime_name = str(regime or "").upper()
    regime_code = 0.5
    if "RANGE" in regime_name or "震荡" in regime_name:
        regime_code = 0.5
    elif "BULL" in regime_name or "UP" in regime_name or "TREND_UP" in regime_name:
        regime_code = 1.0
    elif "BEAR" in regime_name or "DOWN" in regime_name or "TREND_DOWN" in regime_name:
        regime_code = 0.0

    macd_dir = 0.5
    if macd_h > 0:
        macd_dir = 1.0
    elif macd_h < 0:
        macd_dir = 0.0

    return [
        max(0.0, min(1.0, rsi / 100.0)),
        max(0.0, min(1.0, (bb + 100.0) / 200.0 if bb < 0 or bb > 100 else bb / 100.0)),
        max(0.0, min(1.0, adx / 60.0)),
        max(0.0, min(1.0, stoch / 100.0)),
        macd_dir,
        max(0.0, min(1.0, vol / 3.0)),
        atr_pct,
        regime_code,
    ]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _normalize_action(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in ("LONG", "BUY", "做多", "买入", "看多"):
        return "LONG"
    if s in ("SHORT", "SELL", "做空", "卖出", "看空"):
        return "SHORT"
    return "WAIT"


def _signal_from_action(action: str) -> str:
    if action == "LONG":
        return "买入"
    if action == "SHORT":
        return "卖出"
    return "持有"


def _parse_indicators(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _resolve_learning_db(config: Optional[Dict], learner=None) -> Optional[str]:
    if learner is not None and getattr(learner, "db_path", None):
        return str(learner.db_path)
    try:
        from bnb_quant_tool.data_localization import get_localized_db_path
        return str(get_localized_db_path("ai_learning"))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[2]
    candidate = root / "data" / "ai_learning.db"
    return str(candidate) if candidate.is_file() else None


def _lookup_analysis_reuse(
    db_path: str,
    *,
    symbol: str,
    key: str,
    vec: List[float],
    regime: Any,
    cfg: Dict[str, Any],
) -> Optional[ReuseHit]:
    ttl_min = int(cfg.get("reuse_ttl_minutes", 240) or 240)
    min_sim = float(cfg.get("reuse_min_similarity", 0.90) or 0.90)
    trade_min_sim = float(cfg.get("reuse_trade_min_similarity", 0.95) or 0.95)
    trade_min_conf = float(cfg.get("reuse_trade_min_confidence", 0.55) or 0.55)
    # cfg 可能缺顶层 config；WAIT 过滤由调用方传入的 allowed 覆盖
    allowed = {
        _normalize_action(x)
        for x in (cfg.get("_effective_reuse_actions") or cfg.get("reuse_actions") or ["LONG", "SHORT"])
    }
    if not bool(cfg.get("allow_wait_reuse_skip_llm", False)):
        allowed.discard("WAIT")
    cutoff = (datetime.now() - timedelta(minutes=max(5, ttl_min))).isoformat()

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, timestamp, symbol, indicators, final_signal, ai_signal,
                   ai_confidence, consensus_confidence, trading_action, ai_analysis,
                   actual_result
            FROM analysis_records
            WHERE symbol=? AND timestamp >= ?
            ORDER BY id DESC
            LIMIT 80
            """,
            (symbol, cutoff),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("analysis reuse lookup failed: %s", e)
        return None

    best: Optional[ReuseHit] = None
    for row in rows:
        ind = _parse_indicators(row["indicators"])
        row_regime = regime
        if isinstance(ind.get("regime"), str):
            row_regime = ind.get("regime")
        row_key = situation_key(ind, row_regime, symbol=symbol)
        row_vec = situation_vector(ind, row_regime)
        sim = 1.0 if row_key == key else cosine_similarity(vec, row_vec)
        if row_key == key:
            sim = max(sim, 0.99)

        action = _normalize_action(
            row["trading_action"] or row["final_signal"] or row["ai_signal"]
        )
        conf = float(row["ai_confidence"] or row["consensus_confidence"] or 0.0)
        analysis_txt = str(row["ai_analysis"] or "")
        actual = str(row["actual_result"] or "").upper()
        if (
            "402" in analysis_txt
            or "Payment Required" in analysis_txt
            or "主分析失败" in analysis_txt
        ):
            continue
        # 不要复用「复用合成」的结论，否则 WAIT 会自复制锁死
        if (
            "知识复用" in analysis_txt
            or "局面复用" in analysis_txt
            or "知识卡片复用" in analysis_txt
            or '"_reused": true' in analysis_txt
            or '"_provider": "knowledge_reuse"' in analysis_txt
        ):
            continue
        # 已验证亏损的交易方向不要复用（避免重复踩坑）
        if action in ("LONG", "SHORT") and actual == "LOSS":
            continue
        # 交易方向默认要求曾验证盈利，才敢跳过 LLM 直接开仓
        require_win = bool(cfg.get("reuse_trade_require_win", True))
        if action in ("LONG", "SHORT") and require_win and actual != "WIN":
            continue
        if conf < 0.25 and action == "WAIT" and "复用" not in analysis_txt:
            if "失败" in analysis_txt:
                continue

        if action not in allowed:
            continue
        need_sim = min_sim if action == "WAIT" else trade_min_sim
        need_conf = 0.30 if action == "WAIT" else trade_min_conf
        # 有 WIN 反馈的交易方向：略放宽置信门槛，鼓励复用真经验
        if action in ("LONG", "SHORT") and actual == "WIN":
            need_conf = max(0.45, need_conf - 0.05)
            conf = max(conf, 0.55)
        if sim < need_sim or conf < need_conf:
            continue

        hit = ReuseHit(
            reuse=True,
            reason=(
                f"局面复用: 与分析#{row['id']} 相似 {sim:.0%} "
                f"(key={key}) → {action}"
                + (f" [已验证{actual}]" if actual in ("WIN", "LOSS") else "")
            ),
            action=action,
            confidence=max(conf, 0.35),
            similarity=sim,
            source="analysis_record",
            source_id=int(row["id"]),
            situation_key=key,
            signal=_signal_from_action(action),
            analysis_text=(
                f"【知识复用】当前局面与历史分析#{row['id']}高度相似（{sim:.0%}），"
                f"沿用结论 {action}，跳过 LLM 全量分析以节省 token、巩固学习。"
            ),
            meta={
                "prior_timestamp": row["timestamp"],
                "prior_confidence": conf,
                "prior_actual": actual or None,
            },
        )
        # 已验证 WIN 的交易复用优先于普通 WAIT
        score = (
            hit.similarity
            + (0.05 if action in ("LONG", "SHORT") and actual == "WIN" else 0.0)
            + (0.02 if actual == "WIN" else 0.0)
        )
        if best is None or score > (
            best.similarity
            + (0.05 if best.action in ("LONG", "SHORT") and (best.meta or {}).get("prior_actual") == "WIN" else 0.0)
        ):
            best = hit
    return best


def _card_overbought_trigger_matches(blob: str, indicators: Dict[str, Any]) -> bool:
    """「高位追多 / 超买禁入」类卡片：仅当指标真处于高位时才可复用 WAIT。"""
    chase_keys = ("高位", "追多", "超买", "STOCH", "布林", "BB", "上轨")
    if not any(k.upper() in blob.upper() for k in chase_keys):
        return False
    rsi = _safe_float(indicators.get("RSI") if indicators.get("RSI") is not None else indicators.get("rsi"))
    stoch = _safe_float(
        indicators.get("Stoch_K") if indicators.get("Stoch_K") is not None else indicators.get("stoch_k")
    )
    bb = _safe_float(
        indicators.get("BB_Position")
        if indicators.get("BB_Position") is not None
        else indicators.get("bb_position")
    )
    # 与卡 #31 触发条件对齐：BB>80 或 Stoch>70 或 RSI>60 至少两项
    flags = 0
    if bb is not None and bb > 80:
        flags += 1
    if stoch is not None and stoch > 70:
        flags += 1
    if rsi is not None and rsi > 60:
        flags += 1
    return flags >= 2


def _card_is_oversold_rule(blob: str) -> bool:
    """超卖类规则不可被超买匹配器误触。"""
    b = (blob or "").lower()
    return any(
        k in b
        for k in (
            "超卖", "oversold", "rsi < 30", "rsi<30", "stoch_k < 20",
            "bb_position < 20", "禁止逆势做空",
        )
    )


def _lookup_card_reuse(
    learner,
    *,
    symbol: str,
    key: str,
    indicators: Dict[str, Any],
    regime: Any,
    cfg: Dict[str, Any],
) -> Optional[ReuseHit]:
    """用已强化的知识卡片做复用（validated 次数 = 学过几次）。

    注意：validated 只表示「强化强度」，不能当作「任意局面都命中」。
    否则一张高验证 WAIT 卡会永久跳过 LLM、把系统锁死在观望。
    """
    if learner is None:
        return None
    min_val = int(cfg.get("reuse_min_validations", 3) or 3)
    min_conf = float(cfg.get("reuse_min_card_confidence", 0.45) or 0.45)
    allowed = {
        _normalize_action(x)
        for x in (cfg.get("_effective_reuse_actions") or cfg.get("reuse_actions") or ["LONG", "SHORT"])
    }
    if not bool(cfg.get("allow_wait_reuse_skip_llm", False)):
        allowed.discard("WAIT")
    if not allowed:
        return None
    try:
        mem = learner.capability_memory
        cards = mem.retrieve_for_analysis(
            {
                "symbol": symbol,
                "regime": (regime.get("regime") if isinstance(regime, dict) else regime),
                "indicators": indicators,
                "situation_key": key,
            },
            top_k=12,
        )[0]
    except Exception as e:
        logger.debug("card reuse retrieve failed: %s", e)
        return None

    best: Optional[ReuseHit] = None
    for card in cards or []:
        validated = int(card.get("times_validated") or 0)
        conf = float(card.get("confidence") or 0)
        if validated < min_val and conf < max(min_conf, 0.6):
            continue
        if conf < min_conf and validated < min_val:
            continue

        tags = card.get("tags") or []
        trigger = str(card.get("trigger_condition") or "")
        title = str(card.get("title") or "")
        rule = str(card.get("action_rule") or "")
        lesson = str(card.get("lesson") or "")
        blob = " ".join([title, trigger, rule, lesson, " ".join(str(t) for t in tags)])
        blob_u = blob.upper()

        key_hit = False
        # 精确局面键命中（禁止短串子串误伤）
        if f"situation_key={key}" in trigger or key in [str(t) for t in tags]:
            key_hit = True
        sit_tag = next((str(t) for t in tags if str(t).startswith(f"{symbol.upper()}|")), "")
        if sit_tag and sit_tag == key:
            key_hit = True

        # 超卖规则禁止走超买匹配器（卡#571 误触主因）
        overbought_hit = False
        if not _card_is_oversold_rule(blob):
            overbought_hit = _card_overbought_trigger_matches(blob, indicators or {})

        # 禁止：validated 次数高 → 全局面通配。必须局面键或指标触发条件真正对齐。
        if not (key_hit or overbought_hit):
            continue

        action = "WAIT"
        if any(k in blob_u for k in ("WAIT", "观望", "HOLD", "持有", "禁止", "勿", "不要追")):
            action = "WAIT"
        elif any(k in blob_u for k in ("LONG", "BUY", "做多", "买入")):
            action = "LONG"
        elif any(k in blob_u for k in ("SHORT", "SELL", "做空", "卖出")):
            action = "SHORT"

        if action not in allowed:
            continue

        sim = 0.93 if key_hit else 0.90
        hit = ReuseHit(
            reuse=True,
            reason=(
                f"知识卡片复用: #{card.get('id')} [{card.get('title', '')[:24]}] "
                f"已验证{validated}次 conf={conf:.0%} → {action}"
                + (" [超买触发]" if overbought_hit and not key_hit else "")
            ),
            action=action,
            confidence=max(conf, 0.4),
            similarity=sim,
            source="knowledge_card",
            source_id=int(card.get("id") or 0) or None,
            situation_key=key,
            signal=_signal_from_action(action),
            analysis_text=(
                f"【知识复用】命中知识卡片「{card.get('title', '')}」"
                f"（已验证 {validated} 次）。规则: {rule[:120]}。跳过 LLM。"
            ),
            meta={
                "card_confidence": conf,
                "times_validated": validated,
                "match": "key" if key_hit else "overbought",
            },
        )
        if best is None or (validated, conf) > (
            int((best.meta or {}).get("times_validated") or 0),
            float(best.confidence),
        ):
            best = hit
    return best


def evaluate_analysis_reuse(
    *,
    config: Optional[Dict] = None,
    symbol: str = "BNBUSDT",
    indicators: Optional[Dict[str, Any]] = None,
    market_regime: Any = None,
    learner=None,
    learning_db_path: Optional[str] = None,
) -> Optional[ReuseHit]:
    """若当前局面已学过且可复用，返回 ReuseHit；否则 None。

    策略：
    - 默认禁止 WAIT skip-LLM（allow_wait_reuse_skip_llm=false）
    - 可复用已验证盈利的 LONG/SHORT 真经验
    - 距上次完整 LLM 过久或连续复用超限则强制刷新
    """
    if not reuse_enabled(config):
        return None

    cfg = _mem_cfg(config)
    eff = effective_reuse_actions(config)
    cfg = {**cfg, "_effective_reuse_actions": list(eff)}
    ind = indicators or {}
    key = situation_key(ind, market_regime, symbol=symbol)
    vec = situation_vector(ind, market_regime)

    db_path = learning_db_path or _resolve_learning_db(config, learner)

    # 过期刷新：长时间没请教过 AI，即使局面相似也重跑
    # 若回看窗口内全是复用记录（hours=None），视为已过期，强制走 LLM，避免永久卡在 WAIT 复用
    stale_h = float(cfg.get("reuse_stale_refresh_hours", 0) or 0)
    if stale_h > 0 and db_path and Path(db_path).is_file():
        hours = _hours_since_last_full_llm(db_path, symbol=symbol)
        if hours is None or hours >= stale_h:
            logger.info(
                "analysis reuse SKIP (stale refresh): %s since last full LLM (threshold=%.1fh)",
                "no full LLM in lookback" if hours is None else f"{hours:.1f}h",
                stale_h,
            )
            return None

    hits: List[ReuseHit] = []

    if db_path and Path(db_path).is_file():
        hit = _lookup_analysis_reuse(
            db_path,
            symbol=symbol,
            key=key,
            vec=vec,
            regime=market_regime,
            cfg=cfg,
        )
        if hit:
            hits.append(hit)

    card_hit = _lookup_card_reuse(
        learner,
        symbol=symbol,
        key=key,
        indicators=ind,
        regime=market_regime,
        cfg=cfg,
    )
    if card_hit:
        hits.append(card_hit)

    if not hits:
        return None

    hits.sort(
        key=lambda h: (
            1 if h.source == "knowledge_card"
            and int((h.meta or {}).get("times_validated") or 0) >= 5 else 0,
            h.similarity,
            h.confidence,
        ),
        reverse=True,
    )
    best = hits[0]
    logger.info("analysis reuse HIT: %s", best.reason)
    return best


def lookup_execution_template(
    *,
    config: Optional[Dict] = None,
    symbol: str = "BNBUSDT",
    action: str = "LONG",
    indicators: Optional[Dict[str, Any]] = None,
    market_regime: Any = None,
    learner=None,
) -> Optional[Dict[str, Any]]:
    """高胜率指纹 → 执行参数模板（不跳过 LLM，仅覆盖 SL/TP/仓位比例）。"""
    mem = _mem_cfg(config)
    tpl_cfg = mem.get("execution_reuse") or (config or {}).get("execution_reuse") or {}
    if tpl_cfg.get("enabled", True) is False:
        return None

    action_u = _normalize_action(action)
    if action_u not in ("LONG", "SHORT"):
        return None

    ind = indicators or {}
    key = situation_key(ind, market_regime, symbol=symbol)
    vec = situation_vector(ind, market_regime)
    min_sim = float(tpl_cfg.get("min_similarity", 0.88) or 0.88)
    min_wins = int(tpl_cfg.get("min_wins", 2) or 2)
    lookback = int(tpl_cfg.get("lookback", 80) or 80)

    db_path = _resolve_learning_db(config, learner)
    if not db_path or not Path(db_path).is_file():
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=8)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, indicators, market_regime, trading_action, actual_result,
                   trade_advice, current_price
            FROM analysis_records
            WHERE symbol=?
              AND UPPER(COALESCE(trading_action,'')) = ?
              AND UPPER(COALESCE(actual_result,'')) = 'WIN'
            ORDER BY id DESC
            LIMIT ?
            """,
            (symbol, action_u, lookback),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("lookup_execution_template: %s", e)
        return None

    matched: List[Dict[str, Any]] = []
    for r in rows or []:
        hist_ind = _parse_indicators(r["indicators"])
        try:
            mr_raw = r["market_regime"]
            hist_mr = json.loads(mr_raw) if isinstance(mr_raw, str) and mr_raw else mr_raw
        except Exception:
            hist_mr = {}
        hist_key = situation_key(hist_ind, hist_mr, symbol=symbol)
        hist_vec = situation_vector(hist_ind, hist_mr)
        sim = 1.0 if hist_key == key else cosine_similarity(vec, hist_vec)
        if sim < min_sim:
            continue
        advice = {}
        try:
            raw_ta = r["trade_advice"]
            advice = json.loads(raw_ta) if isinstance(raw_ta, str) and raw_ta else (raw_ta or {})
        except Exception:
            advice = {}
        if not isinstance(advice, dict):
            continue
        prices = advice.get("prices") if isinstance(advice.get("prices"), dict) else {}
        pos = advice.get("position") if isinstance(advice.get("position"), dict) else {}
        entry = float(prices.get("entry_mid") or prices.get("entry") or r["current_price"] or 0)
        atr = float(hist_ind.get("ATR") or 0)
        if entry <= 0:
            continue
        sl = float(prices.get("stop_loss") or 0)
        tp = float(prices.get("tp2") or prices.get("tp1") or 0)
        sl_mult = abs(entry - sl) / atr if atr > 0 and sl > 0 else None
        tp_mult = abs(tp - entry) / atr if atr > 0 and tp > 0 else None
        matched.append({
            "sim": sim,
            "sl_atr_mult": sl_mult,
            "tp_atr_mult": tp_mult,
            "size_scale": 1.0,
            "usdt": float(pos.get("usdt_amount") or 0),
            "record_id": int(r["id"] or 0),
        })

    if len(matched) < min_wins:
        return None

    # 取相似度最高的若干笔平均
    matched.sort(key=lambda x: x["sim"], reverse=True)
    top = matched[: max(min_wins, 3)]
    sls = [x["sl_atr_mult"] for x in top if x.get("sl_atr_mult")]
    tps = [x["tp_atr_mult"] for x in top if x.get("tp_atr_mult")]
    if not sls and not tps:
        return None

    template = {
        "situation_key": key,
        "action": action_u,
        "similarity": round(float(top[0]["sim"]), 4),
        "samples": len(top),
        "sl_atr_mult": round(sum(sls) / len(sls), 3) if sls else None,
        "tp_atr_mult": round(sum(tps) / len(tps), 3) if tps else None,
        "size_scale": float(tpl_cfg.get("size_scale", 1.0) or 1.0),
        "source_ids": [x["record_id"] for x in top],
        "reason": (
            f"执行参数复用: 局面指纹命中 {len(top)} 笔 WIN "
            f"(sim≥{min_sim:.0%})，覆盖 SL/TP ATR 倍数"
        ),
    }
    logger.info("execution template HIT: %s", template["reason"])
    return template


def apply_execution_template(
    advice: Dict[str, Any],
    template: Optional[Dict[str, Any]],
    *,
    indicators: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将模板的 ATR 倍数应用到 advice.prices / position（保留 LLM 方向）。"""
    if not template or not isinstance(advice, dict):
        return advice
    action = str(advice.get("action") or "").upper()
    if action not in ("LONG", "SHORT"):
        return advice
    if str(template.get("action") or "").upper() != action:
        return advice

    out = dict(advice)
    prices = dict(out.get("prices") or {})
    atr = float((indicators or {}).get("ATR") or 0)
    entry = float(prices.get("entry_mid") or prices.get("entry") or 0)
    if atr <= 0 or entry <= 0:
        out["execution_template"] = template
        return out

    old_sl = float(prices.get("stop_loss") or 0)
    old_qty = 0.0
    try:
        old_qty = float((out.get("position") or {}).get("quantity") or 0)
    except (TypeError, ValueError):
        old_qty = 0.0

    sl_m = template.get("sl_atr_mult")
    tp_m = template.get("tp_atr_mult")
    if sl_m and float(sl_m) > 0:
        dist = float(sl_m) * atr
        prices["stop_loss"] = round(entry - dist if action == "LONG" else entry + dist, 4)
    if tp_m and float(tp_m) > 0:
        dist = float(tp_m) * atr
        tp = round(entry + dist if action == "LONG" else entry - dist, 4)
        prices["tp1"] = tp
        prices["tp2"] = tp
    out["prices"] = prices

    # 模板改价后重算粗 RR，供后续净 RR / 报告使用
    try:
        entry = float(prices.get("entry_mid") or prices.get("entry") or 0)
        sl = float(prices.get("stop_loss") or 0)
        tp = float(prices.get("tp2") or prices.get("tp1") or 0)
        if entry > 0 and sl > 0 and tp > 0:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            if risk > 1e-12:
                out["risk_reward_ratio"] = round(reward / risk, 3)
    except Exception:
        pass

    # 改 SL 后按原美元风险重算仓位，避免风险偏离
    if isinstance(out.get("position"), dict):
        pos = dict(out["position"])
        try:
            entry = float(prices.get("entry_mid") or prices.get("entry") or 0)
            sl = float(prices.get("stop_loss") or 0)
            risk_amt = float(pos.get("risk_amount") or 0)
            if risk_amt <= 0 and old_sl > 0 and entry > 0 and old_qty > 0:
                risk_amt = old_qty * abs(entry - old_sl)
            per_unit = abs(entry - sl) if entry > 0 and sl > 0 else 0.0
            if risk_amt > 0 and per_unit > 1e-12:
                leverage = int(pos.get("leverage_suggest") or 1) or 1
                qty = risk_amt / per_unit
                usdt = qty * entry
                pos["quantity"] = round(qty, 6)
                pos["usdt_amount"] = round(usdt, 4)
                pos["risk_amount"] = round(risk_amt, 2)
                pos["margin_required"] = round(usdt / max(1, leverage), 2)
                out["position"] = pos
        except (TypeError, ValueError):
            pass

    scale = float(template.get("size_scale") or 1.0)
    if scale != 1.0 and isinstance(out.get("position"), dict):
        pos = dict(out["position"])
        for k in ("quantity", "usdt_amount", "margin_required", "risk_amount"):
            if pos.get(k) is not None:
                try:
                    pos[k] = round(float(pos[k]) * scale, 6)
                except (TypeError, ValueError):
                    pass
        out["position"] = pos

    out["execution_template"] = template
    out["execution_params_reused"] = True
    reasons = list(out.get("gate_reasons") or [])
    msg = str(template.get("reason") or "执行参数复用")
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    if out.get("report_text"):
        out["report_text"] += f"\n\n📐 {msg}"
    return out


def _hours_since_last_full_llm(db_path: str, *, symbol: str = "BNBUSDT") -> Optional[float]:
    """距最近一次非复用主分析的小时数。

    返回 None = 回看窗口内找不到真·LLM（通常意味着长期卡在复用）。
    """
    try:
        conn = sqlite3.connect(db_path, timeout=8)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT timestamp, ai_analysis, trading_action
            FROM analysis_records
            WHERE symbol=?
            ORDER BY id DESC
            LIMIT 200
            """,
            (symbol,),
        ).fetchall()
        conn.close()
    except Exception:
        return None

    for r in row or []:
        txt = str(r["ai_analysis"] or "")
        # 复用合成记录不算「真·LLM」
        if "知识复用" in txt or "局面复用" in txt or "知识卡片复用" in txt:
            continue
        if "主分析失败" in txt or "Payment Required" in txt:
            continue
        # provider 标记
        if '"_provider": "knowledge_reuse"' in txt or '"_reused": true' in txt:
            continue
        ts = str(r["timestamp"] or "")
        if not ts:
            continue
        try:
            raw = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw[:26] if "T" in raw else raw)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
        except Exception:
            continue
    return None


def reinforce_on_reuse(
    learner,
    hit: ReuseHit,
    *,
    symbol: str = "BNBUSDT",
    indicators: Optional[Dict[str, Any]] = None,
    market_regime: Any = None,
) -> Optional[int]:
    """复用命中后记录召回（times_recalled++），不抬 times_validated。"""
    if learner is None or hit is None or not hit.reuse:
        return None
    mem = getattr(learner, "capability_memory", None)
    if mem is None or not getattr(mem, "enabled", True):
        return None

    ind = indicators or {}
    key = hit.situation_key or situation_key(ind, market_regime, symbol=symbol)
    regime_name = ""
    if isinstance(market_regime, dict):
        regime_name = str(market_regime.get("regime") or market_regime.get("label") or "")
    else:
        regime_name = str(market_regime or "")
    action = _normalize_action(hit.action)
    signal = _signal_from_action(action)
    conf = max(0.35, float(hit.confidence or 0.4))

    card = {
        "category": "market_review",
        "title": f"{regime_name or 'UNKNOWN'} · {signal} 局面认知",
        "trigger_condition": (
            f"situation_key={key} | 市场状态={regime_name or 'UNKNOWN'} | "
            f"复用结论={action}"
        )[:500],
        "action_rule": (
            f"相同局面可参考历史 {action}；开仓方向仍须 LLM；"
            f"来源={hit.source}:{hit.source_id or '-'}"
        )[:500],
        "lesson": (
            f"局面键={key} 已召回：{hit.reason[:200]}；"
            f"召回≠验证，待交易结果再强化"
        )[:1000],
        "confidence": min(0.55, conf),
        "situation_key": key,
        "tags": [
            regime_name or "UNKNOWN",
            signal,
            action,
            "analysis_snapshot",
            "knowledge_reuse",
            key,
        ],
    }
    try:
        card_id = mem.save_knowledge_card(
            card,
            source="reuse",
            symbol=symbol,
            record_id=int(hit.source_id) if hit.source == "analysis_record" and hit.source_id else None,
        )
        if card_id:
            logger.info(
                "reuse recall: card #%s key=%s action=%s",
                card_id, key, action,
            )
        return card_id
    except Exception as e:
        logger.debug("reinforce_on_reuse failed: %s", e)
        return None


def _count_consecutive_reuse(db_path: str, *, symbol: str = "BNBUSDT", limit: int = 20) -> int:
    """从最新分析往回数连续「知识复用」条数。"""
    try:
        conn = sqlite3.connect(db_path, timeout=8)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ai_analysis FROM analysis_records
            WHERE symbol=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (symbol, max(1, limit)),
        ).fetchall()
        conn.close()
    except Exception:
        return 0
    n = 0
    for r in rows or []:
        txt = str(r["ai_analysis"] or "")
        if "知识复用" in txt or "局面复用" in txt or "知识卡片复用" in txt:
            n += 1
            continue
        break
    return n


def should_force_full_ai(
    config: Optional[Dict] = None,
    *,
    symbol: str = "BNBUSDT",
    learning_db_path: Optional[str] = None,
    learner=None,
) -> Dict[str, Any]:
    """是否强制真·LLM（并建议三家压力队）。"""
    cfg = _mem_cfg(config)
    reasons: List[str] = []
    force = False
    db_path = learning_db_path or _resolve_learning_db(config, learner)
    stale_h = float(cfg.get("reuse_stale_refresh_hours", 0) or 0)
    if stale_h > 0 and db_path and Path(db_path).is_file():
        hours = _hours_since_last_full_llm(db_path, symbol=symbol)
        if hours is None or hours >= stale_h:
            force = True
            reasons.append(
                "stale_llm" if hours is None else f"stale_{hours:.1f}h"
            )
    max_reuse = int(cfg.get("max_consecutive_reuse", 0) or 0)
    if max_reuse > 0 and db_path and Path(db_path).is_file():
        consec = _count_consecutive_reuse(db_path, symbol=symbol, limit=max_reuse + 5)
        if consec >= max_reuse:
            force = True
            reasons.append(f"consec_reuse_{consec}")
    return {
        "force": force,
        "force_stress": bool(force and cfg.get("force_full_ai_on_stale", True)),
        "reasons": reasons,
    }


def run_market_analyses_with_reuse(
    config: Optional[dict],
    df,
    indicators: Dict[str, Any],
    *,
    symbol: str = "BNBUSDT",
    market_regime: Any = None,
    learner=None,
    force_llm: bool = False,
    bnb_factors: Any = None,
    news_summary: Any = None,
    atr_ratio: Optional[float] = None,
    **analyze_kwargs: Any,
) -> Dict[str, Any]:
    """先尝试知识复用，未命中再跑 LLM 全量分析（含平静期路由降本）。"""
    from bnb_quant_tool.llm_provider import run_market_analyses

    force_meta = should_force_full_ai(config, symbol=symbol, learner=learner)
    if force_meta.get("force"):
        force_llm = True
        logger.info("force full AI: %s", ", ".join(force_meta.get("reasons") or []))

    if not force_llm:
        hit = evaluate_analysis_reuse(
            config=config,
            symbol=symbol,
            indicators=indicators,
            market_regime=market_regime,
            learner=learner,
        )
        if hit and hit.reuse:
            # 强化统一由 record_analysis → _after_analysis_growth 完成
            return hit.to_ai_bundle()

    cfg = config or {}
    route_meta: Dict[str, Any] = {}
    try:
        from bnb_quant_tool.llm_router import apply_route_to_config

        mr = market_regime if isinstance(market_regime, dict) else {}
        ratio = atr_ratio
        if ratio is None:
            ratio = float(mr.get("atr_ratio") or 1.0)
        cfg = apply_route_to_config(
            cfg,
            atr_ratio=float(ratio or 1.0),
            market_regime=mr,
            bnb_factors=bnb_factors if isinstance(bnb_factors, dict) else None,
            news_summary=news_summary if isinstance(news_summary, dict) else None,
            force_stress=bool(force_meta.get("force_stress")),
        )
        route_meta = dict(cfg.get("_llm_route") or {})
        if force_meta.get("reasons"):
            route_meta["force_reasons"] = list(force_meta["reasons"])
    except Exception as e:
        logger.debug("llm route: %s", e)

    bundle = run_market_analyses(
        cfg,
        df,
        indicators,
        learner=learner,
        multi_timeframe=analyze_kwargs.pop("multi_timeframe", None),
        **analyze_kwargs,
    )
    bundle["reused"] = False
    if route_meta:
        bundle["llm_route"] = route_meta
        primary = bundle.get("primary")
        if isinstance(primary, dict):
            primary["_llm_route"] = route_meta
    return bundle


def bind_council_votes_to_record(
    config: Optional[Dict[str, Any]],
    record_id: Optional[int],
    *,
    project_root: Optional[str] = None,
    within_minutes: int = 5,
    max_votes: int = 12,
    created_after_iso: Optional[str] = None,
    skip: bool = False,
) -> int:
    """分析入库后把本轮议会投票挂到 record_id，供平仓回写胜负。

    防串票：短时间窗 + 批次上限；知识复用跳过议会时勿调用（skip=True）。
    """
    if not record_id or skip:
        return 0
    try:
        from pathlib import Path
        from bnb_quant_tool.agents.trader_memory import TraderMemoryStore

        tc = (config or {}).get("trader_council") or {}
        db_path = tc.get("memory_db") or "data/trader_memory.db"
        if not Path(db_path).is_absolute():
            if project_root:
                db_path = str(Path(project_root) / db_path)
            else:
                try:
                    from bnb_quant_tool.data_localization import get_localization_manager
                    db_path = str(Path(get_localization_manager().workspace) / db_path)
                except Exception:
                    pass
        tm = TraderMemoryStore(db_path)
        n = tm.attach_record_to_recent_votes(
            int(record_id),
            within_minutes=within_minutes,
            max_votes=max_votes,
            created_after_iso=created_after_iso,
        )
        if n:
            logger.info("bound %s trader votes → analysis #%s", n, record_id)
        return int(n or 0)
    except Exception as e:
        logger.debug("bind_council_votes_to_record: %s", e)
        return 0


def mark_advice_reused(trade_advice: Dict[str, Any], ai_analysis: Optional[Dict], ai_bundle: Optional[Dict] = None) -> Dict[str, Any]:
    """给 advice 打复用标记，供门控跳过议会。"""
    advice = trade_advice if isinstance(trade_advice, dict) else {}
    ai = ai_analysis if isinstance(ai_analysis, dict) else {}
    bundle = ai_bundle if isinstance(ai_bundle, dict) else {}
    if ai.get("_reused") or bundle.get("reused") or ai.get("_provider") == "knowledge_reuse":
        advice["_reused"] = True
        advice["_reuse_reason"] = (
            ai.get("_reuse_reason") or bundle.get("note") or "知识复用"
        )
    return advice
