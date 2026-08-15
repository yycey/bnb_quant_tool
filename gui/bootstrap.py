"""Mixin: BootstrapMixin"""

from gui._imports import *


class BootstrapMixin:
    def __init__(self, root):
        self.root = root
        self.root.title("BNB 交易员议会 — 圆桌议事厅")
        self.root.geometry("1280x860")
        try:
            self.root.minsize(1100, 720)
        except Exception:
            pass

        from bnb_quant_tool.data_localization import init_workspace
        init_workspace(str(PROJECT_ROOT))

        self.config = self.load_config()

        # 机构策略实例
        self.inst_strategies = InstitutionalStrategies(config=self.config)
        try:
            from bnb_quant_tool.strategy_pool import register_strategy_pool
            register_strategy_pool(self.inst_strategies)
        except Exception:
            pass

        # AI学习系统实例（核心新增）
        self.learner = AILearningSystem(config=self.config)
        # 网格策略实例
        self.grid_strategy = GridStrategy()
        # 开单建议生成器（核心：输出可立即下单的价格参数）
        from bnb_quant_tool.config_access import build_trade_advisor_config, build_position_sizer_config
        self.trade_advisor = TradeAdvisor(build_trade_advisor_config(self.config))
        self.learner.trade_advisor_ref = self.trade_advisor

        # 多周期 / 情绪 / 价格预警
        from bnb_quant_tool.config_access import build_data_fetcher
        self.fetcher = build_data_fetcher(self.config)
        self.mtf_analyzer = MultiTimeframeAnalyzer(fetcher=self.fetcher)
        self.sentiment_engine = MarketSentiment(
            cache_seconds=int((self.config.get('sentiment') or {}).get(
                'cache_seconds',
                int((self.config.get('sentiment') or {}).get('refresh_minutes', 30)) * 60,
            )),
        )
        onchain_cfg = self.config.get("onchain") or {}
        macro_cfg = self.config.get("macro") or {}
        self.onchain_analyzer = OnChainAnalyzer(
            glassnode_api_key=onchain_cfg.get("glassnode_api_key"),
            use_coinmetrics_fallback=bool(onchain_cfg.get("use_coinmetrics_fallback", True)),
            cache_seconds=int(onchain_cfg.get("cache_seconds", 900)),
            etherscan_api_key=onchain_cfg.get("etherscan_api_key") or onchain_cfg.get("bscscan_api_key"),
            bsc_chain_id=int(onchain_cfg.get("bsc_chain_id", 56)),
            bsc_enabled=bool(onchain_cfg.get("bsc_enabled", True)),
        ) if onchain_cfg.get("enabled", True) else None
        self.macro_layer = MacroDataLayer(
            symbols=macro_cfg.get("symbols"),
            correlation_lookback_days=int(macro_cfg.get("correlation_lookback_days", 30)),
            cache_seconds=int(macro_cfg.get("cache_seconds", 900)),
        ) if macro_cfg.get("enabled", True) else None

        bnb_cfg = dict(self.config.get("bnb_factors") or {})
        event_cfg = self.config.get("bnb_event_calendar") or {}
        sentry_cfg = self.config.get("bnb_risk_sentry") or {}
        bnb_cfg["event_calendar"] = event_cfg
        bnb_cfg["risk_sentry"] = sentry_cfg
        from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors
        from bnb_quant_tool.bnb_event_calendar import BNBEventCalendar
        self.bnb_factors_engine = BNBSpecificFactors(
            fetcher=self.fetcher,
            config=bnb_cfg,
        ) if bnb_cfg.get("enabled", True) else None
        self.bnb_event_calendar = BNBEventCalendar(
            config=event_cfg,
            state_path=event_cfg.get("state_path"),
        ) if event_cfg.get("enabled", True) else None
        from bnb_quant_tool.bnb_risk_sentry import BNBRiskSentry
        self.bnb_risk_sentry = BNBRiskSentry(
            fetcher=self.fetcher,
            config=sentry_cfg,
        ) if sentry_cfg.get("enabled", True) else None

        # v2.5: 指标探索器 → 接入 TradeAdvisor
        try:
            from bnb_quant_tool.indicator_explorer import IndicatorExplorer
            self._indicator_explorer = IndicatorExplorer(
                config_path=str(PROJECT_ROOT / "config.yaml"),
                learning_db_path=self.learner.db_path,
            )
            self.trade_advisor.set_indicator_explorer(self._indicator_explorer)
        except Exception as _ie_err:
            logger.debug(f"指标探索器初始化跳过: {_ie_err}")
            self._indicator_explorer = None
        self.alert_engine = PriceAlertEngine(
            symbol=self.config.get('trading', {}).get('symbol', 'BNBUSDT'),
            poll_interval=float(self.config.get('price_alert', {}).get('poll_interval_seconds', 15)),
        )
        self.alert_engine.set_callback(self._on_price_alert)

        # 新闻采集器（接入 BlockBeats API + Odaily REST）
        news_cfg = self.config.get('news', {})
        self.news_collector = NewsCollector(
            blockbeats_api_key=news_cfg.get('blockbeats_api_key'),
            blockbeats_lang=news_cfg.get('blockbeats_lang', 'cn'),
            blockbeats_size=int(news_cfg.get('blockbeats_size', 50)),
            blockbeats_cache_seconds=int(news_cfg.get('blockbeats_cache_seconds', 86400)),
            rss_cache_seconds=int(news_cfg.get('rss_cache_seconds', 1800)),
            cache_dir=news_cfg.get('cache_dir', 'data/news_cache'),
            tikhub_config=news_cfg.get('tikhub') or {},
            odaily_config=news_cfg.get('odaily') or {},
        )
        self.last_news_items = []
        self.last_news_summary = {}

        # 模拟交易引擎
        paper_db = self.config.get('paper_trading', {}).get('db_path')
        # AI复盘引擎
        from bnb_quant_tool.ai_review_engine import AIReviewEngine
        self.ai_review_engine = AIReviewEngine(
            config=self.config,
            deepseek_api_key=self.config.get('deepseek', {}).get('api_key', ''),
            deepseek_model=self.config.get('deepseek', {}).get('model', 'deepseek-chat'),
            deepseek_base_url=self.config.get('deepseek', {}).get('base_url', 'https://api.deepseek.com')
        )
        self.paper_engine = PaperTradingEngine(
            db_path=paper_db,
            config=self.config,
            ai_review_engine=self.ai_review_engine
        )
        self.paper_engine.set_price_provider(self._paper_price_provider)
        self.paper_engine.set_event_callback(self._on_paper_event)
        self._notify_db_recovery()

        # 模拟盘价格缓存（避免 watcher/平仓频繁阻塞网络）
        self._price_cache: Dict[str, tuple] = {}
        self._price_cache_lock = threading.Lock()
        self._price_cache_ttl = 5.0
        self._paper_refresh_pending = None

        # v2.2: 学习反馈闭环 — paper_engine → learner
        self.paper_engine.set_learner(self.learner)

        # v2.0: 将 trade_advisor 与 paper_engine 关联（连亏同步）
        self.paper_engine.set_trade_advisor(self.trade_advisor)

        # v2.1: trade_advisor 查询持仓数（最大持仓数门控）
        self.trade_advisor.set_paper_engine(self.paper_engine)

        from bnb_quant_tool.circuit_breaker import CircuitBreaker
        cb_cfg = self.config.get("circuit_breaker") or {}
        self.circuit_breaker = CircuitBreaker(
            paper_engine=self.paper_engine,
            config=cb_cfg,
        )
        self.circuit_breaker.account_balance = float(
            self.config.get("trading", {}).get("account_balance", 5000)
        )
        if not cb_cfg.get("enabled", True):
            try:
                self.circuit_breaker.reset_cooldown()
            except Exception:
                pass
            self.trade_advisor.set_circuit_breaker(None)
        else:
            self.trade_advisor.set_circuit_breaker(self.circuit_breaker)
        self.trade_advisor.set_structural_config(
            self.config.get("structural_strategies") or {}
        )

        from bnb_quant_tool.autopilot import AutopilotController
        self.autopilot = AutopilotController(self.config)

        # 决策可解释性 (Day1-B)
        self.decision_explainer = DecisionExplainer()

        # AI 模式记忆 (Pattern Memory)
        self.pattern_memory = PatternMemory(
            paper_db_path=self.paper_engine.db_path,
            learning_db_path=self.learner.db_path,
        )

        # 反事实学习 (Counterfactual) — 须在平仓学习管道注入前初始化
        self.counterfactual = CounterfactualAnalyzer(
            fetcher=self.fetcher,
        )

        self.paper_engine.set_learning_pipeline_deps(
            counterfactual=self.counterfactual,
            pattern_memory=self.pattern_memory,
            on_status=self.update_status,
        )
        try:
            from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
            self._learning_evolution = LearningEvolutionCoordinator(
                self.learner,
                capability_memory=self.learner.capability_memory,
                counterfactual=self.counterfactual,
                config=self.config,
            )
            self.paper_engine.set_learning_pipeline_deps(
                counterfactual=self.counterfactual,
                pattern_memory=self.pattern_memory,
                evolution=self._learning_evolution,
                on_status=self.update_status,
            )
        except Exception as ev_e:
            logger.debug("learning evolution init: %s", ev_e)
            self._learning_evolution = None

        try:
            from bnb_quant_tool.advisor_risk_sync import sync_advisor_risk_context
            sync_advisor_risk_context(
                self.config,
                self.trade_advisor,
                calendar=self.bnb_event_calendar,
                sentry=self.bnb_risk_sentry,
                symbol=(self.config.get("trading") or {}).get("symbol", "BNBUSDT"),
            )
        except Exception as rs_e:
            logger.debug("advisor risk sync: %s", rs_e)

        # v2.9/v2.14 多智能体 + 6 人交易员议会（默认启用）
        ma_cfg = self.config.get("multi_agent") or {}
        self.multi_agent: Optional[MultiAgentOrchestrator] = None
        if ma_cfg.get("enabled", True):
            self.multi_agent = MultiAgentOrchestrator(
                config=self.config,
                news_collector=self.news_collector,
                sentiment_engine=self.sentiment_engine,
                onchain_analyzer=self.onchain_analyzer,
                macro_layer=self.macro_layer,
                pattern_memory=self.pattern_memory,
                paper_engine=self.paper_engine,
                project_root=str(PROJECT_ROOT),
            )

        # 深度学习模型：若已训练则自动加载并接入 TradeAdvisor
        self._init_dl_engine()

        # 市场状态 + 动态仓位
        self.regime_detector = MarketRegimeDetector(
            self.config.get('market_regime', {}) or {}
        )
        self.position_sizer = DynamicPositionSizer(
            build_position_sizer_config(self.config)
        )
        # 自动跟单开关（分析完成后是否自动下模拟单）
        self.auto_paper_var = tk.BooleanVar(
            value=bool(self.config.get('paper_trading', {}).get('auto_follow', True))
        )
        self.last_review = {}
        self.last_paper_advice_id: Optional[int] = None
        # 自动复盘状态 (每 N 笔平仓触发一次)
        self._last_auto_review_at: int = 0
        self._auto_review_running: bool = False

        # AI全自动模式状态
        self._analysis_running: bool = False
        self._ai_fullauto_running: bool = False
        self._autopilot_oneshot: bool = False
        self._ai_fullauto_cycle: int = 0
        self._ai_fullauto_interval: int = int(
            self.config.get('auto_run', {}).get('interval_minutes', 60)
        )

        # 主动信号扫描器
        self._scanner: Optional[SignalScanner] = None
        self._scanner_running: bool = False

        # 最后一次分析的记录ID（用于快速反馈）
        self.last_record_id = None

        # 定时自动分析调度器
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_running = False
        self._scheduler_next_ts: float = 0.0
        self._scheduler_runs: int = 0
        self._scheduler_skips: int = 0
        self._scheduler_interval_sec: float = 0.0
        self._analysis_started_ts: float = 0.0

        self.create_widgets()
        self.analysis_thread = None

        # 窗口关闭钩子: 停所有后台线程
        try:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            pass

        # GUI 就绪后按 config 自动开启各功能
        try:
            delay_ms = int((self.config.get("startup") or {}).get("delay_ms", 2500))
            self.root.after(delay_ms, self._startup_automation)
        except Exception:
            pass

    def load_config(self):
        from bnb_quant_tool.config_access import load_app_config
        return load_app_config(PROJECT_ROOT / "config.yaml")

    def _notify_db_recovery(self):
        """若启动时自动修复了损坏数据库，弹窗告知用户备份位置。"""
        lines = []
        for attr, label in (("learner", "AI 学习库 (ai_learning.db)"), ("paper_engine", "模拟盘 (paper_trading.db)")):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            info = getattr(obj, "db_recovery_info", None) or {}
            action = info.get("action")
            if action in ("recovered", "recreated", "failed"):
                lines.append(f"• {label}\n  {info.get('message', action)}")
                if info.get("backup"):
                    lines.append(f"  备份: {info['backup']}")
        if not lines:
            return

        def _show():
            title = "数据库已自动修复" if not any(
                (getattr(getattr(self, a, None), "db_recovery_info", {}) or {}).get("action") == "failed"
                for a in ("learner", "paper_engine")
            ) else "数据库修复失败"
            messagebox.showwarning(
                title,
                "检测到 SQLite 数据库损坏，已自动处理：\n\n"
                + "\n".join(lines)
                + "\n\n若需找回历史数据，请到 data/backups/ 查看备份，或使用「大脑导入」恢复。",
            )

        self.root.after(800, _show)

    def _init_dl_engine(self) -> None:
        """启动时自动加载已训练的深度学习模型，供分析投票使用。"""
        try:
            from pathlib import Path
            from bnb_quant_tool.data_localization import get_localized_model_path
            from bnb_quant_tool.deep_learning_engine import DeepLearningEngine

            model_path = Path(get_localized_model_path("deep_learning"))
            if not model_path.is_file():
                return
            self._dl_engine = DeepLearningEngine(db_path=self.learner.db_path)
            self.trade_advisor.set_dl_engine(self._dl_engine)
            logger.info("DeepLearningEngine 已自动加载: %s", model_path)
        except Exception as e:
            logger.debug("DeepLearningEngine 自动加载跳过: %s", e)

