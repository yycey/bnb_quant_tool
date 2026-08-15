"""Mixin: LearningTabMixin"""

from gui._imports import *


class LearningTabMixin:
    def _create_learning_tab(self, parent):
        """创建 AI学习 仪表盘 Tab内容"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)

        # ============ 🎯 顶部仪表盘：4 个指标卡片 + 13 策略排名 ============
        dash_frame = ttk.LabelFrame(parent, text="AI 学习反哺仪表盘（本次经验如何影响下次研判）", padding=8)
        dash_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        dash_frame.columnconfigure(0, weight=1)

        # 4 个指标卡片
        cards = ttk.Frame(dash_frame)
        cards.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 6))
        for i in range(4):
            cards.columnconfigure(i, weight=1)
        self._learn_card_vars = {
            'maturity': tk.StringVar(value="—"),
            'analyses': tk.StringVar(value="—"),
            'feedbacks': tk.StringVar(value="—"),
            'accuracy': tk.StringVar(value="—"),
        }
        card_defs = [
            ("成熟度", 'maturity', "#1565C0"),
            ("总分析数", 'analyses', "#2E7D32"),
            ("已反馈样本", 'feedbacks', "#EF6C00"),
            ("整体胜率", 'accuracy', "#C62828"),
        ]
        for i, (label, key, color) in enumerate(card_defs):
            card = ttk.Frame(cards, relief="solid", borderwidth=1, padding=8)
            card.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=4)
            ttk.Label(card, text=label, foreground="#666").pack()
            tk.Label(card, textvariable=self._learn_card_vars[key],
                     fg=color, font=("", 14, "bold")).pack(pady=(2, 0))

        # 多维能力条
        dim_frame = ttk.Frame(dash_frame)
        dim_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(4, 6))
        self._learn_dim_vars = {
            "level": tk.StringVar(value="L0"),
            "accuracy": tk.StringVar(value="—"),
            "knowledge": tk.StringVar(value="—"),
            "discipline": tk.StringVar(value="—"),
            "evolution": tk.StringVar(value="—"),
        }
        ttk.Label(dim_frame, text="能力 L:", font=("", 9, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(dim_frame, textvariable=self._learn_dim_vars["level"],
                 fg="#6A1B9A", font=("", 11, "bold")).pack(side=tk.LEFT, padx=(0, 12))
        for label, key in [
            ("预测准确", "accuracy"),
            ("知识质量", "knowledge"),
            ("交易纪律", "discipline"),
            ("进化活跃", "evolution"),
        ]:
            ttk.Label(dim_frame, text=f"{label}:", foreground="#666").pack(side=tk.LEFT, padx=(0, 2))
            tk.Label(dim_frame, textvariable=self._learn_dim_vars[key],
                     fg="#455A64", font=("", 10, "bold")).pack(side=tk.LEFT, padx=(0, 10))

        # 13 策略胜率排名表格
        rank_label = ttk.Frame(dash_frame)
        rank_label.grid(row=2, column=0, sticky=(tk.W, tk.E))
        ttk.Label(rank_label, text="📊 13 机构策略胜率排名 (胜率高者贡献大)", font=("", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(rank_label, text="🔄 刷新仪表盘", command=self._refresh_learning_dashboard).pack(side=tk.RIGHT, padx=3)
        ttk.Button(rank_label, text="🧬 完整进化时间线", command=self._jump_to_evolution_tab).pack(side=tk.RIGHT, padx=3)

        rank_cols = ("rank", "name", "total", "correct", "win_rate", "weight", "streak", "trend")
        self.strategy_rank_tree = ttk.Treeview(dash_frame, columns=rank_cols, show="headings", height=8)
        widths = {"rank": 40, "name": 220, "total": 60, "correct": 60,
                  "win_rate": 80, "weight": 80, "streak": 70, "trend": 100}
        headers = {"rank": "#", "name": "策略名", "total": "总预测",
                   "correct": "正确", "win_rate": "胜率", "weight": "权重",
                   "streak": "连胜最佳", "trend": "趋势"}
        for c in rank_cols:
            self.strategy_rank_tree.heading(c, text=headers[c])
            self.strategy_rank_tree.column(c, width=widths[c], anchor=tk.W)
        self.strategy_rank_tree.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=4)
        # 色彩标记
        self.strategy_rank_tree.tag_configure("good", foreground="#2E7D32")
        self.strategy_rank_tree.tag_configure("bad", foreground="#C62828")
        self.strategy_rank_tree.tag_configure("mid", foreground="#555")

        # ============ 📈 近期胜率趋势（文本柱状图） ============
        trend_frame = ttk.LabelFrame(parent, text="📈 近期 10 笔胜率趋势 (看 AI 越学越准还是越学越偏)", padding=8)
        trend_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        trend_frame.columnconfigure(0, weight=1)
        self.recent_trend_text = scrolledtext.ScrolledText(
            trend_frame, wrap=tk.WORD, height=6, font=("Consolas", 9))
        self.recent_trend_text.grid(row=0, column=0, sticky=(tk.W, tk.E))

        # ============ 📊 分桶权重 / 盈利曲线 / 重复亏损模式 ============
        analytics_frame = ttk.LabelFrame(
            parent, text="📊 学习成效分析（分桶权重 · 盈利曲线 · 亏损模式门控）", padding=8
        )
        analytics_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        analytics_frame.columnconfigure(0, weight=1)

        compare_row = ttk.Frame(analytics_frame)
        compare_row.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 6))
        for i in range(4):
            compare_row.columnconfigure(i, weight=1)
        self._profit_compare_vars = {
            "feedback_early": tk.StringVar(value="—"),
            "feedback_late": tk.StringVar(value="—"),
            "feedback_delta": tk.StringVar(value="—"),
            "paper_delta": tk.StringVar(value="—"),
        }
        compare_defs = [
            ("反馈前半胜率", "feedback_early", "#546E7A"),
            ("反馈后半胜率", "feedback_late", "#1565C0"),
            ("反馈学习Δ", "feedback_delta", "#2E7D32"),
            ("模拟盘学习Δ", "paper_delta", "#EF6C00"),
        ]
        for i, (label, key, color) in enumerate(compare_defs):
            box = ttk.Frame(compare_row, relief="solid", borderwidth=1, padding=6)
            box.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=3)
            ttk.Label(box, text=label, foreground="#666", font=("", 8)).pack()
            tk.Label(box, textvariable=self._profit_compare_vars[key],
                     fg=color, font=("", 11, "bold")).pack(pady=(2, 0))

        analytics_pane = ttk.PanedWindow(analytics_frame, orient=tk.HORIZONTAL)
        analytics_pane.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=4)

        bucket_frame = ttk.Frame(analytics_pane)
        analytics_pane.add(bucket_frame, weight=1)
        ttk.Label(bucket_frame, text="市场状态分桶策略权重", font=("", 9, "bold")).pack(anchor=tk.W)
        self._regime_bucket_text = scrolledtext.ScrolledText(
            bucket_frame, wrap=tk.WORD, height=8, font=("Consolas", 9), width=36
        )
        self._regime_bucket_text.pack(fill=tk.BOTH, expand=True)

        curve_frame = ttk.Frame(analytics_pane)
        analytics_pane.add(curve_frame, weight=1)
        ttk.Label(curve_frame, text="盈利曲线 / 学习前后胜率", font=("", 9, "bold")).pack(anchor=tk.W)
        self._profit_curve_text = scrolledtext.ScrolledText(
            curve_frame, wrap=tk.WORD, height=8, font=("Consolas", 9), width=36
        )
        self._profit_curve_text.pack(fill=tk.BOTH, expand=True)

        loss_frame = ttk.Frame(analytics_frame)
        loss_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(4, 0))
        loss_frame.columnconfigure(0, weight=1)
        loss_hdr = ttk.Frame(loss_frame)
        loss_hdr.grid(row=0, column=0, sticky=(tk.W, tk.E))
        ttk.Label(loss_hdr, text="⚠ 重复亏损模式（自动识别）", font=("", 9, "bold")).pack(side=tk.LEFT)
        self._gate_status_var = tk.StringVar(value="门控: 正常")
        ttk.Label(loss_hdr, textvariable=self._gate_status_var, foreground="#666").pack(side=tk.LEFT, padx=12)
        ttk.Button(loss_hdr, text="🔒 一键收紧门控", command=self._apply_loss_pattern_gate).pack(side=tk.RIGHT, padx=3)
        ttk.Button(loss_hdr, text="🔓 解除收紧", command=self._clear_loss_pattern_gate).pack(side=tk.RIGHT, padx=3)

        loss_cols = ("severity", "title", "detail", "losses")
        self._loss_pattern_tree = ttk.Treeview(
            loss_frame, columns=loss_cols, show="headings", height=4
        )
        for c, h, w in [
            ("severity", "严重度", 60),
            ("title", "模式", 220),
            ("detail", "详情", 280),
            ("losses", "亏损笔", 70),
        ]:
            self._loss_pattern_tree.heading(c, text=h)
            self._loss_pattern_tree.column(c, width=w, anchor=tk.W)
        self._loss_pattern_tree.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=4)
        self._loss_pattern_tree.tag_configure("high", foreground="#C62828")
        self._loss_pattern_tree.tag_configure("mid", foreground="#EF6C00")

        # ---- 胜率学习优化（实时快照）----
        wr_hdr = ttk.Frame(analytics_frame)
        wr_hdr.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=(8, 2))
        ttk.Label(wr_hdr, text="🎯 胜率学习优化（投票修正 · 方向拦截 · 门控）", font=("", 9, "bold")).pack(side=tk.LEFT)

        wr_cards = ttk.Frame(analytics_frame)
        wr_cards.grid(row=4, column=0, sticky=(tk.W, tk.E), pady=(0, 4))
        for i in range(4):
            wr_cards.columnconfigure(i, weight=1)
        self._win_rate_vars = {
            "vote_long": tk.StringVar(value="—"),
            "vote_short": tk.StringVar(value="—"),
            "gate_adj": tk.StringVar(value="—"),
            "block": tk.StringVar(value="—"),
        }
        wr_defs = [
            ("多头投票修正", "vote_long", "#1565C0"),
            ("空头投票修正", "vote_short", "#C62828"),
            ("门控收紧/放宽", "gate_adj", "#6A1B9A"),
            ("方向拦截", "block", "#EF6C00"),
        ]
        for i, (label, key, color) in enumerate(wr_defs):
            box = ttk.Frame(wr_cards, relief="solid", borderwidth=1, padding=6)
            box.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=3)
            ttk.Label(box, text=label, foreground="#666", font=("", 8)).pack()
            tk.Label(box, textvariable=self._win_rate_vars[key],
                     fg=color, font=("", 11, "bold")).pack(pady=(2, 0))

        self._win_rate_detail_text = scrolledtext.ScrolledText(
            analytics_frame, wrap=tk.WORD, height=5, font=("Consolas", 9)
        )
        self._win_rate_detail_text.grid(row=5, column=0, sticky=(tk.W, tk.E), pady=(0, 4))

        # ============ 🧬 近期进化快照 ============
        evo_frame = ttk.LabelFrame(parent, text="🧬 近期学习进化快照（复盘 → 知识 → 注入）", padding=8)
        evo_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), padx=5, pady=4)
        evo_frame.columnconfigure(0, weight=1)
        self._learn_timeline_text = scrolledtext.ScrolledText(
            evo_frame, wrap=tk.WORD, height=5, font=("Consolas", 9)
        )
        self._learn_timeline_text.grid(row=0, column=0, sticky=(tk.W, tk.E))
        ttk.Button(
            evo_frame, text="刷新快照", command=self._refresh_learning_timeline_preview
        ).grid(row=1, column=0, sticky=tk.E, pady=(4, 0))

        # ============ 下半部：待反馈 + 学习报告 ============
        bottom_pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        bottom_pane.grid(row=4, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=4)

        top_frame = ttk.LabelFrame(bottom_pane, text="待反馈记录（标记盈利/亏损帮助AI成长）", padding="6")
        bottom_pane.add(top_frame, weight=1)
        top_frame.columnconfigure(0, weight=1)
        top_frame.rowconfigure(1, weight=1)

        btn_frame = ttk.Frame(top_frame)
        btn_frame.grid(row=0, column=0, sticky=(tk.W, tk.E))
        ttk.Button(btn_frame, text="刷新待反馈", command=self.refresh_pending_feedback).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="标记盈利(选中)", command=lambda: self.submit_feedback_gui('WIN')).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="标记亏损(选中)", command=lambda: self.submit_feedback_gui('LOSS')).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="标记保本", command=lambda: self.submit_feedback_gui('BREAK_EVEN')).pack(side=tk.LEFT, padx=3)

        columns = ('ID', 'Time', 'Symbol', 'Signal', 'Price', 'PnL')
        self.pending_tree = ttk.Treeview(top_frame, columns=columns, show='headings', height=6)
        for col in columns:
            self.pending_tree.heading(col, text=col)
            self.pending_tree.column(col, width=100 if col != 'Time' else 140)
        self.pending_tree.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        bottom_frame = ttk.LabelFrame(bottom_pane, text="学习报告(AI成长状态)", padding="6")
        bottom_pane.add(bottom_frame, weight=1)
        bottom_frame.columnconfigure(0, weight=1)
        bottom_frame.rowconfigure(0, weight=1)
        self.learning_text = scrolledtext.ScrolledText(bottom_frame, wrap=tk.WORD, height=10, font=('Consolas', 9))
        self.learning_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        ttk.Button(bottom_frame, text="刷新学习报告", command=self.refresh_learning_report).grid(row=1, column=0, pady=4)

        # 初始加载
        self.refresh_pending_feedback()
        self.refresh_learning_report()
        self._refresh_learning_dashboard()

    def _create_knowledge_tab(self, parent):
        """📚 知识库 — 独立页面：AI 提炼 · 向量存储 · 语义检索"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        # ---- 顶部说明 ----
        intro = ttk.Label(
            parent,
            text=(
                "交易/复盘后 → AI 提炼结构化知识卡片 → 存入本地向量库 → "
                "下次「开始分析」时自动语义检索并注入 DeepSeek Prompt"
            ),
            foreground="#444", wraplength=1000, justify=tk.LEFT,
        )
        intro.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=8, pady=(8, 4))

        # ---- 4 个状态卡片 ----
        stat_frame = ttk.Frame(parent)
        stat_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=8, pady=4)
        for i in range(4):
            stat_frame.columnconfigure(i, weight=1)
        self._kb_stat_vars = {
            "backend": tk.StringVar(value="—"),
            "total": tk.StringVar(value="0"),
            "ai_extract": tk.StringVar(value="—"),
            "categories": tk.StringVar(value="—"),
        }
        stat_defs = [
            ("向量库", "backend", "#5E35B1"),
            ("知识卡片", "total", "#1565C0"),
            ("AI 提炼", "ai_extract", "#2E7D32"),
            ("分类统计", "categories", "#EF6C00"),
        ]
        for i, (label, key, color) in enumerate(stat_defs):
            box = ttk.Frame(stat_frame, relief="solid", borderwidth=1, padding=10)
            box.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=4)
            ttk.Label(box, text=label, foreground="#666").pack()
            tk.Label(box, textvariable=self._kb_stat_vars[key],
                     fg=color, font=("", 16, "bold")).pack(pady=(4, 0))

        # ---- 工具栏 ----
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=8, pady=(0, 4))
        self._knowledge_status_var = tk.StringVar(value="就绪")
        ttk.Label(toolbar, textvariable=self._knowledge_status_var).pack(side=tk.LEFT)

        ttk.Label(toolbar, text="  分类筛选:").pack(side=tk.LEFT, padx=(12, 2))
        self._kb_filter_var = tk.StringVar(value="全部")
        kb_filter = ttk.Combobox(
            toolbar, textvariable=self._kb_filter_var, width=14, state="readonly",
            values=["全部", "交易逻辑", "止损规则", "市场复盘", "错误教训"],
        )
        kb_filter.pack(side=tk.LEFT)
        kb_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_knowledge_cards())

        ttk.Button(toolbar, text="📥 历史交易提炼", command=self._run_knowledge_backfill).pack(side=tk.RIGHT, padx=3)
        ttk.Button(toolbar, text="🔍 语义试检索", command=self._test_knowledge_retrieval).pack(side=tk.RIGHT, padx=3)
        ttk.Button(toolbar, text="🔄 刷新", command=self._refresh_knowledge_cards).pack(side=tk.RIGHT, padx=3)
        ttk.Button(toolbar, text="🧬 进化时间线", command=self._jump_to_evolution_tab).pack(side=tk.RIGHT, padx=3)
        ttk.Button(toolbar, text="📖 使用说明", command=self._show_knowledge_help).pack(side=tk.RIGHT, padx=3)

        # ---- 主列表 + 详情（上下分割） ----
        main_pane = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        main_pane.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=8, pady=4)
        parent.rowconfigure(3, weight=1)

        list_frame = ttk.LabelFrame(main_pane, text="知识卡片列表", padding=6)
        main_pane.add(list_frame, weight=3)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        kb_cols = (
            "id", "category", "title", "trigger", "action",
            "lesson", "confidence", "source", "validated", "similarity",
        )
        self.knowledge_tree = ttk.Treeview(list_frame, columns=kb_cols, show="headings")
        kb_headers = {
            "id": "ID", "category": "分类", "title": "标题",
            "trigger": "适用条件", "action": "执行规则", "lesson": "核心教训",
            "confidence": "可信度", "source": "来源", "validated": "验证",
            "similarity": "相关度",
        }
        kb_widths = {
            "id": 45, "category": 90, "title": 160, "trigger": 180,
            "action": 180, "lesson": 260, "confidence": 65,
            "source": 60, "validated": 50, "similarity": 65,
        }
        for c in kb_cols:
            self.knowledge_tree.heading(c, text=kb_headers[c])
            self.knowledge_tree.column(c, width=kb_widths[c], anchor=tk.W)
        kb_vscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.knowledge_tree.yview)
        kb_hscroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self.knowledge_tree.xview)
        self.knowledge_tree.configure(yscrollcommand=kb_vscroll.set, xscrollcommand=kb_hscroll.set)
        self.knowledge_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        kb_vscroll.grid(row=0, column=1, sticky=(tk.N, tk.S))
        kb_hscroll.grid(row=1, column=0, sticky=(tk.W, tk.E))
        self.knowledge_tree.tag_configure("semantic", foreground="#1565C0")
        self.knowledge_tree.tag_configure("high_conf", foreground="#2E7D32")
        self.knowledge_tree.bind("<<TreeviewSelect>>", self._on_knowledge_card_select)

        detail_pane = ttk.PanedWindow(main_pane, orient=tk.HORIZONTAL)
        main_pane.add(detail_pane, weight=1)

        detail_left = ttk.LabelFrame(detail_pane, text="选中卡片详情", padding=6)
        detail_pane.add(detail_left, weight=2)
        detail_left.columnconfigure(0, weight=1)
        detail_left.rowconfigure(0, weight=1)
        self.knowledge_detail_text = scrolledtext.ScrolledText(
            detail_left, wrap=tk.WORD, height=8, font=("Consolas", 10),
        )
        self.knowledge_detail_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        detail_right = ttk.LabelFrame(detail_pane, text="如何产生知识卡片", padding=6)
        detail_pane.add(detail_right, weight=1)
        detail_right.columnconfigure(0, weight=1)
        detail_right.rowconfigure(0, weight=1)
        help_text = scrolledtext.ScrolledText(
            detail_right, wrap=tk.WORD, height=8, font=("", 9), foreground="#333",
        )
        help_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        help_text.insert(1.0, (
            "1️⃣ 模拟盘平仓\n"
            "   → 自动调用 AI 提炼（后台约 10 秒）\n"
            "   → 生成：交易逻辑 / 止损规则 / 错误教训\n\n"
            "2️⃣ AI 复盘（「🧠 AI复盘」Tab）\n"
            "   → 复盘结论自动提炼为「市场复盘」类卡片\n\n"
            "3️⃣ 下次分析\n"
            "   → 按当前 RSI/价格/市场状态语义检索\n"
            "   → 最相关的卡片注入 DeepSeek Prompt\n\n"
            "💡 若列表为空：先跑模拟盘并平仓，或触发 AI 复盘，\n"
            "   然后点「🔄 刷新」。向量库显示 tfidf 表示 ChromaDB 未安装，\n"
            "   可执行: pip install chromadb"
        ))
        help_text.config(state=tk.DISABLED)

        self._knowledge_cards_cache: List[dict] = []
        self._knowledge_semantic_mode = False
        self._refresh_knowledge_cards()

    def _show_knowledge_help(self):
        messagebox.showinfo(
            "知识库使用说明",
            "知识卡片来源：\n"
            "• 模拟盘平仓 → AI 自动提炼\n"
            "• AI 复盘 → 自动提炼\n"
            "• 历史交易 → 点「📥 历史交易提炼」批量生成\n\n"
            "使用方式：\n"
            "• 点「开始分析」→ 系统自动语义检索相关卡片注入 AI\n"
            "• 点「语义试检索」→ 用最近一次分析局面手动试检索\n\n"
            "数据位置：data/ai_learning.db + data/chroma_knowledge/",
        )

    def _run_knowledge_backfill(self):
        """从模拟盘历史交易批量提炼知识卡片。"""
        try:
            paper_db = str(getattr(self.paper_engine, "db_path", "") or "")
            if not paper_db:
                from bnb_quant_tool.data_localization import get_localized_db_path
                paper_db = str(get_localized_db_path("paper_trading"))
            mem = self.learner.capability_memory
            status = mem.get_backfill_status(paper_db)
            pending = status.get("pending_trades", 0)
            total = status.get("total_closed_trades", 0)

            popup = tk.Toplevel(self.root)
            popup.title("历史交易批量提炼")
            popup.geometry("420x320")
            popup.transient(self.root)
            popup.grab_set()

            ttk.Label(
                popup,
                text=f"模拟盘已平仓: {total} 笔\n待提炼: {pending} 笔（已提炼的会自动跳过）",
                justify=tk.LEFT,
            ).pack(padx=16, pady=(16, 8), anchor=tk.W)

            mode_var = tk.StringVar(value="ai_summary")
            ttk.Radiobutton(
                popup, text="推荐：AI 汇总 + 规则全量（500+ 笔约 1-2 分钟）",
                variable=mode_var, value="ai_summary",
            ).pack(anchor=tk.W, padx=20)
            ttk.Radiobutton(
                popup, text="快速：仅规则全量（秒级，不耗 API）",
                variable=mode_var, value="rules",
            ).pack(anchor=tk.W, padx=20)

            force_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                popup, text="强制重新提炼（忽略已处理记录，卡片会去重合并）",
                variable=force_var,
            ).pack(anchor=tk.W, padx=20, pady=8)

            prog_var = tk.StringVar(value="等待开始...")
            ttk.Label(popup, textvariable=prog_var, wraplength=380).pack(padx=16, pady=4)
            prog_bar = ttk.Progressbar(popup, mode="indeterminate", length=360)
            prog_bar.pack(padx=16, pady=8)

            def _start():
                btn_start.config(state=tk.DISABLED)
                prog_bar.start(12)
                mode = mode_var.get()
                skip = not force_var.get()

                def _progress(done, total, msg):
                    self.root.after(0, lambda: prog_var.set(msg))

                def _worker():
                    try:
                        res = mem.backfill_from_paper_trades(
                            paper_db_path=paper_db,
                            learner=self.learner,
                            mode=mode,
                            skip_processed=skip,
                            progress_callback=_progress,
                        )
                        def _done():
                            prog_bar.stop()
                            mem.reset_connection()
                            persisted = mem.verify_persisted_count()
                            db_name = Path(mem.db_path).name
                            if res.get("cards_saved", 0) > 0 and persisted == 0:
                                prog_var.set(
                                    f"⚠️ 提炼报告 {res.get('cards_saved', 0)} 张，"
                                    f"但数据库 {db_name} 中仍为 0 条，请检查写入权限或路径"
                                )
                                messagebox.showwarning(
                                    "知识库未落盘",
                                    f"提炼过程已完成，但 {db_name} 中未读到卡片。\n\n"
                                    f"数据路径:\n{mem.db_path}\n\n"
                                    "请确认程序对 data/ 目录有写权限，然后点「强制重新提炼」再试。",
                                )
                            else:
                                prog_var.set(
                                    f"完成！处理 {res.get('trades_processed', 0)} 笔，"
                                    f"生成/合并 {res.get('cards_saved', 0)} 张卡片"
                                    + (f"（含 AI {res.get('ai_cards_saved', 0)} 张）"
                                       if res.get('ai_cards_saved') else "")
                                    + f" · 库内共 {persisted} 条 · {db_name}"
                                )
                            btn_start.config(state=tk.NORMAL)
                            btn_close.config(state=tk.NORMAL)
                            self._refresh_knowledge_cards()
                            self.update_status(
                                f"📥 历史提炼完成: 库内 {persisted} 张知识卡片 ({db_name})"
                            )
                        self.root.after(0, _done)
                    except Exception as ex:
                        def _err():
                            prog_bar.stop()
                            prog_var.set(f"失败: {ex}")
                            btn_start.config(state=tk.NORMAL)
                            messagebox.showerror("提炼失败", str(ex))
                        self.root.after(0, _err)

                threading.Thread(target=_worker, daemon=True).start()

            btn_frame = ttk.Frame(popup)
            btn_frame.pack(pady=12)
            btn_start = ttk.Button(btn_frame, text="开始提炼", command=_start)
            btn_start.pack(side=tk.LEFT, padx=6)
            btn_close = ttk.Button(btn_frame, text="关闭", command=popup.destroy)
            btn_close.pack(side=tk.LEFT, padx=6)

        except Exception as e:
            messagebox.showerror("历史提炼", str(e))

    def _on_knowledge_card_select(self, _event=None):
        """选中卡片时显示详情。"""
        if not hasattr(self, "knowledge_detail_text"):
            return
        sel = self.knowledge_tree.selection()
        if not sel:
            return
        vals = self.knowledge_tree.item(sel[0], "values")
        if not vals:
            return
        card_id = vals[0]
        card = next((c for c in self._knowledge_cards_cache if str(c.get("id")) == str(card_id)), None)
        self.knowledge_detail_text.delete(1.0, tk.END)
        if not card:
            self.knowledge_detail_text.insert(1.0, f"卡片 ID={card_id}")
            return
        lines = [
            f"【{card.get('category_label') or card.get('category')}】 {card.get('title', '')}",
            f"可信度: {card.get('confidence', 0):.0%}  |  验证 {card.get('times_validated', 0)} 次  |  来源: {card.get('source')}",
        ]
        if card.get("similarity") is not None:
            lines.append(f"语义相关度: {card['similarity']:.0%}")
        lines += [
            "",
            "▶ 适用条件",
            card.get("trigger_condition") or "—",
            "",
            "▶ 执行规则",
            card.get("action_rule") or "—",
            "",
            "▶ 核心教训",
            card.get("lesson") or "—",
        ]
        if card.get("tags"):
            lines += ["", f"标签: {', '.join(card['tags'])}"]
        self.knowledge_detail_text.insert(1.0, "\n".join(lines))

    def _refresh_knowledge_cards(self, cards: list = None, retrieval_mode: str = ""):
        """刷新本地知识库列表。"""
        if not hasattr(self, "knowledge_tree"):
            return
        for item in self.knowledge_tree.get_children():
            self.knowledge_tree.delete(item)
        try:
            mem = self.learner.capability_memory
            mem.reset_connection()
            summary = mem.get_summary()
            paper_db = str(getattr(self.paper_engine, "db_path", "") or "")
            if paper_db:
                summary["backfill"] = mem.get_backfill_status(paper_db)

            if cards is None:
                self._knowledge_semantic_mode = False
                cards = mem.list_cards_for_ui(limit=200)
            else:
                self._knowledge_semantic_mode = retrieval_mode == "semantic"

            # 分类筛选
            filt = getattr(self, "_kb_filter_var", None)
            filt_val = filt.get() if filt else "全部"
            cat_map = {
                "交易逻辑": "trading_logic",
                "止损规则": "stop_loss_rule",
                "市场复盘": "market_review",
                "错误教训": "error_lesson",
            }
            if filt_val and filt_val != "全部":
                key = cat_map.get(filt_val, filt_val)
                cards = [
                    c for c in cards
                    if c.get("category") == key
                    or c.get("category_label") == filt_val
                ]

            self._knowledge_cards_cache = list(cards)

            backend = summary.get("vector_backend", "—")
            total = summary.get("total_active", 0)
            ai_on = "开启" if summary.get("ai_extract_enabled") else "关闭"
            by_cat = summary.get("by_category") or {}
            cat_labels = {
                "trading_logic": "交易逻辑",
                "stop_loss_rule": "止损",
                "market_review": "复盘",
                "error_lesson": "教训",
            }
            cat_str = " · ".join(
                f"{cat_labels.get(k, k)}{v}" for k, v in by_cat.items()
            ) or "暂无"

            if hasattr(self, "_kb_stat_vars"):
                self._kb_stat_vars["backend"].set(backend.upper())
                self._kb_stat_vars["total"].set(str(total))
                self._kb_stat_vars["ai_extract"].set(ai_on)
                self._kb_stat_vars["categories"].set(cat_str[:28])

            mode_hint = "语义检索结果" if retrieval_mode == "semantic" else "全部卡片"
            backfill = summary.get("backfill") or {}
            pending = backfill.get("pending_trades")
            db_hint = Path(summary.get("db_path") or mem.db_path).name
            if pending is not None and pending > 0:
                self._knowledge_status_var.set(
                    f"{mode_hint} · 显示 {len(cards)} 条 · 库内共 {total} 条 · {db_hint} · "
                    f"还有 {pending} 笔历史交易可提炼"
                )
            else:
                self._knowledge_status_var.set(
                    f"{mode_hint} · 显示 {len(cards)} 条"
                    + (f" · 库内共 {total} 条 · {db_hint}" if total else f" · 库内暂无卡片 · {db_hint}")
                )

            if not cards and hasattr(self, "knowledge_detail_text"):
                self.knowledge_detail_text.delete(1.0, tk.END)
                backfill = summary.get("backfill") or {}
                pending = int(backfill.get("pending_trades") or 0)
                db_path = summary.get("db_path") or mem.db_path
                hint = (
                    f"数据文件: {db_path}\n"
                    f"（重启后仍为空，说明未成功写入该文件；请确认 data/ 目录可写）\n\n"
                )
                if pending > 0:
                    hint += (
                        f"检测到 {pending} 笔历史平仓尚未提炼。\n"
                        "→ 点上方「📥 历史交易提炼」→「开始提炼」\n"
                        "→ 完成后状态栏应显示「库内共 N 条」\n\n"
                    )
                hint += (
                    "其他来源：\n"
                    "1. 新平仓会自动 AI 提炼（约 10 秒，勿立即关程序）\n"
                    "2. 「AI复盘」Tab 触发复盘\n"
                    "3. 点「🔄 刷新」查看"
                )
                self.knowledge_detail_text.insert(1.0, "暂无知识卡片。\n\n" + hint)

            for c in cards:
                sim = c.get("similarity")
                sim_s = f"{sim:.0%}" if sim is not None else "—"
                conf = c.get("confidence", 0)
                tag = "semantic" if sim is not None else ("high_conf" if conf >= 0.7 else "")
                self.knowledge_tree.insert(
                    "", "end",
                    values=(
                        c.get("id", ""),
                        c.get("category_label") or c.get("category", ""),
                        c.get("title", ""),
                        c.get("trigger_condition", ""),
                        c.get("action_rule", ""),
                        c.get("lesson", ""),
                        f"{conf:.0%}",
                        c.get("source", ""),
                        c.get("times_validated", 0),
                        sim_s,
                    ),
                    tags=(tag,) if tag else (),
                )
        except Exception as e:
            logger.warning(f"刷新知识库失败: {e}")
            self._knowledge_status_var.set(f"加载失败: {e}")

    def _test_knowledge_retrieval(self):
        """用当前/最近一次分析局面做语义试检索。"""
        try:
            ar = getattr(self, "analysis_result", None) or {}
            if not ar:
                messagebox.showinfo(
                    "语义试检索",
                    "请先运行一次「开始分析」，再用当前市场指标检索相关知识卡片。",
                )
                return
            market_ctx = {
                "symbol": ar.get("symbol", "BNBUSDT"),
                "current_price": ar.get("current_price"),
                "indicators": ar.get("indicators") or {},
                "regime": (ar.get("market_regime") or {}).get("regime"),
                "signal": ar.get("final_recommendation"),
            }
            cards = self.learner.capability_memory.retrieve_relevant(market_ctx, top_k=15)
            self._refresh_knowledge_cards(cards=cards, retrieval_mode="semantic")
            n = len(cards)
            top_sim = cards[0].get("similarity") if cards else None
            sim_msg = f"，最高相关度 {top_sim:.0%}" if top_sim else ""
            self.update_status(f"🔍 知识库语义检索: 匹配 {n} 条{sim_msg}")
        except Exception as e:
            messagebox.showerror("语义试检索失败", str(e))

    def refresh_pending_feedback(self):
        """刷新待反馈列表"""
        for item in self.pending_tree.get_children():
            self.pending_tree.delete(item)

        try:
            conn = self.learner._get_conn()
            cursor = conn.cursor()
            cursor.execute("""SELECT id, timestamp, symbol, final_signal, current_price
                FROM analysis_records WHERE actual_result IS NULL ORDER BY timestamp DESC LIMIT 50""")
            for row in cursor.fetchall():
                rid, ts, sym, sig, price = row
                ts_short = str(ts)[:19] if ts else ''
                price_str = f"${price:.2f}" if price else 'N/A'
                self.pending_tree.insert('', 'end', values=(rid, ts_short, sym, sig, price_str, 'Pending'))
        except Exception as e:
            print(f"Refresh pending error: {e}")

    def submit_feedback_gui(self, result: str):
        """在GUI中提交反馈（用户选择一行，点击按钮）"""
        selection = self.pending_tree.selection()
        if not selection:
            messagebox.showwarning("未选择记录", "请先从待反馈列表中选择一条记录。")
            return

        item = self.pending_tree.item(selection[0])
        record_id = item['values'][0]

        # 简单弹窗获取实际价格
        popup = tk.Toplevel(self.root)
        popup.title(f"提交 Feedback for Record #{record_id}")
        popup.geometry("350x150")
        popup.transient(self.root)
        popup.grab_set()

        ttk.Label(popup, text=f"记录 ID: {record_id}\n结果: {result}\n\n请输入当前价格（用于计算盈亏百分比）:").pack(pady=10)
        price_var = tk.StringVar()
        ttk.Entry(popup, textvariable=price_var, width=20).pack(pady=5)

        def do_submit():
            try:
                actual_price = float(price_var.get())
                notes = f"GUI feedback: {result}"
                from bnb_quant_tool.trade_close_learning import (
                    TradeCloseLearningDeps,
                    process_manual_feedback,
                )

                deps = TradeCloseLearningDeps(
                    learner=self.learner,
                    config=self.config,
                    get_position_row=lambda _pid: None,
                    counterfactual=getattr(self, "counterfactual", None),
                    pattern_memory=getattr(self, "pattern_memory", None),
                    on_status=self.update_status if hasattr(self, "update_status") else None,
                )
                fb_result = process_manual_feedback(
                    int(record_id), result, deps,
                    actual_price=actual_price, notes=notes,
                )
                ok = fb_result.feedback_ok
                if ok:
                    messagebox.showinfo("成功", f"反馈已记录，记录 #{record_id}\nAI is now smarter!")
                    popup.destroy()
                    self.refresh_pending_feedback()
                    self.refresh_learning_report()
                    if hasattr(self, "_refresh_learning_dashboard"):
                        self._refresh_learning_dashboard()
                    if hasattr(self, "_refresh_learning_evolution_views"):
                        self._refresh_learning_evolution_views()
                    # 更新主状态栏
                    stats = self.learner.get_statistics_summary()
                    self.status_var.set(f"Feedback recorded! Total analyses: {stats['total_analyses']}, Accuracy: {stats['accuracy']:.1%}")
                else:
                    messagebox.showerror("错误", "提交反馈失败。")
            except ValueError:
                messagebox.showerror("无效输入", "请输入有效的价格数值。")
            except Exception as e:
                messagebox.showerror("错误", f"提交反馈出错: {e}")

        ttk.Button(popup, text="提交", command=do_submit).pack(pady=10)

    def refresh_learning_report(self):
        """刷新学习报告"""
        self.learning_text.delete(1.0, tk.END)
        try:
            report = self.learner.get_learning_report()
            self.learning_text.insert(1.0, report)
        except Exception as e:
            self.learning_text.insert(1.0, f"错误 loading learning report: {e}")

    def _refresh_learning_dashboard(self):
        """刷新 AI 学习仪表盘：4 卡片 + 13 策略排名 + 近期趋势"""
        try:
            insights = self.learner.get_learning_insights()

            # ---- 1. 4 个指标卡片 ----
            maturity_map = {
                'BEGINNER': '初学 🌱', 'INTERMEDIATE': '中级 🌿',
                'ADVANCED': '高级 🌳', 'EXPERT': '专家 🏆'
            }
            self._learn_card_vars['maturity'].set(maturity_map.get(insights['learning_maturity'], '初学'))
            self._learn_card_vars['analyses'].set(str(insights['total_analyses']))
            self._learn_card_vars['feedbacks'].set(str(insights['total_feedbacks']))
            acc = insights['overall_accuracy']
            self._learn_card_vars['accuracy'].set(f"{acc * 100:.1f}%" if insights['total_feedbacks'] > 0 else "—")

            growth = insights.get("growth") or self.learner.get_growth_snapshot()
            dims = growth.get("capability_dimensions") or {}
            if hasattr(self, "_learn_dim_vars"):
                self._learn_dim_vars["level"].set(f"L{growth.get('capability_level', 0)}")
                self._learn_dim_vars["accuracy"].set(f"{dims.get('prediction_accuracy', 0)}")
                self._learn_dim_vars["knowledge"].set(f"{dims.get('knowledge_quality', 0)}")
                self._learn_dim_vars["discipline"].set(f"{dims.get('discipline', 0)}")
                self._learn_dim_vars["evolution"].set(f"{dims.get('evolution_activity', 0)}")

            # ---- 2. 13 策略胜率排名 ----
            for item in self.strategy_rank_tree.get_children():
                self.strategy_rank_tree.delete(item)

            try:
                conn = self.learner._get_conn()
                cur = conn.cursor()
                cur.execute("""SELECT strategy_name, total_predictions, correct_predictions,
                                       win_rate, weight, streak_best, streak_current
                                FROM strategy_performance
                                WHERE is_active = 1
                                ORDER BY win_rate DESC, total_predictions DESC""")
                rows = cur.fetchall()
            except Exception:
                rows = []

            if not rows:
                # 还没有任何反馈 → 显示默认 13 策略等权重
                weights = self.learner._load_strategy_weights()
                rows = [(name, 0, 0, 0.0, w, 0, 0) for name, w in weights.items()]

            for i, r in enumerate(rows, 1):
                name, total, correct, wr, weight, streak_best, streak_cur = r
                wr_pct = f"{wr * 100:.1f}%" if total > 0 else "—"
                wt_pct = f"{(weight or 0) * 100:.2f}%"
                # 趋势符号
                if total < 3:
                    trend = "样本不足"; tag = "mid"
                elif wr >= 0.6:
                    trend = "↑↑ 优秀"; tag = "good"
                elif wr >= 0.5:
                    trend = "→ 中等"; tag = "mid"
                else:
                    trend = "↓↓ 拖后"; tag = "bad"
                streak_disp = f"{streak_best}连" if streak_best else "—"
                self.strategy_rank_tree.insert(
                    "", "end",
                    values=(i, name, total, correct, wr_pct, wt_pct, streak_disp, trend),
                    tags=(tag,)
                )

            # ---- 3. 近期 10 笔趋势柱状图 ----
            self.recent_trend_text.delete(1.0, tk.END)
            recent = insights.get('recent_trend', [])
            if not recent:
                self.recent_trend_text.insert(1.0, "尚无反馈记录。模拟盘平仓后会自动回填，或在「待反馈」中手动标记。\n")
            else:
                lines = []
                # 按时间升序（老→新）
                recent_sorted = list(reversed(recent))
                seq = ""
                for t in recent_sorted:
                    seq += "█" if t['result'] == 'WIN' else ("░" if t['result'] == 'LOSS' else "·")
                wins = sum(1 for t in recent if t['result'] == 'WIN')
                total = len(recent)
                avg_pnl = sum((t.get('pnl') or 0) for t in recent) / total if total else 0
                lines.append(f"近 {total} 笔胜率: {wins}/{total} = {wins/total*100:.1f}%   平均盈亏: {avg_pnl:+.2f}%")
                lines.append(f"时间轴 (老→新): █=胜 ░=负 ·=保本")
                lines.append(f"  {seq}")
                lines.append("")
                lines.append("明细:")
                for t in recent_sorted[-5:]:
                    ts = (t.get('time') or '')[:19]
                    res = t.get('result', '?')
                    pnl = t.get('pnl')
                    pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
                    sig = t.get('signal', '?')
                    lines.append(f"  {ts}  {sig:5s}  → {res:10s}  {pnl_s}")
                self.recent_trend_text.insert(1.0, "\n".join(lines))

            # ---- 4. 分桶权重 / 盈利曲线 / 重复亏损模式 ----
            self._refresh_learning_analytics(insights)

            self._refresh_learning_timeline_preview()
        except Exception as e:
            logger.exception("refresh learning dashboard failed")
            try:
                self.recent_trend_text.delete(1.0, tk.END)
                self.recent_trend_text.insert(1.0, f"刷新仪表盘失败: {e}")
            except Exception:
                pass

    def _learning_analytics(self):
        from bnb_quant_tool.learning_analytics import LearningAnalytics
        return LearningAnalytics(
            self.learner,
            paper_engine=getattr(self, "paper_engine", None),
            pattern_memory=getattr(self, "pattern_memory", None),
        )

    def _refresh_learning_analytics(self, insights=None):
        """刷新分桶权重、盈利曲线对比、重复亏损模式列表。"""
        if not hasattr(self, "_regime_bucket_text"):
            return
        try:
            from bnb_quant_tool.learning_analytics import (
                format_regime_bucket_text,
                load_gate_state,
            )

            analytics = self._learning_analytics()
            bucket_rows = analytics.get_regime_bucket_weights()
            curve = analytics.get_profit_curve_comparison()
            patterns = analytics.detect_repeated_loss_patterns()

            self._regime_bucket_text.delete(1.0, tk.END)
            self._regime_bucket_text.insert(1.0, format_regime_bucket_text(bucket_rows))

            self._profit_curve_text.delete(1.0, tk.END)
            curve_text = curve.get("curve_text") or "样本不足，完成反馈或模拟盘平仓后可对比学习前后胜率。"
            self._profit_curve_text.insert(1.0, curve_text)

            def _pct(v):
                return f"{v * 100:.1f}%" if v is not None else "—"

            def _delta(v):
                if v is None:
                    return "—"
                sign = "+" if v >= 0 else ""
                return f"{sign}{v * 100:.1f}%"

            if hasattr(self, "_profit_compare_vars"):
                self._profit_compare_vars["feedback_early"].set(_pct(curve.get("feedback_early_wr")))
                self._profit_compare_vars["feedback_late"].set(_pct(curve.get("feedback_late_wr")))
                self._profit_compare_vars["feedback_delta"].set(_delta(curve.get("feedback_delta")))
                self._profit_compare_vars["paper_delta"].set(_delta(curve.get("paper_delta")))

            for item in self._loss_pattern_tree.get_children():
                self._loss_pattern_tree.delete(item)
            for p in patterns:
                sev = float(p.get("severity") or 0)
                tag = "high" if sev >= 0.7 else "mid"
                self._loss_pattern_tree.insert(
                    "", "end",
                    values=(
                        f"{sev:.0%}",
                        p.get("title", "?"),
                        p.get("detail", ""),
                        p.get("loss_count", 0),
                    ),
                    tags=(tag,),
                )
            if not patterns:
                self._loss_pattern_tree.insert(
                    "", "end",
                    values=("—", "暂无重复亏损模式", "继续积累反馈后会自动识别", "—"),
                )

            gate = load_gate_state(PROJECT_ROOT)
            if gate.get("gate_tightening_boost"):
                boost = float(gate["gate_tightening_boost"])
                n_pat = len(gate.get("patterns") or [])
                self._gate_status_var.set(
                    f"门控: 已收紧 +{boost:.0%} ({n_pat} 个模式, 剩余约 {gate.get('trades_remaining', '?')} 笔)"
                )
            else:
                self._gate_status_var.set("门控: 正常")

            self._refresh_win_rate_snapshot(insights)
        except Exception as e:
            logger.exception("refresh learning analytics failed")
            try:
                self._regime_bucket_text.delete(1.0, tk.END)
                self._regime_bucket_text.insert(1.0, f"分析数据加载失败: {e}")
            except Exception:
                pass

    def _refresh_win_rate_snapshot(self, insights=None):
        """刷新胜率学习优化面板。"""
        if not hasattr(self, "_win_rate_detail_text"):
            return
        try:
            from bnb_quant_tool.learning_analytics import build_learning_dashboard_snapshot

            snap = build_learning_dashboard_snapshot(
                self.learner,
                paper_engine=getattr(self, "paper_engine", None),
                pattern_memory=getattr(self, "pattern_memory", None),
                config=getattr(self, "config", None) or {},
            )
            wrc = snap.get("win_rate_context") or {}
            lb = float(snap.get("vote_adj_long") or 0)
            sb = float(snap.get("vote_adj_short") or 0)
            gt = float(snap.get("gate_tightening") or 0)
            gr = float(snap.get("gate_relaxation") or 0)

            if hasattr(self, "_win_rate_vars"):
                self._win_rate_vars["vote_long"].set(f"{lb:+.2f}")
                self._win_rate_vars["vote_short"].set(f"{sb:+.2f}")
                if gt > 0 or gr > 0:
                    self._win_rate_vars["gate_adj"].set(f"-{gt:.0%} / +{gr:.0%}")
                else:
                    self._win_rate_vars["gate_adj"].set("正常")
                blocks = []
                if snap.get("block_long"):
                    blocks.append("多")
                if snap.get("block_short"):
                    blocks.append("空")
                self._win_rate_vars["block"].set("拦截 " + "/".join(blocks) if blocks else "无")

            lines = list(snap.get("cockpit_lines") or [])
            for h in (wrc.get("hints") or []):
                if h not in lines:
                    lines.append(f"  • {h}")
            paper = snap.get("paper_trading") or {}
            if paper.get("closed_trades"):
                lines.append(
                    f"\n模拟盘: {paper['closed_trades']} 笔 | "
                    f"胜率 {paper.get('win_rate', 0):.1%} | "
                    f"连亏 {paper.get('consecutive_losses', 0)}"
                )
            if not lines:
                lines = ["胜率学习: 样本积累中，完成模拟盘/反馈后自动生效"]

            self._win_rate_detail_text.delete(1.0, tk.END)
            self._win_rate_detail_text.insert(1.0, "\n".join(lines))
        except Exception as e:
            logger.debug("win rate snapshot: %s", e)
            try:
                self._win_rate_detail_text.delete(1.0, tk.END)
                self._win_rate_detail_text.insert(1.0, f"胜率学习快照加载失败: {e}")
            except Exception:
                pass

    def _apply_loss_pattern_gate(self):
        """一键收紧门控：写入 session boost 并可选提高 confidence_threshold。"""
        try:
            analytics = self._learning_analytics()
            patterns = analytics.detect_repeated_loss_patterns()
            if not patterns:
                messagebox.showinfo("无模式", "尚未检测到重复亏损模式，暂无需收紧门控。")
                return
            if not messagebox.askyesno(
                "确认收紧门控",
                f"检测到 {len(patterns)} 个重复亏损模式。\n"
                f"将对后续分析提高置信度门槛（约 15 笔内生效）。\n\n"
                f"首要模式: {patterns[0].get('title', '?')}\n\n是否继续？",
            ):
                return
            result = analytics.apply_loss_pattern_gate_tightening(
                patterns=patterns,
                config_path=str(PROJECT_ROOT / "config.yaml"),
                project_root=PROJECT_ROOT,
            )
            if result.get("ok"):
                messagebox.showinfo("门控已收紧", result.get("message", "已收紧"))
                self._refresh_learning_analytics()
            else:
                messagebox.showwarning("未应用", result.get("reason", "未知原因"))
        except Exception as e:
            messagebox.showerror("错误", f"收紧门控失败: {e}")

    def _clear_loss_pattern_gate(self):
        """解除 session 门控收紧。"""
        try:
            from bnb_quant_tool.learning_analytics import load_gate_state

            if not load_gate_state(PROJECT_ROOT):
                messagebox.showinfo("提示", "当前未启用门控收紧。")
                return
            if not messagebox.askyesno("确认", "解除本次 session 的门控收紧？"):
                return
            self._learning_analytics().clear_gate_tightening(project_root=PROJECT_ROOT)
            messagebox.showinfo("已解除", "门控已恢复为正常水平。")
            self._refresh_learning_analytics()
        except Exception as e:
            messagebox.showerror("错误", f"解除门控失败: {e}")

    def _refresh_learning_timeline_preview(self):
        """在 AI 学习 Tab 展示近期进化事件快照。"""
        if not hasattr(self, "_learn_timeline_text"):
            return
        try:
            from bnb_quant_tool.learning_timeline import (
                LearningTimelineCollector,
                format_timeline_text,
            )

            collector = (
                self._learning_timeline_collector()
                if hasattr(self, "_learning_timeline_collector")
                else LearningTimelineCollector(self.learner.db_path)
            )
            events = collector.collect(limit=8)
            text = format_timeline_text(events, limit=8)
            self._learn_timeline_text.delete(1.0, tk.END)
            self._learn_timeline_text.insert(1.0, text)
        except Exception as e:
            self._learn_timeline_text.delete(1.0, tk.END)
            self._learn_timeline_text.insert(1.0, f"进化快照加载失败: {e}")

    def submit_feedback_last(self):
        """快速反馈：对最后一次分析提交结果"""
        if not self.last_record_id:
            messagebox.showwarning("未运行分析", "本次会话中尚未运行任何分析。")
            return
        self.submit_feedback_by_id(self.last_record_id)

    def submit_feedback_by_id(self, record_id: int):
        """通过ID提交反馈的通用方法"""
        popup = tk.Toplevel(self.root)
        popup.title(f"Feedback for Record #{record_id}")
        popup.geometry("300x200")
        popup.transient(self.root)

        result_var = tk.StringVar(value='WIN')
        ttk.Label(popup, text=f"记录 ID: {record_id}").pack(pady=5)
        ttk.Radiobutton(popup, text="盈利（预测正确）", variable=result_var, value='WIN').pack(anchor=tk.W, padx=20)
        ttk.Radiobutton(popup, text="亏损（预测错误）", variable=result_var, value='LOSS').pack(anchor=tk.W, padx=20)
        ttk.Radiobutton(popup, text="保本", variable=result_var, value='BREAK_EVEN').pack(anchor=tk.W, padx=20)

        ttk.Label(popup, text="当前实际价格：").pack(pady=(10, 0))
        price_var = tk.StringVar()
        ttk.Entry(popup, textvariable=price_var).pack(pady=5)

        def do_submit():
            try:
                actual_price = float(price_var.get()) if price_var.get() else None
                self.learner.submit_feedback(record_id, result_var.get(), actual_price, "Quick feedback from GUI")
                messagebox.showinfo("成功", "Feedback submitted! AI updated.")
                popup.destroy()
                self.refresh_learning_report()
                if hasattr(self, "_refresh_learning_dashboard"):
                    self._refresh_learning_dashboard()
                if hasattr(self, "_refresh_learning_evolution_views"):
                    self._refresh_learning_evolution_views()
            except Exception as e:
                messagebox.showerror("错误", str(e))

        ttk.Button(popup, text="提交", command=do_submit).pack(pady=10)

