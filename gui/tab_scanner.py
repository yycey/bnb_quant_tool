"""Mixin: ScannerTabMixin"""

from gui._imports import *


class ScannerTabMixin:
    def _create_scanner_tab(self, parent):
        """创建主动信号扫描器标签页"""
        # ─── 顶部：控制面板 ───
        ctrl_frame = ttk.LabelFrame(parent, text="📡 扫描控制", padding=8)
        ctrl_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        ctrl_frame.columnconfigure(0, weight=1)

        btn_row = ttk.Frame(ctrl_frame)
        btn_row.grid(row=0, column=0, sticky=(tk.W, tk.E))

        self._scanner_btn = ttk.Button(btn_row, text="📡 启动扫描", command=self._toggle_scanner)
        self._scanner_btn.pack(side=tk.LEFT, padx=3)

        ttk.Button(btn_row, text="🔄 刷新", command=self._refresh_scanner_tab).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="🗑️ 清空历史", command=self._clear_scanner_history).pack(side=tk.LEFT, padx=3)

        self._scanner_status_var = tk.StringVar(value="扫描器: 关闭")
        ttk.Label(btn_row, textvariable=self._scanner_status_var,
                  foreground="#1565C0", font=("", 10, "bold")).pack(side=tk.LEFT, padx=15)

        # 参数行
        param_row = ttk.Frame(ctrl_frame)
        param_row.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(5, 0))

        ttk.Label(param_row, text="扫描间隔:").pack(side=tk.LEFT, padx=3)
        sc_cfg = (getattr(self, "config", None) or {}).get("signal_scanner") or {}
        self._scanner_interval_var = tk.StringVar(
            value=str(sc_cfg.get("scan_interval", 60))
        )
        ttk.Entry(param_row, textvariable=self._scanner_interval_var, width=5).pack(side=tk.LEFT, padx=3)
        ttk.Label(param_row, text="秒").pack(side=tk.LEFT, padx=3)

        ttk.Label(param_row, text="冷却期:").pack(side=tk.LEFT, padx=10)
        self._scanner_cooldown_var = tk.StringVar(
            value=str(sc_cfg.get("cooldown_seconds", 300))
        )
        ttk.Entry(param_row, textvariable=self._scanner_cooldown_var, width=5).pack(side=tk.LEFT, padx=3)
        ttk.Label(param_row, text="秒").pack(side=tk.LEFT, padx=3)

        ttk.Label(param_row, text="触发强度≥").pack(side=tk.LEFT, padx=10)
        self._scanner_min_strength_var = tk.StringVar(
            value=str(sc_cfg.get("min_strength", 0.6))
        )
        ttk.Entry(param_row, textvariable=self._scanner_min_strength_var, width=4).pack(side=tk.LEFT, padx=3)

        # 触发 fullauto 开关
        self._scanner_trigger_fullauto_var = tk.BooleanVar(
            value=bool(sc_cfg.get("trigger_fullauto", True))
        )
        ttk.Checkbutton(
            param_row, text="信号触发 AI 全自动分析",
            variable=self._scanner_trigger_fullauto_var
        ).pack(side=tk.LEFT, padx=10)

        # ─── 统计卡片 ───
        stats_frame = ttk.Frame(ctrl_frame)
        stats_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(5, 0))
        for i in range(6):
            stats_frame.columnconfigure(i, weight=1)

        self._scanner_card_vars = {
            'scans': tk.StringVar(value="0"),
            'signals': tk.StringVar(value="0"),
            'triggered': tk.StringVar(value="0"),
            'rsi_cross': tk.StringVar(value="0"),
            'bb_touch': tk.StringVar(value="0"),
            'volume': tk.StringVar(value="0"),
        }
        card_defs = [
            ("扫描次数", 'scans', "#1565C0"),
            ("信号数", 'signals', "#2E7D32"),
            ("触发全自动", 'triggered', "#EF6C00"),
            ("RSI穿越", 'rsi_cross', "#00897B"),
            ("布林触边", 'bb_touch', "#6A1B9A"),
            ("量能异动", 'volume', "#C62828"),
        ]
        for i, (label, key, color) in enumerate(card_defs):
            card = ttk.Frame(stats_frame, relief="solid", borderwidth=1, padding=5)
            card.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=2)
            ttk.Label(card, text=label, foreground="#666").pack()
            tk.Label(card, textvariable=self._scanner_card_vars[key],
                     fg=color, font=("", 11, "bold")).pack(pady=(2, 0))

        # ─── 实时日志 ───
        log_frame = ttk.LabelFrame(parent, text="📡 扫描日志", padding=4)
        log_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=4)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        self._scanner_log = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=('Consolas', 9),
            bg='#0d1117', fg='#58a6ff', height=15
        )
        self._scanner_log.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self._scanner_log.insert(tk.END, "📡 信号扫描器就绪。点击「启动扫描」开始主动盯盘。\n")
        self._scanner_log.config(state=tk.DISABLED)

        # ─── 底部：最近信号列表 ───
        sig_frame = ttk.LabelFrame(parent, text="📋 最近扫描信号", padding=4)
        sig_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=4)
        sig_frame.columnconfigure(0, weight=1)
        sig_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        cols = ("id", "time", "type", "dir", "symbol", "price", "strength", "detail", "fullauto")
        self._scanner_tree = ttk.Treeview(sig_frame, columns=cols, show="headings", height=8)
        col_w = {"id": 40, "time": 80, "type": 100, "dir": 60, "symbol": 80,
                 "price": 70, "strength": 55, "detail": 300, "fullauto": 55}
        col_h = {"id": "#", "time": "时间", "type": "类型", "dir": "方向",
                 "symbol": "品种", "price": "价格", "strength": "强度",
                 "detail": "详情", "fullauto": "触发"}
        for c in cols:
            self._scanner_tree.heading(c, text=col_h[c])
            self._scanner_tree.column(c, width=col_w[c], minwidth=30)
        sb2 = ttk.Scrollbar(sig_frame, orient=tk.VERTICAL, command=self._scanner_tree.yview)
        self._scanner_tree.configure(yscrollcommand=sb2.set)
        self._scanner_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        sb2.grid(row=0, column=1, sticky=(tk.N, tk.S))

    def _toggle_scanner(self):
        """切换扫描器开关"""
        if self._scanner and self._scanner.is_running:
            self._stop_scanner()
        else:
            self._start_scanner()

    def _start_scanner(self):
        """启动信号扫描器"""
        try:
            interval = int(self._scanner_interval_var.get())
        except ValueError:
            interval = 60
        try:
            cooldown = int(self._scanner_cooldown_var.get())
        except ValueError:
            cooldown = 300
        try:
            min_strength = float(self._scanner_min_strength_var.get())
        except ValueError:
            min_strength = 0.6

        paper_db = getattr(self.paper_engine, "db_path", None)

        self._scanner = SignalScanner(
            fetcher=self.fetcher,
            db_path=paper_db,
            config={
                'scan_interval': interval,
                'cooldown_seconds': cooldown,
                'min_strength': min_strength,
                'symbols': [self.symbol_var.get()],
                'account_balance': self._get_account_balance(),
            },
            on_signal=self._on_scanner_signal,
        )
        self._scanner.start()
        self._scanner_running = True
        try:
            self._scanner_btn.config(text="⏹️ 停止扫描")
        except Exception:
            pass
        self._scanner_status_var.set("扫描器: 运行中 🚀")
        self._append_scanner_log(f"🚀 扫描器已启动 | 间隔={interval}s | 冷却={cooldown}s | 最低强度={min_strength} | 品种={self.symbol_var.get()}")
        self.update_status("📡 信号扫描器已启动！")

    def _stop_scanner(self):
        """停止信号扫描器"""
        if self._scanner:
            self._scanner.stop()
            self._scanner = None
        self._scanner_running = False
        try:
            self._scanner_btn.config(text="📡 启动扫描")
        except Exception:
            pass
        self._scanner_status_var.set("扫描器: 关闭")
        self._append_scanner_log("⏹️ 扫描器已停止")
        self.update_status("📡 信号扫描器已停止")

    def _on_scanner_signal(self, sig: ScanSignal):
        """扫描器信号回调（在扫描线程中调用）"""
        # 在主线程更新 UI
        try:
            self.root.after(0, lambda: self._handle_scanner_signal(sig))
        except Exception:
            pass

    def _handle_scanner_signal(self, sig: ScanSignal):
        """在主线程处理扫描信号"""
        dir_cn = {"BULLISH": "🟢看多", "BEARISH": "🔴看空", "NEUTRAL": "⚪中性"}.get(sig.direction, sig.direction)
        type_cn = {
            "RSI_CROSS": "RSI穿越", "BB_TOUCH": "布林触边",
            "VOLUME_SPIKE": "量能异动", "BREAKOUT": "价格突破",
            "MTF_CONFLUENCE": "多周期共振", "ATR_SPIKE": "急涨急跌",
        }.get(sig.signal_type, sig.signal_type)

        self._append_scanner_log(
            f"{'🔥' if sig.strength >= 0.6 else '📡'} {type_cn} | {dir_cn} | "
            f"{sig.symbol} @ {sig.price:.2f} | 强度={sig.strength:.2f} | {sig.detail}"
        )

        # 更新统计
        self._refresh_scanner_stats()

        # 是否触发 fullauto
        try:
            min_strength = float(self._scanner_min_strength_var.get())
        except ValueError:
            min_strength = 0.6

        if self._scanner_trigger_fullauto_var.get() and sig.strength >= min_strength:
            ap = getattr(self, "autopilot", None)
            if ap:
                triggered = ap.request_scanner_cycle(
                    strength=sig.strength,
                    min_strength=min_strength,
                    signal_type=getattr(sig, "signal_type", "") or "",
                    on_trigger=lambda: self.root.after(0, self._trigger_autopilot_once),
                    on_skip=lambda msg: self._append_scanner_log(f"   ⏸ {msg}"),
                )
                if triggered:
                    self._append_scanner_log(
                        f"   🚀 大波动 {sig.signal_type} 强度 {sig.strength:.2f}≥{min_strength}，分析已排队"
                    )
            elif self._ai_fullauto_running:
                self._append_scanner_log("   ⚡ 全自动已在运行，跳过触发")
            else:
                # 无 AutopilotController 时，仍按大波动类型过滤
                sc = (getattr(self, "config", None) or {}).get("signal_scanner") or {}
                allow = True
                if bool(sc.get("trigger_on_big_move_only", True)):
                    allowed = {
                        str(t).upper()
                        for t in (sc.get("trigger_signal_types") or [
                            "PRICE_SHOCK", "ATR_SPIKE", "VOLUME_SPIKE", "BREAKOUT"
                        ])
                    }
                    allow = str(getattr(sig, "signal_type", "")).upper() in allowed
                if not allow:
                    self._append_scanner_log(
                        f"   ⏸ {sig.signal_type} 非大波动，仅盯盘不分析"
                    )
                else:
                    self._append_scanner_log(
                        f"   🚀 大波动 {sig.signal_type} 强度 {sig.strength:.2f}≥{min_strength}，触发 AI 分析"
                    )
                    self.root.after(0, self._start_ai_fullauto)
        elif self._scanner_trigger_fullauto_var.get():
            self._append_scanner_log(
                f"   ⏸ 强度 {sig.strength:.2f} < {min_strength}，未触发全自动"
            )

    def _append_scanner_log(self, msg: str):
        """追加扫描日志"""
        try:
            self._scanner_log.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")
            self._scanner_log.insert(tk.END, f"[{ts}] {msg}\n")
            self._scanner_log.see(tk.END)
            # 限制日志行数
            lines = int(self._scanner_log.index('end-1c').split('.')[0])
            if lines > 500:
                self._scanner_log.delete('1.0', f'{lines - 500}.0')
            self._scanner_log.config(state=tk.DISABLED)
        except Exception:
            pass

    def _refresh_scanner_tab(self):
        """刷新扫描器 Tab 的统计和信号列表"""
        self._refresh_scanner_stats()
        self._refresh_scanner_signal_list()

    def _refresh_scanner_stats(self):
        """刷新扫描器统计卡片"""
        if not self._scanner:
            return
        try:
            stats = self._scanner.stats
            self._scanner_card_vars['scans'].set(str(stats.get('scan_count', 0)))
            self._scanner_card_vars['signals'].set(str(stats.get('signal_count', 0)))

            # 从 DB 统计各类型
            signals = self._scanner.get_recent_signals(limit=200)
            triggered = sum(1 for s in signals if s.get('triggered_fullauto'))
            rsi_count = sum(1 for s in signals if s.get('signal_type') == 'RSI_CROSS')
            bb_count = sum(1 for s in signals if s.get('signal_type') == 'BB_TOUCH')
            vol_count = sum(1 for s in signals if s.get('signal_type') == 'VOLUME_SPIKE')
            self._scanner_card_vars['triggered'].set(str(triggered))
            self._scanner_card_vars['rsi_cross'].set(str(rsi_count))
            self._scanner_card_vars['bb_touch'].set(str(bb_count))
            self._scanner_card_vars['volume'].set(str(vol_count))
            try:
                from bnb_quant_tool.trading_profile import get_decision_funnel
                funnel = get_decision_funnel()
                # 三段漏斗：触发分析 / 过门 / 开仓（覆盖「triggered」卡片旁的语义）
                a = int(funnel.get("analysis_triggered") or 0)
                g = int(funnel.get("gate_passed") or 0)
                o = int(funnel.get("opened") or 0)
                self._scanner_card_vars['triggered'].set(f"{triggered} | 漏斗 {a}/{g}/{o}")
            except Exception:
                pass
        except Exception:
            pass

    def _refresh_scanner_signal_list(self):
        """刷新扫描信号列表"""
        if not self._scanner:
            return
        try:
            signals = self._scanner.get_recent_signals(limit=50)
            for item in self._scanner_tree.get_children():
                self._scanner_tree.delete(item)
            for s in signals:
                type_cn = {
                    "RSI_CROSS": "RSI穿越", "BB_TOUCH": "布林触边",
                    "VOLUME_SPIKE": "量能异动", "BREAKOUT": "价格突破",
                    "MTF_CONFLUENCE": "多周期共振", "ATR_SPIKE": "急涨急跌",
                }.get(s.get('signal_type', ''), s.get('signal_type', ''))
                dir_cn = {
                    "BULLISH": "🟢看多", "BEARISH": "🔴看空", "NEUTRAL": "⚪中性",
                }.get(s.get('direction', ''), s.get('direction', ''))
                fullauto_cn = "✅" if s.get('triggered_fullauto') else "—"
                ts = (s.get('created_at') or '')[:16]
                self._scanner_tree.insert('', tk.END, values=(
                    s.get('id', ''), ts, type_cn, dir_cn,
                    s.get('symbol', ''),
                    f"{s.get('price', 0):.2f}" if s.get('price') else "—",
                    f"{s.get('strength', 0):.2f}" if s.get('strength') else "—",
                    s.get('detail', '')[:60],
                    fullauto_cn,
                ))
        except Exception:
            pass

    def _clear_scanner_history(self):
        """清空扫描信号历史"""
        if not messagebox.askyesno("确认", "清空所有扫描信号历史记录？"):
            return
        try:
            conn = sqlite3.connect(self.paper_engine.db_path, timeout=10)
            conn.execute("DELETE FROM scan_signals")
            conn.commit()
            conn.close()
            self._refresh_scanner_tab()
            self._append_scanner_log("🗑️ 扫描信号历史已清空")
        except Exception as e:
            self._append_scanner_log(f"清空失败: {e}")

