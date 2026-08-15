"""Mixin: AnalysisMixin"""

from gui._imports import *


class AnalysisMixin:
    def start_fresh_analysis(self):
        """强制全新分析：跳过知识复用，必跑 LLM。"""
        self.start_analysis(force_fresh=True)

    def start_analysis(self, force_fresh: bool = False):
        busy = False
        if hasattr(self, "_analysis_busy"):
            busy = bool(self._analysis_busy())
        else:
            busy = bool(getattr(self, "_analysis_running", False)) or (
                getattr(self, "analysis_thread", None) is not None
                and self.analysis_thread.is_alive()
            )
        if busy:
            self.update_status("上次分析仍在进行，跳过重复触发")
            return
        try:
            from bnb_quant_tool.strategy_pool import maybe_reload_from_signal

            maybe_reload_from_signal(project_root=PROJECT_ROOT)
        except Exception:
            pass
        self._force_fresh_analysis = bool(force_fresh)
        self.analyze_btn.config(state='disabled')
        if getattr(self, "fresh_analyze_btn", None):
            self.fresh_analyze_btn.config(state='disabled')
        self.feedback_btn.config(state='disabled')
        mode = "全新分析(不复用)" if force_fresh else "AI 分析"
        self.status_var.set(f"正在 {mode}... | 学习闭环: ON")
        self.progress.start(10)
        self._analysis_started_ts = time.time()
        self.analysis_thread = threading.Thread(target=self.run_analysis, daemon=True)
        self.analysis_thread.start()
        self.root.after(100, self.check_thread)

    def run_analysis(self):
        if getattr(self, "_analysis_running", False):
            self.update_status("上次分析仍在进行，跳过重复触发")
            return
        self._analysis_running = True
        force_fresh = bool(getattr(self, "_force_fresh_analysis", False))
        self._force_fresh_analysis = False
        self._analysis_started_ts = time.time()
        try:
            symbol = self.symbol_var.get()
            timeframe = self.timeframe_var.get()
            days = int(self.days_var.get())
            strategy_mode = self.strategy_var.get()
            # 读取本金设置，更新 TradeAdvisor
            try:
                account_balance = float(self.balance_var.get())
            except (ValueError, TypeError):
                account_balance = float(self.config.get('trading', {}).get('account_balance', 5000))
            self.trade_advisor.account_balance = account_balance
            leverage = int(self.leverage_var.get())

            fresh_tag = " [全新·不复用]" if force_fresh else ""
            self.update_status(
                f"分析 {symbol} ({timeframe}, {days}d) 本金={account_balance:.0f} USDT "
                f"杠杆={leverage}x{fresh_tag}..."
            )

            # ==============================================
            # 后台并发任务：新闻拉取 + 市场情绪 + 多周期
            # 这些任务都不依赖主流程数据，可以同时跳
            # ==============================================
            bg_results = {"news_items": [], "news_summary": {}, "sentiment": {}, "mtf": {}, "onchain": {}, "macro": {}, "bnb_factors": {}, "btc_lead": {}, "orderflow": {}}

            def _bg_news():
                try:
                    self.update_status("后台: 拉取加密新闻...")
                    items = self.news_collector.collect(
                        symbol=symbol.replace("USDT", ""), hours=24, max_items=30
                    )
                    bg_results["news_items"] = items
                    self.last_news_items = items
                    self.update_status(f"后台: 拉取到 {len(items)} 条新闻")
                except Exception as e:
                    logger_msg = f"后台: 新闻拉取失败 ({e})"
                    self.update_status(logger_msg)

            def _bg_sentiment():
                try:
                    self.update_status("后台: 获取市场情绪...")
                    s = self.sentiment_engine.fetch_all(symbol=symbol)
                    bg_results["sentiment"] = s or {}
                    self.update_status(
                        f"后台: 情绪 {s.get('label', 'N/A')} "
                        f"score={s.get('sentiment_score', 0):.2f}"
                    )
                except Exception as e:
                    self.update_status(f"后台: 情绪获取失败 ({e})")

            def _bg_orderflow():
                try:
                    of_cfg = (self.config or {}).get("orderflow") or {}
                    if not of_cfg.get("enabled", True):
                        return
                    self.update_status("后台: 订单流微观结构...")
                    from bnb_quant_tool.orderflow_signal import fetch_orderflow
                    of = fetch_orderflow(symbol, self.config) or {}
                    bg_results["orderflow"] = of
                    if of.get("available"):
                        self.update_status(
                            f"后台: 订单流 {of.get('direction', '?')} "
                            f"score={of.get('orderflow_score', 0):+.2f}"
                        )
                except Exception as e:
                    self.update_status(f"后台: 订单流跳过 ({e})")

            def _bg_onchain():
                if not self.onchain_analyzer:
                    return
                try:
                    self.update_status("后台: 链上筹码分析 (BTC/ETH)...")
                    oc = self.onchain_analyzer.fetch_all(symbol=symbol) or {}
                    bg_results["onchain"] = oc
                    self.update_status(
                        f"后台: 链上 score={oc.get('onchain_score', 0):+.2f} "
                        f"({oc.get('data_source', '?')})"
                    )
                except Exception as e:
                    self.update_status(f"后台: 链上分析失败 ({e})")

            def _bg_macro():
                if not self.macro_layer:
                    return
                try:
                    self.update_status("后台: 宏观数据层 (美股/美债/美元)...")
                    mc = self.macro_layer.fetch_all() or {}
                    bg_results["macro"] = mc
                    self.update_status(
                        f"后台: 宏观 score={mc.get('macro_score', 0):+.2f} "
                        f"| {mc.get('interpretation', '')[:40]}"
                    )
                except Exception as e:
                    self.update_status(f"后台: 宏观数据失败 ({e})")

            def _bg_bnb_factors():
                if not getattr(self, "bnb_factors_engine", None):
                    return
                try:
                    self.update_status("后台: BNB 专属因子 (Launchpool/Alpha/监管NLP)...")
                    bf = self.bnb_factors_engine.fetch_all(
                        symbol=symbol,
                        news_items=bg_results.get("news_items") or None,
                    ) or {}
                    bg_results["bnb_factors"] = bf
                    if getattr(self, "bnb_event_calendar", None) and bf.get("event_cycle"):
                        self.bnb_event_calendar.apply_to_trade_advisor(
                            self.trade_advisor, bf["event_cycle"]
                        )
                    self.update_status(
                        f"后台: BNB专属 score={bf.get('bnb_score', 0):+.2f} "
                        f"| {bf.get('trade_bias', 'WAIT')}"
                    )
                except Exception as e:
                    self.update_status(f"后台: BNB专属因子失败 ({e})")

            t_news = threading.Thread(target=_bg_news, daemon=True)
            t_sent = threading.Thread(target=_bg_sentiment, daemon=True)
            t_orderflow = threading.Thread(target=_bg_orderflow, daemon=True)
            t_onchain = threading.Thread(target=_bg_onchain, daemon=True)
            t_macro = threading.Thread(target=_bg_macro, daemon=True)
            t_mtf = None
            t_news.start(); t_sent.start(); t_orderflow.start(); t_onchain.start(); t_macro.start()

            # Step 1: 获取主K线
            self.update_status("Step 1/8: 拉取主周期 K 线...")
            df = self.fetcher.get_historical_klines(
                symbol=symbol, interval=timeframe, start_str=f"{days} days ago"
            )
            if df is None or len(df) == 0:
                raise Exception("获取主周期K线失败")
            current_price = float(self.fetcher.resolve_current_price(symbol, df) or 0)
            if current_price <= 0:
                raise Exception("无法获取有效现价（ticker 失败且 K 线过旧）")
            self.update_status(f"获取到 {len(df)} 根 K 线 · 现价 {current_price}")

            # BTC 领先指标（BNB 交易对才对比 BTC）
            try:
                from bnb_quant_tool.btc_lead_indicator import compute_btc_lead_indicator
                btc_cfg = (self.config.get("btc_lead") or {})
                btc_df = None
                if symbol != "BTCUSDT":
                    btc_df = self.fetcher.get_historical_klines(
                        symbol="BTCUSDT", interval=timeframe, start_str=f"{days} days ago"
                    )
                    btc_lead = compute_btc_lead_indicator(df, btc_df, config=btc_cfg)
                else:
                    btc_lead = {"available": False, "reason": "当前为 BTC 交易对"}
                bg_results["btc_lead"] = btc_lead
                if btc_lead.get("available"):
                    self.update_status(f"BTC领先: {btc_lead.get('summary', '')[:50]}")
            except Exception as e:
                self.update_status(f"BTC领先指标跳过 ({e})")

            # 主 K 线就绪后启动多周期分析（复用主周期数据，避免重复请求）
            def _bg_mtf():
                try:
                    self.update_status("后台: 多周期共振分析...")
                    prefetched = {}
                    mtf_timeframes = ["15m", "1h", "4h", "1d"]
                    if timeframe in mtf_timeframes and len(df) >= 30:
                        prefetched[timeframe] = df
                    mtf = self.mtf_analyzer.analyze(
                        symbol=symbol,
                        prefetched=prefetched or None,
                    )
                    bg_results["mtf"] = mtf or {}
                    self.update_status(
                        f"后台: 多周期 {mtf.get('confluence', '?')} 推荐 {mtf.get('recommended_action', '?')}"
                    )
                except Exception as e:
                    self.update_status(f"后台: 多周期失败 ({e})")

            t_mtf = threading.Thread(target=_bg_mtf, daemon=True)
            t_mtf.start()

            # Step 2: 技术指标
            self.update_status("Step 2/8: 计算技术指标...")
            indicators = TechnicalIndicators.calculate_all_indicators(df)
            self.update_status(f"计算 {len(indicators)} 个技术指标完成")

            # 智能闭环预热：软反馈 / 议会回填 / 反思消化 → 带着记忆决策
            from bnb_quant_tool.intelligence_loop import get_or_create_loop
            _loop = get_or_create_loop(
                self,
                learner=self.learner,
                config=self.config,
                paper_engine=self.paper_engine,
                pattern_memory=self.pattern_memory,
                counterfactual=getattr(self, "counterfactual", None),
            )
            _cycle = _loop.begin_cycle(symbol)
            self._last_loop_cycle = _cycle
            _pf = _loop.preflight(
                symbol=symbol,
                current_price=current_price,
            )
            _loop.mark_perceive(_cycle, has_klines=len(df))
            soft_n = (_pf.get("steps") or {}).get("soft_feedback")
            if soft_n:
                self.update_status(f"闭环预热: 软反馈 {soft_n} 条已消化")

            # Step 3: 市场状态 + 机构策略（学习权重 + 状态加权投票）
            self.update_status("Step 3/8: 识别市场状态并运行机构策略...")
            from bnb_quant_tool.ai_trading_context import (
                analysis_extra_market,
                build_full_analysis_learning_context,
            )
            market_regime = self.regime_detector.detect(
                df, indicators=indicators,
                news_summary=bg_results.get("news_summary") or {},
                sentiment=bg_results.get("sentiment"),
                bnb_factors=bg_results.get("bnb_factors"),
            )
            learning_context = build_full_analysis_learning_context(
                self.learner,
                symbol=symbol,
                current_price=current_price,
                indicators=indicators,
                regime=market_regime.get("regime"),
                paper_engine=self.paper_engine,
                pattern_memory=self.pattern_memory,
                counterfactual=getattr(self, "counterfactual", None),
                news_summary=bg_results.get("news_summary") or {},
            )
            ta_cfg = (self.config.get("analysis") or {}).get("ta_playbook") or {}
            learning_context["ta_playbook_enabled"] = ta_cfg.get("enabled", True) is not False
            learning_context["regime"] = market_regime.get("regime")
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
            inst_results["_regime_tag"] = market_regime.get("regime")
            learning_context = build_full_analysis_learning_context(
                self.learner,
                symbol=symbol,
                current_price=current_price,
                indicators=indicators,
                regime=market_regime.get("regime"),
                paper_engine=self.paper_engine,
                pattern_memory=self.pattern_memory,
                counterfactual=getattr(self, "counterfactual", None),
                inst_results=inst_results,
                news_summary=bg_results.get("news_summary") or {},
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=(bg_results.get("news_summary") or {}).get("polarity"),
                    mtf_action=(bg_results.get("mtf") or {}).get("recommended_action"),
                ),
            )
            learn_weights = learning_context.get("strategy_weights") or learn_weights
            self.update_status(
                f"市场: {market_regime.get('regime')} | 机构 BUY={inst_results['buy_signals']} "
                f"SELL={inst_results['sell_signals']} HOLD={inst_results['hold_signals']}"
                + (" [加权投票]" if inst_results.get("weighted_voting") else "")
            )

            # Step 4: 等待后台任务 (新闻/情绪/多周期/链上/宏观) 完成
            self.update_status("Step 4/8: 等待新闻/情绪/多周期/链上/宏观 后台任务...")
            t_news.join(timeout=20)
            t_sent.join(timeout=15)
            t_orderflow.join(timeout=15)
            if t_mtf is not None:
                t_mtf.join(timeout=30)
            t_onchain.join(timeout=25)
            t_macro.join(timeout=25)

            # BNB 专属因子依赖新闻列表，在新闻拉取后执行
            if getattr(self, "bnb_factors_engine", None):
                t_bnb = threading.Thread(target=_bg_bnb_factors, daemon=True)
                t_bnb.start()
                t_bnb.join(timeout=35)

            # Step 5: AI 总结新闻 (如果拿到了新闻) — 知识复用命中则跳过省 token
            from bnb_quant_tool.llm_provider import (
                build_llm_analyzer_for,
                is_dual_mode,
                list_analyzer_providers,
            )
            from bnb_quant_tool.analysis_reuse import (
                evaluate_analysis_reuse,
                mark_advice_reused,
                run_market_analyses_with_reuse,
            )
            providers = list_analyzer_providers(self.config)
            llm_creds: dict = {"api_key": "", "label": "LLM"}
            ai_analyzer = None
            if providers:
                ai_analyzer, llm_creds = build_llm_analyzer_for(self.config, providers[0])
            api_key = str(llm_creds.get("api_key") or "")


            reuse_hit = None
            if not force_fresh:
                reuse_hit = evaluate_analysis_reuse(
                    config=self.config,
                    symbol=symbol,
                    indicators=indicators,
                    market_regime=market_regime,
                    learner=self.learner,
                )
            else:
                self.update_status("Step 5/8: 全新分析模式 — 跳过知识复用")

            if bg_results["news_items"] and ai_analyzer is not None and not reuse_hit:
                try:
                    self.update_status("Step 5/8: AI 总结新闻利好利空...")
                    ns = ai_analyzer.summarize_news(
                        bg_results["news_items"],
                        symbol=symbol.replace("USDT", "")
                    )
                    from bnb_quant_tool.news_decay import adjust_news_summary
                    bg_results["news_summary"] = adjust_news_summary(
                        ns, bg_results["news_items"]
                    )
                    self.last_news_summary = ns
                    self.update_status(
                        f"新闻结论: {bg_results['news_summary'].get('polarity', 'neutral')} "
                        f"(置信 {bg_results['news_summary'].get('confidence', 0):.0%}) "
                        f"-> {bg_results['news_summary'].get('trade_suggestion', 'WAIT')}"
                    )
                except Exception as e:
                    self.update_status(f"AI 总结新闻失败 ({e})")
                    bg_results["news_summary"] = {}
            elif reuse_hit:
                bg_results["news_summary"] = {
                    "summary": "知识复用轮次：跳过新闻 LLM 摘要",
                    "polarity": "neutral",
                    "confidence": 0.0,
                    "bias": "NEUTRAL",
                    "_reused_skip": True,
                }
                self.update_status(
                    f"Step 5/8: 知识复用命中 → {reuse_hit.action}，跳过新闻 LLM"
                )
            else:
                self.update_status("Step 5/8: 跳过新闻 AI 总结 (无新闻或未配置 API key)")

            # 新闻就绪后刷新市场状态；若状态变化则重新加权机构投票
            market_regime = self.regime_detector.detect(
                df, indicators=indicators,
                news_summary=bg_results.get("news_summary") or {},
                sentiment=bg_results.get("sentiment"),
                bnb_factors=bg_results.get("bnb_factors"),
            )
            if market_regime.get("regime") != inst_results.get("_regime_tag"):
                learning_context = build_full_analysis_learning_context(
                    self.learner,
                    symbol=symbol,
                    current_price=current_price,
                    indicators=indicators,
                    regime=market_regime.get("regime"),
                    paper_engine=self.paper_engine,
                    pattern_memory=self.pattern_memory,
                    counterfactual=getattr(self, "counterfactual", None),
                    inst_results=inst_results,
                    news_summary=bg_results.get("news_summary") or {},
                    extra_market=analysis_extra_market(
                        self.config,
                        news_polarity=(bg_results.get("news_summary") or {}).get("polarity"),
                        mtf_action=(bg_results.get("mtf") or {}).get("recommended_action"),
                    ),
                )
                learn_weights = learning_context.get("strategy_weights") or learn_weights
                strat_perf = learning_context.get("strategy_performance") or {}
                sw_cfg = (self.config or {}).get("win_rate_strategy")
                inst_results = self.inst_strategies.run_all_strategies(
                    df,
                    learning_weights=learn_weights if learn_weights else None,
                    regime_multipliers=market_regime.get("strategy_multipliers"),
                    strategy_performance=strat_perf if strat_perf else None,
                    win_rate_strategy_cfg=sw_cfg,
                )
            inst_results["_regime_tag"] = market_regime.get("regime")

            # Step 6: AI 主分析（先知识复用，未命中再 LLM；全新分析强制 LLM）
            dual = is_dual_mode(self.config)
            if reuse_hit and not force_fresh:
                self.update_status(
                    f"Step 6/8: 知识复用 → {reuse_hit.action} "
                    f"(相似{reuse_hit.similarity:.0%})，跳过主分析 LLM"
                )
            else:
                dual_note = ""
                if dual:
                    from bnb_quant_tool.llm_provider import PROVIDER_LABELS, list_analyzer_providers
                    labs = "+".join(
                        PROVIDER_LABELS.get(p, p) for p in list_analyzer_providers(self.config)
                    ) or "多模"
                    dual_note = f"多模主分析 {labs}"
                prefix = "Step 6/8: 全新分析 · " if force_fresh else "Step 6/8: "
                self.update_status(
                    prefix
                    + (dual_note if dual else f"{llm_creds.get('label') or 'LLM'} 主分析")
                    + "（含学习/模式记忆/反事实）..."
                )
            if not providers:
                raise Exception("未配置 LLM api_key，请在 config.yaml 设置 deepseek / qianwen / volcengine")
            if self.multi_agent and ai_analyzer:
                self.multi_agent.ai_analyzer = ai_analyzer
            learning_context = build_full_analysis_learning_context(
                self.learner,
                symbol=symbol,
                current_price=current_price,
                indicators=indicators,
                regime=market_regime.get("regime"),
                paper_engine=self.paper_engine,
                pattern_memory=self.pattern_memory,
                counterfactual=getattr(self, "counterfactual", None),
                inst_results=inst_results,
                news_summary=bg_results.get("news_summary") or {},
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=(bg_results.get("news_summary") or {}).get("polarity"),
                    mtf_action=(bg_results.get("mtf") or {}).get("recommended_action"),
                ),
            )
            growth_now = (learning_context.get("growth") or {}).get("capability_level", 0)
            cards_n = len(learning_context.get("capability_cards") or [])
            pm_matched = int((learning_context.get("pattern_memory") or {}).get("matched") or 0)
            cf_n = int((learning_context.get("counterfactual_stats") or {}).get("total_analyzed") or 0)
            self.update_status(
                f"注入学习能力: L{growth_now} | 知识库 {cards_n} 条 | "
                f"本次检索 {len(learning_context.get('capability_cards') or [])} 条"
                f" ({learning_context.get('capability_retrieval_mode', '?')}) | "
                f"策略权重 {len(learning_context.get('strategy_weights') or {})} 个 | "
                f"模式记忆 {pm_matched} 条 | 反事实 {cf_n} 笔"
                + (" | 经验摘要 ON" if learning_context.get("experience_injected") else "")
                + (" | 多智能体 ON" if self.multi_agent else "")
                + (" | 三家综合 ON" if dual else "")
                + (" | 全新分析" if force_fresh else "")
                + (" | 复用 ON" if reuse_hit else "")
            )
            learn_weights = learning_context.get("strategy_weights") or learn_weights
            if reuse_hit and reuse_hit.reuse and not force_fresh:
                ai_bundle = reuse_hit.to_ai_bundle()
            else:
                # 全新分析 force_llm=True；普通路径仍可二次复用
                ai_bundle = run_market_analyses_with_reuse(
                    self.config,
                    df,
                    indicators,
                    symbol=symbol,
                    market_regime=market_regime,
                    learner=self.learner,
                    force_llm=bool(force_fresh),
                    learning_context=learning_context,
                    onchain_context=bg_results.get("onchain") or None,
                    macro_context=bg_results.get("macro") or None,
                    bnb_factors_context=bg_results.get("bnb_factors") or None,
                    bnb_factors=bg_results.get("bnb_factors") or None,
                    news_summary=bg_results.get("news_summary") or None,
                    atr_ratio=float((market_regime or {}).get("atr_ratio") or 1.0),
                    multi_timeframe=bg_results.get("mtf") or None,
                )
                if force_fresh and isinstance(ai_bundle.get("primary"), dict):
                    ai_bundle["primary"]["_force_fresh"] = True
                    ai_bundle["note"] = (
                        (ai_bundle.get("note") or "") + "；全新分析(强制LLM)"
                    ).strip("；")
            ai_analysis = ai_bundle["primary"]
            ai_analyses = ai_bundle.get("by_provider") or {}
            status_bits = []
            for pname, pdata in ai_analyses.items():
                if not isinstance(pdata, dict):
                    continue
                lab = pdata.get("_provider_label") or pname
                if pdata.get("_error") or pdata.get("_degraded"):
                    status_bits.append(f"{lab}:失败")
                else:
                    status_bits.append(
                        f"{lab}:{pdata.get('signal', '?')}@{float(pdata.get('confidence') or 0):.0%}"
                    )
            ens = (ai_analysis or {}).get("_ensemble") or ai_bundle.get("ensemble") or {}
            ens_txt = ""
            if ens.get("detail"):
                ens_txt = f" | 综合={ai_analysis.get('signal','?')}@{float(ai_analysis.get('confidence') or 0):.0%}"
            self.update_status(
                "AI: " + (" | ".join(status_bits) if status_bits else "N/A")
                + ens_txt
                + (f" | {ai_bundle.get('note')}" if ai_bundle.get("note") else "")
            )

            # AI 分析后刷新学习上下文（模式记忆含 AI 信号）
            learning_context = build_full_analysis_learning_context(
                self.learner,
                symbol=symbol,
                current_price=current_price,
                indicators=indicators,
                regime=market_regime.get("regime"),
                paper_engine=self.paper_engine,
                pattern_memory=self.pattern_memory,
                counterfactual=getattr(self, "counterfactual", None),
                inst_results=inst_results,
                ai_analysis=ai_analysis,
                news_summary=bg_results.get("news_summary") or {},
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=(bg_results.get("news_summary") or {}).get("polarity"),
                    mtf_action=(bg_results.get("mtf") or {}).get("recommended_action"),
                ),
            )
            learn_weights = learning_context.get("strategy_weights") or learn_weights

            # Step 7: 交易信号 + 交易计划 + 风控
            self.update_status("Step 7/8: 生成交易信号 、 计划、风控...")
            from bnb_quant_tool.config_access import get_confidence_threshold, get_max_open_positions
            trading_config = {
                'symbol': symbol,
                'risk_per_trade': self.config['trading']['risk_per_trade'],
                'confidence_threshold': get_confidence_threshold(self.config),
            }
            generator = TradingSignals(trading_config)
            tech_signals = generator.generate_technical_signals(df, indicators)
            combined = generator.combine_with_ai_analysis(tech_signals, ai_analysis)
            plan = generator.generate_trading_plan(combined, account_balance)
            rm = RiskManager({
                'max_risk_per_trade': self.config.get('trading', {}).get('risk_per_trade', 0.02),
                'max_open_positions': get_max_open_positions(self.config, default=0),
                'max_daily_loss': 0.05, 'min_risk_reward_ratio': 1.5,
                'initial_balance': account_balance
            })
            is_valid, reason = rm.validate_trade(plan)

            # 综合信号
            final_signal = self._combine_all_signals(inst_results, combined, ai_analysis)
            if strategy_mode == 'institutional_only':
                final_signal = inst_results['consensus_signal']
            elif strategy_mode == 'technical_only':
                final_signal = combined['final_signal']
            elif strategy_mode == 'ai_only':
                sig = ai_analysis.get('signal', '')
                final_signal = 'BUY' if sig == '买入' else ('SELL' if sig == '卖出' else 'HOLD')

            # Step 8: 机构信念引擎 + 开单建议
            self.update_status("Step 8/8: 机构信念融合 & 生成开单建议...")
            from bnb_quant_tool.institutional_conviction import compute_institutional_conviction
            institutional_conviction = compute_institutional_conviction(
                inst_results=inst_results,
                market_regime=market_regime,
                indicators=indicators,
                sentiment=bg_results.get("sentiment"),
                onchain=bg_results.get("onchain"),
                macro=bg_results.get("macro"),
                bnb_factors=bg_results.get("bnb_factors"),
                mtf=bg_results.get("mtf"),
                learning_insights=learning_context,
                btc_lead=bg_results.get("btc_lead"),
                orderflow=bg_results.get("orderflow"),
            )
            learning_context["institutional_conviction"] = institutional_conviction
            inst_results["_conviction_family"] = institutional_conviction.get("strategy_family") or {}

            from bnb_quant_tool.factor_attribution_learner import compute_reliability_multipliers
            factor_rel = (
                learning_context.get("factor_reliability")
                or compute_reliability_multipliers(
                    learning_context.get("factor_attribution")
                )
            )
            learning_context["factor_reliability"] = factor_rel
            try:
                trade_advice = self.trade_advisor.build_advice(
                    symbol=symbol,
                    timeframe=timeframe,
                    current_price=current_price,
                    indicators=indicators,
                    ai_analysis=ai_analysis,
                    institutional=inst_results,
                    learning_insights=learning_context,
                    multi_timeframe=bg_results.get("mtf") or None,
                    sentiment=bg_results.get("sentiment") or None,
                    news_summary=bg_results.get("news_summary") or None,
                    market_regime=market_regime,
                    onchain=bg_results.get("onchain") or None,
                    macro=bg_results.get("macro") or None,
                    bnb_factors=bg_results.get("bnb_factors") or None,
                    leverage=leverage,  # 传入用户选择的杠杆
                    factor_reliability=factor_rel,
                    analysis_mode=strategy_mode,
                    technical_combined=combined,
                )
            except Exception as _e:
                trade_advice = {
                    'action': 'WAIT',
                    'gate_reasons': [f'开单建议生成异常: {_e}'],
                    'report_text': f'开单建议生成失败: {_e}',
                    'order_text': '',
                }

            try:
                from bnb_quant_tool.signal_scanner import inject_scanner_into_advice
                inject_scanner_into_advice(trade_advice, symbol=symbol)
            except Exception:
                pass

            trade_advice = mark_advice_reused(trade_advice, ai_analysis, ai_bundle)

            if institutional_conviction.get("summary") and trade_advice.get("report_text"):
                trade_advice["report_text"] = (
                    institutional_conviction["summary"] + "\n\n" + trade_advice["report_text"]
                )
            trade_advice["institutional_conviction"] = institutional_conviction
            # 三家主分析对照：无论 report_text 是否为空都写入，避免界面看不到
            try:
                from bnb_quant_tool.llm_provider import format_ai_analyses_report_block
                dual_ai_txt = format_ai_analyses_report_block(
                    primary=ai_analysis,
                    by_provider=ai_analyses,
                    note=ai_bundle.get("note") or "",
                )
                if dual_ai_txt.strip():
                    base_report = trade_advice.get("report_text") or ""
                    if "[3]" not in base_report and "三家综合" not in base_report and "AI 主分析" not in base_report:
                        trade_advice["report_text"] = (
                            dual_ai_txt + ("\n" + base_report if base_report else "")
                        )
                    trade_advice["_ai_analyses_report"] = dual_ai_txt
            except Exception as _ae:
                logger.debug("format triple AI report: %s", _ae)

            # 模式记忆 / 反事实 / Funding 门控（统一管道）
            from bnb_quant_tool.analysis_pipeline import (
                apply_post_advice_gates,
                apply_dynamic_position,
                attach_decision_explanation,
            )
            pattern_insight = learning_context.get("pattern_memory")
            # 硬门依赖：Platt 校准 / 全成本净 RR / 跨模态冲突
            learning_context["onchain"] = bg_results.get("onchain") or {}
            learning_context["macro"] = bg_results.get("macro") or {}
            learning_context["market_regime"] = market_regime
            learning_context["news_summary"] = bg_results.get("news_summary") or {}
            learning_context["_learner"] = self.learner
            learning_context["_paper_engine"] = self.paper_engine
            trade_advice = apply_post_advice_gates(
                trade_advice,
                learning_context=learning_context,
                config=self.config,
                sentiment=bg_results.get("sentiment"),
                bnb_factors=bg_results.get("bnb_factors"),
                pattern_memory=self.pattern_memory,
                pattern_insight=pattern_insight,
                indicators=indicators,
                ai_analysis=ai_analysis,
                inst_results=inst_results,
                current_price=current_price,
                news_summary=bg_results.get("news_summary"),
                onchain=bg_results.get("onchain"),
                macro=bg_results.get("macro"),
                multi_agent_fn=self._apply_multi_agent_deliberation,
                multi_agent_kwargs={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "current_price": current_price,
                    "indicators": indicators,
                    "ai_analysis": ai_analysis,
                    "inst_results": inst_results,
                    "bg_results": bg_results,
                    "market_regime": market_regime,
                    "learning_context": learning_context,
                    "pattern_insight": pattern_insight,
                },
            )

            trade_advice = apply_dynamic_position(
                trade_advice,
                position_sizer=self.position_sizer,
                regime_detector=self.regime_detector,
                df=df,
                indicators=indicators,
                market_regime=market_regime,
                multi_timeframe=bg_results.get("mtf"),
                news_summary=bg_results.get("news_summary"),
                config=self.config,
            )

            trade_advice = attach_decision_explanation(
                trade_advice,
                self.decision_explainer,
                indicators=indicators,
                ai_analysis=ai_analysis,
                institutional=inst_results,
                learning_insights=learning_context,
                multi_timeframe=bg_results.get("mtf") or None,
                sentiment=bg_results.get("sentiment") or None,
                news_summary=bg_results.get("news_summary") or None,
                bnb_factors=bg_results.get("bnb_factors") or None,
                factor_reliability=factor_rel,
            )

            # 策略模式与 trade_advice 对齐
            ta_action = trade_advice.get("action", "WAIT")
            final_signal = (
                "BUY" if ta_action == "LONG" else
                ("SELL" if ta_action == "SHORT" else "HOLD")
            )

            # v2.9 多智能体已在 apply_post_advice_gates 中调用；保留变量供下方构建结果
            pattern_insight = trade_advice.get("pattern_insight") or learning_context.get("pattern_memory")

            # 构建结果
            from bnb_quant_tool.analysis_pipeline import attach_ta_playbook

            self.analysis_result = attach_ta_playbook({
                'timestamp': datetime.now().isoformat(),
                'symbol': symbol, 'timeframe': timeframe,
                'data_points': len(df),
                'current_price': current_price,
                'strategy_mode': strategy_mode,
                'indicators': {k: float(v) if isinstance(v, (int, float)) else v for k, v in indicators.items()},
                'institutional_strategies': inst_results,
                'ai_analysis': ai_analysis,
                'ai_analyses': ai_analyses,
                'ai_analysis_note': ai_bundle.get("note") or "",
                'ai_primary_provider': ai_bundle.get("primary_provider") or "",
                'trading_plan': plan,
                'risk_check': {'passed': is_valid, 'reason': reason},
                'final_recommendation': final_signal,
                'trade_advice': trade_advice,
                'news_items': bg_results.get("news_items", []),
                'news_summary': bg_results.get("news_summary", {}),
                'sentiment': bg_results.get("sentiment", {}),
                'onchain': bg_results.get("onchain", {}),
                'macro': bg_results.get("macro", {}),
                'bnb_factors': bg_results.get("bnb_factors", {}),
                'multi_timeframe': bg_results.get("mtf", {}),
                'market_regime': market_regime,
                'learning_context': learning_context,
            }, config=self.config, account_balance=account_balance)
            if self.analysis_result.get("ta_playbook"):
                learning_context["ta_playbook"] = self.analysis_result["ta_playbook"]
                trade_advice["ta_playbook"] = self.analysis_result["ta_playbook"]

            # 记录到学习系统
            self.update_status("记录到 AI 学习系统...")
            rid = self.learner.record_analysis(self.analysis_result)
            if rid:
                self.last_record_id = rid
                try:
                    from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
                    ev = LearningEvolutionCoordinator(
                        self.learner,
                        capability_memory=self.learner.capability_memory,
                        counterfactual=getattr(self, "counterfactual", None),
                        config=self.config,
                    )
                    ev.on_analysis_recorded(
                        int(rid),
                        learning_context,
                        trade_advice=trade_advice,
                        ai_analysis=ai_analysis,
                    )
                except Exception as ev_e:
                    logger.debug(f"on_analysis_recorded: {ev_e}")
                growth = self.learner.get_growth_snapshot()
                dims = growth.get("capability_dimensions") or {}
                try:
                    from bnb_quant_tool.intelligence_loop import get_or_create_loop
                    _loop = get_or_create_loop(self, learner=self.learner, config=self.config)
                    _cycle = getattr(self, "_last_loop_cycle", None) or _loop.last_report
                    _loop.after_analysis(
                        _cycle,
                        record_id=int(rid),
                        action=str(trade_advice.get("action") or "WAIT"),
                        reused=bool(
                            trade_advice.get("_reused")
                            or (ai_analysis or {}).get("_reused")
                            or trade_advice.get("_skipped_council_reuse")
                        ),
                        learning_context=learning_context,
                        config=self.config,
                    )
                    health = _loop.get_loop_health(symbol=symbol)
                    self.analysis_result["intelligence_loop"] = (
                        _cycle.to_dict() if _cycle else {}
                    )
                    self.analysis_result["loop_health"] = health
                    loop_txt = (
                        f" | 闭环{health.get('completeness_score', 0)}分"
                        f"({health.get('completeness_label', '?')})"
                    )
                except Exception:
                    loop_txt = ""
                self.update_status(
                    f"分析已记录 (ID={rid}) | 能力 L{growth.get('capability_level', 0)} "
                    f"| 知识库 {growth.get('knowledge_cards', 0)} 条 "
                    f"| 准确{dims.get('prediction_accuracy', 0)}"
                    f"{loop_txt}"
                    + (
                        " | 经验已注入"
                        if (learning_context or {}).get("experience_injected")
                        else ""
                    )
                )
            else:
                self.update_status("分析记录跳过 (DB busy)")

            # 记录信号到追踪表（开仓前登记，便于 mark_signal_followed）
            try:
                regime_tag = (market_regime or {}).get("regime", "")
                sid = self.paper_engine.track_signal(trade_advice, market_regime=regime_tag)
                trade_advice["_signal_tracking_id"] = sid
            except Exception as _e:
                logger.debug(f"信号追踪记录失败: {_e}")

            # 自动跟单到模拟盘（与 AI 全自动统一门控纪律）
            opened_this_cycle = False
            try:
                from bnb_quant_tool.ai_trading_context import (
                    should_open_from_advice,
                    needs_relaxed_open,
                    get_effective_follow_direction,
                    resolve_execution_context,
                )
                ctx = trade_advice.get("execution_context") or resolve_execution_context(
                    trade_advice, self.config,
                    auto_follow_enabled=bool(self.auto_paper_var.get()),
                )
                follow_action = trade_advice.get("action")
                raw_action = trade_advice.get("raw_action")
                effective_dir = get_effective_follow_direction(trade_advice)
                relaxed_mode = needs_relaxed_open(trade_advice, self.config)
                will_open = False

                if self.auto_paper_var.get():
                    will_open = should_open_from_advice(trade_advice, self.config)

                if will_open:
                    from bnb_quant_tool.config_access import is_margin_insufficient
                    margin_required = float(
                        (trade_advice.get("position") or {}).get("margin_required") or 0
                    )
                    margin_blocked = is_margin_insufficient(
                        account_balance,
                        self.paper_engine.get_open_positions(),
                        margin_required,
                        total_realized_pnl=self.paper_engine.get_total_realized_pnl(),
                    )
                    if margin_blocked:
                        self.update_status("未跟单: 可用保证金不足")
                    elif trade_advice.get("_multi_agent_position_id"):
                        self.update_status(
                            f"多智能体已开仓 #{trade_advice['_multi_agent_position_id']}"
                        )
                        opened_this_cycle = True
                    else:
                        pid = self.paper_engine.open_from_advice(
                            trade_advice, equity_usdt=account_balance,
                            learning_record_id=self.last_record_id,
                            relaxed=relaxed_mode,
                        )
                        if pid:
                            opened_this_cycle = True
                            self.last_paper_advice_id = pid
                            if follow_action == "WAIT" and effective_dir in ("LONG", "SHORT"):
                                tag = ctx.get("follow_mode", "direction_follow")
                                self.update_status(
                                    f"模拟盘跟单 #{pid} ({effective_dir}, 风控=WAIT·{tag})"
                                )
                            else:
                                self.update_status(f"模拟盘自动跟单 #{pid} ({effective_dir})")
                            try:
                                entry_price = (
                                    trade_advice.get("prices", {}).get("entry_mid")
                                    or current_price
                                )
                                self.paper_engine.mark_signal_followed(
                                    trade_advice.get("_signal_tracking_id", 0),
                                    actual_entry=float(entry_price),
                                )
                            except Exception:
                                pass
                            if not self.paper_engine.is_watching():
                                self.paper_engine.start_watcher(
                                    interval=float(self.config.get('paper_trading', {}).get('poll_interval', 15))
                                )
                        else:
                            self.update_status("跟单失败: 开仓返回空 (检查价格字段)")
                else:
                    if not self.auto_paper_var.get():
                        self.update_status("未跟单: 「自动跟单」开关未开启")
                    elif effective_dir == "WAIT":
                        self.update_status("未跟单: 信号无明确方向")
                    else:
                        reason = ctx.get("follow_reason") or ""
                        gates = trade_advice.get("gate_reasons") or []
                        gate_str = reason or "; ".join(gates) if gates else "未知原因"
                        self.update_status(
                            f"未跟单 (方向={effective_dir}, 风控={follow_action}): {gate_str}"
                        )
            except Exception as _e:
                self.update_status(f"自动跟单异常: {_e}")

            try:
                from bnb_quant_tool.trading_profile import record_decision_funnel
                from bnb_quant_tool.scoreboard import publish_scoreboard
                record_decision_funnel(
                    analysis_triggered=True,
                    gate_passed=bool(trade_advice.get("passed_gate")),
                    opened=opened_this_cycle,
                    source="gui_analysis",
                    symbol=symbol,
                    config=self.config,
                )
                publish_scoreboard(
                    paper_engine=self.paper_engine,
                    config=self.config,
                    orderflow=bg_results.get("orderflow"),
                )
            except Exception as fe:
                logger.debug("gui funnel/scoreboard: %s", fe)

            self.update_output()
            if hasattr(self, "_refresh_learning_evolution_views"):
                self.root.after(0, self._refresh_learning_evolution_views)
            # 刷新知识库：展示本次分析注入的语义检索结果
            try:
                injected = learning_context.get("capability_cards") or []
                if injected:
                    self._refresh_knowledge_cards(
                        cards=injected,
                        retrieval_mode=learning_context.get("capability_retrieval_mode", ""),
                    )
            except Exception:
                pass
            self.update_status(
                f"完成 | AI决策={trade_advice.get('action', 'WAIT')} "
                f"置信{trade_advice.get('confidence', 0):.0%} "
                f"新闻={bg_results.get('news_summary', {}).get('polarity', 'n/a')} "
                f"多周期={bg_results.get('mtf', {}).get('recommended_action', 'n/a')}"
            )

        except Exception as e:
            err_msg = str(e)
            self.update_status(f"错误: {err_msg}")
            self.root.after(0, lambda msg=err_msg: messagebox.showerror("分析失败", msg))
        finally:
            self._analysis_running = False
            self._analysis_started_ts = 0
            def _finish_analysis_ui():
                self.progress.stop()
                self.analyze_btn.config(state='normal')
                if getattr(self, "fresh_analyze_btn", None):
                    self.fresh_analyze_btn.config(state='normal')
                self.save_btn.config(state='normal')
                if self.last_record_id:
                    self.feedback_btn.config(state='normal')

            self.root.after(0, _finish_analysis_ui)

    def _combine_all_signals(self, inst_results, combined, ai_analysis):
        """综合机构策略(40%) + 技术/AI(60%)"""
        inst_signal = inst_results['consensus_signal']
        tech_ai_signal = combined['final_signal']
        # final_score 范围为 [-1, 1]，取其绝对值作为投票权重
        tech_ai_strength = abs(combined.get('final_score', 0) or 0)

        buy_votes = 0.0
        sell_votes = 0.0

        if inst_signal == 'BUY':
            buy_votes += 0.4 * (inst_results.get('consensus_confidence', 0.5) or 0.5)
        elif inst_signal == 'SELL':
            sell_votes += 0.4 * (inst_results.get('consensus_confidence', 0.5) or 0.5)

        if tech_ai_signal == 'BUY':
            buy_votes += 0.6 * tech_ai_strength
        elif tech_ai_signal == 'SELL':
            sell_votes += 0.6 * tech_ai_strength

        total = buy_votes + sell_votes
        if total < 0.01:
            return 'HOLD'
        buy_ratio = buy_votes / total
        if buy_ratio > 0.65:
            return 'BUY'
        elif buy_ratio < 0.35:
            return 'SELL'
        else:
            return 'HOLD'

