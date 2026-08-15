"""Mixin: MarketTabMixin"""

from gui._imports import *


class MarketTabMixin:
    def _create_market_tab(self, parent):
        """多周期共振 + 情绪面板"""
        ctrl = ttk.Frame(parent)
        ctrl.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Button(ctrl, text="刷新多周期", command=self._refresh_mtf).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="刷新情绪面板", command=self._refresh_sentiment).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="刷新链上筹码", command=self._refresh_onchain).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="刷新宏观数据", command=self._refresh_macro).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="一键刷新全部", command=self._refresh_market_all).pack(side=tk.LEFT, padx=5)

        out = ttk.Frame(parent)
        out.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)
        self.market_text = scrolledtext.ScrolledText(out, wrap=tk.WORD, font=('Consolas', 10))
        self.market_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.market_text.insert(1.0, "点击上方按钮拉取多周期共振与市场情绪。\n建议每 30 分钟手动刷新一次。\n")

    def _refresh_market_all(self):
        self._refresh_mtf()
        self._refresh_sentiment()
        self._refresh_onchain()
        self._refresh_macro()

    def _refresh_onchain(self):
        if not self.onchain_analyzer:
            self._append_market("链上分析未启用 (config.onchain.enabled=false)")
            return
        sym = self.symbol_var.get()
        def _do():
            try:
                res = self.onchain_analyzer.fetch_all(symbol=sym)
                self.last_onchain = res
                report = OnChainAnalyzer.format_report(res)
                self.root.after(0, lambda: self._append_market(report))
            except Exception as e:
                self.root.after(0, lambda: self._append_market(f"链上分析失败: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_macro(self):
        if not self.macro_layer:
            self._append_market("宏观数据层未启用 (config.macro.enabled=false)")
            return
        def _do():
            try:
                res = self.macro_layer.fetch_all()
                self.last_macro = res
                report = MacroDataLayer.format_report(res)
                self.root.after(0, lambda: self._append_market(report))
            except Exception as e:
                self.root.after(0, lambda: self._append_market(f"宏观数据失败: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_mtf(self):
        sym = self.symbol_var.get()
        self.market_text.insert(tk.END, f"\n拉取 {sym} 多周期...\n")
        self.root.update_idletasks()
        def _do():
            try:
                res = self.mtf_analyzer.analyze(symbol=sym)
                self.last_mtf = res
                report = MultiTimeframeAnalyzer.format_report(res)
                self.root.after(0, lambda: self._append_market(report))
            except Exception as e:
                self.root.after(0, lambda: self._append_market(f"多周期失败: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_sentiment(self):
        sym = self.symbol_var.get()
        def _do():
            try:
                res = self.sentiment_engine.fetch_all(symbol=sym)
                self.last_sentiment = res
                report = MarketSentiment.format_report(res)
                self.root.after(0, lambda: self._append_market(report))
            except Exception as e:
                self.root.after(0, lambda: self._append_market(f"情绪拉取失败: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _append_market(self, txt):
        self.market_text.insert(tk.END, "\n" + txt + "\n")
        self.market_text.see(tk.END)

