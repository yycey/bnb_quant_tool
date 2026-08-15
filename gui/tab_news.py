"""Mixin: NewsTabMixin"""

from gui._imports import *


class NewsTabMixin:
    def _create_news_tab(self, parent):
        """创建新闻情报标签页：刷新新闻 + AI 利好利空总结"""
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        ttk.Label(bar, text="币种:").pack(side=tk.LEFT, padx=5)
        self.news_symbol_var = tk.StringVar(value=self.symbol_var.get().replace('USDT', '') or 'BNB')
        ttk.Combobox(bar, textvariable=self.news_symbol_var,
                     values=['BNB', 'BTC', 'ETH', 'SOL'],
                     width=8, state='readonly').pack(side=tk.LEFT, padx=5)

        ttk.Label(bar, text="近 N 小时:").pack(side=tk.LEFT, padx=(10, 0))
        self.news_hours_var = tk.StringVar(value='24')
        ttk.Spinbox(bar, from_=1, to=168, textvariable=self.news_hours_var, width=6).pack(side=tk.LEFT, padx=5)

        ttk.Button(bar, text="拉取新闻", command=self._news_fetch).pack(side=tk.LEFT, padx=10)
        ttk.Button(bar, text="AI 总结利好利空", command=self._news_summarize).pack(side=tk.LEFT, padx=5)

        # 主显示区双面板
        body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)

        left = ttk.LabelFrame(body, text="新闻列表")
        right = ttk.LabelFrame(body, text="AI 总结 / 利好利空判断")
        body.add(left, weight=2)
        body.add(right, weight=1)

        self.news_list_text = scrolledtext.ScrolledText(left, wrap=tk.WORD, height=22, font=('Consolas', 9))
        self.news_list_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.news_summary_text = scrolledtext.ScrolledText(right, wrap=tk.WORD, height=22, font=('Consolas', 9))
        self.news_summary_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 默认提示
        self.news_summary_text.insert(
            1.0,
            "点击 「拉取新闻」 从 RSS 源获取最新加密货币新闻。\n"
            "点击 「AI 总结利好利空」 调用 DeepSeek 判断未来1～7天价格影响。\n\n"
            "提示：首次拉取可能需 5～10 秒（联网 5 个源）。"
        )

    def _news_fetch(self):
        """后台线程拉取新闻，避免阻塞 GUI"""
        symbol = self.news_symbol_var.get() or 'BNB'
        try:
            hours = int(self.news_hours_var.get() or 24)
        except ValueError:
            hours = 24
        self.news_list_text.delete(1.0, tk.END)
        self.news_list_text.insert(1.0, f"正在拉取 {symbol} 近 {hours} 小时新闻...\n")

        def _worker():
            try:
                items = self.news_collector.collect(
                    symbol=symbol, hours=hours, max_items=40
                )
            except Exception as e:
                self.root.after(0, lambda: self._news_show_error(str(e)))
                return
            self.last_news_items = items
            self.root.after(0, lambda: self._news_render_list(items, symbol, hours))

        threading.Thread(target=_worker, daemon=True).start()

    def _news_show_error(self, msg: str):
        self.news_list_text.delete(1.0, tk.END)
        self.news_list_text.insert(1.0, f"拉取失败: {msg}\n")

    def _news_render_list(self, items, symbol, hours):
        self.news_list_text.delete(1.0, tk.END)
        if not items:
            # 未返回时提供本地快速极性估计作为备选
            self.news_list_text.insert(
                1.0,
                f"暂未拉到 {symbol} 近 {hours} 小时的相关新闻。\n"
                "可能原因：\n"
                "  - 网络不可达或被墙\n"
                "  - 关键字未命中 (试试换币种或加長时间)\n"
            )
            return
        polarity = self.news_collector.quick_polarity(items)
        header = (
            f"拉取到 {len(items)} 条 {symbol} 相关新闻 (近 {hours} 小时)\n"
            f"本地极性粗判: {polarity['polarity']} (利好 {polarity['bullish_count']} / 利空 {polarity['bearish_count']})\n"
            + "=" * 80 + "\n\n"
        )
        self.news_list_text.insert(1.0, header)
        for i, n in enumerate(items, 1):
            line = (
                f"[{i:02d}] {n.get('published', '')} | {n.get('source', '?')}\n"
                f"     {n.get('title', '')}\n"
                f"     {n.get('summary', '')[:200]}\n"
                f"     URL: {n.get('url', '')}\n\n"
            )
            self.news_list_text.insert(tk.END, line)

    def _news_summarize(self):
        """调用 LLM 总结利好利空"""
        if not self.last_news_items:
            messagebox.showinfo("提示", "请先点击 「拉取新闻」。")
            return
        from bnb_quant_tool.llm_provider import get_llm_credentials, build_llm_analyzer
        llm = get_llm_credentials(self.config)
        if not llm["api_key"]:
            messagebox.showerror(
                "需要 LLM API Key",
                "在 config.yaml 配置 qianwen / deepseek / volcengine 的 api_key 后再调用 AI 总结。"
            )
            return

        symbol = self.news_symbol_var.get() or 'BNB'
        self.news_summary_text.delete(1.0, tk.END)
        self.news_summary_text.insert(1.0, f"正在调用 {llm['provider']} 总结...\n")

        def _worker():
            try:
                analyzer = build_llm_analyzer(self.config)
                summary = analyzer.summarize_news(self.last_news_items, symbol=symbol)
            except Exception as e:
                self.root.after(0, lambda: self._news_summary_error(str(e)))
                return
            self.last_news_summary = summary
            self.root.after(0, lambda: self._news_render_summary(summary))

        threading.Thread(target=_worker, daemon=True).start()

    def _news_summary_error(self, msg: str):
        self.news_summary_text.delete(1.0, tk.END)
        self.news_summary_text.insert(1.0, f"AI 总结失败: {msg}\n")

    def _news_render_summary(self, summary: dict):
        self.news_summary_text.delete(1.0, tk.END)
        if not summary:
            self.news_summary_text.insert(1.0, "AI 返回为空\n")
            return
        polarity = summary.get('polarity', 'neutral')
        emoji = {'bullish': '📈', 'bearish': '📉', 'neutral': '➡️'}.get(polarity, '❓')
        suggestion = summary.get('trade_suggestion', 'WAIT')
        text = []
        text.append(f"{emoji} 综合方向: {polarity.upper()}")
        text.append(f"置信度: {summary.get('confidence', 0):.0%}")
        text.append(f"评分: {summary.get('score', 0):+.2f}  范围±1.0")
        text.append(f"影响时长: {summary.get('impact_horizon', 'short')}")
        text.append(f"开单建议: {suggestion}")
        text.append("")
        text.append("一句话总结:")
        text.append(f"  {summary.get('summary', '')}")
        text.append("")
        if summary.get('key_bullish'):
            text.append("🟢 利好要点:")
            for b in summary['key_bullish']:
                text.append(f"  • {b}")
            text.append("")
        if summary.get('key_bearish'):
            text.append("🔴 利空要点:")
            for b in summary['key_bearish']:
                text.append(f"  • {b}")
            text.append("")
        if summary.get('caution'):
            text.append(f"⚠️ 风险提示: {summary['caution']}")
        text.append("")
        text.append(f"参考新闻条数: {summary.get('news_count', 0)}")
        text.append("")
        text.append("提示: 下次点击 「开始分析」时，该新闻结论会自动参与开单建议修正。")
        self.news_summary_text.insert(1.0, "\n".join(text))

