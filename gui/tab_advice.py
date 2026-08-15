"""Mixin: AdviceTabMixin"""

from gui._imports import *


class AdviceTabMixin:
    def _reset_circuit_breaker(self):
        """手动解除熔断冷却（连亏/回撤条件仍会在下次检查时触发）。"""
        from tkinter import messagebox
        cb = getattr(self, "circuit_breaker", None)
        if not cb:
            self.update_status("未配置熔断器")
            return
        if not messagebox.askyesno(
            "确认",
            "手动解除熔断冷却？\n连亏/回撤条件仍会在下次分析时重新触发。",
        ):
            return
        try:
            cb.reset_cooldown()
            cb.account_balance = float(self.balance_var.get())
            self.update_status(f"熔断已重置: {cb.format_status()}")
            if getattr(self, "analysis_result", None):
                self._refresh_decision_cockpit(self.analysis_result)
        except Exception as e:
            self.update_status(f"熔断重置失败: {e}")

    def _create_advice_tab(self, parent):
        """创建“开单建议”标签页：展示价格参数与一键复制"""
        # 顶部提示 + 按钮区
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(
            bar,
            text="分析方向 → 风控门控 → 跟单执行 三层分离展示。门控拦截不等于不会跟单（见「跟单状态」）。",
            foreground="#0a6"
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(bar, text="复制完整报告", command=self._copy_advice_full).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bar, text="复制下单参数", command=self._copy_advice_short).pack(side=tk.RIGHT, padx=5)

        # ── 决策驾驶舱（机构信念 + 门控 + 熔断）──
        cockpit = ttk.LabelFrame(parent, text="🎯 决策驾驶舱 — 机构信念 · 门控 · 熔断", padding=6)
        cockpit.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=(0, 4))
        cockpit.columnconfigure(0, weight=1)

        cards_row = ttk.Frame(cockpit)
        cards_row.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 4))
        for i in range(6):
            cards_row.columnconfigure(i, weight=1)
        self._cockpit_vars = {
            "direction": tk.StringVar(value="—"),
            "risk_action": tk.StringVar(value="—"),
            "conviction": tk.StringVar(value="—"),
            "regime": tk.StringVar(value="—"),
            "follow": tk.StringVar(value="—"),
            "ta_playbook": tk.StringVar(value="—"),
        }
        cockpit_defs = [
            ("分析方向", "direction", "#1565C0"),
            ("风控结论", "risk_action", "#5D4037"),
            ("机构信念", "conviction", "#6A1B9A"),
            ("市场状态", "regime", "#2E7D32"),
            ("跟单状态", "follow", "#00796B"),
            ("TA Playbook", "ta_playbook", "#E65100"),
        ]
        for i, (label, key, color) in enumerate(cockpit_defs):
            box = ttk.Frame(cards_row, relief="solid", borderwidth=1, padding=6)
            box.grid(row=0, column=i, sticky=(tk.W, tk.E), padx=3)
            ttk.Label(box, text=label, foreground="#666", font=("", 8)).pack()
            tk.Label(box, textvariable=self._cockpit_vars[key],
                     fg=color, font=("", 11, "bold"), wraplength=120).pack(pady=(2, 0))

        self._cockpit_factors_text = scrolledtext.ScrolledText(
            cockpit, wrap=tk.WORD, height=7, font=("Consolas", 9),
        )
        self._cockpit_factors_text.grid(row=1, column=0, sticky=(tk.W, tk.E))

        breaker_bar = ttk.Frame(cockpit)
        breaker_bar.grid(row=2, column=0, sticky=tk.E, pady=(2, 0))
        ttk.Button(
            breaker_bar,
            text="解除熔断冷却",
            command=self._reset_circuit_breaker,
        ).pack(side=tk.RIGHT, padx=4)

        # 主显示区
        text_frame = ttk.Frame(parent)
        text_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        self.advice_text = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, height=16, font=('Consolas', 10))
        self.advice_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.advice_text.insert(
            1.0,
            "【尚未生成 AI 决策】\n\n"
            "请点击上方「开始分析」或「全新分析」。系统将依次完成：\n"
            "  · 开始分析：允许知识复用（同局面可跳过 LLM）\n"
            "  · 全新分析：强制调用 LLM，不复用历史结论\n"
            "  1. 拉取行情与技术指标\n"
            "  2. 汇总新闻 / 情绪 / 链上 / 宏观 / BNB 专属因子\n"
            "  3. 调用 DeepSeek/千问/火山做 AI 主分析\n"
            "  4. 经风控门控后生成最终开单建议\n"
            "  5. 把本次结论写入学习系统，供下次分析复用\n\n"
            "提示：信号不足时会返回 WAIT，不会硬给价格。\n"
        )

    def _format_learning_injection_block(self, result: dict) -> str:
        """展示本次分析注入了哪些历史学习，帮助用户理解 AI 如何越用越准。"""
        lc = result.get("learning_context") or {}
        if not lc:
            return (
                "[Learning Feedback Loop]\n"
                "- status: 本次未加载历史学习上下文\n"
                "- next: 完成模拟盘/复盘/反馈后，下次分析会自动注入相关知识\n"
            )

        growth = lc.get("growth") or {}
        cards = lc.get("capability_cards") or []
        weights = lc.get("strategy_weights") or {}
        pm = lc.get("pattern_memory") or {}
        cf = lc.get("counterfactual_stats") or {}
        paper = lc.get("paper_trading") or {}
        recs = lc.get("recommendations") or []
        param_changes = lc.get("recent_param_changes") or []

        lines = [
            "[Learning Feedback Loop]",
            f"- capability_level: L{growth.get('capability_level', 0)}",
            f"- maturity: {lc.get('learning_maturity', growth.get('learning_maturity', 'BEGINNER'))}",
            f"- historical_accuracy: {lc.get('overall_accuracy', 0):.1%}",
            f"- knowledge_cards_injected: {len(cards)} ({lc.get('capability_retrieval_mode', 'none')})",
            f"- strategy_weights_loaded: {len(weights)}",
            f"- pattern_memory_matched: {pm.get('matched', 0)}",
            f"- counterfactual_samples: {cf.get('total_analyzed', 0)}",
        ]
        if paper.get("closed_trades"):
            lines.append(
                f"- paper_trading: {paper.get('closed_trades', 0)} closed | "
                f"win_rate {paper.get('win_rate', 0):.1%} | "
                f"pnl {paper.get('total_pnl_usdt', 0):+.2f} USDT"
            )
        if param_changes:
            ch = param_changes[0]
            pname = ch.get("param") or ch.get("param_name") or "?"
            old_v = ch.get("old_value", ch.get("old", "?"))
            new_v = ch.get("new_value", ch.get("new", "?"))
            lines.append(f"- recent_param_change: {pname} {old_v} -> {new_v}")
        if recs:
            lines.append(f"- ai_recommendation: {recs[0][:80]}")
        if cards:
            top = cards[0]
            lines.append(
                f"- top_knowledge_card: [{top.get('category', '?')}] "
                f"{(top.get('title') or '')[:40]}"
            )
        wrc = lc.get("win_rate_context") or {}
        if wrc.get("enabled") and (wrc.get("reasons") or wrc.get("hints")):
            lb = float(wrc.get("long_boost") or 0) - float(wrc.get("long_penalty") or 0)
            sb = float(wrc.get("short_boost") or 0) - float(wrc.get("short_penalty") or 0)
            lines.append(f"- win_rate_vote_adj: long{lb:+.2f} / short{sb:+.2f}")
            if wrc.get("block_long"):
                lines.append("- win_rate_block: LONG (regime 历史亏损)")
            if wrc.get("block_short"):
                lines.append("- win_rate_block: SHORT (regime 历史亏损)")
            for r in (wrc.get("reasons") or [])[:2]:
                lines.append(f"- win_rate_alert: {r[:70]}")
        lines.append("- next_effect: 本次分析结果会再次沉淀，供下次 AI 研判与门控复用")
        snippet = self._format_recent_evolution_snippet(limit=3)
        if snippet:
            lines.append("")
            lines.extend(snippet)
        return "\n".join(lines)

    def _format_recent_evolution_snippet(self, limit: int = 3) -> list:
        """在决策页展示最近几条进化事件，帮助用户看见闭环在运转。"""
        try:
            from bnb_quant_tool.learning_timeline import LearningTimelineCollector

            paper_path = getattr(getattr(self, "paper_engine", None), "db_path", None)
            collector = LearningTimelineCollector(
                self.learner.db_path, paper_db_path=paper_path
            )
            events = collector.collect(limit=limit)
            if not events:
                return ["- recent_evolution: 暂无历史事件（完成模拟盘/复盘后会填充）"]
            lines = ["- recent_evolution:"]
            for ev in events[:limit]:
                ts = (ev.timestamp or "")[:16]
                lines.append(f"  · {ts} | {ev.stage_label} | {ev.title[:50]}")
            return lines
        except Exception:
            return []

    def _format_cockpit_factors(self, result: dict) -> str:
        """机构信念因子条 + 门控原因 + Agent 票。"""
        advice = result.get("trade_advice") or {}
        conv = advice.get("institutional_conviction") or (
            (result.get("learning_context") or {}).get("institutional_conviction")
        ) or {}
        lines = []

        # AI 结论置顶：复用时标注清楚，避免误以为跑了三家 LLM
        ai_map = result.get("ai_analyses") or {}
        ai0 = result.get("ai_analysis") if isinstance(result.get("ai_analysis"), dict) else {}
        advice0 = result.get("trade_advice") or {}
        is_reuse = bool(
            (isinstance(ai_map, dict) and "knowledge_reuse" in ai_map)
            or ai0.get("_reused")
            or ai0.get("_provider") == "knowledge_reuse"
            or advice0.get("_reused")
        )
        if is_reuse or (isinstance(ai_map, dict) and ai_map):
            if is_reuse:
                lines.append("[主分析 ← 知识复用 · 未调用三家 LLM]")
            else:
                lines.append("[三家主分析 → 综合]")
            order = ["knowledge_reuse", "consensus", "deepseek", "qianwen", "volcengine"]
            shown = set()
            for pname in order:
                pdata = ai_map.get(pname) if isinstance(ai_map, dict) else None
                if not isinstance(pdata, dict):
                    if is_reuse and pname == "knowledge_reuse" and isinstance(ai0, dict):
                        pdata = ai0
                    else:
                        continue
                shown.add(pname)
                lab = pdata.get("_provider_label") or pname
                if pdata.get("_error") or pdata.get("_degraded"):
                    lines.append(f"  {lab}: 失败 — {pdata.get('_error') or 'degraded'}")
                    continue
                ens = pdata.get("_ensemble") or {}
                extra = f" | 分票 {ens.get('detail')}" if ens.get("detail") else ""
                lines.append(
                    f"  {lab}: {pdata.get('signal', '?')} "
                    f"@ {float(pdata.get('confidence') or 0):.0%} | "
                    f"趋势 {pdata.get('trend', '?')}{extra}"
                )
                if pdata.get("_reuse_reason"):
                    lines.append(f"  备注: {pdata.get('_reuse_reason')}")
            if isinstance(ai_map, dict):
                for pname, pdata in ai_map.items():
                    if pname in shown or not isinstance(pdata, dict):
                        continue
                    lab = pdata.get("_provider_label") or pname
                    lines.append(
                        f"  {lab}: {pdata.get('signal', '?')} "
                        f"@ {float(pdata.get('confidence') or 0):.0%}"
                    )
            note = result.get("ai_analysis_note") or ""
            if note and f"备注: {note}" not in "\n".join(lines):
                lines.append(f"  备注: {note}")
            lines.append("")

        lines.append("[机构信念因子分解]")
        lines.append("")

        for f in (conv.get("factors") or [])[:8]:
            if not f.get("weight"):
                continue
            sc = float(f.get("score") or 0)
            bar = self._score_bar(sc)
            lines.append(
                f"  {f.get('name', '?'):16s} {bar} {sc:+.2f}  {str(f.get('detail', '') or '')}"
            )

        if conv.get("conflicts"):
            lines.append("")
            lines.append("[冲突项]")
            for c in conv["conflicts"][:3]:
                lines.append(f"  ⚠ {c}")

        gates = advice.get("gate_reasons") or []
        if gates:
            lines.append("")
            lines.append("[门控 / 熔断]")
            for g in gates[:6]:
                lines.append(f"  · {g}")

        cb = advice.get("circuit_breaker") or {}
        if cb.get("level") and cb.get("level") != "NORMAL":
            lines.append(
                f"  熔断级别: {cb.get('level')} | 连亏 {cb.get('consec_losses', 0)} | "
                f"24h亏损 {float(cb.get('daily_loss_pct', 0)):.1%}"
            )

        ma = advice.get("multi_agent_deliberation") or {}
        council = ma.get("council") or {}
        teams = council.get("teams") or {}
        if teams or council.get("merge_note"):
            lines.append("")
            lines.append("[议会分队]")
            if isinstance(teams, dict):
                for team_name, tinfo in teams.items():
                    if not isinstance(tinfo, dict):
                        continue
                    lines.append(
                        f"  {tinfo.get('label') or team_name}: "
                        f"{tinfo.get('final_action') or tinfo.get('action') or '?'} "
                        f"@ {float(tinfo.get('final_confidence') or tinfo.get('confidence') or 0):.0%}"
                    )
            if council.get("merge_note"):
                lines.append(f"  合并: {council.get('merge_note')}")

        # 旧「双模主分析」块已上移到顶部，此处不再重复

        votes = council.get("votes") or ma.get("agent_votes") or ma.get("votes") or []
        if votes:
            lines.append("")
            lines.append("[交易员议会投票]")
            if council.get("final_action"):
                lines.append(
                    f"  共识 → {council.get('final_action')} "
                    f"置信 {float(council.get('final_confidence') or 0):.0%} "
                    f"| 一致度 {float(council.get('agreement') or 0):.0%}"
                )
            for v in votes:
                if isinstance(v, dict):
                    emoji = v.get("emoji") or ""
                    name = v.get("trader_name") or v.get("agent") or v.get("role") or "?"
                    prov = v.get("provider") or ""
                    tag = f"/{prov}" if prov else ""
                    lines.append(
                        f"  {emoji} {name}{tag}: "
                        f"{v.get('vote', v.get('action', '?'))} "
                        f"({float(v.get('confidence', 0)):.0%}) "
                        f"[{v.get('source', '?')}]"
                    )
            if council.get("chair_summary"):
                lines.append(f"  主席: {str(council.get('chair_summary') or '')}")
        elif ma.get("transcript"):
            lines.append("")
            lines.append("[多智能体]")
            lines.append(f"  {str(ma.get('final_action') or '?')} "
                         f"置信 {float(ma.get('final_confidence') or 0):.0%}")

        fam = conv.get("strategy_family") or {}
        if fam.get("text"):
            lines.append("")
            lines.append(f"[策略族] {fam.get('text')}")

        mr = result.get("market_regime") or advice.get("market_regime") or {}
        if isinstance(mr, dict) and mr.get("regime_votes"):
            lines.append("")
            lines.append(f"[Regime 多信号融合] 置信 {float(mr.get('fusion_confidence', 0)):.0%}")
            for v in mr["regime_votes"]:
                lines.append(f"  · {v.get('signal')}: {v.get('bucket')} — {v.get('detail', '')}")
            for c in (mr.get("regime_conflicts") or [])[:2]:
                lines.append(f"  ⚠ {c}")
        if isinstance(mr, dict) and mr.get("hmm_regime"):
            agree = "✓" if mr.get("hmm_agreement") else "≠"
            lines.append(
                f"  HMM {agree} {mr.get('hmm_regime')} "
                f"(置信 {float(mr.get('hmm_confidence', 0)):.0%}) — {str(mr.get('hmm_detail', '') or '')}"
            )

        sv = advice.get("structural_vote") or (advice.get("votes") or {}).get("structural_vote") or {}
        if sv.get("summary"):
            lines.append("")
            lines.append(f"[结构性策略] {str(sv.get('summary') or '')}")

        ta_lines = self._format_ta_playbook_lines(result)
        if ta_lines:
            lines.extend([""] + ta_lines)

        try:
            from bnb_quant_tool.learning_analytics import format_win_rate_cockpit_lines

            wrc = (result.get("learning_context") or {}).get("win_rate_context") or {}
            wr_lines = format_win_rate_cockpit_lines(wrc)
            if wr_lines:
                lines.extend([""] + wr_lines)
        except Exception:
            pass

        if len(lines) <= 2:
            lines.append("  （运行「开始 AI 分析」后显示因子分解）")
        return "\n".join(lines)

    @staticmethod
    def _format_ta_playbook_lines(result: dict) -> list:
        try:
            from bnb_quant_tool.crypto_ta_playbook import format_ta_cockpit_lines

            bundle = (
                result.get("ta_playbook")
                or (result.get("trade_advice") or {}).get("ta_playbook")
                or (result.get("learning_context") or {}).get("ta_playbook")
            )
            return format_ta_cockpit_lines(bundle)
        except Exception:
            return []

    @staticmethod
    def _format_ta_summary_for_output(result: dict) -> str:
        try:
            from bnb_quant_tool.crypto_ta_playbook import format_ta_summary_block

            bundle = (
                result.get("ta_playbook")
                or (result.get("trade_advice") or {}).get("ta_playbook")
            )
            return format_ta_summary_block(bundle).strip()
        except Exception:
            return ""

    @staticmethod
    def _score_bar(score: float, width: int = 12) -> str:
        mid = width // 2
        pos = int(min(mid, max(0, score * mid)))
        neg = int(min(mid, max(0, -score * mid)))
        return "░" * (mid - neg) + ("█" * neg if neg else "") + "│" + ("█" * pos if pos else "") + "░" * (mid - pos)

    def _refresh_decision_cockpit(self, result: dict) -> None:
        """刷新决策驾驶舱卡片与因子条。"""
        if not hasattr(self, "_cockpit_vars"):
            return
        advice = result.get("trade_advice") or {}
        conv = advice.get("institutional_conviction") or (
            (result.get("learning_context") or {}).get("institutional_conviction")
        ) or {}
        mr = result.get("market_regime") or advice.get("market_regime") or {}
        ctx = advice.get("execution_context") or {}
        if not ctx and getattr(self, "config", None):
            try:
                from bnb_quant_tool.ai_trading_context import resolve_execution_context
                auto_on = bool(getattr(self, "auto_paper_var", None) and self.auto_paper_var.get())
                ctx = resolve_execution_context(advice, self.config, auto_follow_enabled=auto_on)
            except Exception:
                ctx = {}

        raw = ctx.get("analysis_direction") or advice.get("raw_action", "WAIT")
        risk = ctx.get("final_action") or advice.get("action", "WAIT")
        self._cockpit_vars["direction"].set(str(raw))
        self._cockpit_vars["risk_action"].set(str(risk))
        cv = float(conv.get("conviction") or 0)
        self._cockpit_vars["conviction"].set(
            f"{conv.get('direction', '—')} {cv:+.2f}" if conv else "—"
        )
        regime = mr.get("regime") if isinstance(mr, dict) else str(mr or "—")
        self._cockpit_vars["regime"].set(str(regime))
        if ctx:
            gate = ctx.get("gate_label", "—")
            follow = ctx.get("follow_label", "—")
            self._cockpit_vars["follow"].set(f"{follow}\n({gate})")
        else:
            passed = advice.get("passed_gate", False)
            self._cockpit_vars["follow"].set("✓ 通过" if passed else "✗ 拦截")

        ta = (
            result.get("ta_playbook")
            or advice.get("ta_playbook")
            or (result.get("learning_context") or {}).get("ta_playbook")
            or {}
        )
        if ta.get("enabled"):
            bias = ta.get("classic_ta_bias", "—")
            align = "✓" if ta.get("classic_ta_aligned_with_consensus") else "≠"
            styles = ta.get("recommended_styles") or []
            hint = (ta.get("indicator_hints") or [""])[0]
            self._cockpit_vars["ta_playbook"].set(
                f"{bias} {align}\n{styles[0] if styles else '—'}\n{hint}"
            )
        else:
            self._cockpit_vars["ta_playbook"].set("—")

        if hasattr(self, "_cockpit_factors_text"):
            self._cockpit_factors_text.delete(1.0, tk.END)
            self._cockpit_factors_text.insert(1.0, self._format_cockpit_factors(result))

    def _copy_advice_full(self):
        """复制完整报告到剪贴板"""
        try:
            content = self.advice_text.get(1.0, tk.END).strip()
            if not content:
                messagebox.showinfo("提示", "暂无内容可复制")
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.update_status("已复制开单报告到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def _copy_advice_short(self):
        """复制精简版下单文本到剪贴板"""
        try:
            advice = (self.analysis_result or {}).get('trade_advice') if hasattr(self, 'analysis_result') else None
            if not advice:
                messagebox.showinfo("提示", "请先运行一次分析")
                return
            order_text = advice.get('order_text') or ''
            if not order_text:
                messagebox.showinfo("提示", "本次分析未生成下单文本")
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(order_text)
            self.update_status("已复制下单参数到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def update_output(self):
        if not self.analysis_result:
            return

        def _update():
            self.summary_text.delete(1.0, tk.END)
            self.inst_text.delete(1.0, tk.END)
            self.detail_text.delete(1.0, tk.END)
            self.advice_text.delete(1.0, tk.END)

            r = self.analysis_result

            self._refresh_decision_cockpit(r)
            if hasattr(self, "_update_traders_from_analysis"):
                self._update_traders_from_analysis(r)
            if hasattr(self, "_refresh_room_from_config"):
                self._refresh_room_from_config()

            # AI 决策 Tab（首要展示）
            advice = r.get('trade_advice') or {}
            learning_block = self._format_learning_injection_block(r)
            report = advice.get('report_text') or '未生成 AI 决策报告'
            ai0 = r.get("ai_analysis") if isinstance(r.get("ai_analysis"), dict) else {}
            ai_map = r.get("ai_analyses") or {}
            is_reuse = bool(
                ai0.get("_reused")
                or ai0.get("_provider") == "knowledge_reuse"
                or (isinstance(ai_map, dict) and "knowledge_reuse" in ai_map)
                or advice.get("_reused")
            )
            # 兜底：非复用且报告缺三家块时，强制插到最前面
            need_ai_header = (
                not is_reuse
                and isinstance(ai_map, dict)
                and len(ai_map) >= 1
                and "三家综合" not in report
                and "AI 主分析" not in report
                and "[3]" not in report
                and "[三家主分析" not in report
            )
            if need_ai_header:
                try:
                    from bnb_quant_tool.llm_provider import format_ai_analyses_report_block
                    blk = (
                        advice.get("_ai_analyses_report")
                        or format_ai_analyses_report_block(
                            primary=r.get("ai_analysis"),
                            by_provider=ai_map,
                            note=r.get("ai_analysis_note") or "",
                        )
                    )
                    if blk and blk.strip():
                        report = blk + "\n" + report
                except Exception:
                    pass
            # 复用时：若报告标题仍写「三家」，改成明确复用标题，避免误导
            if is_reuse and report.lstrip().startswith("[三家主分析"):
                report = report.replace(
                    "[三家主分析 → 综合]",
                    "[主分析 ← 知识复用 · 未调用三家 LLM]",
                    1,
                )
            self.advice_text.insert(1.0, learning_block + "\n\n" + report)

            if hasattr(self, "ai_chain_var"):
                ctx = advice.get("execution_context") or {}
                raw = ctx.get("analysis_direction") or advice.get("raw_action", "WAIT")
                risk = advice.get("action", "WAIT")
                conf = advice.get("confidence", 0)
                follow = ctx.get("follow_label", "")
                extra = f" | {follow}" if follow else ""
                self.ai_chain_var.set(
                    f"本轮: 方向={raw} 风控={risk} | 置信度 {conf:.0%}{extra} | "
                    f"学习已记录 → 下次分析将自动复用知识卡片与历史胜率"
                )

            # AI 分析摘要 Tab
            ai = r['ai_analysis']
            advice_action = advice.get('action', 'WAIT')
            raw_action = advice.get('raw_action', advice_action)
            passed_gate = advice.get('passed_gate', False)
            ctx = advice.get("execution_context") or {}
            gate_line = ctx.get("gate_label") or ('通过' if passed_gate else '拦截')
            follow_line = ctx.get("follow_reason") or (
                "门控通过可跟单" if passed_gate else "门控拦截，默认不跟单"
            )
            exec_dir = ctx.get("effective_direction") or raw_action
            summary = f"""{'='*65}
BNB AI 交易分析报告
{'='*65}

时间:       {r['timestamp'][:19]}
交易对:     {r['symbol']} ({r['timeframe']})
数据点数:   {r['data_points']}
价格:       ${r['current_price']:.2f}
模式:       {r['strategy_mode']}

--- AI 核心结论（三层）---
分析方向:   {raw_action}
风控结论:   {advice_action}
跟单方向:   {exec_dir}
置信度:     {advice.get('confidence', ai.get('confidence', 0)):.2%}
信号强度:   {advice.get('strength', 'N/A')}
门控状态:   {gate_line}
跟单说明:   {follow_line}
AI 信号:    {ai.get('signal', 'N/A')} | 趋势 {ai.get('trend', 'N/A')}
AI 摘要:    {(ai.get('analysis', '') or '')}

--- 学习反哺 ---
{learning_block}

--- 综合信号 ---
最终建议:   {r['final_recommendation']}
记录 ID:    {self.last_record_id}
{'='*65}

[1] 机构策略 (共{r['institutional_strategies'].get('total_strategies', '?')}个)
{'-'*60}
  买入: {r['institutional_strategies']['buy_signals']}  |  卖出: {r['institutional_strategies']['sell_signals']}  |  持有: {r['institutional_strategies']['hold_signals']}
  共识信号: {r['institutional_strategies']['consensus_signal']} (置信度: {r['institutional_strategies']['consensus_confidence']:.2f})
  加权投票: {'是' if r['institutional_strategies'].get('weighted_voting') else '否'}
"""
            ta_block = self._format_ta_summary_for_output(r)
            if ta_block:
                summary += f"""
[技术分析 Playbook]
{'-'*60}
{ta_block}
"""
            mr = r.get('market_regime') or {}
            if mr.get('regime'):
                summary += f"""
[市场状态]
{'-'*60}
  状态: {mr.get('regime')}
  说明: {mr.get('description', '')}
  依据: {', '.join(mr.get('reasons', [])[:3])}
"""
            summary += f"""
[2] 技术指标
{'-'*60}
"""
            for ind in [
                'RSI', 'ADX', 'Stoch_K', 'MACD', 'MACD_Signal',
                'BB_Upper', 'BB_Middle', 'BB_Lower', 'MA_20', 'MA_50',
                'Support', 'Resistance', 'OBV_Slope', 'ATR', 'Volume_Ratio',
            ]:
                if ind in r['indicators']:
                    summary += f"  {ind:<20s} {r['indicators'][ind]:>12.4f}\n"

            from bnb_quant_tool.llm_provider import format_ai_analyses_report_block
            ai_block = format_ai_analyses_report_block(
                primary=ai,
                by_provider=r.get("ai_analyses") or {},
                note=r.get("ai_analysis_note") or "",
            )
            if not ai_block.strip():
                ai_block = format_ai_analyses_report_block(primary=ai)
            summary += "\n" + ai_block

            # 双模议会分队 + 合并终局
            ma = advice.get("multi_agent_deliberation") or {}
            council = ma.get("council") or {}
            teams = council.get("teams") or {}
            if teams or council.get("merge_note"):
                summary += f"""
[3b] 双模议会
{'-'*60}
"""
                if isinstance(teams, dict):
                    for team_name, tinfo in teams.items():
                        if not isinstance(tinfo, dict):
                            continue
                        summary += (
                            f"  {tinfo.get('label') or team_name}: "
                            f"{tinfo.get('final_action') or tinfo.get('action') or '?'} "
                            f"@ {float(tinfo.get('final_confidence') or tinfo.get('confidence') or 0):.0%} "
                            f"(n={tinfo.get('count') or len(tinfo.get('votes') or [])})\n"
                        )
                if council.get("merge_note"):
                    summary += f"  合并: {council.get('merge_note')}\n"
                if council.get("final_action"):
                    summary += (
                        f"  终局: {council.get('final_action')} "
                        f"@ {float(council.get('final_confidence') or 0):.0%}\n"
                    )

            summary += f"""
[4] 执行计划
{'-'*60}
  风控结论: {advice_action}
  跟单方向: {exec_dir}
"""
            show_prices = advice_action != 'WAIT' or (
                ctx.get("will_follow") and (advice.get('prices') or {}).get('entry_mid')
            )
            if show_prices:
                p = advice.get('prices') or {}
                pos = advice.get('position') or {}
                summary += (
                    f"  入场:  {p.get('entry_mid')}\n"
                    f"  止损:  {p.get('stop_loss')}\n"
                    f"  止盈2: {p.get('tp2')}\n"
                    f"  仓位:  {pos.get('quantity')} BNB ~= {pos.get('usdt_amount')} USDT\n"
                )
                if advice_action == 'WAIT' and exec_dir in ('LONG', 'SHORT'):
                    summary += "  备注: 风控观望，参数供按分析方向跟单参考\n"
            else:
                gates = advice.get('gate_reasons') or []
                if gates:
                    summary += "  拦截原因:\n"
                    for g in gates[:5]:
                        summary += f"    - {g}\n"

            rc = r['risk_check']
            summary += f"""
[5] 风险检查
{'-'*60}
  Status: {'PASS' if rc['passed'] else 'FAIL'}
  原因: {rc['reason']}

[下次如何更准]
  - 模拟盘平仓、AI 复盘、手动反馈都会进入学习系统
  - 下次分析会自动注入知识卡片、策略权重与历史胜率修正

{'='*65}
"""
            self.summary_text.insert(1.0, summary)

            # Institutional Tab
            n_inst = r['institutional_strategies'].get('total_strategies', len(
                r['institutional_strategies'].get('strategy_details', {})
            ))
            inst_txt = f"机构策略详情 ({n_inst})\n{'='*65}\n\n"
            classic_keys = {
                "golden_death_cross", "adx_trend", "stochastic_momentum",
                "volume_price_obv", "range_sr_swing", "breakout_volume",
            }
            inst_txt += "── 经典技术分析 (IG/Moomoo) ──\n\n"
            for name, detail in r['institutional_strategies']['strategy_details'].items():
                if name not in classic_keys:
                    continue
                sig = detail.get('signal', 'HOLD')
                marker = '[买]' if sig == 'BUY' else ('[卖]' if sig == 'SELL' else '[HOLD]')
                inst_txt += f"{marker} {detail.get('strategy', name)}\n"
                inst_txt += f"       信号: {sig} | Conf: {detail.get('confidence', 0):.2f}\n"
                reason = detail.get('reason') or detail.get('description') or ''
                if reason:
                    inst_txt += f"       {reason}\n"
                inst_txt += "\n"
            inst_txt += "── 全部策略 ──\n\n"
            for name, detail in r['institutional_strategies']['strategy_details'].items():
                sig = detail.get('signal', 'HOLD')
                marker = '[买]' if sig == 'BUY' else ('[卖]' if sig == 'SELL' else '[HOLD]')
                inst_txt += f"{marker} {detail.get('strategy', name)}\n"
                inst_txt += f"       信号: {sig} | Conf: {detail.get('confidence', 0):.2f}\n"
                if detail.get('description'):
                    inst_txt += f"       {detail['description']}\n"
                inst_txt += "\n"
            self.inst_text.insert(1.0, inst_txt)

            # Raw Data Tab
            self.detail_text.insert(1.0, json.dumps(r, indent=2, ensure_ascii=False, default=str))

            # 自动刷新市场情绪 + 多周期 Tab
            try:
                if hasattr(self, 'market_text'):
                    self.market_text.delete(1.0, tk.END)
                    parts = []
                    mtf = r.get('multi_timeframe') or {}
                    if mtf:
                        try:
                            parts.append(MultiTimeframeAnalyzer.format_report(mtf))
                        except Exception:
                            parts.append(f"多周期: {mtf.get('recommended_action', '?')} 汇聚={mtf.get('confluence', '?')}")
                    sent = r.get('sentiment') or {}
                    if sent:
                        try:
                            parts.append(MarketSentiment.format_report(sent))
                        except Exception:
                            parts.append(f"情绪: {sent.get('label', '?')} score={sent.get('sentiment_score', 0):.2f}")
                    if parts:
                        self.market_text.insert(1.0, "\n\n".join(parts))
                    else:
                        self.market_text.insert(1.0, "本次未拉取到多周期/情绪数据")
                    self.last_mtf = mtf
                    self.last_sentiment = sent
            except Exception:
                pass

            # 自动刷新新闻 Tab
            try:
                if hasattr(self, 'news_list_text'):
                    items = r.get('news_items') or []
                    sym = r.get('symbol', '')
                    self._news_render_list(items, sym, 24)
                if hasattr(self, 'news_summary_text'):
                    ns = r.get('news_summary') or {}
                    if ns:
                        self._news_render_summary(ns)
            except Exception:
                pass

            # 主界面已展示决议；工具柜保持隐藏，不强制切页
            try:
                if hasattr(self, "_tools_win") and self._tools_win.state() == "normal":
                    if "advice" in getattr(self, "_panels", {}):
                        self.notebook.select(self._panels["advice"])
            except Exception:
                pass

        self.root.after(0, _update)

    def save_result(self):
        if not self.analysis_result:
            messagebox.showwarning("警告", "没有可保存的结果")
            return
        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=f"bnb_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(self.analysis_result, f, indent=2, ensure_ascii=False, default=str)
                messagebox.showinfo("成功", f"已保存至：\n{filename}")
            except Exception as e:
                messagebox.showerror("保存失败", str(e))

    def clear_output(self):
        self.summary_text.delete(1.0, tk.END)
        self.inst_text.delete(1.0, tk.END)
        self.detail_text.delete(1.0, tk.END)
        if hasattr(self, 'advice_text'):
            self.advice_text.delete(1.0, tk.END)
        self.analysis_result = None
        self.save_btn.config(state='disabled')
        self.feedback_btn.config(state='disabled')
        self.status_var.set("就绪 | AI 决策引擎: ON | 学习闭环: ON")

