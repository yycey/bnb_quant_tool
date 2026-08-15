"""Mixin: SignalTrackingTabMixin"""

from gui._imports import *


class SignalTrackingTabMixin:
    def _create_signal_tracking_tab(self, parent):
        """创建信号追踪标签页：历史信号胜率 + 手动回填"""
        # ─── 顶部：统计卡片 + 操作按钮 ───
        top_frame = ttk.LabelFrame(parent, text="📊 信号胜率统计", padding=8)
        top_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        top_frame.columnconfigure(0, weight=1)

        cards = ttk.Frame(top_frame)
        cards.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 4))
        for i in range(5):
            cards.columnconfigure(i, weight=1)
        self._sig_card_vars = {
            'total': tk.StringVar(value="—"),
            'followed': tk.StringVar(value="—"),
            'feedbacked': tk.StringVar(value="—"),
            'long_wr': tk.StringVar(value="—"),
            'short_wr': tk.StringVar(value="—"),
        }
        card_defs = [
            ("总信号", 'total', "#1565C0"),
            ("已跟单", 'followed', "#2E7D32"),
            ("已回填", 'feedbacked', "#EF6C00"),
            ("买胜率", 'long_wr', "#00897B"),
            ("卖胜率", 'short_wr', "#C62828"),
        ]
        for i, (label, key, color) in enumerate(card_defs):
            card = ttk.Frame(cards, relief="solid", borderwidth=1, padding=6)
            card.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=3)
            ttk.Label(card, text=label, foreground="#666").pack()
            tk.Label(card, textvariable=self._sig_card_vars[key],
                     fg=color, font=("", 12, "bold")).pack(pady=(2, 0))

        btn_row = ttk.Frame(top_frame)
        btn_row.grid(row=1, column=0, sticky=(tk.W, tk.E))
        ttk.Button(btn_row, text="🔄 刷新", command=self._refresh_signal_stats).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="✅ 标记跟单", command=self._mark_signal_followed).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_row, text="📝 回填结果", command=self._fill_signal_result).pack(side=tk.LEFT, padx=3)
        self._signal_status_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._signal_status_var, foreground="#666").pack(side=tk.LEFT, padx=10)

        # ─── 中部：信号列表 ───
        list_frame = ttk.LabelFrame(parent, text="📋 最近信号", padding=4)
        list_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=4)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        cols = ("id", "time", "symbol", "dir", "entry", "sl", "tp1", "conf", "followed", "pnl", "feedback")
        self.signal_tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=12)
        col_widths = {"id": 40, "time": 130, "symbol": 80, "dir": 60, "entry": 70,
                      "sl": 70, "tp1": 70, "conf": 50, "followed": 55, "pnl": 70, "feedback": 55}
        col_headers = {"id": "#", "time": "时间", "symbol": "交易对", "dir": "方向",
                       "entry": "入场", "sl": "止损", "tp1": "TP1",
                       "conf": "置信", "followed": "跟单", "pnl": "盈亏", "feedback": "回填"}
        for c in cols:
            self.signal_tree.heading(c, text=col_headers[c])
            self.signal_tree.column(c, width=col_widths[c], minwidth=35)
        # 滚动条
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.signal_tree.yview)
        self.signal_tree.configure(yscrollcommand=sb.set)
        self.signal_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        sb.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # 初始加载
        self._refresh_signal_stats()

    def _refresh_signal_stats(self):
        """刷新信号统计和列表"""
        try:
            stats = self.paper_engine.get_signal_stats()
            self._sig_card_vars['total'].set(str(stats.get('total_signals', 0)))
            self._sig_card_vars['followed'].set(str(stats.get('followed', 0)))
            self._sig_card_vars['feedbacked'].set(str(stats.get('feedbacked', 0)))
            by_dir = stats.get('by_direction', {})
            long_stats = by_dir.get('LONG', {})
            short_stats = by_dir.get('SHORT', {})
            self._sig_card_vars['long_wr'].set(
                f"{long_stats.get('win_rate', 0):.0%}" if long_stats else "—")
            self._sig_card_vars['short_wr'].set(
                f"{short_stats.get('win_rate', 0):.0%}" if short_stats else "—")
            # 刷新列表
            for item in self.signal_tree.get_children():
                self.signal_tree.delete(item)
            for s in stats.get('recent', []):
                dir_cn = {"LONG": "买", "SHORT": "卖", "WAIT": "观望"}.get(s.get('direction'), s.get('direction', ''))
                followed_cn = "✅" if s.get('followed') else "❌"
                pnl = s.get('actual_pnl_usdt')
                pnl_str = f"{pnl:+.2f}" if pnl is not None else "—"
                feedback_cn = "✅" if s.get('feedback_at') else "—"
                ts = (s.get('generated_at') or '')[:16]
                self.signal_tree.insert('', tk.END, iid=str(s['id']), values=(
                    s['id'], ts, s.get('symbol', ''), dir_cn,
                    f"{s.get('entry_price', 0):.2f}" if s.get('entry_price') else "—",
                    f"{s.get('stop_loss', 0):.2f}" if s.get('stop_loss') else "—",
                    f"{s.get('tp1', 0):.2f}" if s.get('tp1') else "—",
                    f"{s.get('confidence', 0):.0%}" if s.get('confidence') else "—",
                    followed_cn, pnl_str, feedback_cn,
                ))
        except Exception as e:
            self._signal_status_var.set(f"刷新失败: {e}")

    def _mark_signal_followed(self):
        """标记选中信号为已跟单"""
        sel = self.signal_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个信号")
            return
        sid = int(sel[0])
        if self.paper_engine.mark_signal_followed(sid):
            self._signal_status_var.set(f"信号 #{sid} 已标记为跟单")
            self._refresh_signal_stats()
        else:
            messagebox.showerror("错误", "标记失败")

    def _fill_signal_result(self):
        """回填选中信号的实际交易结果"""
        sel = self.signal_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个信号")
            return
        sid = int(sel[0])
        # 弹出回填对话框
        dialog = tk.Toplevel(self.root)
        dialog.title(f"回填信号 #{sid} 实际结果")
        dialog.geometry("350x320")
        dialog.transient(self.root)
        dialog.grab_set()

        fields = {}
        row = 0
        for label, key, default in [
            ("实际入场价", "actual_entry", ""),
            ("实际平仓价", "actual_exit", ""),
            ("盈亏 (USDT)", "pnl_usdt", ""),
            ("盈亏 (%)", "pnl_pct", ""),
            ("平仓原因", "exit_reason", ""),
            ("备注", "feedback_note", ""),
        ]:
            ttk.Label(dialog, text=label + ":").grid(row=row, column=0, sticky=tk.W, padx=10, pady=5)
            var = tk.StringVar(value=default)
            ttk.Entry(dialog, textvariable=var, width=20).grid(row=row, column=1, padx=10, pady=5)
            fields[key] = var
            row += 1

        def _submit():
            try:
                actual_exit = float(fields['actual_exit'].get() or 0)
                pnl_usdt = float(fields['pnl_usdt'].get() or 0) or None
                pnl_pct = float(fields['pnl_pct'].get() or 0) or None
                self.paper_engine.fill_signal_result(
                    sid,
                    actual_exit=actual_exit,
                    actual_pnl_usdt=pnl_usdt,
                    actual_pnl_pct=pnl_pct,
                    exit_reason=fields['exit_reason'].get(),
                    feedback_note=fields['feedback_note'].get(),
                )
                # 同时标记已跟单
                actual_entry = float(fields['actual_entry'].get() or 0) or None
                if actual_entry:
                    self.paper_engine.mark_signal_followed(sid, actual_entry=actual_entry)
                dialog.destroy()
                self._signal_status_var.set(f"信号 #{sid} 回填成功")
                self._refresh_signal_stats()
            except Exception as e:
                messagebox.showerror("回填失败", str(e))

        ttk.Button(dialog, text="✅ 提交", command=_submit).grid(
            row=row, column=0, columnspan=2, pady=15)

