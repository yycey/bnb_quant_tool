"""Mixin: AutomationMixin"""

from gui._imports import *


class AutomationMixin:
    def _init_llm_provider_vars(self):
        """从 config 初始化三家 LLM 启用开关。"""
        self._llm_provider_labels = {
            "volcengine": "豆包",
            "deepseek": "DeepSeek",
            "qianwen": "千问",
        }
        self._llm_enable_vars = {}
        for key in ("volcengine", "deepseek", "qianwen"):
            sec = (self.config.get(key) or {}) if isinstance(self.config, dict) else {}
            if "enabled" in sec:
                on = bool(sec.get("enabled"))
            else:
                on = bool(str(sec.get("api_key") or "").strip())
            self._llm_enable_vars[key] = tk.BooleanVar(value=on)

    def _refresh_llm_provider_status(self):
        """更新顶栏状态文案。"""
        if not hasattr(self, "_llm_enable_vars"):
            return
        on = [
            self._llm_provider_labels.get(k, k)
            for k, var in self._llm_enable_vars.items()
            if var.get()
        ]
        text = ("仅" + "+".join(on)) if on else "未启用"
        if hasattr(self, "_llm_provider_status_var"):
            self._llm_provider_status_var.set(text)

    def _on_llm_provider_toggle(self):
        """勾选变化：禁止全关，写入 config.yaml 并热更新运行时。"""
        if not hasattr(self, "_llm_enable_vars"):
            return
        enabled = [k for k, v in self._llm_enable_vars.items() if v.get()]
        if not enabled:
            self._llm_enable_vars["volcengine"].set(True)
            enabled = ["volcengine"]
            try:
                messagebox.showwarning("LLM 开关", "至少启用一家分析模型，已自动保留豆包。")
            except Exception:
                pass
        try:
            self._persist_llm_provider_enabled(enabled)
            self._refresh_llm_provider_status()
            names = "+".join(self._llm_provider_labels.get(k, k) for k in enabled)
            self.update_status(f"LLM 已切换为: {names}（已写入配置）")
        except Exception as e:
            logger.exception("保存 LLM 开关失败")
            try:
                messagebox.showerror("保存失败", f"写入 LLM 开关失败: {e}")
            except Exception:
                pass

    def _persist_llm_provider_enabled(self, enabled_keys: list):
        """更新内存 config + 落盘 config.yaml（含 analyzer/council/routing 列表）。"""
        order = ("volcengine", "deepseek", "qianwen")
        enabled_set = set(enabled_keys)
        preferred = ["deepseek", "qianwen", "volcengine"]
        providers = [p for p in preferred if p in enabled_set]
        if not providers:
            providers = ["volcengine"]

        for key in order:
            sec = dict(self.config.get(key) or {})
            sec["enabled"] = key in enabled_set
            self.config[key] = sec

        llm = dict(self.config.get("llm") or {})
        llm["analyzer_providers"] = list(providers)
        llm["council_providers"] = [providers[0]]
        llm["council_fallback_provider"] = providers[0]
        llm["synthesis_min_agree"] = 1 if len(providers) == 1 else 2
        route = dict(llm.get("routing") or {})
        route["calm_providers"] = list(providers[:2] if len(providers) > 1 else providers)
        route["stress_providers"] = list(providers)
        route["enabled"] = True
        llm["routing"] = route
        self.config["llm"] = llm

        path = Path(self.config.get("_config_path") or CONFIG_PATH)
        disk: dict = {}
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                disk = yaml.safe_load(f) or {}
        for key in order:
            sec = dict(disk.get(key) or {})
            sec["enabled"] = key in enabled_set
            disk[key] = sec
        disk_llm = dict(disk.get("llm") or {})
        disk_llm["analyzer_providers"] = list(providers)
        disk_llm["council_providers"] = [providers[0]]
        disk_llm["council_fallback_provider"] = providers[0]
        disk_llm["synthesis_min_agree"] = 1 if len(providers) == 1 else 2
        disk_route = dict(disk_llm.get("routing") or {})
        disk_route["calm_providers"] = list(providers[:2] if len(providers) > 1 else providers)
        disk_route["stress_providers"] = list(providers)
        disk_route["enabled"] = True
        disk_llm["routing"] = disk_route
        disk["llm"] = disk_llm

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(disk, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        data_cfg = PROJECT_ROOT / "data" / "config.yaml"
        if data_cfg.is_file() and data_cfg.resolve() != path.resolve():
            try:
                with open(data_cfg, "r", encoding="utf-8") as f:
                    d2 = yaml.safe_load(f) or {}
                for key in order:
                    s2 = dict(d2.get(key) or {})
                    s2["enabled"] = key in enabled_set
                    d2[key] = s2
                l2 = dict(d2.get("llm") or {})
                l2["analyzer_providers"] = list(providers)
                l2["council_providers"] = [providers[0]]
                l2["council_fallback_provider"] = providers[0]
                l2["synthesis_min_agree"] = 1 if len(providers) == 1 else 2
                r2 = dict(l2.get("routing") or {})
                r2["calm_providers"] = list(providers[:2] if len(providers) > 1 else providers)
                r2["stress_providers"] = list(providers)
                l2["routing"] = r2
                d2["llm"] = l2
                with open(data_cfg, "w", encoding="utf-8") as f:
                    yaml.dump(d2, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            except Exception as e:
                logger.debug("同步 data/config.yaml LLM 开关跳过: %s", e)

    def _want_startup(self, key: str, default: bool = True) -> bool:
        startup = self.config.get("startup") or {}
        if bool(startup.get("auto_enable_all", False)):
            return bool(startup.get(key, default))
        return bool(startup.get(key, False))

    def _startup_automation(self):
        """打开软件后自动开启：跟单、监控、AI全自动、扫描器、价格预警、复盘学习。"""
        startup = self.config.get("startup") or {}
        autopilot_cfg = self.config.get("autopilot") or {}
        auto_all = bool(startup.get("auto_enable_all", False))
        ap_mode = str(autopilot_cfg.get("mode") or "off").lower()
        automation = self.config.get("automation") or {}

        if not auto_all and ap_mode in ("off",) and not bool(
            self.config.get("auto_run", {}).get("enabled", False)
        ):
            return

        enabled_parts = []
        try:
            # 全自动剖面：让 TradeAdvisor 跟三家综合方向
            try:
                ai_cfg = self.config.get("ai_trading") or {}
                if hasattr(self, "trade_advisor") and self.trade_advisor is not None:
                    self.trade_advisor.follow_ai_direction = bool(
                        ai_cfg.get("follow_ai_direction", True)
                    )
            except Exception as e:
                logger.debug("startup follow_ai_direction: %s", e)

            if self._want_startup("auto_follow", True):
                if hasattr(self, "auto_paper_var"):
                    self.auto_paper_var.set(True)
                enabled_parts.append("自动跟单")

            if self._want_startup("paper_watcher", True) or bool(
                (self.config.get("paper_trading") or {}).get("auto_start_watcher", True)
            ):
                if not self.paper_engine.is_watching():
                    interval = float(
                        self.config.get("paper_trading", {}).get("poll_interval", 15)
                    )
                    self.paper_engine.start_watcher(interval=interval)
                    if hasattr(self, "paper_status_var"):
                        self.paper_status_var.set(f"watcher: 运行中 ({interval}s)")
                enabled_parts.append("模拟盘监控")

            use_fullauto = ap_mode in ("fullauto", "unified", "legacy") or self._want_startup(
                "ai_fullauto", ap_mode in ("fullauto", "unified", "legacy")
            )
            if use_fullauto and not getattr(self, "_ai_fullauto_running", False):
                interval_min = int(
                    autopilot_cfg.get("interval_minutes")
                    or self.config.get("auto_run", {}).get("interval_minutes", 60)
                )
                self._ai_fullauto_interval = interval_min
                if hasattr(self, "auto_run_interval_var"):
                    self.auto_run_interval_var.set(str(interval_min))
                if hasattr(self, "ai_fullauto_var"):
                    self.ai_fullauto_var.set(True)
                self._start_ai_fullauto()
                enabled_parts.append("AI全自动(三家综合)")
            elif (
                bool(self.config.get("auto_run", {}).get("enabled", False))
                or self._want_startup("auto_run", False)
            ) and not getattr(self, "_scheduler_running", False):
                self._start_auto_run()
                enabled_parts.append("定时分析")

            sc_cfg = self.config.get("signal_scanner") or {}
            if self._want_startup("signal_scanner", sc_cfg.get("enabled", True)):
                if not getattr(self, "_scanner_running", False):
                    if sc_cfg.get("scan_interval") and hasattr(self, "_scanner_interval_var"):
                        self._scanner_interval_var.set(str(sc_cfg["scan_interval"]))
                    if sc_cfg.get("cooldown_seconds") and hasattr(self, "_scanner_cooldown_var"):
                        self._scanner_cooldown_var.set(str(sc_cfg["cooldown_seconds"]))
                    if sc_cfg.get("min_strength") is not None and hasattr(
                        self, "_scanner_min_strength_var"
                    ):
                        self._scanner_min_strength_var.set(str(sc_cfg["min_strength"]))
                    if hasattr(self, "_scanner_trigger_fullauto_var"):
                        self._scanner_trigger_fullauto_var.set(
                            bool(sc_cfg.get("trigger_fullauto", True))
                        )
                    self._start_scanner()
                enabled_parts.append("信号扫描")

            pa_cfg = self.config.get("price_alert") or {}
            if self._want_startup("price_alert", pa_cfg.get("enabled", True)):
                try:
                    if hasattr(self, "alert_engine") and not self.alert_engine.is_running():
                        self.alert_engine.start()
                        if hasattr(self, "alert_status_var"):
                            self.alert_status_var.set(
                                f"状态: 运行中 ({self.alert_engine.symbol})"
                            )
                    enabled_parts.append("价格预警")
                except Exception as e:
                    logger.debug("startup price_alert: %s", e)

            if self._want_startup("auto_review", True):
                enabled_parts.append("自动复盘")

            if self._want_startup("intelligence_loop", True):
                enabled_parts.append("智能闭环")

            profile = automation.get("profile") or "autonomous_trader"
            desc = automation.get("description") or "盯盘→分析→跟单→学习"
            msg = (
                f"全自动已启动 [{profile}]: "
                + " · ".join(enabled_parts)
                + f" | {desc}"
            )
            self.update_status(msg)
            if hasattr(self, "ai_fullauto_status_var") and use_fullauto:
                self.ai_fullauto_status_var.set("全自动:运行中")
            if not enabled_parts and not auto_all:
                self.update_status("启动完成（未配置 startup.auto_enable_all）")
        except Exception as e:
            logger.exception("startup automation: %s", e)
            self.update_status(f"自动启动异常: {e}")

    def _toggle_auto_run(self):
        if self._scheduler_running:
            self._stop_auto_run()
        else:
            self._start_auto_run()

    def _start_auto_run(self):
        if self._scheduler_running:
            return
        # 与 AI 全自动互斥：全自动已接管周期调度
        if getattr(self, "_ai_fullauto_running", False):
            self.update_status("全自动运行中，定时分析已跳过（避免双调度）")
            return
        try:
            mins = int(self.auto_run_interval_var.get())
        except Exception:
            mins = 60
        if mins < 5:
            mins = 5
        self._scheduler_running = True
        # 启动后稍等再跑第一轮，避免与启动自动化抢锁；随后按间隔循环
        self._scheduler_next_ts = time.time() + 5
        self._scheduler_interval_sec = mins * 60
        self._scheduler_runs = int(getattr(self, "_scheduler_runs", 0) or 0)
        self._scheduler_skips = int(getattr(self, "_scheduler_skips", 0) or 0)
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, args=(mins * 60,), daemon=True
        )
        self._scheduler_thread.start()
        try:
            self.auto_run_btn.config(text="停止定时分析")
        except Exception:
            pass
        self._update_auto_run_status()
        self.update_status(f"定时分析已启动: 每 {mins} 分钟一次（5 秒后首轮）")

    def _stop_auto_run(self):
        self._scheduler_running = False
        try:
            self.auto_run_btn.config(text="启动定时分析")
        except Exception:
            pass
        try:
            self.auto_run_status_var.set("定时: 关闭")
        except Exception:
            pass
        self.update_status("定时分析已停止")

    def _analysis_busy(self) -> bool:
        """分析是否仍在进行（线程或标志）。卡死超过 2 个间隔则强制释放。"""
        thread_alive = bool(
            getattr(self, "analysis_thread", None) is not None
            and self.analysis_thread.is_alive()
        )
        flag = bool(getattr(self, "_analysis_running", False))
        if not thread_alive and not flag:
            return False

        # 卡死看门狗：标志 True 但线程已死，或占用过久
        started = float(getattr(self, "_analysis_started_ts", 0) or 0)
        interval = float(getattr(self, "_scheduler_interval_sec", 0) or 0)
        if interval <= 0:
            try:
                interval = float(self.auto_run_interval_var.get()) * 60
            except Exception:
                interval = 3600
        max_hold = max(interval * 2, 20 * 60)
        stale = (not thread_alive and flag) or (
            started > 0 and (time.time() - started) > max_hold
        )
        if stale:
            self._analysis_running = False
            self._analysis_started_ts = 0
            try:
                self.root.after(0, lambda: self.update_status(
                    "定时: 检测到分析锁卡住，已强制释放，本轮将重试"
                ))
            except Exception:
                pass
            return False
        return True

    def _scheduler_loop(self, interval_seconds: int):
        """后台调度循环: 到点触发主线程跳 start_analysis."""
        self._scheduler_interval_sec = interval_seconds
        while self._scheduler_running:
            now = time.time()
            if now >= self._scheduler_next_ts:
                if self._analysis_busy():
                    self._scheduler_skips = int(getattr(self, "_scheduler_skips", 0) or 0) + 1
                    try:
                        self.root.after(0, lambda: self.update_status(
                            f"定时触发: 上次分析未完成, 跳过本轮 "
                            f"(已跳 {self._scheduler_skips})"
                        ))
                    except Exception:
                        pass
                    # 未完成时缩短重试，避免整点空等一个完整间隔
                    retry = min(60, max(15, interval_seconds // 10))
                    self._scheduler_next_ts = time.time() + retry
                else:
                    try:
                        self.root.after(0, self._auto_run_trigger)
                    except Exception:
                        pass
                    self._scheduler_next_ts = time.time() + interval_seconds
            try:
                self.root.after(0, self._update_auto_run_status)
            except Exception:
                pass
            for _ in range(2):
                if not self._scheduler_running:
                    break
                time.sleep(0.5)

    def _auto_run_trigger(self):
        """主线程中调起 start_analysis."""
        try:
            if self._analysis_busy():
                self._scheduler_skips = int(getattr(self, "_scheduler_skips", 0) or 0) + 1
                self.update_status(
                    f"定时触发: 分析仍在进行, 跳过 (已跳 {self._scheduler_skips})"
                )
                return
            self._scheduler_runs = int(getattr(self, "_scheduler_runs", 0) or 0) + 1
            self.update_status(f"定时触发第 {self._scheduler_runs} 轮分析...")
            self.start_analysis()
        except Exception as e:
            self.update_status(f"定时触发失败: {e}")

    def _update_auto_run_status(self):
        if not self._scheduler_running:
            return
        remain = max(0, int(self._scheduler_next_ts - time.time()))
        mm, ss = divmod(remain, 60)
        runs = int(getattr(self, "_scheduler_runs", 0) or 0)
        skips = int(getattr(self, "_scheduler_skips", 0) or 0)
        try:
            self.auto_run_status_var.set(
                f"定时: 运行中 | 下次 {mm:02d}:{ss:02d} | 已跑 {runs} | 已跳 {skips}"
            )
        except Exception:
            pass

    def _trigger_autopilot_once(self):
        """扫描器 / 外部触发：执行单次完整分析周期（不启动无限 fullauto 循环）。"""
        if getattr(self, "_analysis_running", False):
            self.update_status("Autopilot: 上次分析未完成，跳过")
            return
        cycle = int(getattr(self, "_ai_fullauto_cycle", 0)) + 1
        self._ai_fullauto_cycle = cycle
        self.update_status(f"Autopilot 单次触发 (cycle {cycle})...")
        self._autopilot_oneshot = True
        threading.Thread(
            target=self._ai_fullauto_analysis_worker, args=(cycle,), daemon=True
        ).start()

    def _toggle_ai_fullauto(self):
        """切换AI全自动模式"""
        if self._ai_fullauto_running:
            self._stop_ai_fullauto()
        else:
            self._start_ai_fullauto()

    def _start_ai_fullauto(self):
        """启动AI全自动模式"""
        if getattr(self, "_analysis_running", False):
            self.update_status("分析进行中，请稍后再启全自动")
            return
        if getattr(self, "_scheduler_running", False):
            self._stop_auto_run()
            self.update_status("已停止定时分析，避免与全自动冲突")
        self._ai_fullauto_running = True
        self._ai_fullauto_cycle = 0
        self._ai_fullauto_interval = int(
            (self.config.get("autopilot") or {}).get("interval_minutes")
            or self.config.get("auto_run", {}).get("interval_minutes", 60)
        )
        try:
            self.ai_fullauto_btn.config(text="🔴 停止AI全自动")
        except Exception:
            pass
        self.ai_fullauto_status_var.set("AI全自动: 运行中 🚀")
        if hasattr(self, "ai_fullauto_var"):
            try:
                self.ai_fullauto_var.set(True)
            except Exception:
                pass
        self.update_status("🚀 AI全自动模式已启动!")
        # 确保自动跟单开启
        self.auto_paper_var.set(True)
        # 启动自动跟单watcher
        if not self.paper_engine.is_watching():
            interval = float(self.config.get('paper_trading', {}).get('poll_interval', 15))
            self.paper_engine.start_watcher(interval=interval)
        # 开始第一轮
        self._ai_fullauto_next_cycle()

    def _stop_ai_fullauto(self):
        """停止AI全自动模式"""
        self._ai_fullauto_running = False
        try:
            self.ai_fullauto_btn.config(text="🤖 AI全自动")
        except Exception:
            pass
        self.ai_fullauto_status_var.set("AI全自动: 关闭")
        if hasattr(self, "ai_fullauto_var"):
            try:
                self.ai_fullauto_var.set(False)
            except Exception:
                pass
        self.update_status("⏹️ AI全自动模式已停止")

    def _ai_fullauto_next_cycle(self):
        """调度下一轮全自动循环"""
        if not self._ai_fullauto_running:
            return
        self._ai_fullauto_cycle += 1
        cycle = self._ai_fullauto_cycle
        self.update_status(f"🤖 第 {cycle} 轮全自动分析...")
        # 在后台线程运行分析
        threading.Thread(target=self._ai_fullauto_analysis_worker, args=(cycle,), daemon=True).start()

    def _ai_fullauto_analysis_worker(self, cycle: int):
        """AI全自动分析工作线程"""
        continuous = bool(getattr(self, "_ai_fullauto_running", False))
        oneshot = bool(getattr(self, "_autopilot_oneshot", False))
        if not continuous and not oneshot:
            return
        if getattr(self, "_analysis_running", False):
            self.root.after(0, lambda: self.update_status(f"[{cycle}] 跳过: 分析仍在进行"))
            if continuous:
                self._schedule_next_cycle(60)
            return
        self._analysis_running = True
        self._analysis_started_ts = time.time()
        try:
            # 1. 获取市场数据
            self.root.after(0, lambda: self.update_status(f"[{cycle}] 获取市场数据..."))
            symbol = self.symbol_var.get()
            timeframe = self.timeframe_var.get()
            days = int(self.days_var.get())

            try:
                df = self.fetcher.get_historical_klines(
                    symbol=symbol, interval=timeframe, start_str=f"{days} days ago"
                )
                if df is None or len(df) == 0:
                    raise Exception("获取K线失败")
                current_price = float(self.fetcher.resolve_current_price(symbol, df) or 0)
                if current_price <= 0:
                    raise Exception("无法获取有效现价（ticker 失败且 K 线过旧）")
            except Exception as e:
                self.root.after(0, lambda: self.update_status(f"[{cycle}] 数据获取失败: {e}"))
                if continuous:
                    self._schedule_next_cycle(60)
                return

            # 2. 获取AI分析结果
            self.root.after(0, lambda: self.update_status(f"[{cycle}] AI分析中... 价格={current_price}"))
            ai_result = self._ai_fullauto_get_advice(symbol, timeframe, df)

            if not ai_result:
                self.root.after(0, lambda: self.update_status(f"[{cycle}] AI分析无结果，等待下一轮"))
                self._schedule_next_cycle(self._ai_fullauto_interval * 60)
                return

            action = ai_result.get('action', 'WAIT')
            raw_action = ai_result.get('raw_action', action)
            confidence = ai_result.get('confidence', 0)
            passed_gate = ai_result.get('passed_gate', False)
            ctx = ai_result.get('execution_context') or {}
            effective_dir = ctx.get('effective_direction') or raw_action

            # 刷新 GUI 决策 Tab（全自动与手动分析同屏展示）
            try:
                meta = ai_result.get("_fullauto_meta") or {}
                self.root.after(0, lambda: self._push_fullauto_analysis_ui(
                    cycle, ai_result, symbol, timeframe, current_price, meta,
                ))
            except Exception as ui_e:
                logger.debug(f"全自动 UI 刷新: {ui_e}")

            # 4. 记录信号到追踪表（无论是否跟单都记录）
            try:
                regime_tag = (self.analysis_result or {}).get('market_regime', {}).get('regime', '') if self.analysis_result else ''
                # fullauto 模式下 analysis_result 可能为空，从 ai_result 取 regime
                if not regime_tag and isinstance(ai_result.get('market_regime'), dict):
                    regime_tag = ai_result['market_regime'].get('regime', '')
                elif not regime_tag and isinstance(ai_result.get('market_regime'), str):
                    regime_tag = ai_result['market_regime']
                sid = self.paper_engine.track_signal(ai_result, market_regime=regime_tag)
                ai_result['_signal_tracking_id'] = sid
            except Exception as _e:
                logger.debug(f"全自动信号追踪记录失败: {_e}")

            # 5. 自动执行交易（跟单纪律见 execution_context / require_gate_pass）
            equity = self._get_account_balance()
            margin_required = float(
                ((ai_result.get("position") or {}).get("margin_required")) or 0
            )
            from bnb_quant_tool.config_access import is_margin_insufficient
            from bnb_quant_tool.ai_trading_context import needs_relaxed_open
            margin_state = self.paper_engine.get_margin_state(equity)
            margin_blocked = is_margin_insufficient(
                equity,
                self.paper_engine.get_open_positions(),
                margin_required,
                total_realized_pnl=self.paper_engine.get_total_realized_pnl(),
            )
            can_open = should_open_from_advice(ai_result, self.config)
            execute_pid = None
            execute_opened = False

            if not can_open and effective_dir in ('LONG', 'SHORT'):
                reason = ctx.get('follow_reason') or '; '.join(
                    (ai_result.get('gate_reasons') or ['未通过门控'])[:2]
                )
                self.root.after(0, lambda r=reason: self.update_status(
                    f"[{cycle}] 🚫 方向 {effective_dir} 未跟单: {r}"
                ))
            elif not can_open and action in ('LONG', 'SHORT'):
                reasons = ai_result.get('gate_reasons') or ['未通过门控']
                self.root.after(0, lambda r=reasons: self.update_status(
                    f"[{cycle}] 🚫 信号 {action} 被拦截: {'; '.join(r[:2])}"
                ))
            elif margin_blocked:
                avail = margin_state.get("available_margin", 0)
                used = margin_state.get("used_margin", 0)
                self.root.after(0, lambda: self.update_status(
                    f"[{cycle}] 💰 保证金已用尽 (可用 {avail:.2f} / 已占 {used:.2f} USDT)，等待平仓释放"
                ))
            elif can_open:
                if ai_result.get("_multi_agent_position_id"):
                    execute_pid = int(ai_result["_multi_agent_position_id"])
                    execute_opened = True
                    self.root.after(0, lambda: self.update_status(
                        f"[{cycle}] 多智能体已开仓 #{ai_result['_multi_agent_position_id']}"
                    ))
                else:
                    relaxed = needs_relaxed_open(ai_result, self.config)
                    open_dir = effective_dir if effective_dir in ('LONG', 'SHORT') else action
                    pid = self.paper_engine.open_from_advice(
                        ai_result, equity_usdt=equity,
                        learning_record_id=self.last_record_id,
                        relaxed=relaxed,
                    )
                    if pid:
                        execute_pid = int(pid)
                        execute_opened = True
                        try:
                            entry_price = ai_result.get('prices', {}).get('entry_mid', current_price)
                            self.paper_engine.mark_signal_followed(
                                ai_result.get('_signal_tracking_id', 0),
                                actual_entry=float(entry_price)
                            )
                        except Exception:
                            pass
                        status_tag = (
                            f"风控=WAIT·按{open_dir}" if action == "WAIT" and open_dir in ("LONG", "SHORT")
                            else open_dir
                        )
                        self.root.after(0, lambda t=status_tag: self.update_status(
                            f"[{cycle}] ✅ 自动开仓 #{pid} {t} 置信={confidence:.0%} 价格={current_price}"
                        ))
                        if not self.paper_engine.is_watching():
                            self.paper_engine.start_watcher(interval=15)
                    else:
                        self.root.after(0, lambda: self.update_status(f"[{cycle}] 开仓失败"))
            else:
                self.root.after(0, lambda: self.update_status(
                    f"[{cycle}] ⏸️ 等待更好机会 (信号={action} 置信={confidence:.0%} 门控={'通过' if passed_gate else '未过'})"
                ))

            try:
                from bnb_quant_tool.intelligence_loop import get_or_create_loop
                _loop = get_or_create_loop(
                    self, learner=self.learner, config=self.config
                )
                _loop.mark_execute(
                    getattr(self, "_last_loop_cycle", None),
                    position_id=execute_pid,
                    opened=execute_opened,
                )
            except Exception:
                pass

            # 6. 平仓事件会触发 _maybe_trigger_auto_review，此处不再重复检查

            # 7. 调度下一轮（仅 continuous fullauto 模式）
            if continuous:
                self._schedule_next_cycle(self._ai_fullauto_interval * 60)

        except Exception as e:
            self.root.after(0, lambda: self.update_status(f"[{cycle}] 全自动分析异常: {e}"))
            if continuous:
                self._schedule_next_cycle(120)
        finally:
            # 必须释放，否则定时/全自动后续轮次会永久跳过
            self._analysis_running = False
            if oneshot:
                self._autopilot_oneshot = False

    def _apply_multi_agent_deliberation(
        self,
        trade_advice: Dict,
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
    ) -> Dict:
        """运行四 Agent 协同决策，风控拥有一票否决权。"""
        if not self.multi_agent:
            logger.debug("多智能体未初始化，跳过协同决策")
            return trade_advice

        open_count = len(self.paper_engine.get_open_positions())
        advice_copy = dict(trade_advice)
        advice_copy["open_positions"] = open_count
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

        equity = self._get_account_balance()
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

    def _push_fullauto_analysis_ui(
        self, cycle, trade_advice, symbol, timeframe, current_price, meta,
    ):
        """将全自动分析结果推送到决策驾驶舱。"""
        try:
            from datetime import datetime
            ai_analysis = meta.get("ai_analysis") or {}
            inst_results = meta.get("institutional") or {}
            learning_context = meta.get("learning_context") or {}
            indicators = meta.get("indicators") or {}
            market_regime = trade_advice.get("market_regime") or meta.get("market_regime") or {}

            from bnb_quant_tool.analysis_pipeline import attach_ta_playbook

            self.analysis_result = attach_ta_playbook({
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "data_points": 0,
                "current_price": current_price,
                "strategy_mode": "ai_fullauto",
                "indicators": indicators,
                "institutional_strategies": inst_results,
                "ai_analysis": ai_analysis,
                "ai_analyses": meta.get("ai_analyses") or {},
                "ai_analysis_note": meta.get("ai_analysis_note") or "",
                "ai_primary_provider": meta.get("ai_primary_provider") or "",
                "trading_plan": {"action": trade_advice.get("action")},
                "risk_check": {
                    "passed": trade_advice.get("passed_gate", False),
                    "reason": "; ".join((trade_advice.get("gate_reasons") or [])[:2]),
                },
                "final_recommendation": trade_advice.get("action", "WAIT"),
                "trade_advice": trade_advice,
                "market_regime": market_regime,
                "learning_context": learning_context,
                "news_summary": meta.get("news_summary") or {},
                "sentiment": meta.get("sentiment") or {},
                "multi_timeframe": meta.get("mtf") or {},
            }, config=self.config, account_balance=self._get_account_balance())
            if self.analysis_result.get("ta_playbook"):
                trade_advice["ta_playbook"] = self.analysis_result["ta_playbook"]
            self.update_output()
            if hasattr(self, "ai_chain_var"):
                self.ai_chain_var.set(
                    f"🤖 全自动第{cycle}轮 | {trade_advice.get('action')} "
                    f"置信{float(trade_advice.get('confidence', 0)):.0%} | "
                    f"门控{'通过' if trade_advice.get('passed_gate') else '拦截'}"
                )
        except Exception as e:
            logger.debug(f"push fullauto ui: {e}")

    def _get_account_balance(self) -> float:
        """读取界面本金，回退到 config。"""
        try:
            return float(self.balance_var.get())
        except (ValueError, TypeError):
            return float(self.config.get("trading", {}).get("account_balance", 5000.0))

    def _ai_fullauto_get_advice(self, symbol: str, timeframe: str, df):
        """AI 全自动 — 与手动分析同级别的完整决策链路（含多周期/情绪/模式记忆/动态仓位）"""
        try:
            account_balance = self._get_account_balance()
            self.trade_advisor.account_balance = account_balance

            indicators = TechnicalIndicators.calculate_all_indicators(df)
            current_price = float(self.fetcher.resolve_current_price(symbol, df) or 0)
            if current_price <= 0:
                raise Exception("无法获取有效现价（ticker 失败且 K 线过旧）")
            market_regime = self.regime_detector.detect(df, indicators=indicators)

            # 智能闭环预热（与手动分析 / headless 对齐）
            try:
                from bnb_quant_tool.intelligence_loop import get_or_create_loop
                _loop = get_or_create_loop(
                    self,
                    learner=self.learner,
                    config=self.config,
                    paper_engine=self.paper_engine,
                    pattern_memory=getattr(self, "pattern_memory", None),
                    counterfactual=getattr(self, "counterfactual", None),
                )
                self._last_loop_cycle = _loop.begin_cycle(symbol)
                _loop.preflight(symbol=symbol, current_price=current_price)
                _loop.mark_perceive(self._last_loop_cycle, price=current_price)
            except Exception as pe:
                logger.debug(f"fullauto intelligence_loop preflight: {pe}")

            # 多周期 + 市场情绪（全自动必跑，比纯定时盲等更准）
            mtf_result = {}
            sentiment = {}
            onchain_data = {}
            macro_data = {}
            bnb_factors_data = {}
            orderflow_data = {}
            try:
                mtf_result = self.mtf_analyzer.analyze(symbol=symbol) or {}
            except Exception as e:
                logger.debug(f"全自动多周期失败: {e}")
            try:
                sentiment = self.sentiment_engine.fetch_all(symbol=symbol) or {}
            except Exception as e:
                logger.debug(f"全自动情绪失败: {e}")
            try:
                of_cfg = (self.config or {}).get("orderflow") or {}
                if of_cfg.get("enabled", True):
                    from bnb_quant_tool.orderflow_signal import fetch_orderflow
                    orderflow_data = fetch_orderflow(symbol, self.config) or {}
            except Exception as e:
                logger.debug(f"全自动订单流失败: {e}")
            if self.onchain_analyzer:
                try:
                    onchain_data = self.onchain_analyzer.fetch_all(symbol=symbol) or {}
                except Exception as e:
                    logger.debug(f"全自动链上失败: {e}")
            if self.macro_layer:
                try:
                    macro_data = self.macro_layer.fetch_all() or {}
                except Exception as e:
                    logger.debug(f"全自动宏观失败: {e}")

            from bnb_quant_tool.llm_provider import (
                build_llm_analyzer,
                get_llm_credentials,
            )
            from bnb_quant_tool.analysis_reuse import (
                evaluate_analysis_reuse,
                mark_advice_reused,
                run_market_analyses_with_reuse,
            )
            llm_creds = get_llm_credentials(self.config)
            api_key = llm_creds.get("api_key") or ""
            if not api_key:
                return None

            ai_analyzer = build_llm_analyzer(self.config)
            if self.multi_agent:
                self.multi_agent.ai_analyzer = ai_analyzer

            # 尽早判断知识复用，避免新闻摘要空烧
            early_regime = self.regime_detector.detect(
                df,
                indicators=indicators,
                news_summary={},
                sentiment=sentiment or None,
                bnb_factors=None,
            )
            reuse_hit = evaluate_analysis_reuse(
                config=self.config,
                symbol=symbol,
                indicators=indicators,
                market_regime=early_regime,
                learner=self.learner,
            )

            news_summary = {}
            news_items = []
            try:
                news_items = self.news_collector.collect(
                    symbol=symbol.replace("USDT", ""), hours=24, max_items=30
                )
                if news_items and not reuse_hit:
                    news_summary = ai_analyzer.summarize_news(
                        news_items, symbol=symbol.replace("USDT", "")
                    ) or {}
                    from bnb_quant_tool.news_decay import adjust_news_summary
                    news_summary = adjust_news_summary(news_summary, news_items)
                    market_regime = self.regime_detector.detect(
                        df, indicators=indicators,
                        news_summary=news_summary,
                    )
                elif news_items and reuse_hit:
                    news_summary = {
                        "summary": "知识复用轮次：跳过新闻 LLM 摘要",
                        "polarity": 0.0,
                        "bias": "NEUTRAL",
                        "_reused_skip": True,
                    }
            except Exception as e:
                logger.debug(f"全自动新闻失败: {e}")

            if getattr(self, "bnb_factors_engine", None):
                try:
                    bnb_factors_data = self.bnb_factors_engine.fetch_all(
                        symbol=symbol, news_items=news_items or None,
                    ) or {}
                    btc_lead = {}
                    try:
                        from bnb_quant_tool.btc_lead_indicator import compute_btc_lead_indicator
                        if symbol != "BTCUSDT":
                            btc_df = self.fetcher.get_historical_klines(
                                symbol="BTCUSDT", interval=timeframe,
                                start_str=f"{int(self.days_var.get())} days ago",
                            )
                            btc_lead = compute_btc_lead_indicator(
                                df, btc_df, config=(self.config.get("btc_lead") or {}),
                            )
                            bnb_factors_data["btc_lead"] = btc_lead
                    except Exception as ble:
                        logger.debug(f"全自动 BTC领先: {ble}")
                except Exception as e:
                    logger.debug(f"全自动 BNB 专属因子失败: {e}")

            market_regime = self.regime_detector.detect(
                df,
                indicators=indicators,
                news_summary=news_summary or {},
                sentiment=sentiment or None,
                bnb_factors=bnb_factors_data or None,
            )

            from bnb_quant_tool.ai_trading_context import (
                analysis_extra_market,
                build_full_analysis_learning_context,
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
                news_summary=news_summary or None,
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=news_summary.get("polarity") if news_summary else None,
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
                counterfactual=getattr(self, "counterfactual", None),
                inst_results=inst_results,
                news_summary=news_summary or None,
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=news_summary.get("polarity") if news_summary else None,
                    mtf_action=(mtf_result or {}).get("recommended_action"),
                ),
            )

            if not reuse_hit:
                reuse_hit = evaluate_analysis_reuse(
                    config=self.config,
                    symbol=symbol,
                    indicators=indicators,
                    market_regime=market_regime,
                    learner=self.learner,
                )
            if reuse_hit and reuse_hit.reuse:
                ai_bundle = reuse_hit.to_ai_bundle()
            else:
                ai_bundle = run_market_analyses_with_reuse(
                    self.config,
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
            ai_analysis = ai_bundle["primary"]
            ai_analyses = ai_bundle.get("by_provider") or {}
            ai_analysis_note = ai_bundle.get("note") or ""
            ai_primary_provider = ai_bundle.get("primary_provider") or ""

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
                news_summary=news_summary or None,
                extra_market=analysis_extra_market(
                    self.config,
                    news_polarity=news_summary.get("polarity") if news_summary else None,
                    mtf_action=(mtf_result or {}).get("recommended_action"),
                ),
            )

            from bnb_quant_tool.factor_attribution_learner import compute_reliability_multipliers
            factor_rel = (
                learning_context.get("factor_reliability")
                or compute_reliability_multipliers(
                    learning_context.get("factor_attribution")
                )
            )
            learning_context["factor_reliability"] = factor_rel

            from bnb_quant_tool.institutional_conviction import compute_institutional_conviction
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
            learning_context["institutional_conviction"] = institutional_conviction
            inst_results["_conviction_family"] = institutional_conviction.get("strategy_family") or {}

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
                leverage=int(self.leverage_var.get()),
                factor_reliability=factor_rel,
                analysis_mode=(
                    self.strategy_var.get()
                    if getattr(self, "autopilot", None)
                    and (self.config.get("autopilot") or {}).get("respect_analysis_mode", True)
                    else "all"
                ),
            )
            try:
                from bnb_quant_tool.signal_scanner import inject_scanner_into_advice
                inject_scanner_into_advice(trade_advice, symbol=symbol)
            except Exception:
                pass

            trade_advice = mark_advice_reused(trade_advice, ai_analysis, ai_bundle)

            # 统一后处理门控 + 多智能体
            from bnb_quant_tool.analysis_pipeline import (
                apply_post_advice_gates,
                attach_decision_explanation,
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
            learning_context["onchain"] = onchain_data or {}
            learning_context["macro"] = macro_data or {}
            learning_context["market_regime"] = market_regime
            learning_context["news_summary"] = news_summary or {}
            learning_context["_learner"] = self.learner
            learning_context["_paper_engine"] = self.paper_engine
            trade_advice = apply_post_advice_gates(
                trade_advice,
                learning_context=learning_context,
                config=self.config,
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
                multi_agent_fn=self._apply_multi_agent_deliberation,
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
                    # 开仓统一由 fullauto worker 在入库后执行，避免无 learning_record_id 双开
                    "execute": False,
                },
            )

            trade_advice["institutional_conviction"] = institutional_conviction
            if institutional_conviction.get("summary") and trade_advice.get("report_text"):
                trade_advice["report_text"] = (
                    institutional_conviction["summary"] + "\n\n" + trade_advice["report_text"]
                )
            try:
                from bnb_quant_tool.llm_provider import format_ai_analyses_report_block
                dual_ai_txt = format_ai_analyses_report_block(
                    primary=ai_analysis,
                    by_provider=ai_analyses,
                    note=ai_analysis_note or "",
                )
                if dual_ai_txt.strip():
                    base_report = trade_advice.get("report_text") or ""
                    if "[3]" not in base_report and "三家综合" not in base_report:
                        trade_advice["report_text"] = (
                            dual_ai_txt + ("\n" + base_report if base_report else "")
                        )
                    trade_advice["_ai_analyses_report"] = dual_ai_txt
            except Exception as _ae:
                logger.debug(f"fullauto format triple AI: {_ae}")

            # 动态仓位（含半凯利真实胜率 / 议会仓位系数 / 硬顶）
            try:
                from bnb_quant_tool.analysis_pipeline import apply_dynamic_position

                trade_advice = apply_dynamic_position(
                    trade_advice,
                    position_sizer=self.position_sizer,
                    regime_detector=self.regime_detector,
                    df=df,
                    indicators=indicators,
                    market_regime=market_regime,
                    multi_timeframe=mtf_result,
                    news_summary=news_summary or None,
                    config=self.config,
                )
            except Exception as dpe:
                logger.debug(f"全自动动态仓位: {dpe}")

            try:
                from bnb_quant_tool.factor_attribution_learner import (
                    compute_reliability_multipliers,
                )
                factor_rel = (
                    learning_context.get("factor_reliability")
                    or compute_reliability_multipliers(
                        learning_context.get("factor_attribution")
                    )
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
            except Exception as ex:
                logger.debug(f"全自动决策解释: {ex}")

            rid = self.learner.record_analysis({
                'symbol': symbol,
                'timeframe': timeframe,
                'current_price': current_price,
                'indicators': indicators,
                'ai_analysis': ai_analysis,
                'ai_analyses': ai_analyses,
                'ai_analysis_note': ai_analysis_note,
                'institutional': inst_results,
                'trade_advice': trade_advice,
                'market_regime': market_regime,
                'news_summary': news_summary,
                'multi_timeframe': mtf_result,
                'timestamp': datetime.now().isoformat(),
            })
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
                    logger.debug(f"on_analysis_recorded (fullauto): {ev_e}")
                try:
                    from bnb_quant_tool.intelligence_loop import get_or_create_loop
                    _loop = get_or_create_loop(self, learner=self.learner, config=self.config)
                    _loop.after_analysis(
                        getattr(self, "_last_loop_cycle", None),
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
                except Exception as le:
                    logger.debug(f"fullauto after_analysis: {le}")

            trade_advice['_indicators'] = indicators
            trade_advice['market_regime'] = market_regime
            trade_advice['multi_timeframe'] = mtf_result
            trade_advice['sentiment'] = sentiment
            trade_advice['onchain'] = onchain_data
            trade_advice['macro'] = macro_data
            trade_advice['_fullauto_meta'] = {
                'ai_analysis': ai_analysis,
                'ai_analyses': ai_analyses,
                'ai_analysis_note': ai_analysis_note,
                'ai_primary_provider': ai_primary_provider,
                'institutional': inst_results,
                'learning_context': learning_context,
                'indicators': indicators,
                'market_regime': market_regime,
                'news_summary': news_summary,
                'sentiment': sentiment,
                'mtf': mtf_result,
            }
            return trade_advice

        except Exception as e:
            logger.error(f"AI建议获取失败: {e}")
            return None

    def _schedule_next_cycle(self, delay_seconds: int):
        """调度下一轮循环"""
        if not self._ai_fullauto_running:
            return
        # 使用 after 调度下一轮
        self.root.after(delay_seconds * 1000, self._ai_fullauto_next_cycle)

