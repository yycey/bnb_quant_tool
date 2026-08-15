"""AI 交易上下文增强 — 把模拟盘真实盈亏注入 DeepSeek，提升决策质量。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from bnb_quant_tool.config_access import get_confidence_threshold


def is_learning_phase(config: Optional[Dict[str, Any]] = None) -> bool:
    """模拟盘学习期：优先积累真开仓样本。样本够了自动退出。"""
    ai = (config or {}).get("ai_trading") or {}
    if not bool(ai.get("learning_phase", False)):
        return False
    min_trades = int(ai.get("learning_phase_min_trades", 30) or 30)
    if min_trades <= 0:
        return True
    since = str(ai.get("learning_phase_since") or "2026-07-28")
    db = _paper_db_path()
    if not db or not Path(db).is_file():
        return True
    try:
        conn = sqlite3.connect(db, timeout=5)
        n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='CLOSED' "
            "AND opened_at >= ?",
            (since,),
        ).fetchone()[0]
        conn.close()
        return int(n or 0) < min_trades
    except Exception:
        return True


def analysis_extra_market(
    config: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """构建 build_full_analysis_learning_context 的 extra_market（含 app_config）。"""
    em = dict(fields)
    if config is not None:
        em["app_config"] = config
    return em


def _paper_db_path(paper_engine=None) -> Optional[str]:
    if paper_engine is not None and getattr(paper_engine, "db_path", None):
        return str(paper_engine.db_path)
    try:
        from bnb_quant_tool.data_localization import get_localized_db_path
        return str(get_localized_db_path("paper_trading"))
    except ImportError:
        return None


def get_paper_trading_stats(paper_engine=None, *, auto_only: Optional[bool] = None) -> Dict[str, Any]:
    """从 paper_trading.db 读取真实模拟盘绩效（比 analysis_records 反馈更可靠）。

    默认与 PaperTradingEngine.stats_auto_only_default 对齐，排除 MANUAL 平仓。
    """
    db = _paper_db_path(paper_engine)
    if not db or not Path(db).is_file():
        return {}

    if auto_only is None:
        auto_only = True
        try:
            if paper_engine is not None and hasattr(paper_engine, "stats_auto_only_default"):
                auto_only = bool(paper_engine.stats_auto_only_default)
        except Exception:
            auto_only = True

    manual_filter = ""
    if auto_only:
        manual_filter = " AND (close_reason IS NULL OR close_reason NOT LIKE 'MANUAL%')"

    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        row = cur.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN realized_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(realized_pnl_usdt) AS total_pnl,
                   AVG(r_multiple) AS avg_r,
                   AVG(realized_pnl_usdt) AS avg_pnl
            FROM paper_positions
            WHERE status='CLOSED' AND r_multiple IS NOT NULL
            {manual_filter}
        """).fetchone()

        recent: List[Dict] = []
        for r in cur.execute(f"""
            SELECT id, side, entry_price, close_avg_price, realized_pnl_usdt,
                   r_multiple, close_reason, closed_at
            FROM paper_positions
            WHERE status='CLOSED'
            {manual_filter}
            ORDER BY closed_at DESC
            LIMIT 8
        """):
            recent.append(dict(r))

        open_count = cur.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'"
        ).fetchone()[0]

        conn.close()
    except Exception:
        return {}

    total = int(row["total"] or 0)
    wins = int(row["wins"] or 0)
    stats = {
        "closed_trades": total,
        "win_rate": round(wins / total, 4) if total > 0 else 0.0,
        "total_pnl_usdt": round(float(row["total_pnl"] or 0), 2),
        "avg_r_multiple": round(float(row["avg_r"] or 0), 3),
        "avg_pnl_usdt": round(float(row["avg_pnl"] or 0), 2),
        "open_positions": int(open_count),
        "recent_trades": recent,
        "consecutive_losses": _count_consecutive_losses(recent),
        "auto_only": bool(auto_only),
    }
    return stats


def _count_consecutive_losses(recent: List[Dict]) -> int:
    """连亏计数；认错/超时等主动学习平仓与熔断器 ignore 列表对齐，跳过不计入。"""
    try:
        from bnb_quant_tool.circuit_breaker import (
            DEFAULT_CONSEC_IGNORE_REASONS,
            is_ignored_consec_close_reason,
        )
        ignore = set(DEFAULT_CONSEC_IGNORE_REASONS)
    except Exception:
        ignore = {"ADMIT_WRONG", "TIMEOUT", "TIMEOUT_NO_TP"}

        def is_ignored_consec_close_reason(reason, _ignore=None):  # type: ignore
            return str(reason or "").strip().upper() in ignore

    streak = 0
    for t in recent:
        if is_ignored_consec_close_reason(t.get("close_reason"), ignore):
            continue
        pnl = float(t.get("realized_pnl_usdt") or 0)
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def enrich_learning_insights(
    base: Dict[str, Any],
    paper_engine=None,
    pattern_insight: Optional[Dict] = None,
) -> Dict[str, Any]:
    """合并学习系统洞察 + 模拟盘绩效 + 模式记忆。"""
    enriched = dict(base)
    paper = get_paper_trading_stats(paper_engine)
    if paper:
        enriched["paper_trading"] = paper
        enriched["recommendations"] = list(enriched.get("recommendations") or [])
        wr = paper.get("win_rate", 0)
        if paper.get("closed_trades", 0) >= 20 and wr < 0.45:
            enriched["recommendations"].insert(
                0,
                f"模拟盘胜率仅 {wr:.1%}（{paper['closed_trades']}笔），应提高门槛、减少交易频率",
            )
        if paper.get("consecutive_losses", 0) >= 3:
            enriched["recommendations"].insert(
                0,
                f"连亏 {paper['consecutive_losses']} 笔，优先 WAIT，勿强行开仓",
            )
    if pattern_insight and pattern_insight.get("matched", 0) > 0:
        enriched["pattern_memory"] = pattern_insight
    return enriched


