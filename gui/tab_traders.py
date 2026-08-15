"""Mixin: TradersTabMixin — 6 人交易员机器人卡片墙 + 独立 Key 配置。"""

from gui._imports import *


class TradersTabMixin:
    def _create_traders_tab(self, parent):
        """交易员议会：6 个机器人独立配置与投票展示。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        # ── 顶栏 ──
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(
            bar,
            text="6 位交易员独立 LLM 研判 → 讨论投票 → 风控 → 执行。每人可配独立 API Key（留空=共用启用中的主分析家）。",
            foreground="#0a6",
            wraplength=900,
        ).pack(side=tk.LEFT, padx=5)

        btns = ttk.Frame(bar)
        btns.pack(side=tk.RIGHT)
        ttk.Button(btns, text="刷新状态", command=self._refresh_traders_tab).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="保存配置", command=self._save_trader_council_config).pack(side=tk.LEFT, padx=3)

        tc = (self.config.get("trader_council") or {})
        self._council_enabled_var = tk.BooleanVar(value=bool(tc.get("enabled", True)))
        ttk.Checkbutton(
            btns, text="启用议会", variable=self._council_enabled_var,
            command=self._on_council_enabled_toggle,
        ).pack(side=tk.LEFT, padx=8)

        self._council_status_var = tk.StringVar(value="议会: —")
        ttk.Label(btns, textvariable=self._council_status_var, foreground="#1565C0").pack(side=tk.LEFT, padx=6)

        # ── 主分析 LLM 开关（与主界面顶栏共用 BooleanVar）──
        if not hasattr(self, "_llm_enable_vars"):
            self._init_llm_provider_vars()
        llm_box = ttk.LabelFrame(
            parent,
            text="主分析 LLM（关掉的不参与综合；议会默认跟启用家）",
            padding=6,
        )
        llm_box.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=(0, 4))
        for key, title, tip in (
            ("volcengine", "豆包（火山）", "当前主用"),
            ("deepseek", "DeepSeek", "关=不调用"),
            ("qianwen", "千问", "关=不调用"),
        ):
            ttk.Checkbutton(
                llm_box,
                text=f"{title}  · {tip}",
                variable=self._llm_enable_vars[key],
                command=self._on_llm_provider_toggle,
            ).pack(side=tk.LEFT, padx=10, pady=2)
        ttk.Label(
            llm_box,
            text="改完立即写入 config.yaml 并热更新",
            foreground="#666",
        ).pack(side=tk.LEFT, padx=8)

        # ── 6 张机器人卡片 ──
        cards = ttk.LabelFrame(parent, text="🤖 交易员机器人", padding=6)
        cards.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=(0, 4))
        for i in range(3):
            cards.columnconfigure(i, weight=1)

        self._trader_card_vars: dict = {}
        personas = [
            ("momentum", "🚀 趋势猎手", "趋势/动量", "#E65100"),
            ("mean_reversion", "🔄 均值回归", "超买超卖", "#6A1B9A"),
            ("macro", "🌍 宏观情绪", "新闻/宏观", "#1565C0"),
            ("structure", "📐 结构派", "多周期结构", "#2E7D32"),
            ("flow", "🐋 资金流", "链上/机构", "#00838F"),
            ("contrarian", "🎭 反共识", "拥挤反向", "#C62828"),
        ]
        traders_cfg = {str(t.get("id")): t for t in (tc.get("traders") or []) if isinstance(t, dict)}

        for idx, (tid, title, style, color) in enumerate(personas):
            row, col = divmod(idx, 3)
            box = ttk.Frame(cards, relief="solid", borderwidth=1, padding=8)
            box.grid(row=row, column=col, sticky=(tk.W, tk.E, tk.N), padx=4, pady=4)

            cfg_row = traders_cfg.get(tid) or {}
            vars_map = {
                "title": title,
                "color": color,
                "enabled": tk.BooleanVar(value=bool(cfg_row.get("enabled", True))),
                "use_llm": tk.BooleanVar(value=bool(cfg_row.get("use_llm", True))),
                "api_key": tk.StringVar(value=str(cfg_row.get("api_key") or "")),
                "model": tk.StringVar(value=str(cfg_row.get("model") or "")),
                "base_url": tk.StringVar(value=str(cfg_row.get("base_url") or "")),
                "temperature": tk.StringVar(value=str(cfg_row.get("temperature", 0.4))),
                "status": tk.StringVar(value="—"),
                "vote": tk.StringVar(value="票: —"),
                "acc": tk.StringVar(value="准确率: —"),
            }
            self._trader_card_vars[tid] = vars_map

            tk.Label(box, text=title, fg=color, font=("", 11, "bold")).pack(anchor=tk.W)
            ttk.Label(box, text=style, foreground="#666").pack(anchor=tk.W)

            sw = ttk.Frame(box)
            sw.pack(anchor=tk.W, pady=(4, 0))
            ttk.Checkbutton(sw, text="启用", variable=vars_map["enabled"]).pack(side=tk.LEFT)
            ttk.Checkbutton(sw, text="LLM", variable=vars_map["use_llm"]).pack(side=tk.LEFT, padx=6)

            ttk.Label(box, text="API Key:").pack(anchor=tk.W, pady=(4, 0))
            ttk.Entry(box, textvariable=vars_map["api_key"], show="*", width=28).pack(fill=tk.X)

            mm = ttk.Frame(box)
            mm.pack(fill=tk.X, pady=(2, 0))
            ttk.Label(mm, text="模型").pack(side=tk.LEFT)
            ttk.Entry(mm, textvariable=vars_map["model"], width=16).pack(side=tk.LEFT, padx=4)
            ttk.Label(mm, text="T").pack(side=tk.LEFT)
            ttk.Entry(mm, textvariable=vars_map["temperature"], width=5).pack(side=tk.LEFT, padx=2)

            ttk.Label(box, textvariable=vars_map["status"], foreground="#555").pack(anchor=tk.W, pady=(4, 0))
            tk.Label(box, textvariable=vars_map["vote"], fg=color, font=("", 9, "bold")).pack(anchor=tk.W)
            ttk.Label(box, textvariable=vars_map["acc"], foreground="#444").pack(anchor=tk.W)

        # ── 议会记录 ──
        log_frame = ttk.LabelFrame(parent, text="💬 最近一次议会讨论", padding=4)
        log_frame.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=4)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self._council_transcript = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, height=14, font=("Consolas", 9),
        )
        self._council_transcript.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self._council_transcript.insert(tk.END, "运行「开始 AI 分析」后，此处显示 6 人投票与讨论记录。\n")

        try:
            self.root.after(800, self._refresh_traders_tab)
        except Exception:
            pass

    def _on_council_enabled_toggle(self):
        tc = self.config.setdefault("trader_council", {})
        tc["enabled"] = bool(self._council_enabled_var.get())
        self._reload_council_runtime()
        self._refresh_traders_tab()

    def _collect_trader_council_from_ui(self) -> dict:
        """从卡片表单收集配置。"""
        existing = dict(self.config.get("trader_council") or {})
        traders = []
        order = existing.get("order") or list(self._trader_card_vars.keys())
        for tid in order:
            vars_map = self._trader_card_vars.get(tid)
            if not vars_map:
                continue
            try:
                temp = float(vars_map["temperature"].get() or 0.4)
            except ValueError:
                temp = 0.4
            traders.append({
                "id": tid,
                "name": str(vars_map["title"]).split(" ", 1)[-1] if " " in str(vars_map["title"]) else tid,
                "enabled": bool(vars_map["enabled"].get()),
                "use_llm": bool(vars_map["use_llm"].get()),
                "api_key": str(vars_map["api_key"].get() or "").strip(),
                "base_url": str(vars_map["base_url"].get() or "").strip(),
                "model": str(vars_map["model"].get() or "").strip(),
                "temperature": temp,
            })
        existing["enabled"] = bool(self._council_enabled_var.get())
        existing["traders"] = traders
        existing.setdefault("parallel", True)
        existing.setdefault("max_workers", 6)
        existing.setdefault("min_consensus", 0.45)
        existing.setdefault("wait_if_split", True)
        existing.setdefault("chair_llm_summary", True)
        existing.setdefault("memory_db", "data/trader_memory.db")
        existing.setdefault("order", order)
        return existing

    def _save_trader_council_config(self):
        try:
            import yaml
            tc = self._collect_trader_council_from_ui()
            self.config["trader_council"] = tc
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self._reload_council_runtime()
            self._refresh_traders_tab()
            self.update_status("交易员议会配置已保存")
            messagebox.showinfo("已保存", "6 位交易员配置已写入 config.yaml，并热加载到运行时。")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _reload_council_runtime(self):
        ma = getattr(self, "multi_agent", None)
        if not ma:
            return
        try:
            if getattr(ma, "council", None):
                ma.council.reload_from_config(self.config)
            else:
                tc = self.config.get("trader_council") or {}
                if tc.get("enabled", True):
                    from bnb_quant_tool.agents import TraderCouncil
                    ma.council = TraderCouncil(
                        config=self.config,
                        ai_analyzer=getattr(ma, "ai_analyzer", None),
                        project_root=str(PROJECT_ROOT),
                    )
        except Exception as e:
            logger.warning("reload council: %s", e)

    def _refresh_traders_tab(self):
        """刷新卡片状态、准确率、最近投票。"""
        dash = None
        ma = getattr(self, "multi_agent", None)
        if ma and getattr(ma, "council", None):
            try:
                dash = ma.council.dashboard()
            except Exception as e:
                logger.debug("council dashboard: %s", e)

        by_id = {}
        if dash:
            for card in dash.get("traders") or []:
                by_id[card["id"]] = card
            en = "ON" if dash.get("enabled") else "OFF"
            self._council_status_var.set(
                f"议会: {en} | {dash.get('count', 0)} 人"
                + (" · 双模" if dash.get("dual_mode") else "")
            )
        else:
            en = "ON" if self._council_enabled_var.get() else "OFF"
            self._council_status_var.set(f"议会: {en} | 未初始化")

        last_votes = {}
        votes_by_base = {}
        result = getattr(self, "analysis_result", None) or {}
        advice = result.get("trade_advice") or {}
        ma_data = advice.get("multi_agent_deliberation") or {}
        council = ma_data.get("council") or {}
        try:
            from bnb_quant_tool.llm_provider import PROVIDER_SHORT, persona_base_id
        except Exception:
            PROVIDER_SHORT = {"deepseek": "DS", "qianwen": "千问", "volcengine": "火山"}
            persona_base_id = lambda x: str(x or "").split("__", 1)[0]

        for v in (council.get("votes") or ma_data.get("agent_votes") or []):
            if isinstance(v, dict) and v.get("trader_id"):
                last_votes[v["trader_id"]] = v
                base = persona_base_id(v.get("persona_id") or v.get("trader_id"))
                votes_by_base.setdefault(base, []).append(v)

        for tid, vars_map in (getattr(self, "_trader_card_vars", {}) or {}).items():
            card = by_id.get(tid) or {}
            key_ok = card.get("has_llm_key") or card.get("api_key_set")
            inherit = not str(vars_map["api_key"].get() or "").strip()
            if key_ok or inherit:
                src = "独立Key" if (key_ok and not inherit) else ("共用Key" if inherit else "无Key→规则")
            else:
                src = "无Key→规则先验"
            model = card.get("model") or vars_map["model"].get() or "继承"
            vars_map["status"].set(f"{src} | {model}")

            samples = int(card.get("samples") or 0)
            acc = float(card.get("accuracy") or 0.5)
            w = float(card.get("weight") or 1.0)
            if samples:
                vars_map["acc"].set(f"准确率 {acc:.0%} (n={samples}) 权重×{w:.2f}")
            else:
                vars_map["acc"].set("准确率: 暂无样本")

            votes = votes_by_base.get(tid) or (
                [last_votes[tid]] if tid in last_votes else []
            )
            if not votes:
                vars_map["vote"].set("票: —")
            elif len(votes) == 1:
                vote = votes[0]
                vars_map["vote"].set(
                    f"票: {vote.get('action', '?')} "
                    f"({float(vote.get('confidence') or 0):.0%}) "
                    f"[{vote.get('source', '?')}]"
                )
            else:
                # 双模：同角色两队票并排显示
                parts = []
                for vote in votes:
                    prov = vote.get("provider") or ""
                    if not prov and "__" in str(vote.get("trader_id") or ""):
                        prov = str(vote.get("trader_id")).split("__", 1)[1]
                    short = PROVIDER_SHORT.get(prov, (prov or "?")[:2].upper())
                    parts.append(
                        f"{short}:{vote.get('action', '?')}"
                        f"({float(vote.get('confidence') or 0):.0%})"
                    )
                vars_map["vote"].set("票: " + " | ".join(parts))

        transcript = council.get("transcript") or ma_data.get("transcript") or ""
        if transcript and getattr(self, "_council_transcript", None):
            self._council_transcript.delete("1.0", tk.END)
            self._council_transcript.insert(tk.END, transcript)

    def _update_traders_from_analysis(self, result: Optional[dict] = None):
        """分析完成后刷新议会展示。"""
        if result is not None:
            self.analysis_result = result
        try:
            if hasattr(self, "_trader_card_vars"):
                self._refresh_traders_tab()
        except Exception:
            pass
