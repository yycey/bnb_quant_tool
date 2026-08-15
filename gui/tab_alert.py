"""Mixin: AlertTabMixin"""

from gui._imports import *


class AlertTabMixin:
    def _create_alert_tab(self, parent):
        ctrl = ttk.Frame(parent)
        ctrl.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.alert_status_var = tk.StringVar(value="状态: 未启动")
        ttk.Label(ctrl, textvariable=self.alert_status_var).pack(side=tk.LEFT, padx=10)
        ttk.Button(ctrl, text="从当前开单建议加载规则", command=self._alert_load_from_advice).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="启动监控", command=self._alert_start).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="停止", command=self._alert_stop).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="清空规则", command=self._alert_clear).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="刷新状态", command=self._alert_refresh).pack(side=tk.LEFT, padx=5)

        out = ttk.Frame(parent)
        out.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)
        cols = ('ID', 'Name', 'Direction', 'Target', 'Triggered', 'TriggerTime', 'TriggerPrice')
        self.alert_tree = ttk.Treeview(out, columns=cols, show='headings', height=10)
        for c in cols:
            self.alert_tree.heading(c, text=c)
            self.alert_tree.column(c, width=130 if c == 'Name' else 100)
        self.alert_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

    def _alert_load_from_advice(self):
        advice = (self.analysis_result or {}).get('trade_advice') if self.analysis_result else None
        if not advice:
            messagebox.showwarning("提示", "请先运行一次分析生成开单建议")
            return
        self.alert_engine.load_from_advice(advice)
        self._alert_refresh()
        messagebox.showinfo("成功", f"已加载 {len(self.alert_engine.rules)} 条规则")

    def _alert_start(self):
        self.alert_engine.start()
        self.alert_status_var.set(f"状态: 运行中 ({self.alert_engine.symbol})")

    def _alert_stop(self):
        self.alert_engine.stop()
        self.alert_status_var.set("状态: 已停止")

    def _alert_clear(self):
        self.alert_engine.clear_rules()
        self._alert_refresh()

    def _alert_refresh(self):
        for item in self.alert_tree.get_children():
            self.alert_tree.delete(item)
        for r in self.alert_engine.get_rules_status():
            target_str = f"{r['target']}" + (f"~{r['range_high']}" if r['range_high'] else "")
            self.alert_tree.insert('', 'end', values=(
                r['id'], r['name'], r['direction'], target_str,
                'Yes' if r['triggered'] else 'No',
                r['trigger_time'], r['trigger_price']
            ))
        if self.alert_engine.is_running():
            self.alert_status_var.set(f"状态: 运行中  最近价={self.alert_engine.last_price}")

    def _on_price_alert(self, rule, price):
        """价格规则触发时的回调（在后台线程，需用 root.after 跳回主线程）"""
        msg = f"价格预警触发!\n\n{rule.name}\n当前价: {price}\n时间: {rule.trigger_time}"
        self.root.after(0, lambda: messagebox.showwarning("价格预警", msg))
        self.root.after(0, self._alert_refresh)