def build_analysis_learning_context(
    learner,
    *,
    symbol: str = "BNBUSDT",
    current_price: float = 0,
    indicators: Optional[Dict[str, Any]] = None,
    regime: Optional[str] = None,
    final_signal: Optional[str] = None,
    paper_engine=None,
    pattern_insight: Optional[Dict] = None,
    counterfactual=None,
    extra_market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建单次 AI 分析用的完整学习上下文（每次分析必调）。

    聚合：能力等级、知识卡片、策略权重、模拟盘绩效、模式记忆、反事实统计、近期调参。
    """
    if learner is None:
        return {}

    if hasattr(learner, "refresh_before_analysis"):
        learner.refresh_before_analysis(regime=regime)

    market_ctx: Dict[str, Any] = {
        "symbol": symbol,
        "current_price": current_price,
        "indicators": indicators or {},
        "regime": regime,
        "market_regime": regime,
        "signal": final_signal,
        "final_signal": final_signal,
    }
    if extra_market:
        market_ctx.update(extra_market)

    base = learner.get_learning_insights(market_context=market_ctx)
    enriched = enrich_learning_insights(
        base,
        paper_engine=paper_engine,
        pattern_insight=pattern_insight,
    )
    if extra_market and extra_market.get("app_config"):
        enriched["app_config"] = extra_market["app_config"]
    # 保留 get_learning_insights 按 regime 融合后的权重，勿用全局缓存覆盖
    regime_weights = enriched.get("strategy_weights") or {}
    if regime_weights:
        enriched["strategy_weights"] = regime_weights
    else:
        enriched["strategy_weights"] = learner.get_adaptive_weights() or {}
    enriched["regime_bucket"] = enriched.get("regime_bucket")
    enriched["knowledge_base_loaded"] = bool(enriched.get("knowledge_base_loaded"))
    kb_cards = enriched.get("capability_cards") or []
    kb_mode = enriched.get("capability_retrieval_mode") or "none"
    enriched["knowledge_base_injected"] = len(kb_cards) > 0
    enriched["knowledge_base_retrieval"] = {
        "mode": kb_mode,
        "cards_count": len(kb_cards),
        "total_active": int((enriched.get("capability_summary") or {}).get("total_active") or 0),
    }

    try:
        enriched["recent_param_changes"] = learner.get_recent_param_changes(
            limit=3, paper_engine=paper_engine
        )
    except Exception:
        enriched["recent_param_changes"] = []

    growth = enriched.get("growth") or {}
    if growth:
        enriched["learning_maturity"] = growth.get(
            "learning_maturity", enriched.get("learning_maturity")
        )

    if counterfactual is not None:
        try:
            cf_stats = counterfactual.get_summary_stats(limit=50)
            if cf_stats.get("total_analyzed"):
                enriched["counterfactual_stats"] = cf_stats
        except Exception:
            pass

    return enriched


def build_full_analysis_learning_context(
    learner,
    *,
    symbol: str = "BNBUSDT",
    current_price: float = 0,
    indicators: Optional[Dict[str, Any]] = None,
    regime: Optional[str] = None,
    final_signal: Optional[str] = None,
    paper_engine=None,
    pattern_memory=None,
    counterfactual=None,
    inst_results: Optional[Dict[str, Any]] = None,
    ai_analysis: Optional[Dict[str, Any]] = None,
    news_summary: Optional[Dict[str, Any]] = None,
    extra_market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """分析流水线统一入口：聚合全部学习成果 + 模式记忆查询。

    供 GUI 手动分析 / AI 全自动 / 多智能体协同共用，确保每次分析注入：
    策略权重、知识卡片、模拟盘绩效、模式记忆、反事实统计、因子归因等。
    """
    pattern_insight = None
    if pattern_memory is not None:
        try:
            pattern_insight = pattern_memory.get_insight({
                "indicators": indicators or {},
                "ai_analysis": ai_analysis or {},
                "institutional_strategies": inst_results or {},
                "current_price": current_price,
                "news_summary": news_summary or {},
            })
        except Exception:
            pattern_insight = None

    ctx = build_analysis_learning_context(
        learner,
        symbol=symbol,
        current_price=current_price,
        indicators=indicators,
        regime=regime,
        final_signal=final_signal,
        paper_engine=paper_engine,
        pattern_insight=pattern_insight,
        counterfactual=counterfactual,
        extra_market=extra_market,
    )
    if learner is not None:
        try:
            from bnb_quant_tool.learning_analytics import LearningAnalytics

            app_cfg = (extra_market or {}).get("app_config") or {}
            analytics = LearningAnalytics(learner)
            ctx = analytics.enrich_analysis_context(
                ctx,
                regime=regime,
                config=app_cfg,
            )
            if inst_results:
                try:
                    from bnb_quant_tool.win_rate_strategy import (
                        analyze_institutional_consensus,
                        merge_consensus_into_win_rate_context,
                        resolve_strategy_win_rate_config,
                    )

                    perf = ctx.get("strategy_performance") or {}
                    sw_cfg = resolve_strategy_win_rate_config(app_cfg)
                    consensus = analyze_institutional_consensus(
                        inst_results,
                        perf,
                        ctx.get("strategy_weights"),
                        sw_cfg,
                    )
                    ctx["strategy_consensus"] = consensus
                    ctx["win_rate_context"] = merge_consensus_into_win_rate_context(
                        ctx.get("win_rate_context") or {}, consensus
                    )
                except ImportError:
                    pass
        except Exception:
            pass
    # 智能闭环：注入议会教训 + 经验摘要 + 健康度（带着记忆决策）
    try:
        from bnb_quant_tool.intelligence_loop import IntelligenceLoop

        app_cfg = (extra_market or {}).get("app_config") or {}
        loop = IntelligenceLoop(
            learner=learner,
            config=app_cfg or getattr(learner, "config", {}) or {},
            paper_engine=paper_engine,
            pattern_memory=pattern_memory,
            counterfactual=counterfactual,
        )
        ctx = loop.enrich_learning_context(
            ctx,
            symbol=symbol,
            indicators=indicators,
            regime=regime,
        )
    except Exception:
        pass
    if paper_engine is not None:
        ctx["_paper_engine"] = paper_engine
    if learner is not None:
        ctx["_learner"] = learner
    return ctx


def apply_pattern_memory_gate(
    advice: Dict[str, Any],
    insight: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """相似历史局面胜率过低时拦截开仓。"""
    ai_cfg = config.get("ai_trading") or {}
    min_wr = float(ai_cfg.get("pattern_memory_block_wr", 0.35))
    min_samples = int(ai_cfg.get("pattern_memory_min_samples", 5))

    matched = int(insight.get("matched") or 0)
    win_rate = float(insight.get("win_rate") or 0)
    if matched < min_samples or win_rate >= min_wr:
        return advice

    action = advice.get("action")
    if action not in ("LONG", "SHORT"):
        return advice

    advice = dict(advice)
    advice["action"] = "WAIT"
    advice["passed_gate"] = False
    reasons = list(advice.get("gate_reasons") or [])
    reasons.append(
        f"模式记忆拦截: 相似局面 {matched} 次历史胜率仅 {win_rate:.1%} "
        f"(<{min_wr:.0%})"
    )
    advice["gate_reasons"] = reasons
    advice["pattern_blocked"] = True
    return advice


def apply_counterfactual_gate(
    advice: Dict[str, Any],
    learning_insights: Optional[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """反事实统计表明过度交易/方向错误时，确定性拦截或收紧。"""
    ai_cfg = config.get("ai_trading") or {}
    cf_cfg = ai_cfg.get("counterfactual_gate") or {}
    if cf_cfg.get("enabled") is False:
        return advice
    # 学习期关闭反事实硬拦，避免「观望 WIN」统计把开仓锁死
    if is_learning_phase(config) and bool(
        ai_cfg.get("learning_phase_disable_counterfactual", True)
    ):
        out = dict(advice)
        reasons = list(out.get("gate_reasons") or [])
        msg = "学习期：反事实硬门控暂关"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        return out

    cf = (learning_insights or {}).get("counterfactual_stats") or {}
    total = int(cf.get("total_analyzed") or 0)
    min_samples = int(cf_cfg.get("min_samples", 8))
    if total < min_samples:
        return advice

    waited = int(cf.get("should_have_waited") or 0)
    reversed_n = int(cf.get("should_have_reversed") or 0)
    wait_rate = waited / total
    reverse_rate = reversed_n / total
    block_wait_rate = float(cf_cfg.get("block_wait_rate", 0.45))
    block_reverse_rate = float(cf_cfg.get("block_reverse_rate", 0.35))

    action = advice.get("action")
    if action not in ("LONG", "SHORT"):
        return advice

    advice = dict(advice)
    reasons = list(advice.get("gate_reasons") or [])

    if wait_rate >= block_wait_rate:
        advice["action"] = "WAIT"
        advice["passed_gate"] = False
        reasons.append(
            f"反事实门控: 最近 {total} 笔中 {waited} 笔「不交易更好」"
            f"({wait_rate:.0%}≥{block_wait_rate:.0%})，优先观望"
        )
        advice["gate_reasons"] = reasons
        advice["counterfactual_blocked"] = True
        return advice

    if reverse_rate >= block_reverse_rate:
        advice["action"] = "WAIT"
        advice["passed_gate"] = False
        reasons.append(
            f"反事实门控: 最近 {total} 笔中 {reversed_n} 笔「反向更好」"
            f"({reverse_rate:.0%})，暂停同向开仓"
        )
        advice["gate_reasons"] = reasons
        advice["counterfactual_blocked"] = True
        return advice

    return advice


def apply_funding_direction_gate(
    advice: Dict[str, Any],
    sentiment: Optional[Dict] = None,
    bnb_factors: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Funding 极端拥挤时确定性压方向（不只 Prompt / 信念分）。"""
    cfg = (config or {}).get("ai_trading") or {}
    fd = cfg.get("funding_direction_gate") or {}
    if fd.get("enabled") is False:
        return advice

    rate = None
    rs = (bnb_factors or {}).get("risk_sentry") or {}
    fr = rs.get("funding_extreme") or {}
    if fr.get("rate") is not None:
        rate = float(fr["rate"])
    elif sentiment:
        fr2 = sentiment.get("funding_rate") or {}
        if isinstance(fr2, dict) and fr2.get("rate") is not None:
            rate = float(fr2["rate"])

    if rate is None:
        return advice

    block_long_rate = float(fd.get("block_long_rate", 0.001))
    block_short_rate = float(fd.get("block_short_rate", -0.001))
    action = advice.get("action")
    if action not in ("LONG", "SHORT"):
        return advice

    advice = dict(advice)
    reasons = list(advice.get("gate_reasons") or [])

    if action == "LONG" and rate >= block_long_rate:
        advice["action"] = "WAIT"
        advice["passed_gate"] = False
        reasons.append(
            f"Funding 门控: 费率 {rate:+.4%} ≥ {block_long_rate:+.4%}，多头拥挤禁止追多"
        )
        advice["gate_reasons"] = reasons
        advice["funding_blocked"] = True
        return advice

    if action == "SHORT" and rate <= block_short_rate:
        advice["action"] = "WAIT"
        advice["passed_gate"] = False
        reasons.append(
            f"Funding 门控: 费率 {rate:+.4%} ≤ {block_short_rate:+.4%}，空头拥挤禁止追空"
        )
        advice["gate_reasons"] = reasons
        advice["funding_blocked"] = True
        return advice

    return advice


def get_effective_follow_direction(advice: Dict[str, Any]) -> str:
    """跟单/展示用的有效方向：优先 final action，否则回退 raw_action / AI 方向。"""
    action = (advice.get("action") or "").upper()
    if action in ("LONG", "SHORT"):
        return action
    raw = (advice.get("raw_action") or "").upper()
    if raw in ("LONG", "SHORT"):
        return raw
    intended = (advice.get("intended_direction") or "").upper()
    if intended in ("LONG", "SHORT"):
        return intended
    ai_act = (advice.get("ai_action") or "").upper()
    if ai_act in ("LONG", "SHORT"):
        return ai_act
    votes = advice.get("votes") or {}
    ai_dir = (votes.get("ai_direction") or "").upper()
    if ai_dir in ("LONG", "SHORT"):
        return ai_dir
    vote_action = (votes.get("vote_action") or "").upper()
    if vote_action in ("LONG", "SHORT"):
        return vote_action
    return "WAIT"


def _dir_from_token(v: Any) -> str:
    s = str(v or "").upper().strip()
    if s in ("LONG", "BUY", "BULLISH", "做多", "买入"):
        return "LONG"
    if s in ("SHORT", "SELL", "BEARISH", "做空", "卖出"):
        return "SHORT"
    return "WAIT"


def get_validation_probe_direction(advice: Dict[str, Any]) -> str:
    """验证试探方向：AI HOLD 时仍可用投票/机构/订单流/扫盘/MTF 开小仓。

    没有成交就无法验证想法；有行情不等于必须等 AI 先开口。
    """
    d = get_effective_follow_direction(advice)
    if d in ("LONG", "SHORT"):
        return d

    scan = advice.get("scanner_signal") or advice.get("scan_signal") or {}
    if isinstance(scan, dict):
        sd = _dir_from_token(scan.get("direction") or scan.get("trade_direction"))
        if sd in ("LONG", "SHORT"):
            return sd
    sd = _dir_from_token(advice.get("scanner_direction"))
    if sd in ("LONG", "SHORT"):
        return sd

    votes = advice.get("votes") or {}
    for key in (
        "vote_action",
        "institutional_direction",
        "tech_direction",
        "dl_direction",
        "scanner_direction",
    ):
        v = _dir_from_token(votes.get(key))
        if v in ("LONG", "SHORT"):
            return v

    inst = advice.get("institutional") or advice.get("institutional_results") or {}
    if isinstance(inst, dict):
        cons = _dir_from_token(inst.get("consensus_signal") or inst.get("consensus"))
        if cons in ("LONG", "SHORT"):
            return cons
        buy = int(inst.get("buy_signals") or 0)
        sell = int(inst.get("sell_signals") or 0)
        if buy >= sell + 2 and buy >= 2:
            return "LONG"
        if sell >= buy + 2 and sell >= 2:
            return "SHORT"

    ic = advice.get("institutional_conviction") or {}
    if isinstance(ic, dict):
        cd = _dir_from_token(ic.get("direction"))
        if cd in ("LONG", "SHORT"):
            return cd

    of = advice.get("orderflow") or {}
    if isinstance(of, dict):
        sv = of.get("soft_vote") or {}
        sig = _dir_from_token(sv.get("signal") or of.get("direction"))
        if sig in ("LONG", "SHORT"):
            return sig

    mtf = advice.get("multi_timeframe") or {}
    if isinstance(mtf, dict):
        for key in ("recommended_action", "bias", "consensus", "signal", "aligned_bias", "overall_bias"):
            md = _dir_from_token(mtf.get(key))
            if md in ("LONG", "SHORT"):
                return md

    news = advice.get("news_summary") or {}
    if isinstance(news, dict):
        nd = _dir_from_token(news.get("trade_suggestion") or news.get("suggestion"))
        if nd in ("LONG", "SHORT"):
            return nd

    return "WAIT"


_LEARNING_PROBE_HARD_KEYWORDS = (
    "熔断",
    "只平不开",
    "反记忆",
    "保证金不足",
    "连亏",
    "连续亏损",
    "强制停手",
    "风控否决",
    "监管",
    "unlock_dump",
    "解锁砸盘",
    "circuit",
    "anti_memory",
)

# 验证小仓仍不可绕过（真危险）。不含「只平不开/连亏/熔断」——由 bypass_circuit 缩仓处理
_VALIDATION_PROBE_HARD_KEYWORDS = (
    "反记忆",
    "保证金不足",
    "监管",
    "unlock_dump",
    "解锁砸盘",
    "anti_memory",
    "atr熔断",
    "ATR熔断",
    "atr突变",
    "ATR突变",
    "黑天鹅",
    "black_swan",
    "偏离 MA20",
    "偏离MA20",
    "ma20_dev",
)


def _norm_gate_text(s: str) -> str:
    """去空白后小写，避免『ATR 突变』与『ATR突变』对不上。"""
    return "".join(str(s).lower().split())


_MARKET_DANGER_NORMS = (
    "atr突变",
    "atr熔断",
    "黑天鹅",
    "blackswan",
    "black_swan",
    "偏离ma20",
    "ma20dev",
    "价格偏离",
)


def _learning_probe_hard_blocked(
    advice: Dict[str, Any],
    *,
    validation_mode: bool = False,
    bypass_circuit: bool = False,
) -> Optional[str]:
    """试探仍不可绕过的硬拦。验证模式可绕过连亏/冷却，不可绕过市场级危险。"""
    if advice.get("anti_memory_blocked"):
        return "反记忆硬拦"

    reasons = advice.get("gate_reasons") or []
    reason_blob = _norm_gate_text(" ".join(str(r) for r in reasons))
    cb = advice.get("circuit_breaker") or {}
    cb_blob = ""
    if isinstance(cb, dict):
        cb_blob = _norm_gate_text(
            " ".join(str(x) for x in (cb.get("reasons") or []))
        )
    danger_blob = reason_blob + " " + cb_blob
    market_danger = any(d in danger_blob for d in _MARKET_DANGER_NORMS)

    # 市场级 STOPPED：即使验证 bypass 也不可开
    if market_danger and (
        advice.get("circuit_breaker_blocked")
        or (isinstance(cb, dict) and cb.get("allowed") is False)
        or any("只平不开" in str(r) for r in reasons)
    ):
        return "市场级熔断不可探针绕过"

    if advice.get("risk_vetoed") and not (validation_mode and bypass_circuit):
        return "风控否决硬拦"
    if advice.get("circuit_breaker_blocked") and not (validation_mode and bypass_circuit):
        return "熔断硬拦"
    if isinstance(cb, dict) and cb.get("allowed") is False and not (
        validation_mode and bypass_circuit
    ):
        return "熔断硬拦"

    keywords = (
        _VALIDATION_PROBE_HARD_KEYWORDS
        if (validation_mode and bypass_circuit)
        else _LEARNING_PROBE_HARD_KEYWORDS
    )
    for r in reasons:
        s = str(r)
        blob = _norm_gate_text(s)
        for kw in keywords:
            if _norm_gate_text(kw) in blob:
                return s[:120]
    return None


def _ensure_probe_prices(advice: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """试探开仓时补全 / 纠正 entry/SL/TP。

    探针方向相对原 advice 翻转时，必须重算 SL/TP，否则会留下反方向止损并在开仓瞬间扫损。
    """
    out = dict(advice)
    prices = dict(out.get("prices") or {})
    entry = float(prices.get("entry_mid") or out.get("current_price") or 0)
    if entry <= 0:
        return out
    atr = float(prices.get("atr") or 0)
    if atr <= 0:
        atr = entry * 0.01
    direction = str(direction or "").upper()
    if not prices.get("entry_mid"):
        prices["entry_mid"] = round(entry, 4)

    sl = float(prices.get("stop_loss") or 0)
    tp1 = float(prices.get("tp1") or 0)
    wrong_side = False
    if sl > 0:
        if direction == "LONG" and sl >= entry:
            wrong_side = True
        elif direction == "SHORT" and sl <= entry:
            wrong_side = True
    if tp1 > 0 and not wrong_side:
        if direction == "LONG" and tp1 <= entry:
            wrong_side = True
        elif direction == "SHORT" and tp1 >= entry:
            wrong_side = True

    need_sl = (not prices.get("stop_loss") or sl <= 0 or wrong_side)
    need_tp = (not prices.get("tp1") or tp1 <= 0 or wrong_side)
    if need_sl:
        if direction == "LONG":
            prices["stop_loss"] = round(entry - atr * 1.5, 4)
        else:
            prices["stop_loss"] = round(entry + atr * 1.5, 4)
    if need_tp:
        if direction == "LONG":
            prices["tp1"] = round(entry + atr * 1.5, 4)
            prices["tp2"] = round(entry + atr * 3.0, 4)
            prices["tp3"] = round(entry + atr * 4.5, 4)
        else:
            prices["tp1"] = round(entry - atr * 1.5, 4)
            prices["tp2"] = round(entry - atr * 3.0, 4)
            prices["tp3"] = round(entry - atr * 4.5, 4)
    out["prices"] = prices
    return out


def apply_learning_phase_probe(
    advice: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """学习期 / 验证模式：AI/意图已有 LONG/SHORT 但被软门控打成 WAIT 时，恢复试探小仓。

    硬拦（熔断/反记忆/保证金/监管等）仍生效。用于打破「有方向却零成交」空转。
    持续验证模式见 validation_trading.enabled 或 trading_profile=validation。
    """
    cfg = config or {}
    ai = cfg.get("ai_trading") or {}
    try:
        from bnb_quant_tool.validation_trading import is_validation_trading_enabled
        validation_mode = is_validation_trading_enabled(cfg)
    except Exception:
        validation_mode = False

    probe_allowed = bool(ai.get("learning_phase_probe_open", True))
    if validation_mode:
        vt = cfg.get("validation_trading") or {}
        probe_allowed = bool(vt.get("probe_open", True))
    if not probe_allowed:
        return advice
    if not validation_mode and not is_learning_phase(cfg):
        return advice

    out = dict(advice or {})
    if str(out.get("action") or "").upper() in ("LONG", "SHORT") and out.get("passed_gate"):
        return out

    vt_local = (cfg.get("validation_trading") or {}) if validation_mode else {}
    bypass_circuit = bool(vt_local.get("bypass_circuit_for_probe", True)) if validation_mode else False
    allow_vote_dir = bool(vt_local.get("allow_vote_direction_probe", True)) if validation_mode else False

    if validation_mode and allow_vote_dir:
        direction = get_validation_probe_direction(out)
        direction_source = "vote_or_ai"
    else:
        direction = get_effective_follow_direction(out)
        direction_source = "effective"
    if direction not in ("LONG", "SHORT"):
        return out

    hard = _learning_probe_hard_blocked(
        out, validation_mode=validation_mode, bypass_circuit=bypass_circuit
    )
    if hard:
        reasons = list(out.get("gate_reasons") or [])
        msg = f"学习期试探仍硬拦: {hard}"
        if validation_mode:
            msg = f"验证试探仍硬拦: {hard}"
        if msg not in reasons:
            reasons.append(msg)
        out["gate_reasons"] = reasons
        return out

    conf = float(
        out.get("calibrated_confidence")
        or out.get("confidence")
        or 0.0
    )
    vote_floor = float(vt_local.get("vote_direction_confidence_floor", 0.50) or 0.50)
    # AI HOLD / 扫盘方向：抬到可开试探的最低置信；开仓饿死时强制抬到 floor
    encourage = False
    if validation_mode:
        try:
            from bnb_quant_tool.validation_trading import should_encourage_validation_opens
            from bnb_quant_tool.trading_profile import get_decision_funnel
            funnel = get_decision_funnel(cfg)
        except Exception:
            funnel = {}
        try:
            from bnb_quant_tool.validation_trading import should_encourage_validation_opens
            encourage = should_encourage_validation_opens(cfg, funnel=funnel or {})
        except Exception:
            encourage = False
    if validation_mode and (
        direction_source == "vote_or_ai" or encourage or out.get("scanner_signal")
    ):
        votes = out.get("votes") or {}
        vote_conf = float(
            votes.get("vote_confidence")
            or out.get("consensus_confidence")
            or 0
        )
        conf = max(conf, vote_conf, vote_floor)

    min_conf = float(ai.get("learning_phase_min_open_confidence", 0.55) or 0.55)
    floor = float(ai.get("probe_confidence_floor", 0.50) or 0.50)
    scale = float(ai.get("probe_position_scale", 0.35) or 0.35)
    if validation_mode:
        try:
            from bnb_quant_tool.validation_trading import validation_cfg
            vc = validation_cfg(cfg)
            floor = float(vc.get("probe_confidence_floor", floor) or floor)
            scale = float(vc.get("probe_position_scale", scale) or scale)
            min_conf = float(vc.get("min_open_confidence", min_conf) or min_conf)
        except Exception:
            pass
        if bypass_circuit and (
            out.get("circuit_breaker_blocked")
            or any(
                ("连亏" in str(r) or "连续亏损" in str(r) or "冷却" in str(r) or "只平不开" in str(r))
                for r in (out.get("gate_reasons") or [])
            )
        ):
            scale = min(scale, float(vt_local.get("circuit_bypass_scale", 0.20) or 0.20))

    if conf < floor:
        if validation_mode and encourage:
            conf = max(conf, floor)
        else:
            return out
    if conf < min_conf:
        scale = min(scale, float(ai.get("probe_position_scale", 0.35) or 0.35))

    if not out.get("current_price"):
        pe = (out.get("prices") or {}).get("entry_mid")
        if pe:
            out["current_price"] = pe

    out = _ensure_probe_prices(out, direction)
    if float((out.get("prices") or {}).get("entry_mid") or 0) <= 0:
        return out

    pos = dict(out.get("position") or {})
    for k in ("quantity", "usdt_amount", "margin_required", "risk_amount"):
        if pos.get(k) is not None:
            try:
                pos[k] = round(float(pos[k]) * scale, 6)
            except (TypeError, ValueError):
                pass
    if float(pos.get("quantity") or 0) <= 0 and float(pos.get("usdt_amount") or 0) <= 0:
        entry = float((out.get("prices") or {}).get("entry_mid") or 0)
        pos["usdt_amount"] = round(max(50.0, entry * 0.01), 4) if entry > 0 else 50.0
        pos["note"] = "validation_probe" if validation_mode else "learning_phase_probe"
    out["position"] = pos

    out["action"] = direction
    raw_u = str(out.get("raw_action") or "").upper()
    out["raw_action"] = direction if raw_u in ("", "WAIT", "HOLD") else out.get("raw_action")
    out["passed_gate"] = True
    out["learning_phase_probe"] = True
    out["validation_probe"] = validation_mode
    out["probe_position"] = True
    out["confidence_hard_probe"] = True
    out["probe_direction_source"] = direction_source
    if validation_mode and bypass_circuit:
        out["circuit_breaker_blocked"] = False
        out["risk_vetoed"] = False
    reasons = list(out.get("gate_reasons") or [])
    tag = "验证试探" if validation_mode else "学习期试探"
    src_note = "投票/机构方向" if direction_source == "vote_or_ai" else "AI/意图方向"
    msg = (
        f"{tag}开仓: {direction}({src_note}) conf={conf:.0%} "
        f"仓位×{scale:.2f}（软门控旁路；平仓验对错）"
    )
    if msg not in reasons:
        reasons.append(msg)
    out["gate_reasons"] = reasons
    for flag in (
        "net_rr_blocked",
        "mtf_resonance_blocked",
        "confidence_hard_blocked",
        "cross_modal_blocked",
        "pattern_blocked",
        "win_rate_blocked",
        "counterfactual_blocked",
        "funding_blocked",
        "ta_playbook_blocked",
        "factor_attribution_blocked",
    ):
        if out.get(flag):
            out[flag] = False
    return out


def should_open_from_advice(advice: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """AI 全自动是否允许跟单。

    验证模式：软门控未过时，只允许已打上 validation_probe/learning_phase_probe 的小仓，
    禁止 require_gate_pass=false 时全仓旁路。
    """
    ai_cfg = config.get("ai_trading") or {}
    require_gate = bool(ai_cfg.get("require_gate_pass", True))
    direction = get_effective_follow_direction(advice)
    if direction not in ("LONG", "SHORT"):
        return False

    is_probe = bool(
        advice.get("learning_phase_probe") or advice.get("validation_probe")
    )
    if is_probe:
        try:
            from bnb_quant_tool.validation_trading import is_validation_trading_enabled
            validation_on = is_validation_trading_enabled(config)
        except Exception:
            validation_on = False
        bypass = bool((config.get("validation_trading") or {}).get("bypass_circuit_for_probe", True))
        if _learning_probe_hard_blocked(
            advice, validation_mode=validation_on, bypass_circuit=bypass and validation_on
        ):
            return False
        return True

    try:
        from bnb_quant_tool.validation_trading import is_validation_trading_enabled
        validation_on = is_validation_trading_enabled(config)
    except Exception:
        validation_on = False

    passed = bool(advice.get("passed_gate", True))
    # 验证模式：未过门控 → 必须走 probe 小仓；禁止全仓 relaxed 旁路
    if validation_on and not passed:
        return False

    if require_gate and not passed:
        # 学习期 + 宽松：意图方向明确且非硬拦 → 允许
        if is_learning_phase(config) and bool(ai_cfg.get("learning_phase_use_relaxed", True)):
            if _learning_probe_hard_blocked(advice):
                return False
            conf = float(advice.get("calibrated_confidence") or advice.get("confidence") or 0)
            floor = float(ai_cfg.get("probe_confidence_floor", 0.50) or 0.50)
            return conf >= floor
        return False
    return True


def use_relaxed_follow(config: Dict[str, Any]) -> bool:
    ai_cfg = config.get("ai_trading") or {}
    try:
        from bnb_quant_tool.validation_trading import is_validation_trading_enabled
        if is_validation_trading_enabled(config):
            return True
    except Exception:
        pass
    if is_learning_phase(config) and bool(ai_cfg.get("learning_phase_use_relaxed", True)):
        return True
    if "use_relaxed_mode" in ai_cfg:
        return bool(ai_cfg["use_relaxed_mode"])
    return bool((config.get("paper_trading") or {}).get("relaxed_mode", False))


def resolve_execution_context(
    advice: Dict[str, Any],
    config: Dict[str, Any],
    *,
    auto_follow_enabled: bool = True,
) -> Dict[str, Any]:
    """统一解析：分析方向、风控结论、是否允许跟单（供 GUI / 全自动共用）。"""
    action = (advice.get("action") or "WAIT").upper()
    raw = (advice.get("raw_action") or action).upper()
    votes = advice.get("votes") or {}
    ai_dir = (votes.get("ai_direction") or "").upper()
    analysis_dir = raw if raw in ("LONG", "SHORT", "WAIT") else "WAIT"
    if analysis_dir == "WAIT":
        if ai_dir in ("LONG", "SHORT"):
            analysis_dir = ai_dir
        else:
            va = (votes.get("vote_action") or "").upper()
            if va in ("LONG", "SHORT"):
                analysis_dir = va
            else:
                intended = (advice.get("intended_direction") or "").upper()
                if intended in ("LONG", "SHORT"):
                    analysis_dir = intended

    effective = get_effective_follow_direction(advice)
    ai_cfg = config.get("ai_trading") or {}
    require_gate = bool(ai_cfg.get("require_gate_pass", True))
    relaxed = use_relaxed_follow(config)
    passed = bool(advice.get("passed_gate", False))
    probe = bool(advice.get("learning_phase_probe") or advice.get("validation_probe"))
    val_probe = bool(advice.get("validation_probe"))

    will_follow = False
    follow_mode = "blocked"
    follow_reason = "无明确方向"
    if effective not in ("LONG", "SHORT"):
        follow_reason = "分析无 LONG/SHORT 方向"
    elif not auto_follow_enabled:
        follow_reason = "自动跟单未开启"
    elif probe:
        try:
            from bnb_quant_tool.validation_trading import is_validation_trading_enabled
            _von = is_validation_trading_enabled(config)
        except Exception:
            _von = False
        _bypass = bool((config.get("validation_trading") or {}).get("bypass_circuit_for_probe", True))
        hard_now = _learning_probe_hard_blocked(
            advice, validation_mode=_von, bypass_circuit=_bypass and _von
        )
        will_follow = not bool(hard_now)
        follow_mode = "validation_probe" if val_probe else "learning_probe"
        follow_reason = (
            f"验证试探跟单 {effective}（小仓验策略）"
            if val_probe and will_follow
            else (
                f"学习期试探跟单 {effective}"
                if will_follow
                else f"试探被硬拦: {hard_now}"
            )
        )
    elif require_gate and not passed:
        if relaxed and should_open_from_advice(advice, config):
            will_follow = True
            follow_mode = "relaxed"
            follow_reason = f"学习期宽松跟单 {effective}"
        else:
            follow_reason = "需过门控但未通过: " + "; ".join(
                (advice.get("gate_reasons") or ["未知"])[:2]
            )
    else:
        # require_gate=false 或已过门：仍统一走 should_open，避免验证模式全仓旁路
        if should_open_from_advice(advice, config):
            will_follow = True
            if action in ("LONG", "SHORT") and passed:
                follow_mode = "normal"
                follow_reason = "门控通过，按风控结论跟单"
            elif action in ("LONG", "SHORT") and not passed:
                follow_mode = "aggressive"
                follow_reason = "门控未过但已配置不要求门控，仍跟单"
            elif action == "WAIT" and effective in ("LONG", "SHORT"):
                follow_mode = "direction_follow" if not relaxed else "relaxed"
                follow_reason = (
                    f"风控建议观望，将按分析方向 {effective} 跟单"
                    + (" (宽松模式)" if relaxed else " (不要求门控)")
                )
        else:
            follow_reason = "验证/门控未允许开仓: " + "; ".join(
                (advice.get("gate_reasons") or ["需验证试探或过门"])[:2]
            )

    gate_label = "通过" if passed else "拦截"
    if probe:
        tag = "验证试探" if val_probe else "学习期试探"
        gate_label = f"{tag} · {effective}"
    elif not passed and will_follow and effective in ("LONG", "SHORT"):
        gate_label = f"拦截 · 仍将跟 {effective}"

    follow_label = (
        f"✓ 跟 {effective}" if will_follow else f"✗ 不跟 ({follow_reason[:28]})"
    )

    return {
        "analysis_direction": analysis_dir,
        "final_action": action,
        "effective_direction": effective,
        "passed_gate": passed,
        "gate_label": gate_label,
        "will_follow": will_follow,
        "follow_mode": follow_mode,
        "follow_reason": follow_reason,
        "follow_label": follow_label,
        "require_gate_pass": require_gate,
        "use_relaxed_follow": relaxed,
        "learning_phase_probe": bool(advice.get("learning_phase_probe")),
        "validation_probe": val_probe,
    }


def needs_relaxed_open(advice: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """open_from_advice 是否需 relaxed=True（action=WAIT 但按 raw 跟单）。"""
    action = (advice.get("action") or "").upper()
    effective = get_effective_follow_direction(advice)
    if action == "WAIT" and effective in ("LONG", "SHORT"):
        if should_open_from_advice(advice, config):
            return True
    is_probe = bool(
        advice.get("learning_phase_probe") or advice.get("validation_probe")
    )
    if is_probe and effective in ("LONG", "SHORT"):
        # 已恢复 action，通常不需要 relaxed；若仍 WAIT 则放开
        return action == "WAIT"
    # 验证模式禁止「未过门控却全仓 relaxed」；仅 probe/正常过门可开
    try:
        from bnb_quant_tool.validation_trading import is_validation_trading_enabled
        if is_validation_trading_enabled(config) and not advice.get("passed_gate"):
            return False
    except Exception:
        pass
    return use_relaxed_follow(config) and action == "WAIT" and effective in ("LONG", "SHORT")


def enrich_advice_execution_metadata(
    advice: Dict[str, Any],
    config: Dict[str, Any],
    *,
    auto_follow_enabled: bool = True,
) -> Dict[str, Any]:
    """写入 execution_context，并追加可读的执行层说明到报告。"""
    out = dict(advice or {})
    ctx = resolve_execution_context(out, config, auto_follow_enabled=auto_follow_enabled)
    out["execution_context"] = ctx
    out["effective_direction"] = ctx["effective_direction"]

    block = [
        "",
        "[执行层解读]",
        f"- 分析方向: {ctx['analysis_direction']}",
        f"- 风控结论: {ctx['final_action']} (门控: {ctx['gate_label']})",
        f"- 跟单状态: {ctx['follow_label']}",
        f"- 说明: {ctx['follow_reason']}",
    ]
    suffix = "\n".join(block)
    if out.get("report_text") and "[执行层解读]" not in out["report_text"]:
        out["report_text"] = out["report_text"] + suffix
    return out


def format_paper_stats_for_prompt(paper: Dict[str, Any]) -> str:
    if not paper or not paper.get("closed_trades"):
        return ""
    lines = [
        "",
        "【模拟盘真实交易绩效 — 这是你赚钱能力的真实反馈，务必参考】",
        f"已平仓: {paper['closed_trades']} 笔 | 胜率: {paper.get('win_rate', 0):.1%} | "
        f"累计PnL: {paper.get('total_pnl_usdt', 0):+.2f} USDT | 平均R: {paper.get('avg_r_multiple', 0):+.3f}",
        f"当前持仓: {paper.get('open_positions', 0)} | 连亏: {paper.get('consecutive_losses', 0)} 笔",
    ]
    recent = paper.get("recent_trades") or []
    if recent:
        lines.append("最近平仓:")
        for t in recent[:5]:
            pnl = float(t.get("realized_pnl_usdt") or 0)
            r = t.get("r_multiple")
            r_str = f"{float(r):+.2f}R" if r is not None else "-"
            lines.append(
                f"  #{t.get('id')} {t.get('side')} PnL={pnl:+.2f} {r_str} ({t.get('close_reason', '')})"
            )
    lines.append(
        "规则: 多开多平、错了就认 — 方向错尽快平仓(ADMIT_WRONG)，"
        "未止盈勿长期扛；每笔平仓都是学习样本，胜率靠复盘抬升而非怕错停手。"
        "连亏时缩小仓位继续验证，不要用长期观望逃避标签。"
    )
    lines.append("")
    return "\n".join(lines)
