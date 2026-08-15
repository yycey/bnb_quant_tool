"""
机构级方向信念引擎 — 将大机构策略逻辑融合为可量化 directional conviction。

参考框架：
- Citadel / AQR: 多因子动量共识
- Renaissance: 均值回归 / 统计套利（震荡 regime）
- Two Sigma: 多源信号 ensemble + 冲突检测
- Jump: 高波动降方向暴露
- Bridgewater: 波动率调整信念强度

输出供 TradeAdvisor、DeepSeek Prompt、GUI 决策驾驶舱使用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# 策略族 → institutional_strategies 注册 key
TREND_FOLLOWING = frozenset({
    "ema_crossover", "sma_crossover", "turtle_trading", "citadel_momentum",
    "macd_crossover", "aqr_value_momentum",
    "golden_death_cross", "adx_trend", "breakout_volume", "volume_price_obv",
})
MEAN_REVERSION = frozenset({
    "bollinger_bands", "rsi_extreme", "fibonacci_retracement",
    "renissance_stat_arb", "jump_market_making",
    "range_sr_swing", "stochastic_momentum",
})
ML_FACTOR = frozenset({"two_sigma_ml", "bridgewater_risk_parity"})

REGIME_TREND = frozenset({"TRENDING", "EUPHORIA"})
REGIME_RANGE = frozenset({"RANGING", "LOW_VOLATILITY"})
REGIME_DANGER = frozenset({"PANIC", "HIGH_VOLATILITY"})


def compute_institutional_conviction(
    *,
    inst_results: Optional[Dict[str, Any]] = None,
    market_regime: Optional[Dict[str, Any]] = None,
    indicators: Optional[Dict[str, Any]] = None,
    sentiment: Optional[Dict[str, Any]] = None,
    onchain: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
    bnb_factors: Optional[Dict[str, Any]] = None,
    mtf: Optional[Dict[str, Any]] = None,
    learning_insights: Optional[Dict[str, Any]] = None,
    btc_lead: Optional[Dict[str, Any]] = None,
    orderflow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """计算机构级方向信念分（-1 ~ +1）及结构化分解。"""
    inst_results = inst_results or {}
    market_regime = market_regime or {}
    indicators = indicators or {}
    factors: List[Dict[str, Any]] = []
    score = 0.0
    weight_sum = 0.0

    regime = str(market_regime.get("regime") or "UNKNOWN").upper()

    # ── 1. 机构策略族共识（按 regime 适配加权）──
    family_score, family_detail = _strategy_family_consensus(
        inst_results, regime
    )
    if family_detail:
        w = 35.0
        score += family_score * w
        weight_sum += w
        factors.append({
            "name": "机构策略族共识",
            "family": family_detail.get("dominant_family"),
            "score": round(family_score, 3),
            "weight": w,
            "detail": family_detail.get("text", ""),
            "regime_aligned": family_detail.get("regime_aligned", True),
        })

    # ── 2. 多周期结构（Two Sigma 多 horizon）──
    mtf_score, mtf_text = _mtf_score(mtf)
    if mtf_score is not None:
        w = 20.0
        score += mtf_score * w
        weight_sum += w
        factors.append({
            "name": "多周期结构",
            "score": round(mtf_score, 3),
            "weight": w,
            "detail": mtf_text,
        })

    # ── 3. 衍生品结构 — Funding 拥挤（机构 delta-neutral 核心，权重提升）──
    funding_score, funding_text = _funding_crowding_score(bnb_factors, sentiment)
    if funding_score is not None:
        w = 22.0
        score += funding_score * w
        weight_sum += w
        factors.append({
            "name": "Funding拥挤度",
            "score": round(funding_score, 3),
            "weight": w,
            "detail": funding_text,
        })

    # ── 3b. BNB/BTC 相对强度（Renaissance 式 stat arb）──
    rs_score, rs_text = _bnb_btc_relative_score(bnb_factors)
    if rs_score is not None:
        w = 12.0
        score += rs_score * w
        weight_sum += w
        factors.append({
            "name": "BNB/BTC相对强度",
            "score": round(rs_score, 3),
            "weight": w,
            "detail": rs_text,
        })

    # ── 3c. BTC 领先 / BNB 滞后（Two Sigma 跨资产 lead-lag）──
    from bnb_quant_tool.btc_lead_indicator import btc_lead_conviction_score
    lead_src = btc_lead or (bnb_factors or {}).get("btc_lead")
    lead_score, lead_text = btc_lead_conviction_score(lead_src)
    if lead_score is not None:
        w = 10.0
        score += lead_score * w
        weight_sum += w
        factors.append({
            "name": "BTC领先指标",
            "score": round(lead_score, 3),
            "weight": w,
            "detail": lead_text,
        })

    # ── 3d. 订单流微观结构（AICoin / Freqtrade 风格软票）──
    from bnb_quant_tool.orderflow_signal import orderflow_conviction_score
    of_score, of_text = orderflow_conviction_score(orderflow)
    if of_score is not None:
        w = 12.0
        score += of_score * w
        weight_sum += w
        factors.append({
            "name": "订单流微观结构",
            "score": round(of_score, 3),
            "weight": w,
            "detail": of_text,
        })

    # ── 4. 情绪 + 链上 + 宏观（alt data 层）──
    alt_score, alt_parts = _alt_data_score(sentiment, onchain, macro, bnb_factors)
    if alt_score is not None:
        w = 15.0
        score += alt_score * w
        weight_sum += w
        factors.append({
            "name": "情绪/链上/宏观",
            "score": round(alt_score, 3),
            "weight": w,
            "detail": "; ".join(alt_parts),
        })

    # ── 5. 技术指标趋势（基础 price action）──
    ta_score, ta_text = _technical_bias(indicators)
    w = 10.0
    score += ta_score * w
    weight_sum += w
    factors.append({
        "name": "技术指标偏向",
        "score": round(ta_score, 3),
        "weight": w,
        "detail": ta_text,
    })

    # ── 6. 学习系统偏置（paper + 归因）──
    learn_score, learn_text = _learning_bias(learning_insights)
    if learn_score is not None:
        w = 5.0
        score += learn_score * w
        weight_sum += w
        factors.append({
            "name": "学习反馈偏置",
            "score": round(learn_score, 3),
            "weight": w,
            "detail": learn_text,
        })

    conviction = score / weight_sum if weight_sum > 0 else 0.0

    # Regime 危险态：压缩信念（Bridgewater 波动率缩放思想）
    if regime in REGIME_DANGER:
        conviction *= 0.45
        factors.append({
            "name": "Regime 风险缩放",
            "score": 0.0,
            "weight": 0,
            "detail": f"{regime} 状态 — 方向信念 ×0.45，优先风控",
        })

    direction, strength = _conviction_to_direction(conviction, regime)
    conflicts = _detect_conflicts(factors, inst_results, regime)

    if market_regime and market_regime.get("regime_conflicts"):
        conflicts.extend(market_regime["regime_conflicts"])
    if market_regime and market_regime.get("regime_votes"):
        low_conf = float(market_regime.get("fusion_confidence") or 1)
        if low_conf < 0.5:
            conflicts.append(f"Regime 多信号分歧 (置信 {low_conf:.0%}) — 优先 WAIT")

    return {
        "conviction": round(conviction, 4),
        "direction": direction,
        "strength": round(strength, 4),
        "regime": regime,
        "regime_bucket": _regime_bucket(regime),
        "factors": factors,
        "conflicts": conflicts,
        "strategy_family": family_detail if family_detail else {},
        "summary": _format_summary(direction, conviction, strength, regime, conflicts),
        "institutional_thesis": _build_thesis(direction, conviction, regime, factors, conflicts),
    }


def _strategy_family_consensus(
    inst_results: Dict[str, Any],
    regime: str,
) -> Tuple[float, Dict[str, Any]]:
    details = inst_results.get("strategy_details") or {}
    if not details:
        buy = int(inst_results.get("buy_signals") or 0)
        sell = int(inst_results.get("sell_signals") or 0)
        total = buy + sell + int(inst_results.get("hold_signals") or 0)
        if total == 0:
            return 0.0, {}
        raw = (buy - sell) / max(total, 1)
        return max(-1.0, min(1.0, raw)), {
            "text": f"投票 BUY={buy} SELL={sell}",
            "dominant_family": "mixed",
            "regime_aligned": True,
        }

    trend_buy = trend_sell = trend_w = 0.0
    mr_buy = mr_sell = mr_w = 0.0
    ml_buy = ml_sell = ml_w = 0.0

    for key, res in details.items():
        if not isinstance(res, dict) or res.get("signal") == "ERROR":
            continue
        sig = res.get("signal", "HOLD")
        conf = float(res.get("confidence") or 0.5)
        vw = float(res.get("vote_weight") or 1.0)

        def _accum(buy, sell, w, s, c, vw):
            if s == "BUY":
                return buy + c * vw, sell, w + vw
            if s == "SELL":
                return buy, sell + c * vw, w + vw
            return buy, sell, w + vw

        if key in TREND_FOLLOWING:
            trend_buy, trend_sell, trend_w = _accum(trend_buy, trend_sell, trend_w, sig, conf, vw)
        elif key in MEAN_REVERSION:
            mr_buy, mr_sell, mr_w = _accum(mr_buy, mr_sell, mr_w, sig, conf, vw)
        elif key in ML_FACTOR:
            ml_buy, ml_sell, ml_w = _accum(ml_buy, ml_sell, ml_w, sig, conf, vw)

    def _family_score(buy, sell, w):
        if w <= 0:
            return 0.0
        return max(-1.0, min(1.0, (buy - sell) / w))

    trend_s = _family_score(trend_buy, trend_sell, trend_w)
    mr_s = _family_score(mr_buy, mr_sell, mr_w)
    ml_s = _family_score(ml_buy, ml_sell, ml_w)

    # Regime 路由：机构核心 — 趋势市信动量，震荡市信均值回归
    if regime in REGIME_TREND:
        primary, secondary = trend_s, mr_s
        dominant = "trend_following"
        aligned = abs(trend_s) >= abs(mr_s) * 0.8
        blend = 0.72 * trend_s + 0.28 * ml_s
    elif regime in REGIME_RANGE:
        primary, secondary = mr_s, trend_s
        dominant = "mean_reversion"
        aligned = abs(mr_s) >= abs(trend_s) * 0.8
        blend = 0.68 * mr_s + 0.32 * ml_s
    else:
        dominant = "mixed"
        aligned = True
        blend = 0.4 * trend_s + 0.35 * mr_s + 0.25 * ml_s

    text = (
        f"趋势族 {trend_s:+.2f} | 均值回归族 {mr_s:+.2f} | ML/风控族 {ml_s:+.2f} "
        f"→ Regime={regime} 主用{dominant}"
    )
    return blend, {
        "text": text,
        "dominant_family": dominant,
        "regime_aligned": aligned,
        "trend_score": round(trend_s, 3),
        "mean_reversion_score": round(mr_s, 3),
        "ml_score": round(ml_s, 3),
    }


def _mtf_score(mtf: Optional[Dict]) -> Tuple[Optional[float], str]:
    if not mtf:
        return None, ""
    action = str(mtf.get("recommended_action") or mtf.get("confluence") or "").upper()
    ws = float(mtf.get("weighted_score") or 0)
    if "LONG" in action or "BUY" in action:
        return min(1.0, 0.5 + abs(ws) * 0.1), f"MTF 偏多 {action} (得分 {ws:.2f})"
    if "SHORT" in action or "SELL" in action:
        return max(-1.0, -0.5 - abs(ws) * 0.1), f"MTF 偏空 {action} (得分 {ws:.2f})"
    return 0.0, f"MTF 中性 {action}"


def _funding_crowding_score(
    bnb_factors: Optional[Dict],
    sentiment: Optional[Dict],
) -> Tuple[Optional[float], str]:
    rate = None
    text = ""
    bnb = bnb_factors or {}
    rs = bnb.get("risk_sentry") or {}
    fr = rs.get("funding_extreme") or {}
    if fr.get("rate") is not None:
        rate = float(fr["rate"])
        text = fr.get("interpretation") or f"Funding {rate:+.4%}"
    elif sentiment:
        fr2 = sentiment.get("funding_rate") or {}
        if isinstance(fr2, dict) and fr2.get("rate") is not None:
            rate = float(fr2["rate"])
            text = f"Funding {rate:+.4%} ({fr2.get('level', '?')})"

    if rate is None:
        return None, ""

    # 机构逻辑：极端正 funding = 多头拥挤 → 降多/偏空；极端负 = 空头拥挤 → 偏多
    if rate >= 0.001:
        return -0.7, f"{text} — 多头拥挤，机构 delta-neutral 会压多"
    if rate >= 0.0005:
        return -0.35, f"{text} — 多头偏拥挤"
    if rate <= -0.001:
        return 0.6, f"{text} — 空头拥挤，反弹概率升"
    if rate <= -0.0005:
        return 0.3, f"{text} — 空头偏拥挤"
    return 0.0, f"{text} — 中性"


def _bnb_btc_relative_score(bnb_factors: Optional[Dict]) -> Tuple[Optional[float], str]:
    """BNB 相对 BTC 强弱 — 弱势时压多，强势时略偏多。"""
    if not bnb_factors:
        return None, ""
    rs = (bnb_factors.get("risk_sentry") or {}).get("bnb_btc_weakness")
    if not rs:
        rs = bnb_factors.get("bnb_btc_weakness")
    if not rs or not isinstance(rs, dict):
        return None, ""

    weak = bool(rs.get("weak"))
    ch = float(rs.get("ratio_change_pct") if rs.get("ratio_change_pct") is not None else rs.get("ratio_change") or 0)

    text = rs.get("interpretation") or f"BNB/BTC 变化 {ch:+.2f}%"
    if weak:
        return -0.65, text
    if ch >= 1.5:
        return 0.45, f"{text} — BNB 跑赢 BTC"
    if ch <= -1.5:
        return -0.35, f"{text} — BNB 跑输 BTC"
    return 0.0, text


def _alt_data_score(
    sentiment: Optional[Dict],
    onchain: Optional[Dict],
    macro: Optional[Dict],
    bnb_factors: Optional[Dict],
) -> Tuple[Optional[float], List[str]]:
    parts: List[str] = []
    scores: List[float] = []

    if sentiment:
        pol = str(sentiment.get("polarity") or sentiment.get("interpretation") or "").lower()
        if "bull" in pol or "贪婪" in pol:
            scores.append(0.4)
            parts.append("情绪偏多")
        elif "bear" in pol or "恐惧" in pol:
            scores.append(-0.4)
            parts.append("情绪偏空")

    if onchain:
        pol = str(onchain.get("polarity") or onchain.get("signal") or "").lower()
        if "accumulation" in pol or "bull" in pol:
            scores.append(0.35)
            parts.append("链上积累")
        elif "distribution" in pol or "bear" in pol:
            scores.append(-0.35)
            parts.append("链上派发")

    if macro:
        pol = str(macro.get("polarity") or macro.get("risk_tone") or "").lower()
        if "risk_on" in pol or "bull" in pol:
            scores.append(0.25)
            parts.append("宏观 risk-on")
        elif "risk_off" in pol:
            scores.append(-0.3)
            parts.append("宏观 risk-off")

    if bnb_factors:
        score_val = bnb_factors.get("composite_score")
        if score_val is not None:
            s = max(-1.0, min(1.0, float(score_val) / 50.0))
            scores.append(s)
            parts.append(f"BNB专属因子 {score_val:+.0f}")

    if not scores:
        return None, []
    return sum(scores) / len(scores), parts


def _technical_bias(indicators: Dict) -> Tuple[float, str]:
    rsi = float(indicators.get("RSI") or 50)
    macd = float(indicators.get("MACD") or 0)
    bb = float(indicators.get("BB_Position") or 50)

    score = 0.0
    if rsi > 60:
        score += 0.3
    elif rsi < 40:
        score -= 0.3
    if macd > 0:
        score += 0.25
    elif macd < 0:
        score -= 0.25
    if bb > 70:
        score += 0.15
    elif bb < 30:
        score -= 0.15

    score = max(-1.0, min(1.0, score))
    return score, f"RSI={rsi:.0f} MACD={macd:+.2f} BB位={bb:.0f}"


def _learning_bias(insights: Optional[Dict]) -> Tuple[Optional[float], str]:
    if not insights:
        return None, ""
    paper = insights.get("paper_trading") or {}
    if paper.get("closed_trades", 0) >= 5:
        wr = float(paper.get("win_rate") or 0.5)
        streak = int(paper.get("consecutive_losses") or 0)
        if streak >= 5:
            return -0.5, f"连亏 {streak} 笔 — 机构纪律：降暴露"
        if streak >= 3:
            return -0.25, f"连亏 {streak} 笔 — 适度降暴露"
        if wr < 0.4:
            return -0.3, f"模拟盘胜率 {wr:.0%} — 提高门槛"
        if wr > 0.6:
            return 0.15, f"模拟盘胜率 {wr:.0%} — 策略有效"
    return None, ""


def _conviction_to_direction(conviction: float, regime: str) -> Tuple[str, float]:
    threshold = 0.12
    if regime in REGIME_DANGER:
        threshold = 0.22
    strength = abs(conviction)
    if conviction >= threshold:
        return "LONG", strength
    if conviction <= -threshold:
        return "SHORT", strength
    return "WAIT", strength


def _detect_conflicts(
    factors: List[Dict],
    inst_results: Dict,
    regime: str,
) -> List[str]:
    conflicts: List[str] = []
    scored = [(f["name"], f.get("score", 0)) for f in factors if f.get("weight", 0) > 0]
    if len(scored) >= 2:
        bulls = [n for n, s in scored if s > 0.25]
        bears = [n for n, s in scored if s < -0.25]
        if bulls and bears:
            conflicts.append(f"因子冲突: {', '.join(bulls)} 偏多 vs {', '.join(bears)} 偏空")

    family = inst_results.get("_conviction_family") or {}
    if family.get("regime_aligned") is False:
        conflicts.append(
            f"Regime={regime} 但主导策略族与状态不匹配 — 机构规则：降权或 WAIT"
        )

    consensus = str(inst_results.get("consensus_signal") or "HOLD").upper()
    if consensus == "HOLD" and inst_results.get("buy_signals", 0) + inst_results.get("sell_signals", 0) > 0:
        buy = int(inst_results.get("buy_signals") or 0)
        sell = int(inst_results.get("sell_signals") or 0)
        if buy > 0 and sell > 0 and abs(buy - sell) <= 2:
            conflicts.append(f"策略票分裂 BUY={buy} SELL={sell} — 无共识")

    return conflicts


def _regime_bucket(regime: str) -> str:
    if regime in REGIME_TREND:
        return "TREND"
    if regime in REGIME_RANGE:
        return "RANGE"
    if regime in REGIME_DANGER:
        return "VOLATILE"
    return "GLOBAL"


def _format_summary(
    direction: str, conviction: float, strength: float, regime: str, conflicts: List[str]
) -> str:
    lines = [
        f"【机构信念】{direction} | 信念分 {conviction:+.3f} | 强度 {strength:.0%} | Regime={regime}",
    ]
    if conflicts:
        lines.append("⚠ 冲突: " + "; ".join(conflicts[:3]))
    else:
        lines.append("✓ 各因子方向基本一致")
    return "\n".join(lines)


def _build_thesis(
    direction: str,
    conviction: float,
    regime: str,
    factors: List[Dict],
    conflicts: List[str],
) -> str:
    """供 AI Prompt 使用的结构化 thesis。"""
    top = sorted(
        [f for f in factors if f.get("weight", 0) > 0],
        key=lambda x: -abs(x.get("score", 0)),
    )[:4]
    support = [f"{f['name']}({f['score']:+.2f})" for f in top if f.get("score", 0) > 0.1]
    oppose = [f"{f['name']}({f['score']:+.2f})" for f in top if f.get("score", 0) < -0.1]

    lines = [
        f"机构方向研判: {direction} (conviction={conviction:+.3f}, regime={regime})",
        f"支撑: {', '.join(support) if support else '弱'}",
        f"压制: {', '.join(oppose) if oppose else '无'}",
    ]
    if conflicts:
        lines.append(f"冲突需 AI 仲裁: {'; '.join(conflicts)}")
    lines.append(
        "规则: Regime 不匹配时优先 WAIT；Funding 极端拥挤时不追多；连亏≥3 不加仓。"
    )
    return "\n".join(lines)


def format_conviction_for_prompt(conviction: Dict[str, Any]) -> str:
    if not conviction:
        return ""
    return (
        f"\n{conviction.get('summary', '')}\n"
        f"{conviction.get('institutional_thesis', '')}\n"
    )
