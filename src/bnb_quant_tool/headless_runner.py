"""
无 GUI 分析执行器 — 供 autopilot_daemon / Web 调度使用。

与 GUI 全自动链路对齐：拉数 → 机构策略 → AI → 信念 → build_advice → 后处理门控 → 可选模拟开仓。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HeadlessAnalysisRunner:
    """不依赖 Tkinter 的完整分析周期。"""

    def __init__(self, project_root: str, config: Optional[Dict] = None):
        from bnb_quant_tool.data_localization import init_workspace

        self.project_root = Path(project_root)
        init_workspace(str(self.project_root))
        self.config = config or self._load_config()
        if isinstance(self.config, dict) and not self.config.get("_config_path"):
            self.config["_config_path"] = str((self.project_root / "config.yaml").resolve())
        self._init_engines()

    def _load_config(self) -> Dict:
        from bnb_quant_tool.config_access import load_app_config
        return load_app_config(self.project_root / "config.yaml")

    def _init_engines(self) -> None:
        from bnb_quant_tool.ai_learning_system import AILearningSystem
        from bnb_quant_tool.config_access import build_data_fetcher, build_trade_advisor_config
        from bnb_quant_tool.institutional_strategies import InstitutionalStrategies
        from bnb_quant_tool.market_regime import MarketRegimeDetector
        from bnb_quant_tool.multi_timeframe import MultiTimeframeAnalyzer
        from bnb_quant_tool.market_sentiment import MarketSentiment
        from bnb_quant_tool.news_collector import NewsCollector
        from bnb_quant_tool.paper_trading import PaperTradingEngine
        from bnb_quant_tool.pattern_memory import PatternMemory
        from bnb_quant_tool.technical_indicators import TechnicalIndicators
        from bnb_quant_tool.trade_advisor import TradeAdvisor
        from bnb_quant_tool.dynamic_position import DynamicPositionSizer
        from bnb_quant_tool.ai_analyzer import DeepSeekAnalyzer
        from bnb_quant_tool.factor_attribution_learner import compute_reliability_multipliers
        from bnb_quant_tool.ai_review_engine import AIReviewEngine

        try:
            from bnb_quant_tool.trading_profile import apply_trading_profile
            self.config = apply_trading_profile(self.config or {})
        except Exception:
            pass
        try:
            from bnb_quant_tool.knowledge_hygiene import sanitize_hold_ban_cards
            sanitize_hold_ban_cards()
        except Exception as e:
            logger.debug("knowledge hygiene: %s", e)

        cfg = self.config
        self.learner = AILearningSystem(config=cfg)
        self.inst_strategies = InstitutionalStrategies(config=cfg)
        try:
            from bnb_quant_tool.strategy_pool import register_strategy_pool
            register_strategy_pool(self.inst_strategies)
        except Exception as e:
            logger.debug("register strategy pool: %s", e)
        self.trade_advisor = TradeAdvisor(build_trade_advisor_config(cfg))
        self.trade_advisor.set_structural_config(cfg.get("structural_strategies") or {})
        self.fetcher = build_data_fetcher(cfg)
        self.mtf_analyzer = MultiTimeframeAnalyzer(fetcher=self.fetcher)
        self.sentiment_engine = MarketSentiment()
        self.regime_detector = MarketRegimeDetector(cfg.get("market_regime") or {})
        from bnb_quant_tool.config_access import build_position_sizer_config
        self.position_sizer = DynamicPositionSizer(build_position_sizer_config(cfg))

        onchain_cfg = cfg.get("onchain") or {}
        if onchain_cfg.get("enabled", True):
            from bnb_quant_tool.onchain_analysis import OnChainAnalyzer
            self.onchain_analyzer = OnChainAnalyzer(
                glassnode_api_key=onchain_cfg.get("glassnode_api_key"),
                use_coinmetrics_fallback=bool(onchain_cfg.get("use_coinmetrics_fallback", True)),
                cache_seconds=int(onchain_cfg.get("cache_seconds", 900)),
            )
        else:
            self.onchain_analyzer = None

        macro_cfg = cfg.get("macro") or {}
        if macro_cfg.get("enabled", True):
            from bnb_quant_tool.macro_data import MacroDataLayer
            self.macro_layer = MacroDataLayer(
                symbols=macro_cfg.get("symbols"),
                correlation_lookback_days=int(macro_cfg.get("correlation_lookback_days", 30)),
            )
        else:
            self.macro_layer = None

        bnb_cfg = dict(cfg.get("bnb_factors") or {})
        bnb_cfg["event_calendar"] = cfg.get("bnb_event_calendar") or {}
        bnb_cfg["risk_sentry"] = cfg.get("bnb_risk_sentry") or {}
        if bnb_cfg.get("enabled", True):
            from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors
            self.bnb_factors_engine = BNBSpecificFactors(
                fetcher=self.fetcher, config=bnb_cfg,
            )
        else:
            self.bnb_factors_engine = None

        news_cfg = cfg.get("news") or {}
        self.news_collector = NewsCollector(
            blockbeats_api_key=news_cfg.get("blockbeats_api_key"),
            blockbeats_lang=news_cfg.get("blockbeats_lang", "cn"),
            blockbeats_size=int(news_cfg.get("blockbeats_size", 50)),
            blockbeats_cache_seconds=int(news_cfg.get("blockbeats_cache_seconds", 86400)),
            rss_cache_seconds=int(news_cfg.get("rss_cache_seconds", 1800)),
            cache_dir=news_cfg.get("cache_dir", "data/news_cache"),
            tikhub_config=news_cfg.get("tikhub") or {},
            odaily_config=news_cfg.get("odaily") or {},
        )
        self.news_collector._incremental_fetch = bool(
            news_cfg.get("incremental_fetch", True)
        )

        from bnb_quant_tool.llm_provider import get_llm_credentials
        llm_creds = get_llm_credentials(cfg)
        review = AIReviewEngine(
            config=cfg,
            deepseek_api_key=llm_creds["api_key"],
            deepseek_model=llm_creds["model"],
            deepseek_base_url=llm_creds["base_url"],
        )
        paper_db = (cfg.get("paper_trading") or {}).get("db_path")
        self.paper_engine = PaperTradingEngine(
            db_path=paper_db, config=cfg, ai_review_engine=review,
        )
        self.paper_engine.set_trade_advisor(self.trade_advisor)
        self.trade_advisor.set_paper_engine(self.paper_engine)
        # 无头/autopilot 平仓必须接上学习管道，否则「每笔必学」静默失效
        self.paper_engine.set_learner(self.learner)
        self.paper_engine.set_price_provider(
            lambda symbol: self.fetcher.get_price_with_fallback(symbol)
        )

        cb_cfg = cfg.get("circuit_breaker") or {}
        if cb_cfg.get("enabled", True):
            from bnb_quant_tool.circuit_breaker import CircuitBreaker
            self.circuit_breaker = CircuitBreaker(
                paper_engine=self.paper_engine, config=cb_cfg,
            )
            self.circuit_breaker.account_balance = float(
                (cfg.get("trading") or {}).get("account_balance", 5000)
            )
            self.trade_advisor.set_circuit_breaker(self.circuit_breaker)
        else:
            self.circuit_breaker = None
            self.trade_advisor.set_circuit_breaker(None)

        self.pattern_memory = PatternMemory(
            paper_db_path=self.paper_engine.db_path,
            learning_db_path=self.learner.db_path,
        )

        try:
            from bnb_quant_tool.counterfactual_analyzer import CounterfactualAnalyzer
            self.counterfactual = CounterfactualAnalyzer(fetcher=self.fetcher)
        except Exception:
            self.counterfactual = None

        try:
            from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
            self.learning_evolution = LearningEvolutionCoordinator(
                self.learner,
                capability_memory=self.learner.capability_memory,
                counterfactual=self.counterfactual,
                config=cfg,
            )
        except Exception:
            self.learning_evolution = None

        self.paper_engine.set_learning_pipeline_deps(
            counterfactual=self.counterfactual,
            pattern_memory=self.pattern_memory,
            evolution=self.learning_evolution,
            on_status=lambda msg: logger.info("%s", msg),
        )

        try:
            from bnb_quant_tool.indicator_explorer import IndicatorExplorer
            explorer = IndicatorExplorer(
                config_path=str(self.project_root / "config.yaml"),
                learning_db_path=self.learner.db_path,
            )
            self.trade_advisor.set_indicator_explorer(explorer)
        except Exception as e:
            logger.debug("indicator explorer: %s", e)

        self._compute_reliability = compute_reliability_multipliers
        self._technical = TechnicalIndicators
        self._deepseek_cls = DeepSeekAnalyzer
        from bnb_quant_tool.decision_explainer import DecisionExplainer
        self.decision_explainer = DecisionExplainer()

        ma_cfg = cfg.get("multi_agent") or {}
        self.multi_agent = None
        if ma_cfg.get("enabled", True):
            try:
                from bnb_quant_tool.agents import MultiAgentOrchestrator
                self.multi_agent = MultiAgentOrchestrator(
                    config=cfg,
                    news_collector=self.news_collector,
                    sentiment_engine=self.sentiment_engine,
                    onchain_analyzer=self.onchain_analyzer,
                    macro_layer=self.macro_layer,
                    pattern_memory=self.pattern_memory,
                    paper_engine=self.paper_engine,
                    project_root=str(self.project_root),
                )
            except Exception as e:
                logger.debug("multi_agent init: %s", e)

    def _apply_multi_agent_deliberation(
        self,
        trade_advice: Dict[str, Any],
        *,
        symbol: str,
        timeframe: str,
        current_price: float,
        indicators: Dict,
        ai_analysis: Dict,
        inst_results: Dict,
        bg_results: Dict,
        market_regime: Dict,
        learning_context: Dict,
        pattern_insight: Optional[Dict] = None,
        execute: bool = False,
        learning_record_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.multi_agent:
            return trade_advice

        from bnb_quant_tool.agents import MarketContext

        trading = self.config.get("trading") or {}
        equity = float(trading.get("account_balance", 5000))
        advice_copy = dict(trade_advice)
        advice_copy["open_positions"] = len(self.paper_engine.get_open_positions())
        advice_copy["open_positions_list"] = self.paper_engine.get_open_positions()
        advice_copy["total_realized_pnl"] = self.paper_engine.get_total_realized_pnl()

        ctx = MarketContext(
            symbol=symbol,
            timeframe=timeframe,
            current_price=current_price,
            indicators=indicators,
            trade_advice=advice_copy,
            ai_analysis=ai_analysis,
            institutional=inst_results,
            multi_timeframe=bg_results.get("mtf") or trade_advice.get("multi_timeframe"),
            sentiment=bg_results.get("sentiment") or trade_advice.get("sentiment"),
            news_summary=bg_results.get("news_summary"),
            news_items=bg_results.get("news_items"),
            onchain=bg_results.get("onchain") or trade_advice.get("onchain"),
            macro=bg_results.get("macro") or trade_advice.get("macro"),
            bnb_factors=bg_results.get("bnb_factors") or trade_advice.get("bnb_factors"),
            market_regime=market_regime,
            learning_insights=learning_context,
            pattern_insight=pattern_insight or trade_advice.get("pattern_insight"),
        )

        deliberation = self.multi_agent.deliberate(
            ctx,
            execute=execute,
            learning_record_id=learning_record_id,
            equity_usdt=equity,
        )
        trade_advice = self.multi_agent.apply_to_advice(trade_advice, deliberation)
        trade_advice["multi_agent_deliberation"] = deliberation.to_dict()

        if deliberation.risk_verdict.vetoed:
            trade_advice["passed_gate"] = False
        if deliberation.transcript:
            trade_advice.setdefault("report_text", "")
            trade_advice["report_text"] += "\n\n" + deliberation.transcript
        if deliberation.trading_result and deliberation.trading_result.executed:
            trade_advice["_multi_agent_position_id"] = deliberation.trading_result.position_id

        return trade_advice

    def run_cycle(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        days: Optional[int] = None,
        leverage: Optional[int] = None,
        open_paper: Optional[bool] = None,
        analysis_mode: str = "all",
    ) -> Dict[str, Any]:
        """执行一轮完整分析，可选自动模拟开仓。"""
        cfg = self.config
        try:
            from bnb_quant_tool.strategy_pool import maybe_reload_from_signal

            rel = maybe_reload_from_signal(project_root=Path(self.project_root))
            if rel.get("ok") and not rel.get("skipped"):
                logger.info(
                    "策略池信号热加载: n=%s ver=%s",
                    rel.get("reloaded"),
                    rel.get("version"),
                )
        except Exception as e:
            logger.debug("strategy pool signal reload: %s", e)

        trading = cfg.get("trading") or {}
        symbol = symbol or trading.get("symbol", "BNBUSDT")
        timeframe = timeframe or trading.get("timeframe", "1h")
        days = int(days or trading.get("lookback_days", 30))
        leverage = int(leverage or trading.get("leverage", 1))
        account_balance = float(trading.get("account_balance", 5000))
        self.trade_advisor.account_balance = account_balance

        if open_paper is None:
            ap = cfg.get("autopilot") or {}
            pt = cfg.get("paper_trading") or {}
            open_paper = bool(ap.get("open_paper_on_fullauto", pt.get("auto_follow", True)))

        from bnb_quant_tool.llm_provider import (
            build_llm_analyzer_for,
            get_llm_credentials,
            list_analyzer_providers,
        )
        from bnb_quant_tool.analysis_reuse import (
            evaluate_analysis_reuse,
            run_market_analyses_with_reuse,
        )
        providers = list_analyzer_providers(cfg)
        if not providers:
            return {"ok": False, "error": "未配置 LLM api_key（qianwen / deepseek / volcengine）"}
        llm_creds = get_llm_credentials(cfg, provider=providers[0], fallback=False)
        api_key = llm_creds["api_key"]
        if not api_key:
            return {"ok": False, "error": "未配置 LLM api_key（qianwen / deepseek / volcengine）"}

        df = self.fetcher.get_historical_klines(
            symbol=symbol, interval=timeframe, start_str=f"{days} days ago",
        )
        if df is None or len(df) == 0:
            return {"ok": False, "error": "获取K线失败"}

        current_price = float(self.fetcher.resolve_current_price(symbol, df) or 0)
        if current_price <= 0:
            return {"ok": False, "error": "无法获取有效现价（ticker 失败且 K 线过旧）"}

        indicators = self._technical.calculate_all_indicators(df)

        # 智能闭环：预热记忆，带着经验进入本轮感知/决策
        from bnb_quant_tool.intelligence_loop import get_or_create_loop

        loop = get_or_create_loop(
            self,
            learner=self.learner,
            config=cfg,
            paper_engine=self.paper_engine,
            pattern_memory=self.pattern_memory,
            counterfactual=self.counterfactual,
            project_root=str(self.project_root),
        )
        cycle = loop.begin_cycle(symbol)
        preflight = loop.preflight(symbol=symbol, current_price=current_price)
        cycle.notes.append(f"preflight={preflight.get('steps')}")
        loop.mark_perceive(
            cycle,
            has_klines=len(df),
            price=current_price,
        )

        mtf_result = {}
        sentiment = {}
        onchain_data = {}
        macro_data = {}
        bnb_factors_data = {}
        news_summary = {}
        news_items = []
        orderflow_data = {}

        try:
            mtf_result = self.mtf_analyzer.analyze(symbol=symbol) or {}
        except Exception as e:
            logger.debug("mtf: %s", e)
        try:
            sentiment = self.sentiment_engine.fetch_all(symbol=symbol) or {}
        except Exception as e:
            logger.debug("sentiment: %s", e)
        try:
            of_cfg = (cfg.get("orderflow") or {})
            if of_cfg.get("enabled", True):
                from bnb_quant_tool.orderflow_signal import fetch_orderflow
                orderflow_data = fetch_orderflow(symbol, cfg) or {}
        except Exception as e:
            logger.debug("orderflow: %s", e)
        if self.onchain_analyzer:
            try:
                onchain_data = self.onchain_analyzer.fetch_all(symbol=symbol) or {}
            except Exception as e:
                logger.debug("onchain: %s", e)
            try:
                from bnb_quant_tool.onchain_lead_lag import lead_lag_score

                px_chg = 0.0
                try:
                    if df is not None and len(df) >= 2:
                        c0 = float(df["close"].iloc[-2])
                        c1 = float(df["close"].iloc[-1])
                        if c0 > 0:
                            px_chg = (c1 - c0) / c0 * 100.0
                except Exception:
                    px_chg = 0.0
                ll = lead_lag_score(
                    onchain_data,
                    price_change_pct=px_chg,
                    config=cfg,
                )
                onchain_data["lead_lag"] = ll
                # 映射到 onchain_score（-1~1）供 trade_advisor 缩仓
                if ll.get("enabled") is not False:
                    score01 = float(ll.get("score") or 0) / 100.0
                    prev = float(onchain_data.get("onchain_score") or 0)
                    w = float(ll.get("as_factor_weight") or 0.12)
                    onchain_data["onchain_score"] = round(
                        max(-1.0, min(1.0, prev * (1 - w) + score01 * (0.5 + w))),
                        4,
                    )
            except Exception as e:
                logger.debug("onchain lead_lag: %s", e)
        if self.macro_layer:
            try:
                macro_data = self.macro_layer.fetch_all() or {}
            except Exception as e:
                logger.debug("macro: %s", e)

        from bnb_quant_tool.llm_provider import build_llm_analyzer_for
        ai_analyzer, _ = build_llm_analyzer_for(cfg, providers[0])

        # 先算 regime，尽早判断能否知识复用（避免新闻摘要+主分析+议会空烧）
        market_regime = self.regime_detector.detect(
            df,
            indicators=indicators,
            news_summary={},
            sentiment=sentiment or None,
            bnb_factors=None,
        )
        reuse_hit = evaluate_analysis_reuse(
            config=cfg,
            symbol=symbol,
            indicators=indicators,
            market_regime=market_regime,
            learner=self.learner,
        )

        try:
            news_items = self.news_collector.collect(
                symbol=symbol.replace("USDT", ""), hours=24, max_items=30,
            )
            if news_items and not reuse_hit:
                news_summary = ai_analyzer.summarize_news(
                    news_items, symbol=symbol.replace("USDT", ""),
                ) or {}
                from bnb_quant_tool.news_decay import adjust_news_summary
                news_summary = adjust_news_summary(news_summary, news_items)
            elif news_items and reuse_hit:
                news_summary = {
                    "summary": "知识复用轮次：跳过新闻 LLM 摘要",
                    "polarity": "neutral",
                    "confidence": 0.0,
                    "bias": "NEUTRAL",
                    "_reused_skip": True,
                }
        except Exception as e:
            logger.debug("news: %s", e)

        if self.bnb_factors_engine:
            try:
                bnb_factors_data = self.bnb_factors_engine.fetch_all(
                    symbol=symbol, news_items=news_items or None,
                ) or {}
                # BTC lead 是通用领先指标，非 BNB 对仍可计算（不启用 BNB 门控）
                if symbol != "BTCUSDT":
                    from bnb_quant_tool.btc_lead_indicator import compute_btc_lead_indicator
                    btc_df = self.fetcher.get_historical_klines(
                        symbol="BTCUSDT", interval=timeframe, start_str=f"{days} days ago",
                    )
                    bnb_factors_data = dict(bnb_factors_data)
                    bnb_factors_data["btc_lead"] = compute_btc_lead_indicator(
                        df, btc_df, config=cfg.get("btc_lead") or {},
                    )
                if (
                    not bnb_factors_data.get("skipped")
                    and isinstance(onchain_data, dict)
                    and onchain_data.get("lead_lag")
                ):
                    bnb_factors_data["onchain_lead_lag"] = onchain_data["lead_lag"]
                # 新闻可信度写入 news_summary（仅 BNB 专属因子启用时）
                nc = bnb_factors_data.get("news_credibility") or {}
                if (
                    not bnb_factors_data.get("skipped")
                    and news_summary is not None
                    and isinstance(nc, dict)
                    and nc
                ):
                    if not isinstance(news_summary, dict):
                        news_summary = {}
                    news_summary = dict(news_summary)
                    cs = float(nc.get("cred_score") or 0.55)
                    news_summary["cred_score"] = cs
                    news_summary["credibility"] = cs
                    if nc.get("regime_impact"):
                        news_summary["news_cred_regime"] = nc.get("regime_impact")
            except Exception as e:
                logger.debug("bnb_factors: %s", e)

        market_regime = self.regime_detector.detect(
            df,
            indicators=indicators,
            news_summary=news_summary or {},
            sentiment=sentiment or None,
            bnb_factors=bnb_factors_data or None,
        )
        try:
            if self.paper_engine and hasattr(self.paper_engine, "set_atr_ratio"):
                self.paper_engine.set_atr_ratio(
                    float(market_regime.get("atr_ratio") or 1.0)
                )
        except Exception:
            pass

        from bnb_quant_tool.ai_trading_context import (
            analysis_extra_market,
            build_full_analysis_learning_context,
        )
        from bnb_quant_tool.institutional_conviction import compute_institutional_conviction
        from bnb_quant_tool.analysis_pipeline import (
            apply_post_advice_gates,
            apply_dynamic_position,
            attach_decision_explanation,
        )

        learning_context = build_full_analysis_learning_context(
            self.learner,
            symbol=symbol,
            current_price=current_price,
            indicators=indicators,
            regime=market_regime.get("regime"),
            paper_engine=self.paper_engine,
            pattern_memory=self.pattern_memory,
            counterfactual=self.counterfactual,
            news_summary=news_summary or None,
            extra_market=analysis_extra_market(
                self.config,
                news_polarity=(news_summary or {}).get("polarity"),
                mtf_action=(mtf_result or {}).get("recommended_action"),
            ),
        )
        learn_weights = learning_context.get("strategy_weights") or {}
        strat_perf = learning_context.get("strategy_performance") or {}
        sw_cfg = (self.config or {}).get("win_rate_strategy")
        inst_results = self.inst_strategies.run_all_strategies(
            df,
            learning_weights=learn_weights if learn_weights else None,
            regime_multipliers=market_regime.get("strategy_multipliers"),
            strategy_performance=strat_perf if strat_perf else None,
            win_rate_strategy_cfg=sw_cfg,
        )
        learning_context = build_full_analysis_learning_context(
            self.learner,
            symbol=symbol,
            current_price=current_price,
            indicators=indicators,
            regime=market_regime.get("regime"),
            paper_engine=self.paper_engine,
            pattern_memory=self.pattern_memory,
            counterfactual=self.counterfactual,
            inst_results=inst_results,
            news_summary=news_summary or None,
            extra_market=analysis_extra_market(
                self.config,
                news_polarity=(news_summary or {}).get("polarity"),
                mtf_action=(mtf_result or {}).get("recommended_action"),
            ),
        )

        # 终态 regime 下再确认一次；命中则跳过主分析 LLM（进步：同局面不重烧）
        if not reuse_hit:
            reuse_hit = evaluate_analysis_reuse(
                config=cfg,
                symbol=symbol,
                indicators=indicators,
                market_regime=market_regime,
                learner=self.learner,
            )
        if reuse_hit and reuse_hit.reuse:
            ai_bundle = reuse_hit.to_ai_bundle()
            # 强化放到 record_analysis → _after_analysis_growth，保证「每分析必学」统一入口
        else:
            ai_bundle = run_market_analyses_with_reuse(
                cfg,
                df,
                indicators,
                symbol=symbol,
                market_regime=market_regime,
                learner=self.learner,
                learning_context=learning_context,
                onchain_context=onchain_data or None,
                macro_context=macro_data or None,
                bnb_factors_context=bnb_factors_data or None,
                bnb_factors=bnb_factors_data or None,
                news_summary=news_summary or None,
                atr_ratio=float((market_regime or {}).get("atr_ratio") or 1.0),
                multi_timeframe=mtf_result or None,
            )
            # with_reuse 内若命中也会 reinforce；非复用路径无强化，由 growth 写快照
        ai_analysis = ai_bundle["primary"]
        ai_analyses = ai_bundle.get("by_provider") or {}
        ai_analysis_note = ai_bundle.get("note") or ""
        # 供 growth 强化使用的局面上下文
        if isinstance(ai_analysis, dict):
            ai_analysis.setdefault("_situation_indicators", indicators)
            ai_analysis.setdefault("_situation_regime", market_regime)
            if reuse_hit and reuse_hit.reuse:
                ai_analysis.setdefault("_reuse_hit_meta", {
                    "source": reuse_hit.source,
                    "source_id": reuse_hit.source_id,
                    "situation_key": reuse_hit.situation_key,
                    "action": reuse_hit.action,
                    "confidence": reuse_hit.confidence,
                    "similarity": reuse_hit.similarity,
                    "reason": reuse_hit.reason,
                })
            elif ai_analysis.get("_reused"):
                ai_analysis.setdefault("_reuse_hit_meta", {
                    "source": ai_analysis.get("_reuse_source"),
                    "source_id": ai_analysis.get("_reuse_source_id"),
                    "situation_key": "",
                    "action": ai_analysis.get("trade_suggestion") or "WAIT",
                    "confidence": ai_analysis.get("confidence") or 0.4,
                    "similarity": ai_analysis.get("_reuse_similarity") or 0.9,
                    "reason": ai_analysis.get("_reuse_reason") or "知识复用",
                })

        learning_context = build_full_analysis_learning_context(
            self.learner,
            symbol=symbol,
            current_price=current_price,
            indicators=indicators,
            regime=market_regime.get("regime"),
            paper_engine=self.paper_engine,
            pattern_memory=self.pattern_memory,
            counterfactual=self.counterfactual,
            inst_results=inst_results,
            ai_analysis=ai_analysis,
            news_summary=news_summary or None,
            extra_market=analysis_extra_market(
                self.config,
                news_polarity=(news_summary or {}).get("polarity"),
                mtf_action=(mtf_result or {}).get("recommended_action"),
            ),
        )
        factor_rel = (
            learning_context.get("factor_reliability")
            or self._compute_reliability(learning_context.get("factor_attribution"))
        )

        institutional_conviction = compute_institutional_conviction(
            inst_results=inst_results,
            market_regime=market_regime,
            indicators=indicators,
            sentiment=sentiment or None,
            onchain=onchain_data or None,
            macro=macro_data or None,
            bnb_factors=bnb_factors_data or None,
            mtf=mtf_result or None,
            learning_insights=learning_context,
            btc_lead=(bnb_factors_data or {}).get("btc_lead"),
            orderflow=orderflow_data or None,
        )
        # 注入投票加权（与 GUI 路径对齐；勿仅挂到最终 advice 展示）
        if isinstance(learning_context, dict):
            learning_context = dict(learning_context)
            learning_context["institutional_conviction"] = institutional_conviction
            learning_context["onchain"] = onchain_data or {}
            learning_context["macro"] = macro_data or {}
            learning_context["market_regime"] = market_regime
            learning_context["news_summary"] = news_summary or {}
            learning_context["_learner"] = self.learner
            learning_context["_paper_engine"] = self.paper_engine

        trade_advice = self.trade_advisor.build_advice(
            symbol=symbol,
            timeframe=timeframe,
            current_price=current_price,
            indicators=indicators,
            ai_analysis=ai_analysis,
            institutional=inst_results,
            learning_insights=learning_context,
            multi_timeframe=mtf_result or None,
            sentiment=sentiment or None,
            news_summary=news_summary or None,
            market_regime=market_regime,
            onchain=onchain_data or None,
            macro=macro_data or None,
            bnb_factors=bnb_factors_data or None,
            leverage=leverage,
            factor_reliability=factor_rel,
            analysis_mode=analysis_mode,
        )
        try:
            from bnb_quant_tool.signal_scanner import inject_scanner_into_advice
            inject_scanner_into_advice(trade_advice, symbol=symbol)
        except Exception as se:
            logger.debug("scanner inject: %s", se)
        if ai_analysis.get("_reused") or ai_bundle.get("reused"):
            trade_advice["_reused"] = True
            trade_advice["_reuse_reason"] = (
                ai_analysis.get("_reuse_reason") or ai_bundle.get("note") or "知识复用"
            )

        bg_pack = {
            "mtf": mtf_result,
            "sentiment": sentiment,
            "onchain": onchain_data,
            "macro": macro_data,
            "bnb_factors": bnb_factors_data,
            "news_summary": news_summary,
            "news_items": news_items,
            "orderflow": orderflow_data,
        }
        pattern_insight = learning_context.get("pattern_memory")
        ma_cfg = cfg.get("multi_agent") or {}

        trade_advice = apply_post_advice_gates(
            trade_advice,
            learning_context=learning_context,
            config=cfg,
            sentiment=sentiment,
            bnb_factors=bnb_factors_data,
            pattern_memory=self.pattern_memory,
            pattern_insight=pattern_insight,
            indicators=indicators,
            ai_analysis=ai_analysis,
            inst_results=inst_results,
            current_price=current_price,
            news_summary=news_summary,
            onchain=onchain_data or None,
            macro=macro_data or None,
            multi_agent_fn=self._apply_multi_agent_deliberation if self.multi_agent else None,
            multi_agent_kwargs={
                "symbol": symbol,
                "timeframe": timeframe,
                "current_price": current_price,
                "indicators": indicators,
                "ai_analysis": ai_analysis,
                "inst_results": inst_results,
                "bg_results": bg_pack,
                "market_regime": market_regime,
                "learning_context": learning_context,
                "pattern_insight": pattern_insight,
                # 开仓统一在 record_analysis 之后走 open_from_advice，保证 learning_record_id
                "execute": False,
            },
        )
        trade_advice["institutional_conviction"] = institutional_conviction

        trade_advice = apply_dynamic_position(
            trade_advice,
            position_sizer=self.position_sizer,
            regime_detector=self.regime_detector,
            df=df,
            indicators=indicators,
            market_regime=market_regime,
            multi_timeframe=mtf_result,
            news_summary=news_summary,
            config=cfg,
        )

        trade_advice = attach_decision_explanation(
            trade_advice,
            self.decision_explainer,
            indicators=indicators,
            ai_analysis=ai_analysis,
            institutional=inst_results,
            learning_insights=learning_context,
            multi_timeframe=mtf_result or None,
            sentiment=sentiment or None,
            news_summary=news_summary or None,
            bnb_factors=bnb_factors_data or None,
            factor_reliability=factor_rel,
        )

        record_id = self.learner.record_analysis({
            "symbol": symbol,
            "timeframe": timeframe,
            "current_price": current_price,
            "indicators": indicators,
            "ai_analysis": ai_analysis,
            "ai_analyses": ai_analyses,
            "ai_analysis_note": ai_analysis_note,
            "institutional_strategies": inst_results,
            "trade_advice": trade_advice,
            "market_regime": market_regime,
            "news_summary": news_summary,
            "multi_timeframe": mtf_result,
            "learning_context": learning_context,
            "final_recommendation": (
                "BUY" if trade_advice.get("action") == "LONG"
                else ("SELL" if trade_advice.get("action") == "SHORT" else "HOLD")
            ),
            "timestamp": datetime.now().isoformat(),
        })

        if record_id:
            try:
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
                ev = LearningEvolutionCoordinator(
                    self.learner,
                    capability_memory=self.learner.capability_memory,
                    counterfactual=self.counterfactual,
                    config=cfg,
                )
                ev.on_analysis_recorded(
                    int(record_id),
                    learning_context,
                    trade_advice=trade_advice,
                    ai_analysis=ai_analysis,
                )
            except Exception as ev_e:
                logger.debug("on_analysis_recorded: %s", ev_e)

            # 议会投票绑定统一由 loop.after_analysis 完成（防串票 / 防重复）
            try:
                loop.after_analysis(
                    cycle,
                    record_id=int(record_id),
                    action=str(trade_advice.get("action") or "WAIT"),
                    reused=bool(
                        (ai_analysis or {}).get("_reused")
                        or trade_advice.get("_reused")
                        or trade_advice.get("_skipped_council_reuse")
                    ),
                    learning_context=learning_context,
                    config=cfg,
                    project_root=str(self.project_root),
                )
            except Exception as le:
                logger.debug("intelligence_loop after_analysis: %s", le)

        try:
            regime_tag = (market_regime or {}).get("regime", "")
            sid = self.paper_engine.track_signal(trade_advice, market_regime=regime_tag)
            trade_advice["_signal_tracking_id"] = sid
        except Exception as e:
            logger.debug("track_signal: %s", e)

        position_id = trade_advice.get("_multi_agent_position_id")
        if position_id is None:
            from bnb_quant_tool.ai_trading_context import should_open_from_advice, needs_relaxed_open
            from bnb_quant_tool.config_access import is_margin_insufficient

            if open_paper and should_open_from_advice(trade_advice, cfg):
                margin_required = float(
                    (trade_advice.get("position") or {}).get("margin_required") or 0
                )
                if is_margin_insufficient(
                    account_balance,
                    self.paper_engine.get_open_positions(),
                    margin_required,
                    total_realized_pnl=self.paper_engine.get_total_realized_pnl(),
                ):
                    logger.info("headless: 保证金不足，跳过开仓")
                else:
                    position_id = self.paper_engine.open_from_advice(
                        trade_advice,
                        equity_usdt=account_balance,
                        learning_record_id=record_id,
                        relaxed=needs_relaxed_open(trade_advice, cfg),
                    )
                    if position_id and not self.paper_engine.is_watching():
                        poll = float((cfg.get("paper_trading") or {}).get("poll_interval", 15))
                        self.paper_engine.start_watcher(interval=poll)

        try:
            loop.mark_execute(
                cycle,
                position_id=int(position_id) if position_id else None,
                opened=bool(position_id),
            )
        except Exception:
            pass

        funnel = {}
        try:
            from bnb_quant_tool.trading_profile import record_decision_funnel

            funnel = record_decision_funnel(
                analysis_triggered=True,
                gate_passed=bool(trade_advice.get("passed_gate")),
                opened=bool(position_id),
                source="headless",
                symbol=symbol,
                config=cfg,
            )
        except Exception as fe:
            logger.debug("decision_funnel: %s", fe)

        scoreboard = {}
        try:
            from bnb_quant_tool.scoreboard import publish_scoreboard

            scoreboard = publish_scoreboard(
                paper_engine=self.paper_engine,
                config=cfg,
                orderflow=orderflow_data or None,
            )
        except Exception as se:
            logger.debug("scoreboard: %s", se)

        return {
            "ok": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "price": current_price,
            "action": trade_advice.get("action"),
            "raw_action": trade_advice.get("raw_action"),
            "effective_direction": (trade_advice.get("execution_context") or {}).get(
                "effective_direction"
            ) or trade_advice.get("raw_action"),
            "passed_gate": trade_advice.get("passed_gate"),
            "block_reason": trade_advice.get("block_reason"),
            "blockers": trade_advice.get("blockers"),
            "decision_state": trade_advice.get("decision_state"),
            "confidence": trade_advice.get("confidence"),
            "gate_reasons": trade_advice.get("gate_reasons"),
            "execution_context": trade_advice.get("execution_context"),
            "record_id": record_id,
            "position_id": position_id,
            "trade_advice": trade_advice,
            "market_regime": market_regime,
            "intelligence_loop": cycle.to_dict() if cycle else {},
            "loop_health": loop.get_loop_health(symbol=symbol),
            "decision_funnel": funnel,
            "scoreboard": scoreboard,
            "orderflow": orderflow_data or {},
            "experience_injected": bool(
                (learning_context or {}).get("experience_injected")
            ),
        }
