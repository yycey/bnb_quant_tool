"""Mixin: EvolutionTimelineMixin — 学习进化时间线（复盘→知识→注入→参数）。"""

from __future__ import annotations

import json

from gui._imports import *


class EvolutionTimelineMixin:
    """🧬 进化时间线：展示学习闭环事件，并提供跳转复盘/知识库。"""

    def _learning_timeline_collector(self):
        from bnb_quant_tool.learning_timeline import LearningTimelineCollector

        paper_path = getattr(getattr(self, "paper_engine", None), "db_path", None)
        return LearningTimelineCollector(self.learner.db_path, paper_db_path=paper_path)

    def _create_evolution_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=6, pady=6)
        ttk.Label(
            bar,
            text="智能闭环：感知 → 决策 → 执行 → 反思 → 记忆（带着经验交易，不是从零开始）",
            foreground="#555",
        ).pack(side=tk.LEFT)
        ttk.Button(bar, text="刷新", command=self._refresh_evolution_timeline).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="打开复盘", command=self._jump_to_review_tab).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="打开知识库", command=self._jump_to_knowledge_tab).pack(side=tk.RIGHT, padx=2)

        pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        pane.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=6, pady=(0, 6))

        left = ttk.Frame(pane)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        pane.add(left, weight=3)

        cols = ("time", "stage", "title", "impact")
        self.evolution_tree = ttk.Treeview(
            left, columns=cols, show="headings", height=18, selectmode="browse"
        )
        self.evolution_tree.heading("time", text="时间")
        self.evolution_tree.heading("stage", text="阶段")
        self.evolution_tree.heading("title", text="事件")
        self.evolution_tree.heading("impact", text="影响")
        self.evolution_tree.column("time", width=140, stretch=False)
        self.evolution_tree.column("stage", width=90, stretch=False)
        self.evolution_tree.column("title", width=320, stretch=True)
        self.evolution_tree.column("impact", width=160, stretch=True)
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.evolution_tree.yview)
        self.evolution_tree.configure(yscrollcommand=scroll.set)
        self.evolution_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scroll.grid(row=0, column=1, sticky=(tk.N, tk.S))

        for stage, color in (
            ("analyze", "#E3F2FD"),
            ("trade", "#E8F5E9"),
            ("feedback", "#FFF3E0"),
            ("review", "#F3E5F5"),
            ("knowledge", "#E0F7FA"),
            ("inject", "#FFF8E1"),
            ("evolve", "#FCE4EC"),
            ("guard", "#FFEBEE"),
        ):
            try:
                self.evolution_tree.tag_configure(stage, background=color)
            except Exception:
                pass

        self.evolution_tree.bind("<<TreeviewSelect>>", self._on_evolution_select)

        right = ttk.LabelFrame(pane, text="事件详情 / 快捷操作", padding=6)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        pane.add(right, weight=2)

        self.evolution_detail_text = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, height=16, font=("Consolas", 9)
        )
        self.evolution_detail_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.evolution_detail_text.insert(
            "1.0",
            "选择左侧事件查看详情。\n"
            "智能闭环：感知 → 决策 → 执行 → 反思 → 记忆。\n"
            "完成分析 / 模拟盘平仓 / AI 复盘后，时间线会自动填充。\n",
        )

        self._loop_health_var = tk.StringVar(value="闭环健康: —")
        ttk.Label(parent, textvariable=self._loop_health_var, foreground="#333").grid(
            row=2, column=0, sticky=tk.W, padx=8, pady=(0, 6)
        )

        self._evolution_event_cache = {}
        try:
            self.root.after(800, self._refresh_evolution_timeline)
        except Exception:
            pass

    def _refresh_evolution_timeline(self):
        if not hasattr(self, "evolution_tree"):
            return
        try:
            from bnb_quant_tool.intelligence_loop import get_or_create_loop
            loop = get_or_create_loop(
                self,
                learner=getattr(self, "learner", None),
                config=getattr(self, "config", {}) or {},
                paper_engine=getattr(self, "paper_engine", None),
            )
            health = loop.get_loop_health()
            if hasattr(self, "_loop_health_var"):
                self._loop_health_var.set(
                    f"闭环健康: {health.get('completeness_score', 0)}/100 "
                    f"({health.get('completeness_label', '?')}) | "
                    f"反馈{health.get('total_feedbacks', 0)} "
                    f"知识卡{health.get('knowledge_cards', 0)} "
                    f"模拟盘{health.get('paper_closed', 0)} "
                    f"议会样本{health.get('council_outcome_samples', 0)} | "
                    f"成熟度{health.get('learning_maturity', '?')}"
                )
        except Exception:
            if hasattr(self, "_loop_health_var"):
                self._loop_health_var.set("闭环健康: 暂不可用")

        try:
            collector = self._learning_timeline_collector()
            events = collector.collect(limit=80)
        except Exception as e:
            self.evolution_detail_text.delete("1.0", tk.END)
            self.evolution_detail_text.insert("1.0", f"加载进化时间线失败: {e}")
            return

        self._evolution_event_cache = {}
        for item in self.evolution_tree.get_children():
            self.evolution_tree.delete(item)

        if not events:
            self.evolution_detail_text.delete("1.0", tk.END)
            self.evolution_detail_text.insert(
                "1.0",
                "暂无进化事件。\n\n"
                "闭环路径：感知行情 → 决策(注入记忆) → 执行模拟盘 → 平仓反思 → 写入知识/议会记忆。\n"
                "先运行「开始分析」、模拟盘平仓或 AI 复盘，时间线会自动填充。\n",
            )
            return

        for idx, ev in enumerate(events):
            iid = f"evo_{idx}"
            self._evolution_event_cache[iid] = ev
            stage = ev.stage if ev.stage in (
                "perceive", "decide", "execute", "reflect", "memory",
                "analyze", "trade", "feedback", "review",
                "knowledge", "inject", "evolve", "guard",
            ) else "analyze"
            self.evolution_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    (ev.timestamp or "")[:19],
                    ev.stage_label,
                    (ev.title or "")[:120],
                    (ev.impact or "")[:80],
                ),
                tags=(stage,),
            )

        # 默认选中第一条
        children = self.evolution_tree.get_children()
        if children:
            self.evolution_tree.selection_set(children[0])
            self.evolution_tree.focus(children[0])
            self._on_evolution_select()

    def _on_evolution_select(self, event=None):
        if not hasattr(self, "evolution_tree"):
            return
        sel = self.evolution_tree.selection()
        if not sel:
            return
        ev = (self._evolution_event_cache or {}).get(sel[0])
        if not ev:
            return

        lines = [
            f"时间: {ev.timestamp}",
            f"阶段: {ev.stage_label} ({ev.stage})",
            f"标题: {ev.title}",
            "",
            f"详情: {ev.detail or '—'}",
            f"影响: {ev.impact or '—'}",
            f"后续: {ev.next_effect or '—'}",
            f"来源: {ev.source or '—'}",
            f"引用: {ev.ref_id or '—'}",
            "",
            "── 快捷操作 ──",
        ]
        stage = ev.stage or ""
        if stage in ("review", "evolve"):
            lines.append("· 可前往「复盘」查看 AI 复盘与参数建议")
        if stage in ("knowledge", "inject"):
            lines.append("· 可前往「知识库」查看沉淀卡片与注入状态")
        if stage in ("trade", "feedback"):
            lines.append("· 可前往「模拟盘 / 学习」查看成交与反馈")
        if stage == "analyze":
            lines.append("· 对应一次 AI 分析沉淀，下次研判会复用知识与权重")

        payload = ev.payload or {}
        if payload:
            lines.append("")
            lines.append("── Payload ──")
            try:
                lines.append(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            except Exception:
                lines.append(str(payload))

        self.evolution_detail_text.delete("1.0", tk.END)
        self.evolution_detail_text.insert("1.0", "\n".join(lines))

    def _refresh_learning_evolution_views(self):
        """分析/平仓/复盘/反馈后统一刷新进化相关 UI。"""
        try:
            if hasattr(self, "_refresh_learning_timeline_preview"):
                self._refresh_learning_timeline_preview()
        except Exception:
            pass
        try:
            if hasattr(self, "_refresh_review_timeline_preview"):
                self._refresh_review_timeline_preview()
        except Exception:
            pass
        try:
            if getattr(self, "_active_tool_key", None) == "evolution":
                self._refresh_evolution_timeline()
            elif hasattr(self, "evolution_tree"):
                # 后台轻量刷新，下次打开即最新
                self._refresh_evolution_timeline()
        except Exception:
            pass

    def _jump_to_evolution_tab(self):
        if hasattr(self, "_open_tool_window"):
            self._open_tool_window("evolution")
            return
        try:
            for i in range(self.notebook.index("end")):
                if "进化" in str(self.notebook.tab(i, "text")):
                    self.notebook.select(i)
                    self._refresh_evolution_timeline()
                    return
        except Exception:
            pass

    def _jump_to_review_tab(self):
        if hasattr(self, "_open_tool_window"):
            self._open_tool_window("review")
            return
        try:
            for i in range(self.notebook.index("end")):
                if "复盘" in str(self.notebook.tab(i, "text")):
                    self.notebook.select(i)
                    return
        except Exception:
            pass

    def _jump_to_knowledge_tab(self):
        if hasattr(self, "_open_tool_window"):
            self._open_tool_window("knowledge")
            return
        try:
            for i in range(self.notebook.index("end")):
                if "知识" in str(self.notebook.tab(i, "text")):
                    self.notebook.select(i)
                    return
        except Exception:
            pass
