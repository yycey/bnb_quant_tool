"""Mixin: PaperTabMixin"""

from gui._imports import *


class PaperTabMixin:
    def _create_paper_tab(self, parent):
        """模拟交易面板: 后台跟单 + 复盘改进"""
        # 让 body 可拉伸，参数/备份面板固定在底部
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        ctrl = ttk.Frame(parent)
        ctrl.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        ttk.Checkbutton(
            ctrl, text="分析后自动跟单",
            variable=self.auto_paper_var
        ).pack(side=tk.LEFT, padx=5)
        self.paper_status_var = tk.StringVar(value="watcher: 未启动")
        ttk.Label(ctrl, textvariable=self.paper_status_var, foreground="#888").pack(side=tk.LEFT, padx=10)

        ttk.Button(ctrl, text="启动后台监控", command=self._paper_start_watcher).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="停止监控", command=self._paper_stop_watcher).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="手动跟单当前建议", command=self._paper_manual_open).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="刷新", command=lambda: self._paper_refresh(0)).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="导入数据", command=self._import_paper_data).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="导出数据", command=self._export_paper_data).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="平仓选中", command=self._paper_close_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="AI 复盘改进", command=self._paper_review).pack(side=tk.LEFT, padx=10)

        body = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        body.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)

        # 上面面板: 当前持仓 + 历史交易 (表格)
        top = ttk.Frame(body)
        body.add(top, weight=1)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=1)

        ttk.Label(top, text="当前持仓 + 历史交易", font=("", 10, "bold")).grid(row=0, column=0, sticky=tk.W)
        cols = ("id", "status", "side", "opened_at", "closed_at",
                "entry", "close", "qty", "sl", "pnl", "reason")
        self.paper_tree = ttk.Treeview(top, columns=cols, show="headings", height=10)
        widths = {"id": 50, "status": 70, "side": 60, "opened_at": 140,
                  "closed_at": 140, "entry": 80, "close": 80, "qty": 70,
                  "sl": 80, "pnl": 80, "reason": 110}
        for c in cols:
            self.paper_tree.heading(c, text=c)
            self.paper_tree.column(c, width=widths.get(c, 80), anchor=tk.W)
        self.paper_tree.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        sb = ttk.Scrollbar(top, orient=tk.VERTICAL, command=self.paper_tree.yview)
        sb.grid(row=1, column=1, sticky=(tk.N, tk.S))
        self.paper_tree.configure(yscrollcommand=sb.set)

        # 下面面板: 统计 + AI 复盘
        bot = ttk.Frame(body)
        body.add(bot, weight=1)
        bot.columnconfigure(0, weight=1)
        bot.columnconfigure(1, weight=1)
        bot.rowconfigure(1, weight=1)
        ttk.Label(bot, text="统计", font=("", 10, "bold")).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(bot, text="AI 复盘改进建议", font=("", 10, "bold")).grid(row=0, column=1, sticky=tk.W)
        self.paper_stats_text = scrolledtext.ScrolledText(bot, wrap=tk.WORD, height=14, font=("Consolas", 10))
        self.paper_stats_text.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 4))
        self.paper_review_text = scrolledtext.ScrolledText(bot, wrap=tk.WORD, height=14, font=("Consolas", 9))
        self.paper_review_text.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(4, 0))

        self.paper_stats_text.insert(1.0,
            "点击 「开始分析」后, 若开单建议不为WAIT且 「自动跟单」开启, 会自动创建模拟仓位。\n"
            "后台 watcher 定时检查价格是否触发 SL/TP, 自动平仓、记录盈亏。\n"
            "跨 10 笔+ 交易后点 「AI 复盘改进」, 让 DeepSeek 总结胜负原因并给出参数调整建议。\n"
        )
        self.paper_review_text.insert(1.0, "尚未复盘。点击 「AI 复盘改进」 调用 DeepSeek 分析交易历史。\n")

        # ---- 参数调整面板 (挂在 parent row=2，避免被 PanedWindow 下半部挤出可见区) ----
        param_frame = ttk.LabelFrame(parent, text="参数调整 (复盘建议自动填入，修改后点应用)", padding=5)
        param_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=(2, 0))

        # 参数定义: (label, config_section, config_key, default)
        self._param_defs = [
            ("置信度阈值", "analysis", "confidence_threshold", 0.7),
            ("新闻过滤阈值", "trade_advisor", "news_filter_threshold", 0.65),
            ("止损 ATR倍数", "trade_advisor", "atr_sl_mult", 1.5),
            ("TP1 ATR倍数", "trade_advisor", "atr_tp1_mult", 1.5),
            ("TP2 ATR倍数", "trade_advisor", "atr_tp2_mult", 3.0),
            ("TP3 ATR倍数", "trade_advisor", "atr_tp3_mult", 5.0),
            ("仓位上限%", "risk_management", "max_position_pct", 0.25),
        ]
        self._param_entries = {}  # key -> Entry widget
        for i, (label, section, key, default) in enumerate(self._param_defs):
            ttk.Label(param_frame, text=label, width=12).grid(row=0, column=i*2, sticky=tk.E, padx=(4, 2))
            var = tk.StringVar(value=str(self.config.get(section, {}).get(key, default)))
            entry = ttk.Entry(param_frame, textvariable=var, width=8)
            entry.grid(row=0, column=i*2+1, sticky=tk.W, padx=(0, 8))
            self._param_entries[key] = var

        btn_area = ttk.Frame(param_frame)
        btn_area.grid(row=1, column=0, columnspan=len(self._param_defs)*2, pady=(6, 0))
        ttk.Button(btn_area, text="⬅ 填入复盘建议", command=self._fill_review_params).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_area, text="✅ 应用到配置文件", command=self._apply_params_to_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_area, text="🔬 回测此参数", command=self._backtest_current_params).pack(side=tk.LEFT, padx=5)
        self._param_apply_label = tk.StringVar(value="")
        ttk.Label(btn_area, textvariable=self._param_apply_label, foreground="#228B22").pack(side=tk.LEFT, padx=10)

        # ---- 训练包导入/导出 (挂在 parent row=3，始终可见) ----
        backup_frame = ttk.LabelFrame(parent, text="训练包备份 (换电脑/重装系统时一键迁移 AI 学习成果)", padding=5)
        backup_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), padx=5, pady=(2, 6))
        ttk.Button(backup_frame, text="📦 导出训练包", command=self._export_brain_zip).pack(side=tk.LEFT, padx=5)
        ttk.Button(backup_frame, text="📥 导入训练包", command=self._import_brain_zip).pack(side=tk.LEFT, padx=5)
        self._brain_status_label = tk.StringVar(value="包含: ai_learning.db + paper_trading.db + config.yaml")
        ttk.Label(backup_frame, textvariable=self._brain_status_label, foreground="#555555").pack(side=tk.LEFT, padx=10)

        # 首次加载
        self._paper_refresh()

    def _paper_price_provider(self, symbol: str) -> float:
        return self.fetcher.get_price_with_fallback(symbol)

    def _on_paper_event(self, event_type: str, payload: dict):
        # 从后台线程调这里, 必须走 root.after 才能改 UI
        try:
            self.root.after(0, lambda: self._handle_paper_event(event_type, payload))
        except Exception:
            pass

    def _handle_paper_event(self, event_type: str, payload: dict):
        try:
            if hasattr(self, 'paper_tree'):
                self._paper_refresh()
            self.update_status(f"模拟盘事件: {event_type} #{payload.get('id')} @ {payload.get('price')}")
            # 仓位完全关闭 (TP3/SL/MANUAL_CLOSE) 后触发自动复盘 + 自动回填学习反馈
            if event_type in ("TP3", "SL", "MANUAL_CLOSE"):
                pid = payload.get('id')
                self._maybe_trigger_auto_review()
                # 学习由 paper_engine._mark_closed 统一管道触发（幂等）；
                # 这里只做 UI/复盘，避免重复反事实与静默吞错
                if hasattr(self, "_refresh_learning_evolution_views"):
                    self.root.after(1500, self._refresh_learning_evolution_views)
            # 数据变化时同步刷新其他关联Tab（模拟盘 Tab 已由上方防抖刷新）
            idx = getattr(self, '_active_tab_index', -1)
            if idx != 12:
                self._refresh_active_tab()
            if hasattr(self, "_refresh_learning_evolution_views"):
                self.root.after(2000, self._refresh_learning_evolution_views)
        except Exception:
            pass

    def _paper_start_watcher(self):
        try:
            interval = float(self.config.get('paper_trading', {}).get('poll_interval', 15))
            self.paper_engine.start_watcher(interval=interval)
            self.paper_status_var.set(f"watcher: 运行中 ({interval:.0f}s)")
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _paper_stop_watcher(self):
        self.paper_engine.stop_watcher()
        self.paper_status_var.set("watcher: 已停止")

    def _paper_manual_open(self):
        if not self.analysis_result:
            messagebox.showwarning("提示", "先点 「开始分析」 生成开单建议")
            return
        adv = self.analysis_result.get('trade_advice') or {}
        action = adv.get('action')
        raw = adv.get('raw_action')
        if action not in ('LONG', 'SHORT') and raw not in ('LONG', 'SHORT'):
            messagebox.showinfo("提示", f"当前建议=WAIT 且 原始信号=WAIT，无明确方向，不下单")
            return
        equity = self._get_account_balance() if hasattr(self, "_get_account_balance") else float(
            self.config.get('trading', {}).get('account_balance', 5000.0)
        )
        # 手动跟单始终使用宽松模式 (用户明确点了跳单按钮, 意思就是要上)
        pid = self.paper_engine.open_from_advice(
            adv, equity_usdt=equity,
            learning_record_id=self.last_record_id,
            relaxed=True
        )
        if pid:
            self.last_paper_advice_id = pid
            if not self.paper_engine.is_watching():
                self._paper_start_watcher()
            self._paper_refresh()
            used = action if action in ('LONG', 'SHORT') else raw
            messagebox.showinfo("成功", f"已手动跟单 #{pid} ({used})")
        else:
            state = self.paper_engine.get_margin_state(equity)
            messagebox.showwarning(
                "失败",
                "未能开仓。\n"
                f"可用保证金: {state['available_margin']:.2f} USDT\n"
                f"已占用: {state['used_margin']:.2f} / 权益: {state['equity']:.2f} USDT"
            )

    def _paper_close_selected(self):
        sel = self.paper_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在表格中选中一笔仓位")
            return
        pid = int(self.paper_tree.item(sel[0])["values"][0])
        # 按仓位自身品种取价，避免配置默认 BNB 误平 BTC
        opens = {int(p["id"]): p for p in self.paper_engine.get_open_positions()}
        pos = opens.get(pid) or {}
        symbol = (pos.get("symbol")
                  or self.config.get('trading', {}).get('symbol', 'BNBUSDT')).upper()
        self.update_status(f"正在平仓 #{pid} {symbol}...")

        def _worker(close_pid=pid, sym=symbol):
            try:
                price = self._paper_price_provider(sym)
                if price <= 0:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "提示", f"无法获取 {sym} 当前价格，请检查网络后重试"))
                    return
                ok = self.paper_engine.close_manual(close_pid, price=price, reason="MANUAL")

                def _done():
                    if ok:
                        self.update_status(f"#{close_pid} 已以 {price} 平仓")
                        self._paper_refresh()
                    else:
                        messagebox.showwarning("提示", "仓位不存在或已平")

                self.root.after(0, _done)
            except Exception as e:
                self.root.after(0, lambda err=e: messagebox.showerror("平仓失败", str(err)))

        threading.Thread(target=_worker, daemon=True).start()

    def _paper_refresh(self, delay_ms: int = 150):
        """防抖刷新：合并短时间内的多次刷新请求"""
        pending = getattr(self, '_paper_refresh_pending', None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass
        self._paper_refresh_pending = self.root.after(
            delay_ms, self._paper_refresh_now
        )

    def _paper_refresh_now(self):
        self._paper_refresh_pending = None
        try:
            from bnb_quant_tool.paper_trading import PaperTradingEngine
            for it in self.paper_tree.get_children():
                self.paper_tree.delete(it)
            opens = self.paper_engine.get_open_positions()
            closed_all = self.paper_engine.get_closed_positions(limit=10000)
            closed_display = closed_all[:50]
            default_symbol = self.config.get('trading', {}).get('symbol', 'BNBUSDT')
            mark_by_symbol = {}
            for p in opens:
                sym = (p.get("symbol") or default_symbol).upper()
                if sym not in mark_by_symbol:
                    try:
                        mark_by_symbol[sym] = float(self._paper_price_provider(sym) or 0)
                    except Exception:
                        mark_by_symbol[sym] = 0.0
                mark_price = mark_by_symbol[sym]
                unreal = PaperTradingEngine.calc_unrealized_pnl(p, mark_price, self.paper_engine.fee_rate)
                self.paper_tree.insert("", tk.END, values=(
                    p["id"], p["status"], {"LONG":"买","SHORT":"卖"}.get(p["side"], p["side"]), p["opened_at"], "",
                    f"{p['entry_price']:.4f}", f"{mark_price:.4f}" if mark_price > 0 else "",
                    f"{p['qty_remaining']:.4f}", f"{p['sl']:.4f}",
                    f"{unreal:+.2f}", "OPEN"
                ))
            for p in closed_display:
                self.paper_tree.insert("", tk.END, values=(
                    p["id"], p["status"], {"LONG":"买","SHORT":"卖"}.get(p["side"], p["side"]), p["opened_at"], p["closed_at"] or "",
                    f"{p['entry_price']:.4f}",
                    f"{(p['close_avg_price'] or 0):.4f}",
                    f"{p['qty_total']:.4f}", f"{p['sl']:.4f}",
                    f"{p['realized_pnl_usdt']:+.2f}", p["close_reason"] or ""
                ))
            stats = self.paper_engine.get_stats(opens=opens, closed=closed_all)
            self.paper_stats_text.delete(1.0, tk.END)
            self.paper_stats_text.insert(1.0, PaperTradingEngine.format_stats_report(stats))
            # 状态栏 — 主看 E[R] / 自动单 PnL
            er = stats.get("expectancy_r", stats.get("avg_r", 0)) or 0
            self.paper_status_var.set(
                f"watcher: {'运行中' if self.paper_engine.is_watching() else '未启动'} | "
                f"持仓 {stats.get('open_count', 0)} | "
                f"自动单 {stats.get('total_trades', 0)} | "
                f"E[R] {er:+.3f} | "
                f"胜率 {stats.get('win_rate', 0):.0%} | "
                f"累计 {stats.get('total_realized_pnl', 0):+.2f}"
            )
        except Exception as e:
            logger.error(f"_paper_refresh 错误: {e}") if False else None

    def _import_paper_data(self):
        """导入交易数据 from external paper_trading.db"""
        from tkinter import filedialog
        import sqlite3
        import shutil
        import os

        # 选择要导入的数据库文件
        file_path = filedialog.askopenfilename(
            title="选择要导入的 paper_trading.db",
            filetypes=[("SQLite数据库", "*.db"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(self.paper_engine.db_path) or "."
        )
        if not file_path:
            return

        try:
            # 连接到源数据库
            src_conn = sqlite3.connect(file_path)
            src_cur = src_conn.cursor()

            # 检查源数据库的表结构
            src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_positions'")
            if not src_cur.fetchone():
                messagebox.showerror("导入失败", "选择的文件不是有效的 paper_trading.db（缺少 paper_positions 表）")
                src_conn.close()
                return

            # 获取源数据库的记录数
            src_cur.execute('SELECT count(*) FROM paper_positions')
            src_count = src_cur.fetchone()[0]
            src_cur.execute('SELECT count(*) FROM paper_positions WHERE status="OPEN"')
            src_open = src_cur.fetchone()[0]
            src_cur.execute('SELECT count(*) FROM paper_positions WHERE status="CLOSED"')
            src_closed = src_cur.fetchone()[0]

            src_conn.close()

            if src_count == 0:
                messagebox.showwarning("导入", "选择的数据库没有交易记录")
                return

            # 确认导入
            confirm = messagebox.askyesno(
                "确认导入",
                f"将导入 {src_count} 笔交易记录：\n"
                f"- 持仓中: {src_open} 笔\n"
                f"- 已平仓: {src_closed} 笔\n\n"
                f"是否继续？"
            )
            if not confirm:
                return

            # 备份当前数据库
            backup_path = self.paper_engine.db_path + '.backup_import'
            shutil.copy2(self.paper_engine.db_path, backup_path)

            # 复制源数据库覆盖当前数据库
            shutil.copy2(file_path, self.paper_engine.db_path)

            # 刷新 DB 连接并同步 WAL
            self.paper_engine.reset_connection()
            self.paper_engine.checkpoint_wal()
            if hasattr(self.learner, "reset_connection"):
                self.learner.reset_connection()
            if getattr(self, "pattern_memory", None):
                self.pattern_memory.reset_connection()
            if getattr(self, "ai_review_engine", None):
                if hasattr(self.ai_review_engine, "reset_connection"):
                    self.ai_review_engine.reset_connection()

            # 刷新显示
            self._paper_refresh()

            messagebox.showinfo(
                "导入成功",
                f"已成功导入 {src_count} 笔交易记录！\n\n"
                f"原数据库已备份至: {os.path.basename(backup_path)}"
            )

        except Exception as e:
            logger.error(f"导入失败: {e}")
            messagebox.showerror("导入失败", str(e))

    def _export_paper_data(self):
        """导出交易数据 to external file"""
        from tkinter import filedialog
        import sqlite3
        import os

        # 选择导出保存位置
        default_name = f"paper_trading_export_{os.path.splitext(os.path.basename(self.paper_engine.db_path))[0]}.db"
        file_path = filedialog.asksaveasfilename(
            title="导出交易数据到...",
            defaultextension=".db",
            filetypes=[("SQLite数据库", "*.db"), ("所有文件", "*.*")],
            initialfile=default_name,
            initialdir=os.path.dirname(self.paper_engine.db_path) or "."
        )
        if not file_path:
            return

        try:
            # 获取当前数据库记录数
            conn = sqlite3.connect(self.paper_engine.db_path)
            cur = conn.cursor()

            cur.execute('SELECT count(*) FROM paper_positions')
            total_count = cur.fetchone()[0]
            cur.execute('SELECT count(*) FROM paper_positions WHERE status="OPEN"')
            open_count = cur.fetchone()[0]
            cur.execute('SELECT count(*) FROM paper_positions WHERE status="CLOSED"')
            closed_count = cur.fetchone()[0]

            conn.close()

            if total_count == 0:
                messagebox.showwarning("导出", "当前没有交易记录可导出")
                return

            # 确认导出
            confirm = messagebox.askyesno(
                "确认导出",
                f"将导出 {total_count} 笔交易记录：\n"
                f"- 持仓中: {open_count} 笔\n"
                f"- 已平仓: {closed_count} 笔\n\n"
                f"保存到: {os.path.basename(file_path)}\n\n"
                f"是否继续？"
            )
            if not confirm:
                return

            # 复制当前数据库到目标位置
            import shutil
            shutil.copy2(self.paper_engine.db_path, file_path)

            messagebox.showinfo(
                "导出成功",
                f"已成功导出 {total_count} 笔交易记录！\n\n"
                f"保存位置: {file_path}"
            )

        except Exception as e:
            logger.error(f"导出失败: {e}")
            messagebox.showerror("导出失败", str(e))

    def _paper_review(self):
        """调用 LLM 复盘 (后台线程)"""
        from bnb_quant_tool.llm_provider import get_llm_credentials, build_llm_analyzer
        llm = get_llm_credentials(self.config)
        if not llm["api_key"]:
            messagebox.showerror("未配置", "未配置 LLM api_key（qianwen / deepseek）")
            return
        self.paper_review_text.delete(1.0, tk.END)
        self.paper_review_text.insert(1.0, f"正在调用 {llm['provider']} 复盘...\n")

        def _do():
            try:
                payload = self._build_enriched_review_payload(max_trades=30)
                analyzer = build_llm_analyzer(self.config)
                review = analyzer.review_paper_trades(
                    payload, symbol=self.symbol_var.get().replace("USDT", "")
                )
                self.last_review = review
                self.root.after(0, lambda: self._render_paper_review(review))
            except Exception as e:
                self.root.after(0, lambda: self._render_paper_review({
                    "grade": "?", "summary": f"复盘调用失败: {e}",
                    "key_findings": [], "mistakes": [], "what_works": [],
                    "param_suggestions": [], "next_focus": ""
                }))
        threading.Thread(target=_do, daemon=True).start()

    def _render_paper_review(self, review: dict):
        if not isinstance(review, dict):
            review = {}
        lines = []
        lines.append("=" * 50)
        lines.append(f"📊 AI 复盘评分: {review.get('grade', '?')}")
        lines.append("=" * 50)
        # 🆕 顶部补充: 分桶快照 (仅本地计算, 不依赖 AI)
        try:
            payload_preview = self._build_enriched_review_payload(max_trades=30)
            bd = (payload_preview or {}).get('breakdown') or {}
            by_side = bd.get('by_side') or {}
            by_regime = bd.get('by_regime') or {}
            diag = bd.get('diagnostics') or {}
            if by_side:
                lines.append("【多空分桶】 " + " | ".join(
                    f"{k}: WR{(v.get('win_rate') or 0)*100:.0f}% PF{v.get('pf') or 0:.2f} (n={v.get('n', 0)})"
                    for k, v in by_side.items() if v.get('n', 0) > 0
                ))
            if by_regime:
                lines.append("【Regime分桶】 " + " | ".join(
                    f"{k}: WR{(v.get('win_rate') or 0)*100:.0f}% (n={v.get('n', 0)})"
                    for k, v in by_regime.items() if v.get('n', 0) > 0
                ))
            if diag.get('big_mfe_small_r_count'):
                lines.append(f"【MFE诊断】 赢单中有2R以上浮盈但最终仅含≤1R: {diag['big_mfe_small_r_count']} 笔"
                              + (f" → {diag.get('big_mfe_small_r_hint', '')}" if diag.get('big_mfe_small_r_hint') else ''))
            if diag.get('deep_mae_loss_count'):
                lines.append(f"【MAE诊断】 损单 MAE≤-1R 才止损: {diag['deep_mae_loss_count']} 笔"
                              + (f" → {diag.get('deep_mae_loss_hint', '')}" if diag.get('deep_mae_loss_hint') else ''))
            # 上次参数调整事后效果
            rpc = (payload_preview or {}).get('recent_param_changes') or []
            if rpc:
                last = rpc[0]
                wr_d = last.get('wr_delta')
                if wr_d is not None:
                    arrow = "↗" if wr_d > 0 else ("↘" if wr_d < 0 else "→")
                    lines.append(
                        f"【上次调参】 {last.get('param_name')} "
                        f"{last.get('old_value')}→{last.get('new_value')}, "
                        f"调后 {last.get('trades_after_change', 0)} 笔, "
                        f"胜率变化 {arrow} {wr_d*100:+.1f}%"
                    )
            lines.append("-" * 50)
        except Exception as e:
            logger.debug(f"复盘顶部快照异常: {e}")
        if review.get('summary'):
            lines.append(f"总结: {review['summary']}")
            lines.append("")
        if review.get('key_findings'):
            lines.append("关键发现:")
            for f in review['key_findings']:
                lines.append(f"  - {f}")
            lines.append("")
        if review.get('what_works'):
            lines.append("表现好的:")
            for f in review['what_works']:
                lines.append(f"  + {f}")
            lines.append("")
        if review.get('mistakes'):
            lines.append("存在问题:")
            for f in review['mistakes']:
                lines.append(f"  ! {f}")
            lines.append("")
        if review.get('param_suggestions'):
            lines.append("参数调整建议:")
            for ps in review['param_suggestions']:
                if isinstance(ps, dict):
                    lines.append(
                        f"  * {ps.get('param', '?')}: "
                        f"{ps.get('current', '?')} -> {ps.get('suggest', '?')}"
                    )
                    if ps.get('reason'):
                        lines.append(f"     原因: {ps['reason']}")
                else:
                    lines.append(f"  * {ps}")
            lines.append("")
        # 🆕 策略权重调整建议 (方向1)
        if review.get('strategy_adjustments'):
            lines.append("机构策略权重调整:")
            for sa in review['strategy_adjustments']:
                if isinstance(sa, dict):
                    lines.append(
                        f"  > {sa.get('name', '?')}: "
                        f"权重 {sa.get('current_weight', '?')} → {sa.get('suggest_weight', '?')} "
                        f"[{sa.get('action', '?')}]"
                    )
                    if sa.get('reason'):
                        lines.append(f"     原因: {sa['reason']}")
            lines.append("")
        # 🆕 regime 停手规则 (方向1)
        if review.get('regime_rules'):
            lines.append("坏行情停手规则:")
            for rr in review['regime_rules']:
                if isinstance(rr, dict):
                    lines.append(
                        f"  ⚠ 当 [{rr.get('condition', '?')}] → {rr.get('action', '?')}"
                    )
                    if rr.get('reason'):
                        lines.append(f"     原因: {rr['reason']}")
            lines.append("")
        # 🆕 止盈止损诊断 (方向3)
        sl_tp = review.get('sl_tp_diagnosis') or {}
        if sl_tp:
            lines.append("止盈止损诊断 (MFE/MAE/R):")
            if sl_tp.get('hint'):
                lines.append(f"  🔍 {sl_tp['hint']}")
            if sl_tp.get('tp1_too_close'):
                lines.append("  ⚠ TP1 太近: 考虑将 atr_tp1_mult 上调")
            if sl_tp.get('sl_too_loose'):
                lines.append("  ⚠ SL 太松: 考虑将 atr_sl_mult 下调")
            lines.append("")
        # 🆕 上次参数调整回滚建议 (方向4)
        revert = review.get('revert_suggestion') or {}
        if revert.get('should_revert'):
            lines.append("⚠️ 上次调参后效果不佳 → 建议回滚:")
            lines.append(f"  目标参数: {revert.get('target_param', '?')}")
            if revert.get('reason'):
                lines.append(f"  原因: {revert['reason']}")
            lines.append("")
        if review.get('next_focus'):
            lines.append(f"下一阶段重点: {review['next_focus']}")
        self.paper_review_text.delete(1.0, tk.END)
        self.paper_review_text.insert(1.0, "\n".join(lines))
        # 自动填入参数建议到输入框
        if review.get('param_suggestions'):
            self._fill_review_params(review['param_suggestions'])
            # 🆕 后台跑双回测对比当前 vs 建议参数
            self._compare_review_backtest(review['param_suggestions'])

    def _compare_review_backtest(self, suggestions):
        """在复盘文本框末尾追加 '当前参数 vs 建议参数' 回测对比"""
        # 当前参数
        cur_params = self._read_param_entries()
        # 建议参数 (以 cur_params 为基础覆盖)
        sug_params = dict(cur_params)
        sug_map = {
            'confidence_threshold': 'confidence_threshold',
            'min_confidence': 'confidence_threshold',
            'news_filter_threshold': 'news_filter_threshold',
            'atr_sl_mult': 'atr_sl_mult',
            'atr_tp1_mult': 'atr_tp1_mult',
            'atr_tp2_mult': 'atr_tp2_mult',
            'atr_tp3_mult': 'atr_tp3_mult',
            'max_position_pct': 'max_position_pct',
        }
        for ps in suggestions:
            if not isinstance(ps, dict):
                continue
            pname = (ps.get('param') or '').lower()
            target = sug_map.get(pname)
            if not target:
                continue
            try:
                sug_params[target] = float(ps.get('suggest'))
            except (TypeError, ValueError):
                pass

        self.paper_review_text.insert(tk.END, "\n\n⏳ 后台回测对比中（近 30 天 1h K线）...\n")

        def _worker():
            try:
                cur_res = self._run_backtest_with_params(cur_params, days=30, interval="1h")
                sug_res = self._run_backtest_with_params(sug_params, days=30, interval="1h")
                self.root.after(0, lambda: self._append_compare_result(cur_res, sug_res))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: self.paper_review_text.insert(tk.END, f"⚠ 对比回测失败: {err}\n"))

        threading.Thread(target=_worker, daemon=True).start()

    def _append_compare_result(self, cur_res: dict, sug_res: dict):
        """将对比结果追加到复盘文本框、并高亮提升指标"""
        wr_diff = (sug_res['win_rate'] - cur_res['win_rate']) * 100
        ret_diff = sug_res['total_return_pct'] - cur_res['total_return_pct']
        dd_diff = sug_res['max_drawdown_pct'] - cur_res['max_drawdown_pct']

        def fmt_diff(v, suffix='%', good_when_positive=True):
            sign = '+' if v >= 0 else ''
            arrow = ('↗' if v > 0.01 else ('↘' if v < -0.01 else '→'))
            return f"{sign}{v:.2f}{suffix} {arrow}"

        block = [
            "",
            "═" * 50,
            "🔬 参数建议预期效果 (当前 vs 建议)",
            "═" * 50,
            f"胜率:      {cur_res['win_rate']*100:.1f}% → {sug_res['win_rate']*100:.1f}%   ({fmt_diff(wr_diff)})",
            f"总收益:    {cur_res['total_return_pct']:+.2f}% → {sug_res['total_return_pct']:+.2f}%   ({fmt_diff(ret_diff)})",
            f"最大回撤:  {cur_res['max_drawdown_pct']:.2f}% → {sug_res['max_drawdown_pct']:.2f}%   ({fmt_diff(dd_diff)} 越小越好)",
            f"夏普比:    {cur_res['sharpe_ratio']:.2f} → {sug_res['sharpe_ratio']:.2f}",
            f"交易数:    {cur_res['total_trades']} → {sug_res['total_trades']}",
        ]
        # 给出结论
        better = (wr_diff >= 0) and (ret_diff >= 0) and (dd_diff <= 1)
        if better:
            block.append("✅ 建议参数优于当前，可点「✅ 应用到配置文件」后重启生效。")
        else:
            block.append("⚠️ 建议参数未明显优于当前，谨慎应用。")
        self.paper_review_text.insert(tk.END, "\n".join(block) + "\n")
        self.paper_review_text.see(tk.END)

    def _fill_review_params(self, suggestions=None):
        """把 AI 复盘的 param_suggestions 填入参数输入框"""
        if suggestions is None:
            suggestions = (self.last_review or {}).get('param_suggestions', [])
        if not suggestions:
            messagebox.showinfo("提示", "没有复盘参数建议，请先执行 AI 复盘")
            return
        filled = 0
        for ps in suggestions:
            if not isinstance(ps, dict):
                continue
            param = ps.get('param', '')
            suggest = ps.get('suggest')
            if suggest is None:
                continue
            # 参数名映射 (处理 AI 可能返回的不同名称)
            name_map = {
                'confidence_threshold': 'confidence_threshold',
                'min_confidence': 'confidence_threshold',
                'news_filter_threshold': 'news_filter_threshold',
                'atr_sl_mult': 'atr_sl_mult',
                'atr_tp1_mult': 'atr_tp1_mult',
                'atr_tp2_mult': 'atr_tp2_mult',
                'atr_tp3_mult': 'atr_tp3_mult',
                'max_position_pct': 'max_position_pct',
            }
            key = name_map.get(param)
            if key and key in self._param_entries:
                self._param_entries[key].set(str(suggest))
                filled += 1
        if filled > 0:
            self._param_apply_label.set(f"✨ 已填入 {filled} 项建议，确认后点「应用到配置文件」")

    def _apply_params_to_config(self):
        """把界面上的参数写入 config.yaml"""
        import yaml
        try:
            config_path = str(PROJECT_ROOT / "config.yaml")
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}

            changes = []
            change_pairs = []  # 供 learner.log_param_change 使用
            for label, section, key, default in self._param_defs:
                val_str = self._param_entries[key].get().strip()
                if not val_str:
                    continue
                try:
                    new_val = float(val_str)
                except ValueError:
                    continue
                # 确保 section 存在
                if section not in cfg:
                    cfg[section] = {}
                old_val = cfg[section].get(key, default)
                if abs(float(old_val) - new_val) > 1e-9:
                    cfg[section][key] = new_val
                    changes.append(f"{key}: {old_val} → {new_val}")
                    change_pairs.append((key, old_val, new_val))

            if not changes:
                self._param_apply_label.set("无变化")
                return

            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

            # 🆕 方向4: 写入 param_change_log 供下次复盘坐 A/B 对比
            try:
                review_summary = (self.last_review or {}).get('summary', '') if hasattr(self, 'last_review') else ''
                source = "AI_REVIEW" if review_summary else "MANUAL"
                if hasattr(self.learner, 'log_param_change'):
                    for k, ov, nv in change_pairs:
                        try:
                            self.learner.log_param_change(
                                param_name=k, old_value=ov, new_value=nv,
                                source=source, review_summary=review_summary,
                                paper_engine=self.paper_engine
                            )
                        except Exception as e:
                            logger.debug(f"log_param_change 失败 {k}: {e}")
            except Exception as e:
                logger.debug(f"参数变更日志写入异常: {e}")

            self._param_apply_label.set(f"✅ 已保存 {len(changes)} 项，重启生效")
            self.update_status(f"参数已保存到 config.yaml: {'; '.join(changes)}")
            messagebox.showinfo("参数已保存", f"以下参数已写入 config.yaml:\n\n" + "\n".join(changes) + "\n\n重启工具后生效。")
        except Exception as e:
            messagebox.showerror("保存失败", f"写入 config.yaml 失败: {e}")

    def _read_param_entries(self) -> dict:
        """从参数面板读取当前输入值 (float)"""
        out = {}
        for key, var in self._param_entries.items():
            try:
                out[key] = float(var.get())
            except Exception:
                out[key] = None
        return out

    def _build_enriched_review_payload(self, max_trades: int = 30) -> dict:
        """汇总多源信号后返回充足 payload (供 AI 复盘 v2 使用).

        包含：
          - 分桶表现 / MFE / MAE / r_multiple (由 paper_engine 提供)
          - 13 机构策略表现 (learner.get_strategy_performance)
          - 近期参数调整及事后胜率变化 (learner.get_recent_param_changes)
          - Pattern Memory 胜率汇总 (如可用)
          - 反事实统计 (counterfactual_analyzer.get_summary_stats)
        任何一项获取失败都静默降级，不影响复盘.
        """
        # 1. 策略表现
        strategy_perf = []
        try:
            if hasattr(self.learner, "get_strategy_performance"):
                strategy_perf = self.learner.get_strategy_performance(min_total=1) or []
        except Exception as e:
            logger.debug(f"strategy_perf 获取失败: {e}")

        # 2. 参数变更日志
        recent_changes = []
        try:
            if hasattr(self.learner, "get_recent_param_changes"):
                recent_changes = self.learner.get_recent_param_changes(
                    limit=5, paper_engine=self.paper_engine
                ) or []
        except Exception as e:
            logger.debug(f"recent_param_changes 获取失败: {e}")

        # 3. Pattern Memory 汇总
        pattern_insight = None
        try:
            if getattr(self, "pattern_memory", None) is not None:
                pcount = self.pattern_memory.get_pattern_count()
                pattern_insight = {"total_patterns": pcount}
        except Exception as e:
            logger.debug(f"pattern_insight 获取失败: {e}")

        # 4. 反事实统计
        cf_stats = None
        try:
            if getattr(self, "counterfactual", None) is not None:
                cf_stats = self.counterfactual.get_summary_stats(limit=max_trades)
                if isinstance(cf_stats, dict) and "text" in cf_stats:
                    cf_stats = {k: v for k, v in cf_stats.items() if k != "text"}
        except Exception as e:
            logger.debug(f"counterfactual_stats 获取失败: {e}")

        # 5. 调 paper_engine
        try:
            return self.paper_engine.build_review_payload(
                max_trades=max_trades,
                strategy_perf=strategy_perf or None,
                recent_param_changes=recent_changes or None,
                pattern_insight=pattern_insight,
                counterfactual_stats=cf_stats,
            )
        except TypeError:
            return self.paper_engine.build_review_payload(max_trades=max_trades)

