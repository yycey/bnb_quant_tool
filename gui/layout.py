"""Mixin: LayoutMixin — 像素风交易室主界面（左房间 / 右输出 / 底功能区）。"""

from gui._imports import *
from gui.trading_room import TradingRoomCanvas, TRADER_DEFS, PALETTE


class LayoutMixin:
    def create_widgets(self):
        self.root.configure(bg=PALETTE["floor"])
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = tk.Frame(self.root, bg=PALETTE["wood"], padx=6, pady=6)
        main.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        # 右侧决议区加宽，保证双模分析全文可见
        main.columnconfigure(0, weight=2)
        main.columnconfigure(1, weight=3)
        main.rowconfigure(1, weight=1)

        # ── 顶栏：快捷参数 ──
        top = tk.Frame(main, bg=PALETTE["wall"], pady=6, padx=8)
        top.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 6))

        self.ai_chain_var = tk.StringVar(
            value="6 交易员围桌讨论 → 共识 → 风控 → 执行"
        )
        tk.Label(
            top, textvariable=self.ai_chain_var,
            bg=PALETTE["wall"], fg=PALETTE["paper"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(top, text="标的", bg=PALETTE["wall"], fg=PALETTE["paper"]).pack(side=tk.LEFT)
        self.symbol_var = tk.StringVar(value=self.config.get("trading", {}).get("symbol", "BNBUSDT"))
        ttk.Combobox(
            top, textvariable=self.symbol_var,
            values=["BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"],
            width=10, state="readonly",
        ).pack(side=tk.LEFT, padx=4)

        tk.Label(top, text="周期", bg=PALETTE["wall"], fg=PALETTE["paper"]).pack(side=tk.LEFT, padx=(8, 0))
        self.timeframe_var = tk.StringVar(value=self.config.get("trading", {}).get("timeframe", "1h"))
        ttk.Combobox(
            top, textvariable=self.timeframe_var,
            values=["1m", "5m", "15m", "1h", "4h", "1d"],
            width=6, state="readonly",
        ).pack(side=tk.LEFT, padx=4)

        tk.Label(top, text="天数", bg=PALETTE["wall"], fg=PALETTE["paper"]).pack(side=tk.LEFT, padx=(8, 0))
        self.days_var = tk.StringVar(value=str(self.config.get("trading", {}).get("lookback_days", 360)))
        ttk.Spinbox(top, from_=7, to=720, textvariable=self.days_var, width=5).pack(side=tk.LEFT, padx=4)

        tk.Label(top, text="本金", bg=PALETTE["wall"], fg=PALETTE["paper"]).pack(side=tk.LEFT, padx=(8, 0))
        self.balance_var = tk.StringVar(
            value=str(float(self.config.get("trading", {}).get("account_balance", 5000)))
        )
        ttk.Entry(top, textvariable=self.balance_var, width=8).pack(side=tk.LEFT, padx=4)

        self.strategy_var = tk.StringVar(value="all")
        self.leverage_var = tk.StringVar(value="1")

        # ── 中部：左交易室 | 右决议（可左右拖拽加宽右侧）──
        mid_pane = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        mid_pane.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))

        room_wrap = tk.Frame(mid_pane, bg=PALETTE["table_edge"], bd=3, relief=tk.SUNKEN)
        room_wrap.rowconfigure(0, weight=1)
        room_wrap.columnconfigure(0, weight=1)
        mid_pane.add(room_wrap, weight=2)

        self.trading_room = TradingRoomCanvas(
            room_wrap,
            on_trader_click=self._on_room_trader_click,
            width=640,
            height=420,
        )
        self.trading_room.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.trading_room.set_quote(self.symbol_var.get(), None, None)
        self.symbol_var.trace_add("write", lambda *_: self._on_room_symbol_changed())
        self._room_price_poll_id = None
        self.root.after(400, self._poll_room_price)
        tk.Label(
            room_wrap,
            text="点击高清机器人 → 配置独立 AI Key / 策略 · 中间屏幕显示所选币种实时价",
            bg=PALETTE["table_edge"], fg=PALETTE["paper"],
            font=("Microsoft YaHei UI", 8),
        ).grid(row=1, column=0, sticky=tk.W, padx=6, pady=2)

        # ── 右：决策输出（加宽 + 可拖分栏，全文可滚）──
        right = tk.Frame(mid_pane, bg=PALETTE["wall"], bd=3, relief=tk.RIDGE)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        mid_pane.add(right, weight=3)

        tk.Label(
            right, text="📜 议会决议 / 策略输出",
            bg=PALETTE["wall"], fg=PALETTE["paper"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).grid(row=0, column=0, sticky=tk.W, padx=8, pady=(6, 2))

        # 迷你驾驶舱
        cockpit = tk.Frame(right, bg=PALETTE["wall"])
        cockpit.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=6, pady=2)
        for i in range(6):
            cockpit.columnconfigure(i, weight=1)
        self._cockpit_vars = {
            "direction": tk.StringVar(value="—"),
            "risk_action": tk.StringVar(value="—"),
            "conviction": tk.StringVar(value="—"),
            "regime": tk.StringVar(value="—"),
            "follow": tk.StringVar(value="—"),
            "ta_playbook": tk.StringVar(value="—"),
        }
        for i, (label, key, color) in enumerate([
            ("方向", "direction", "#FFE082"),
            ("风控", "risk_action", "#FFCCBC"),
            ("信念", "conviction", "#E1BEE7"),
            ("状态", "regime", "#C8E6C9"),
            ("跟单", "follow", "#B2DFDB"),
            ("TA", "ta_playbook", "#FFCC80"),
        ]):
            cell = tk.Frame(cockpit, bg=PALETTE["wood"], padx=4, pady=2)
            cell.grid(row=0, column=i, padx=2, sticky=(tk.W, tk.E))
            tk.Label(cell, text=label, bg=PALETTE["wood"], fg="#BCAAA4", font=("", 7)).pack()
            tk.Label(
                cell, textvariable=self._cockpit_vars[key],
                bg=PALETTE["wood"], fg=color,
                font=("", 8, "bold"), wraplength=110, justify=tk.CENTER,
            ).pack(fill=tk.X)

        right_pane = ttk.PanedWindow(right, orient=tk.VERTICAL)
        right_pane.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=6, pady=4)

        advice_wrap = tk.Frame(right_pane, bg=PALETTE["paper"])
        advice_wrap.rowconfigure(0, weight=1)
        advice_wrap.columnconfigure(0, weight=1)
        right_pane.add(advice_wrap, weight=5)

        self.advice_text = scrolledtext.ScrolledText(
            advice_wrap, wrap=tk.WORD, height=18,
            font=("Consolas", 9),
            bg=PALETTE["paper"], fg=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            relief=tk.FLAT, bd=0,
        )
        self.advice_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.advice_text.insert(
            "1.0",
            "欢迎来到交易员议会。\n\n"
            "左侧 6 位机器人围桌议事。\n"
            "点击「开始分析」后，每人独立表态，气泡显示投票。\n"
            "右侧输出最终决议与执行参数（可滚动看全文；拖中间分割条调高度）。\n\n"
            "提示：点击机器人可配置独立 AI Key。\n",
        )

        factors_wrap = tk.Frame(right_pane, bg="#EFE0C0")
        factors_wrap.rowconfigure(0, weight=1)
        factors_wrap.columnconfigure(0, weight=1)
        right_pane.add(factors_wrap, weight=2)

        self._cockpit_factors_text = scrolledtext.ScrolledText(
            factors_wrap, wrap=tk.WORD, height=10,
            font=("Consolas", 8),
            bg="#EFE0C0", fg=PALETTE["ink"],
        )
        self._cockpit_factors_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self._cockpit_factors_text.insert(
            "1.0",
            "因子分解 / 三家主分析综合 / 议会投票将显示在此（可滚动全文）。\n",
        )

        # 隐藏工具柜窗口（底部功能按钮会唤起）
        self._init_compat_panels()

        # ── 底：功能区 ──
        dock = tk.Frame(main, bg=PALETTE["table"], pady=8, padx=6)
        dock.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(6, 0))

        self.analyze_btn = tk.Button(
            dock, text="▶ 开始分析", command=self.start_analysis,
            bg="#2E7D32", fg="white", font=("Microsoft YaHei UI", 10, "bold"),
            relief=tk.RAISED, bd=3, padx=12, pady=4, activebackground="#1B5E20",
        )
        self.analyze_btn.pack(side=tk.LEFT, padx=4)

        self.fresh_analyze_btn = tk.Button(
            dock,
            text="✦ 全新分析",
            command=self.start_fresh_analysis,
            bg="#1565C0",
            fg="white",
            font=("Microsoft YaHei UI", 10, "bold"),
            relief=tk.RAISED,
            bd=3,
            padx=12,
            pady=4,
            activebackground="#0D47A1",
        )
        self.fresh_analyze_btn.pack(side=tk.LEFT, padx=4)
        self.fresh_analyze_btn.bind(
            "<Enter>",
            lambda _e: self.status_var.set(
                "全新分析：强制调用 LLM，不复用历史局面结论"
            ),
        )

        self.save_btn = tk.Button(
            dock, text="保存", command=self.save_result, state="disabled",
            bg=PALETTE["wall_light"], fg=PALETTE["ink"], padx=8, pady=4,
        )
        self.save_btn.pack(side=tk.LEFT, padx=2)

        self.feedback_btn = tk.Button(
            dock, text="反馈", command=self.submit_feedback_last, state="disabled",
            bg=PALETTE["wall_light"], fg=PALETTE["ink"], padx=8, pady=4,
        )
        self.feedback_btn.pack(side=tk.LEFT, padx=2)

        tk.Button(
            dock, text="清除", command=self.clear_output,
            bg=PALETTE["wall_light"], fg=PALETTE["ink"], padx=8, pady=4,
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            dock, text="复制决议", command=self._copy_advice_full,
            bg=PALETTE["wall_light"], fg=PALETTE["ink"], padx=8, pady=4,
        ).pack(side=tk.LEFT, padx=2)

        ttk.Separator(dock, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.ai_fullauto_var = tk.BooleanVar(
            value=bool(
                ((self.config.get("startup") or {}).get("ai_fullauto", False))
                or str((self.config.get("autopilot") or {}).get("mode") or "").lower()
                in ("fullauto", "unified", "legacy")
            )
        )
        self.ai_fullauto_btn = tk.Checkbutton(
            dock, text="AI全自动", variable=self.ai_fullauto_var,
            command=self._toggle_ai_fullauto,
            bg=PALETTE["table"], fg=PALETTE["paper"], selectcolor=PALETTE["wood"],
            activebackground=PALETTE["table"], font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.ai_fullauto_btn.pack(side=tk.LEFT, padx=4)
        _fa_on = bool(self.ai_fullauto_var.get())
        self.ai_fullauto_status_var = tk.StringVar(
            value=("全自动:待启动" if _fa_on else "全自动:关")
        )
        tk.Label(dock, textvariable=self.ai_fullauto_status_var, bg=PALETTE["table"], fg="#80CBC4").pack(side=tk.LEFT)

        self.auto_run_interval_var = tk.StringVar(
            value=str(int(self.config.get("auto_run", {}).get("interval_minutes", 60)))
        )
        self.auto_run_btn = tk.Button(
            dock, text="定时", command=self._toggle_auto_run,
            bg=PALETTE["wall_light"], fg=PALETTE["ink"], padx=6, pady=2,
        )
        self.auto_run_btn.pack(side=tk.LEFT, padx=4)
        self.auto_run_status_var = tk.StringVar(value="定时:关")
        tk.Label(dock, textvariable=self.auto_run_status_var, bg=PALETTE["table"], fg="#A5D6A7").pack(side=tk.LEFT)

        ttk.Separator(dock, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        # 主分析 LLM 开关（DeepSeek / 千问 / 豆包）
        self._init_llm_provider_vars()
        llm_bar = tk.Frame(dock, bg=PALETTE["table"])
        llm_bar.pack(side=tk.LEFT, padx=2)
        tk.Label(
            llm_bar, text="LLM:", bg=PALETTE["table"], fg="#FFE082",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 2))
        for key, label in (
            ("volcengine", "豆包"),
            ("deepseek", "DS"),
            ("qianwen", "千问"),
        ):
            tk.Checkbutton(
                llm_bar,
                text=label,
                variable=self._llm_enable_vars[key],
                command=self._on_llm_provider_toggle,
                bg=PALETTE["table"],
                fg=PALETTE["paper"],
                selectcolor=PALETTE["wood"],
                activebackground=PALETTE["table"],
                font=("Microsoft YaHei UI", 8),
            ).pack(side=tk.LEFT, padx=1)
        self._llm_provider_status_var = tk.StringVar(value="")
        tk.Label(
            llm_bar, textvariable=self._llm_provider_status_var,
            bg=PALETTE["table"], fg="#80CBC4",
            font=("Microsoft YaHei UI", 8),
        ).pack(side=tk.LEFT, padx=4)
        self._refresh_llm_provider_status()

        ttk.Separator(dock, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        tools = [
            ("💰 模拟盘", "paper"),
            ("🧠 复盘", "review"),
            ("📈 学习", "learning"),
            ("🧬 进化", "evolution"),
            ("📰 新闻", "news"),
            ("📡 扫描", "scanner"),
            ("🔔 预警", "alert"),
            ("📊 回测", "backtest"),
            ("🌊 情绪", "market"),
            ("📋 信号", "signal"),
            ("📐 网格", "grid"),
            ("📚 知识", "knowledge"),
            ("👥 配置", "traders"),
            ("🧾 摘要", "summary"),
            ("🏛 机构", "inst"),
            ("{ } JSON", "detail"),
        ]
        for label, key in tools:
            tk.Button(
                dock, text=label,
                command=lambda k=key: self._open_tool_window(k),
                bg="#A1887F", fg=PALETTE["ink"],
                font=("Microsoft YaHei UI", 8),
                relief=tk.RAISED, bd=2, padx=4, pady=2,
            ).pack(side=tk.LEFT, padx=1)

        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(4, 0))

        self.status_var = tk.StringVar(value="就绪 | 交易室已开启 | 点击机器人配置 Key")
        tk.Label(
            main, textvariable=self.status_var,
            bg=PALETTE["wood"], fg=PALETTE["paper"],
            anchor=tk.W, font=("Microsoft YaHei UI", 9),
        ).grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(4, 0))

        self.analysis_result = None
        self._auto_refresh_enabled = True
        self._auto_refresh_interval_ms = 30000
        self._active_tab_index = 0
        self._active_tool_key = None
        self._schedule_auto_refresh()

        try:
            self.root.after(600, self._refresh_room_from_config)
        except Exception:
            pass

    def _init_compat_panels(self):
        """工具柜：独立隐藏 Toplevel + Notebook，底部按钮唤起并切页。"""
        self._tools_win = tk.Toplevel(self.root)
        self._tools_win.title("工具柜")
        self._tools_win.geometry("1000x680")
        self._tools_win.withdraw()
        self._tools_win.protocol("WM_DELETE_WINDOW", self._hide_tools_win)

        self.notebook = ttk.Notebook(self._tools_win)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._panels = {}
        self._panel_index = {}

        def _panel(key, title, rowspan=0):
            fr = ttk.Frame(self.notebook)
            self.notebook.add(fr, text=title)
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(rowspan if rowspan else 0, weight=1)
            self._panels[key] = fr
            self._panel_index[key] = len(self._panels) - 1
            return fr

        summary_frame = _panel("summary", "摘要")
        self.summary_text = scrolledtext.ScrolledText(
            summary_frame, wrap=tk.WORD, height=22, font=("Consolas", 10)
        )
        self.summary_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        inst_frame = _panel("inst", "机构")
        self.inst_text = scrolledtext.ScrolledText(
            inst_frame, wrap=tk.WORD, height=22, font=("Consolas", 9)
        )
        self.inst_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        detail_frame = _panel("detail", "JSON")
        self.detail_text = scrolledtext.ScrolledText(
            detail_frame, wrap=tk.WORD, height=22, font=("Consolas", 9)
        )
        self.detail_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        learn = _panel("learning", "学习", rowspan=2)
        self._create_learning_tab(learn)

        evo = _panel("evolution", "进化")
        self._create_evolution_tab(evo)

        know = _panel("knowledge", "知识")
        self._create_knowledge_tab(know)

        grid = _panel("grid", "网格", rowspan=1)
        self._create_grid_tab(grid)

        bt = _panel("backtest", "回测", rowspan=1)
        self._create_backtest_tab(bt)

        market = _panel("market", "情绪", rowspan=1)
        self._create_market_tab(market)

        alert = _panel("alert", "预警", rowspan=1)
        self._create_alert_tab(alert)

        news = _panel("news", "新闻", rowspan=1)
        self._create_news_tab(news)

        paper = _panel("paper", "模拟", rowspan=1)
        self._create_paper_tab(paper)

        review = _panel("review", "复盘", rowspan=1)
        self._create_review_tab(review)

        signal = _panel("signal", "信号", rowspan=1)
        self._create_signal_tracking_tab(signal)

        scanner = _panel("scanner", "扫描", rowspan=1)
        self._create_scanner_tab(scanner)

        traders = _panel("traders", "交易员", rowspan=2)
        self._create_traders_tab(traders)

        # 占位「决策」页：兼容 notebook.select(0) / 进化页跳转
        advice_stub = _panel("advice", "决策")
        tk.Label(
            advice_stub,
            text="主界面右侧即为决议输出。\n此页保留兼容。",
            font=("Microsoft YaHei UI", 11),
        ).pack(expand=True)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tools_tab_changed)

    def _hide_tools_win(self):
        try:
            self._tools_win.withdraw()
        except Exception:
            pass
        self._active_tool_key = None

    def _open_tool_window(self, key: str):
        """底部功能区：打开工具柜并切到对应页。"""
        fr = self._panels.get(key)
        if fr is None:
            messagebox.showinfo("提示", f"面板未就绪: {key}")
            return
        try:
            self._tools_win.deiconify()
            self._tools_win.lift()
            self.notebook.select(fr)
            self._active_tool_key = key
            self._refresh_tool(key)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _on_tools_tab_changed(self, event=None):
        try:
            fr = self.nametowidget(self.notebook.select())
            for k, v in self._panels.items():
                if v == fr:
                    self._active_tool_key = k
                    self._active_tab_index = self.notebook.index(fr)
                    self._refresh_tool(k)
                    break
        except Exception:
            pass

    def _refresh_tool(self, key: str):
        try:
            if key == "learning":
                self._refresh_learning_dashboard()
            elif key == "evolution":
                self._refresh_evolution_timeline()
            elif key == "knowledge":
                self._refresh_knowledge_cards()
            elif key == "paper":
                self._paper_refresh()
            elif key == "review":
                self._refresh_review_history()
                if hasattr(self, "_refresh_review_timeline_preview"):
                    self._refresh_review_timeline_preview()
            elif key == "signal":
                self._refresh_signal_stats()
            elif key == "scanner":
                self._refresh_scanner_tab()
            elif key == "traders":
                self._refresh_traders_tab()
        except Exception:
            pass

    def _on_tab_changed(self, event=None):
        self._on_tools_tab_changed(event)

    def _refresh_active_tab(self):
        if self._active_tool_key:
            self._refresh_tool(self._active_tool_key)

    def _schedule_auto_refresh(self):
        if not self._auto_refresh_enabled:
            return
        try:
            self._refresh_active_tab()
        except Exception:
            pass
        self.root.after(self._auto_refresh_interval_ms, self._schedule_auto_refresh)

    def update_status(self, message):
        self.root.after(0, lambda: self.status_var.set(message))

    def check_thread(self):
        if self.analysis_thread and self.analysis_thread.is_alive():
            self.root.after(100, self.check_thread)
        else:
            self.progress.stop()
            self.analyze_btn.config(state="normal")
            if getattr(self, "fresh_analyze_btn", None):
                self.fresh_analyze_btn.config(state="normal")

    def _on_room_trader_click(self, trader_id: str):
        self._open_trader_config_dialog(trader_id)

    def _open_trader_config_dialog(self, trader_id: str):
        if not getattr(self, "_trader_card_vars", None):
            messagebox.showinfo("提示", "交易员配置尚未就绪，请先点底部「👥 配置」")
            self._open_tool_window("traders")
            return
        meta = next((t for t in TRADER_DEFS if t["id"] == trader_id), None)
        vars_map = self._trader_card_vars.get(trader_id)
        if not meta or not vars_map:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"配置 · {meta['emoji']} {meta['name']}")
        dlg.geometry("420x360")
        dlg.configure(bg=PALETTE["paper"])
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(
            dlg, text=f"{meta['emoji']} {meta['name']}",
            bg=PALETTE["paper"], fg=meta["body"],
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(pady=(12, 4))
        tk.Label(
            dlg, text="独立 LLM Key（留空=共用全局 deepseek）",
            bg=PALETTE["paper"], fg=PALETTE["ink"],
        ).pack()

        form = tk.Frame(dlg, bg=PALETTE["paper"], padx=16, pady=8)
        form.pack(fill=tk.X)

        tk.Checkbutton(
            form, text="启用此交易员", variable=vars_map["enabled"],
            bg=PALETTE["paper"],
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=4)
        tk.Checkbutton(
            form, text="使用 LLM（关闭则仅规则先验）", variable=vars_map["use_llm"],
            bg=PALETTE["paper"],
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=4)

        tk.Label(form, text="API Key", bg=PALETTE["paper"]).grid(row=2, column=0, sticky=tk.W)
        ttk.Entry(form, textvariable=vars_map["api_key"], show="*", width=36).grid(row=2, column=1, pady=4)

        tk.Label(form, text="模型", bg=PALETTE["paper"]).grid(row=3, column=0, sticky=tk.W)
        ttk.Entry(form, textvariable=vars_map["model"], width=36).grid(row=3, column=1, pady=4)

        tk.Label(form, text="Base URL", bg=PALETTE["paper"]).grid(row=4, column=0, sticky=tk.W)
        ttk.Entry(form, textvariable=vars_map["base_url"], width=36).grid(row=4, column=1, pady=4)

        tk.Label(form, text="Temperature", bg=PALETTE["paper"]).grid(row=5, column=0, sticky=tk.W)
        ttk.Entry(form, textvariable=vars_map["temperature"], width=8).grid(row=5, column=1, sticky=tk.W, pady=4)

        def _save():
            try:
                self._save_trader_council_config()
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=dlg)

        btns = tk.Frame(dlg, bg=PALETTE["paper"])
        btns.pack(pady=12)
        tk.Button(
            btns, text="保存并热加载", command=_save,
            bg="#2E7D32", fg="white", padx=12,
        ).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="取消", command=dlg.destroy, padx=12).pack(side=tk.LEFT, padx=6)
        tk.Button(
            btns, text="打开完整配置",
            command=lambda: (dlg.destroy(), self._open_tool_window("traders")),
            padx=8,
        ).pack(side=tk.LEFT, padx=6)

    def _refresh_room_from_config(self):
        if not hasattr(self, "trading_room"):
            return
        votes = {}
        result = getattr(self, "analysis_result", None) or {}
        advice = result.get("trade_advice") or {}
        ma = advice.get("multi_agent_deliberation") or {}
        council = ma.get("council") or {}
        for v in (council.get("votes") or ma.get("agent_votes") or []):
            if isinstance(v, dict) and v.get("trader_id"):
                votes[v["trader_id"]] = v
        self.trading_room.set_votes(votes)

    def _on_room_symbol_changed(self):
        if not hasattr(self, "trading_room"):
            return
        sym = (self.symbol_var.get() or "BNBUSDT").upper()
        self.trading_room.set_quote(sym, None, None)
        # 立刻拉一次，不必等下一轮轮询
        self.root.after(50, self._poll_room_price_once)

    def _poll_room_price_once(self):
        """后台拉一次 ticker，回写中间电脑屏。"""
        if not hasattr(self, "trading_room") or not getattr(self, "fetcher", None):
            return
        symbol = (self.symbol_var.get() or "BNBUSDT").upper()

        def work():
            price = None
            change_pct = None
            try:
                ticker = self.fetcher.get_ticker(symbol)
                if isinstance(ticker, dict):
                    raw = ticker.get("lastPrice") or ticker.get("close") or ticker.get("last")
                    if raw is not None:
                        price = float(raw)
                    chg = ticker.get("priceChangePercent")
                    if chg is not None:
                        change_pct = float(chg)
            except Exception:
                pass
            if price is None or price <= 0:
                try:
                    price = float(self.fetcher.get_price_with_fallback(symbol))
                    if price <= 0:
                        price = None
                except Exception:
                    price = None

            def apply():
                if not hasattr(self, "trading_room"):
                    return
                cur = (self.symbol_var.get() or "BNBUSDT").upper()
                if cur != symbol:
                    return  # 用户已切币种，丢弃过期结果
                self.trading_room.set_quote(symbol, price, change_pct)

            try:
                self.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _poll_room_price(self):
        """每 3 秒刷新圆桌屏幕行情。"""
        try:
            self._poll_room_price_once()
        except Exception:
            pass
        try:
            self._room_price_poll_id = self.root.after(3000, self._poll_room_price)
        except Exception:
            self._room_price_poll_id = None

    def _on_close(self):
        self._ai_fullauto_running = False
        poll_id = getattr(self, "_room_price_poll_id", None)
        if poll_id is not None:
            try:
                self.root.after_cancel(poll_id)
            except Exception:
                pass
            self._room_price_poll_id = None
        if getattr(self, "_scanner", None):
            self._scanner.stop()
            self._scanner = None
        try:
            self._scheduler_running = False
        except Exception:
            pass
        try:
            if self.alert_engine.is_running():
                self.alert_engine.stop()
        except Exception:
            pass
        try:
            self.paper_engine.stop_watcher()
        except Exception:
            pass
        try:
            if getattr(self, "learner", None) is not None:
                self.learner.reset_connection()
                mem = self.learner.capability_memory
                mem.checkpoint_wal()
                mem.reset_connection()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
