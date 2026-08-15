"""
加密货币技术分析 Playbook
================================================================
综合自公开教育材料（FinLab 量化框架、IG 五大加密策略、Moomoo 比特币策略），
转为可注入 AI Prompt / 策略路由的结构化知识。

原则（FinLab）：
- 量化 = 可复现规则 + 历史验证，不是把主观感觉写成代码
- 评价看 Sharpe / Sortino / MaxDD / 成本，不只看 CAGR
- WAIT 是合法输出；单一因子常跑输基准，多因子更稳
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# 本轮新增的经典技术分析策略 key（IG / Moomoo）
CLASSIC_TA_STRATEGY_KEYS = frozenset({
    "golden_death_cross",
    "adx_trend",
    "stochastic_momentum",
    "volume_price_obv",
    "range_sr_swing",
    "breakout_volume",
})

# IG + Moomoo 策略 → 本工具模块映射
STRATEGY_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "ma_cross",
        "name": "移动平均交叉 / 黄金死亡交叉",
        "source": "IG",
        "style": "trend",
        "tool_keys": ["sma_crossover", "ema_crossover", "golden_death_cross"],
        "rules": "短均线上穿长均线偏多；下穿偏空。50/200 为经典黄金/死亡交叉。",
        "best_regime": ["TRENDING", "EUPHORIA"],
    },
    {
        "id": "rsi",
        "name": "RSI 超买超卖",
        "source": "IG",
        "style": "mean_reversion",
        "tool_keys": ["rsi_extreme"],
        "rules": "震荡市：RSI 低位做多、高位做空；趋势市慎用，需 ADX 过滤。",
        "best_regime": ["RANGING", "LOW_VOLATILITY"],
    },
    {
        "id": "event_driven",
        "name": "事件驱动",
        "source": "IG/Moomoo",
        "style": "event",
        "tool_keys": ["bnb_event_calendar", "news", "announcement_nlp"],
        "rules": "利好消息后确认再跟；利空先减仓。加密波动大，宜等公告落地。",
        "best_regime": ["NEWS_DRIVEN"],
    },
    {
        "id": "scalping",
        "name": "超短线 / 剥头皮",
        "source": "IG/Moomoo",
        "style": "scalp",
        "tool_keys": ["jump_market_making", "signal_scanner"],
        "rules": "秒~分钟级进出，严格止损；本工具以模拟盘短持仓 + 扫描器近似，不做无风险保证。",
        "best_regime": ["HIGH_VOLATILITY"],
    },
    {
        "id": "dca",
        "name": "平均成本法 DCA",
        "source": "IG",
        "style": "accumulation",
        "tool_keys": ["dca_plan"],
        "rules": "定额定投摊薄波动，适合长周期；非短线方向信号。",
        "best_regime": ["TRENDING", "RANGING", "LOW_VOLATILITY"],
    },
    {
        "id": "trend",
        "name": "趋势交易 (ADX)",
        "source": "Moomoo",
        "style": "trend",
        "tool_keys": ["adx_trend", "citadel_momentum", "turtle_trading"],
        "rules": "多指标确认趋势；ADX 过低时不做趋势单，改区间。",
        "best_regime": ["TRENDING"],
    },
    {
        "id": "swing",
        "name": "波段 / 支撑阻力",
        "source": "Moomoo",
        "style": "swing",
        "tool_keys": ["range_sr_swing", "fibonacci_retracement"],
        "rules": "支撑附近做多、阻力附近做空；必须带止损。",
        "best_regime": ["RANGING"],
    },
    {
        "id": "mean_reversion",
        "name": "均值回归",
        "source": "Moomoo/FinLab",
        "style": "mean_reversion",
        "tool_keys": ["renissance_stat_arb", "bollinger_bands"],
        "rules": "价格偏离均值过远期待回归；强趋势中权重应降低。",
        "best_regime": ["RANGING", "LOW_VOLATILITY"],
    },
    {
        "id": "breakout",
        "name": "突破交易",
        "source": "Moomoo",
        "style": "breakout",
        "tool_keys": ["breakout_volume", "turtle_trading"],
        "rules": "突破关键位需量能确认；假突破用成交量与回测过滤。",
        "best_regime": ["TRENDING", "HIGH_VOLATILITY", "NEWS_DRIVEN"],
    },
    {
        "id": "volume_price",
        "name": "量价分析 OBV",
        "source": "Moomoo",
        "style": "confirmation",
        "tool_keys": ["volume_price_obv"],
        "rules": "价量同向确认趋势；背离提示反转风险。",
        "best_regime": ["TRENDING", "NEWS_DRIVEN"],
    },
    {
        "id": "momentum",
        "name": "动能交易",
        "source": "Moomoo",
        "style": "momentum",
        "tool_keys": ["stochastic_momentum", "macd_crossover", "citadel_momentum"],
        "rules": "顺强趋势；关注背离作为退出信号。",
        "best_regime": ["TRENDING", "EUPHORIA"],
    },
]


def build_dca_plan(
    total_usdt: float,
    weeks: int = 24,
    *,
    symbol: str = "BNBUSDT",
) -> Dict[str, Any]:
    """IG 式 DCA：总预算均摊到固定周数（教育/规划用，非自动下单）。"""
    weeks = max(1, int(weeks))
    total = max(0.0, float(total_usdt))
    per_week = round(total / weeks, 2)
    return {
        "strategy": "DCA",
        "symbol": symbol,
        "total_usdt": total,
        "weeks": weeks,
        "per_week_usdt": per_week,
        "schedule_hint": "每周固定日定额买入，忽略短期波动",
        "note": "DCA 是仓位建设计划，不产生方向投票信号",
    }


def recommend_styles_for_regime(regime: Optional[str]) -> List[str]:
    """按市场状态推荐策略风格。"""
    r = str(regime or "UNKNOWN").upper()
    mapping = {
        "TRENDING": ["trend", "momentum", "breakout", "ma_cross"],
        "RANGING": ["mean_reversion", "swing", "rsi"],
        "HIGH_VOLATILITY": ["scalping", "breakout"],
        "LOW_VOLATILITY": ["mean_reversion", "rsi", "dca"],
        "NEWS_DRIVEN": ["event_driven", "breakout", "volume_price"],
        "PANIC": ["event_driven"],
        "EUPHORIA": ["momentum", "ma_cross"],
    }
    return mapping.get(r, ["trend", "mean_reversion"])


def build_playbook_prompt_section(
    *,
    regime: Optional[str] = None,
    indicators: Optional[Dict[str, Any]] = None,
) -> str:
    """生成注入 DeepSeek 的技术分析纪律摘要。"""
    regime = str(regime or "UNKNOWN").upper()
    styles = recommend_styles_for_regime(regime)
    matched = [
        s for s in STRATEGY_CATALOG
        if s["id"] in styles or s["style"] in styles
    ][:5]

    lines = [
        "【技术分析 Playbook — FinLab/IG/Moomoo 纪律】",
        "1. 决策必须是可复现规则，禁止纯感觉；弱证据时输出观望。",
        "2. 评估看风险调整收益（夏普/回撤/盈亏比），不要只追收益率。",
        f"3. 当前 regime={regime}，优先风格: {', '.join(styles)}。",
    ]
    if matched:
        lines.append("4. 优先参考策略:")
        for s in matched:
            lines.append(f"   - {s['name']}: {s['rules']}")

    ind = indicators or {}
    hints: List[str] = []
    adx = ind.get("ADX")
    if adx is not None:
        try:
            adx_f = float(adx)
            if adx_f < 20:
                hints.append(f"ADX={adx_f:.1f} 偏低 → 偏区间/均值回归，少做趋势追单")
            elif adx_f >= 25:
                hints.append(f"ADX={adx_f:.1f} 确认趋势 → 顺势 + 突破优先")
        except (TypeError, ValueError):
            pass
    rsi = ind.get("RSI")
    if rsi is not None:
        try:
            rsi_f = float(rsi)
            if rsi_f < 30:
                hints.append(f"RSI={rsi_f:.1f} 超卖区，震荡市可低吸，趋势下跌中勿盲目抄底")
            elif rsi_f > 70:
                hints.append(f"RSI={rsi_f:.1f} 超买区，震荡市可高抛，强趋势中可持有但收紧止损")
        except (TypeError, ValueError):
            pass
    stoch_k = ind.get("Stoch_K")
    if stoch_k is not None:
        try:
            sk = float(stoch_k)
            if sk < 20:
                hints.append(f"Stoch_K={sk:.1f} 动能超卖")
            elif sk > 80:
                hints.append(f"Stoch_K={sk:.1f} 动能超买")
        except (TypeError, ValueError):
            pass
    sr_sup = ind.get("Support")
    sr_res = ind.get("Resistance")
    if sr_sup is not None and sr_res is not None:
        hints.append(f"关键位 Support≈{float(sr_sup):.2f} / Resistance≈{float(sr_res):.2f}")

    if hints:
        lines.append("5. 当前指标提示:")
        for h in hints:
            lines.append(f"   - {h}")

    lines.append(
        "6. 输出须含：方向、置信度、入场区、止损、止盈梯、失效条件；"
        "冲突或低置信度时明确 WAIT。"
    )
    return "\n".join(lines)


def summarize_catalog() -> Dict[str, Any]:
    return {
        "count": len(STRATEGY_CATALOG),
        "strategies": [
            {"id": s["id"], "name": s["name"], "keys": s["tool_keys"]}
            for s in STRATEGY_CATALOG
        ],
    }


def _indicator_hints(indicators: Optional[Dict[str, Any]]) -> List[str]:
    """从指标快照提取简短提示（GUI / Web 共用）。"""
    ind = indicators or {}
    hints: List[str] = []
    adx = ind.get("ADX")
    if adx is not None:
        try:
            adx_f = float(adx)
            if adx_f < 20:
                hints.append(f"ADX {adx_f:.1f} 偏低 → 区间/均值回归优先")
            elif adx_f >= 25:
                hints.append(f"ADX {adx_f:.1f} 确认趋势 → 顺势/突破优先")
            else:
                hints.append(f"ADX {adx_f:.1f} 趋势未强确认")
        except (TypeError, ValueError):
            pass
    rsi = ind.get("RSI")
    if rsi is not None:
        try:
            rsi_f = float(rsi)
            if rsi_f < 30:
                hints.append(f"RSI {rsi_f:.1f} 超卖")
            elif rsi_f > 70:
                hints.append(f"RSI {rsi_f:.1f} 超买")
        except (TypeError, ValueError):
            pass
    stoch_k = ind.get("Stoch_K")
    if stoch_k is not None:
        try:
            sk = float(stoch_k)
            if sk < 20:
                hints.append(f"Stoch {sk:.1f} 动能偏弱")
            elif sk > 80:
                hints.append(f"Stoch {sk:.1f} 动能偏强")
        except (TypeError, ValueError):
            pass
    gc = ind.get("Golden_Cross_State")
    if gc is not None:
        try:
            g = float(gc)
            if g > 0:
                hints.append("SMA50>200 黄金交叉结构偏多")
            elif g < 0:
                hints.append("SMA50<200 死亡交叉结构偏空")
        except (TypeError, ValueError):
            pass
    sup = ind.get("Support")
    res = ind.get("Resistance")
    if sup is not None and res is not None:
        hints.append(f"S/R {float(sup):.2f} — {float(res):.2f}")
    obv_slope = ind.get("OBV_Slope")
    if obv_slope is not None:
        try:
            s = float(obv_slope)
            if abs(s) > 1e-6:
                hints.append(f"OBV 斜率 {'向上' if s > 0 else '向下'}")
        except (TypeError, ValueError):
            pass
    return hints


def _classic_ta_summary(inst_results: Optional[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """统计经典 TA 策略投票与亮点列表。"""
    details = (inst_results or {}).get("strategy_details") or {}
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    highlights: List[Dict[str, Any]] = []
    for key in CLASSIC_TA_STRATEGY_KEYS:
        d = details.get(key)
        if not d or d.get("signal") == "ERROR":
            continue
        sig = str(d.get("signal") or "HOLD").upper()
        if sig in counts:
            counts[sig] += 1
        if sig in ("BUY", "SELL"):
            highlights.append({
                "key": key,
                "name": d.get("strategy", key),
                "signal": sig,
                "confidence": float(d.get("confidence") or 0.5),
                "reason": d.get("reason") or d.get("description") or "",
            })
    highlights.sort(key=lambda x: x["confidence"], reverse=True)
    return counts, highlights


def build_ta_analysis_bundle(
    *,
    regime: Optional[str] = None,
    indicators: Optional[Dict[str, Any]] = None,
    inst_results: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    account_balance: Optional[float] = None,
    symbol: str = "BNBUSDT",
) -> Dict[str, Any]:
    """构建可序列化的技术分析 Playbook 快照（GUI / Web / 学习记录）。"""
    cfg = (config or {}).get("analysis", {}).get("ta_playbook") or (config or {}).get("ta_playbook") or {}
    if cfg.get("enabled") is False:
        return {"enabled": False}

    regime_u = str(regime or "UNKNOWN").upper()
    styles = recommend_styles_for_regime(regime_u)
    matched = [
        {"id": s["id"], "name": s["name"], "rules": s["rules"]}
        for s in STRATEGY_CATALOG
        if s["id"] in styles or s["style"] in styles
    ][:5]

    classic_counts, highlights = _classic_ta_summary(inst_results)
    hints = _indicator_hints(indicators)

    # 经典 TA 与机构共识对齐度
    consensus = str((inst_results or {}).get("consensus_signal") or "HOLD").upper()
    buy_c = classic_counts.get("BUY", 0)
    sell_c = classic_counts.get("SELL", 0)
    classic_bias = "HOLD"
    if buy_c > sell_c:
        classic_bias = "BUY"
    elif sell_c > buy_c:
        classic_bias = "SELL"
    aligned = (
        (consensus == classic_bias)
        or consensus == "HOLD"
        or classic_bias == "HOLD"
    )

    ind_snap: Dict[str, Any] = {}
    for k in (
        "RSI", "ADX", "Plus_DI", "Minus_DI", "Stoch_K", "Stoch_D",
        "OBV_Slope", "Support", "Resistance", "Golden_Cross_State",
        "BB_Position", "Volume_Ratio",
    ):
        if indicators and k in indicators and indicators[k] is not None:
            try:
                ind_snap[k] = round(float(indicators[k]), 4)
            except (TypeError, ValueError):
                ind_snap[k] = indicators[k]

    dca_weeks = int(cfg.get("dca_default_weeks", 24))
    dca_plan = None
    if account_balance is not None and float(account_balance) > 0:
        dca_plan = build_dca_plan(float(account_balance), weeks=dca_weeks, symbol=symbol)

    total_strategies = int((inst_results or {}).get("total_strategies") or 0)

    return {
        "enabled": True,
        "regime": regime_u,
        "recommended_styles": styles,
        "regime_strategies": matched,
        "indicator_hints": hints,
        "indicator_snapshot": ind_snap,
        "classic_ta_votes": classic_counts,
        "classic_ta_bias": classic_bias,
        "classic_ta_aligned_with_consensus": aligned,
        "classic_ta_highlights": highlights[:6],
        "institutional_total": total_strategies,
        "institutional_consensus": consensus,
        "prompt_excerpt": build_playbook_prompt_section(regime=regime_u, indicators=indicators),
        "dca_plan": dca_plan,
        "discipline": [
            "可复现规则优先，弱证据输出 WAIT",
            "评价看夏普/回撤/盈亏比，不单看收益",
            "单因子易失效，多策略投票更稳（FinLab）",
        ],
    }


def format_ta_cockpit_lines(bundle: Optional[Dict[str, Any]]) -> List[str]:
    """决策驾驶舱因子区：技术分析 Playbook 文本行。"""
    b = bundle or {}
    if not b.get("enabled"):
        return []
    lines = ["[技术分析 Playbook]", ""]
    styles = b.get("recommended_styles") or []
    if styles:
        lines.append(f"  Regime {b.get('regime', '?')} → 优先: {', '.join(styles)}")
    hints = b.get("indicator_hints") or []
    for h in hints[:5]:
        lines.append(f"  · {h}")
    cv = b.get("classic_ta_votes") or {}
    if any(cv.values()):
        lines.append(
            f"  经典TA票: 买{cv.get('BUY', 0)} 卖{cv.get('SELL', 0)} 观望{cv.get('HOLD', 0)}"
            f" → 偏向 {b.get('classic_ta_bias', 'HOLD')}"
        )
        align = "✓" if b.get("classic_ta_aligned_with_consensus") else "≠"
        lines.append(
            f"  与机构共识({b.get('institutional_consensus', '?')}) {align}"
        )
    for h in (b.get("classic_ta_highlights") or [])[:4]:
        lines.append(
            f"  [{h.get('signal')}] {h.get('name')}: {str(h.get('reason', ''))[:55]}"
        )
    dca = b.get("dca_plan")
    if dca:
        lines.append(
            f"  DCA参考: 每周 {dca.get('per_week_usdt')} USDT × {dca.get('weeks')} 周"
        )
    return lines


def format_ta_summary_block(bundle: Optional[Dict[str, Any]]) -> str:
    """AI 摘要 Tab 用的 Playbook 段落。"""
    lines = format_ta_cockpit_lines(bundle)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
