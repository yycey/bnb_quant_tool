"""Mixin: ReviewTabMixin"""

import sqlite3

from gui._imports import *


class ReviewTabMixin:
    def _create_review_tab(self, parent):
        """创建AI复盘标签页"""
        intro = ttk.Label(
            parent,
            text=(
                "AI 复盘是进化闭环的核心环节：复盘结论会写入知识库、触发参数演化，"
                "并在「学习进化」时间线中与知识注入事件串联展示。"
            ),
            foreground="#444",
            wraplength=1000,
            justify=tk.LEFT,
        )
        intro.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=8, pady=(8, 2))

        # 顶部按钮区
        btn_frame = ttk.Frame(parent)
        btn_frame.grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)

        ttk.Button(btn_frame, text="🔄 手动复盘", command=self._run_manual_review).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="✅ 应用全部建议", command=self._apply_all_params).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📜 参数历史", command=self._show_param_history).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="⏪ 版本回滚", command=self._rollback_version).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔮 影子策略", command=self._show_shadow_strategies).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🧬 指标探索", command=self._run_indicator_exploration).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🧠 深度学习", command=self._run_deep_learning).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🗑️ 清空历史", command=self._clear_review_history).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🧬 进化时间线", command=self._jump_to_evolution_tab).pack(side=tk.LEFT, padx=5)

        self.review_status_var = tk.StringVar(value="点击手动复盘或等待自动触发")
        ttk.Label(btn_frame, textvariable=self.review_status_var, foreground="#666").pack(side=tk.LEFT, padx=10)

        # 主区域：三列布局
        main_frame = ttk.Frame(parent)
        main_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        parent.rowconfigure(2, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.columnconfigure(2, weight=1)
        main_frame.rowconfigure(0, weight=1)

        # 左列：最近复盘结果
        left_frame = ttk.LabelFrame(main_frame, text="最近复盘结果")
        left_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)

        self.review_result_text = scrolledtext.ScrolledText(left_frame, width=40, height=20, wrap=tk.WORD)
        self.review_result_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=2, pady=2)

        # 中列：参数调整建议
        mid_frame = ttk.LabelFrame(main_frame, text="参数调整建议")
        mid_frame.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        mid_frame.columnconfigure(0, weight=1)
        mid_frame.rowconfigure(0, weight=1)

        self.param_changes_tree = ttk.Treeview(mid_frame, columns=("param", "old", "new", "reason"), show="headings", height=10)
        self.param_changes_tree.heading("param", text="参数")
        self.param_changes_tree.heading("old", text="原值")
        self.param_changes_tree.heading("new", text="新值")
        self.param_changes_tree.heading("reason", text="原因")
        self.param_changes_tree.column("param", width=100)
        self.param_changes_tree.column("old", width=60)
        self.param_changes_tree.column("new", width=60)
        self.param_changes_tree.column("reason", width=150)
        self.param_changes_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=2, pady=2)

        # 右列：新策略候选
        right_frame = ttk.LabelFrame(main_frame, text="新策略候选 (影子测试)")
        right_frame.grid(row=0, column=2, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        right_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(0, weight=1)

        self.shadow_strategies_text = scrolledtext.ScrolledText(right_frame, width=40, height=20, wrap=tk.WORD)
        self.shadow_strategies_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=2, pady=2)

        # 底部：复盘历史
        history_frame = ttk.LabelFrame(parent, text="复盘历史")
        history_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        history_frame.columnconfigure(0, weight=1)

        self.review_history_tree = ttk.Treeview(history_frame, columns=("time", "trades", "winrate", "status"), show="headings", height=5)
        self.review_history_tree.heading("time", text="时间")
        self.review_history_tree.heading("trades", text="分析笔数")
        self.review_history_tree.heading("winrate", text="胜率")
        self.review_history_tree.heading("status", text="状态")
        self.review_history_tree.column("time", width=150)
        self.review_history_tree.column("trades", width=80)
        self.review_history_tree.column("winrate", width=80)
        self.review_history_tree.column("status", width=100)
        self.review_history_tree.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=2, pady=2)

        # 进化快照：串联复盘 → 知识 → 注入
        evo_snap = ttk.LabelFrame(parent, text="🧬 近期学习进化快照（复盘后在此可见知识沉淀与注入）")
        evo_snap.grid(row=4, column=0, sticky=(tk.W, tk.E), padx=5, pady=(0, 5))
        evo_snap.columnconfigure(0, weight=1)
        self._review_timeline_text = scrolledtext.ScrolledText(
            evo_snap, wrap=tk.WORD, height=4, font=("Consolas", 9)
        )
        self._review_timeline_text.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=2, pady=2)
        snap_btns = ttk.Frame(evo_snap)
        snap_btns.grid(row=1, column=0, sticky=tk.E, pady=(2, 4))
        ttk.Button(snap_btns, text="刷新快照", command=self._refresh_review_timeline_preview).pack(side=tk.LEFT, padx=3)
        ttk.Button(snap_btns, text="🧬 完整时间线", command=self._jump_to_evolution_tab).pack(side=tk.LEFT, padx=3)

        # 加载历史
        self.review_history_tree.bind("<<TreeviewSelect>>", self._on_review_history_select)
        self._review_history_cache = []
        self._refresh_review_history()

    def _run_manual_review(self):
        """手动触发AI复盘"""
        if not self.ai_review_engine:
            messagebox.showwarning("提示", "AI复盘引擎未初始化")
            return

        self.update_status("正在执行AI复盘...")

        def _run():
            try:
                result = self.ai_review_engine.run_review()
                self.root.after(0, lambda: self._handle_review_result(result))
            except Exception as e:
                logger.error(f"AI复盘失败: {e}")
                self.root.after(0, lambda: self.update_status(f"AI复盘失败: {e}"))

        threading.Thread(target=_run, daemon=True).start()

    def _handle_review_result(self, result: dict):
        """处理AI复盘结果"""
        if result.get("status") != "success":
            self.review_status_var.set(f"复盘失败: {result.get('reason', '未知')}")
            return

        ai_result = result.get("ai_result", {})

        # 更新复盘结果显示
        self.review_result_text.delete(1.0, tk.END)
        output_lines = [
            f"【AI复盘】{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"分析了 {result.get('trades_analyzed')} 笔交易",
            f"胜率: {result.get('win_rate', 0):.1%}",
            "",
            "=== 亏损特征 ===",
        ]
        for p in ai_result.get("loss_patterns", []):
            output_lines.append(f"  - {p}")
        output_lines.append("\n=== 盈利特征 ===")
        for p in ai_result.get("win_patterns", []):
            output_lines.append(f"  - {p}")
        output_lines.append(f"\n置信度: {ai_result.get('confidence', 0):.0%}")

        if ai_result.get("new_strategy_idea"):
            output_lines.append(f"\n新策略思路: {ai_result['new_strategy_idea']}")

        self.review_result_text.insert(tk.END, "\n".join(output_lines))

        # 更新参数建议表格
        self.param_changes_tree.delete(*self.param_changes_tree.get_children())
        for sug in ai_result.get("param_suggestions", []):
            self.param_changes_tree.insert("", tk.END, values=(
                sug.get("param"),
                sug.get("old"),
                sug.get("new"),
                sug.get("reason", "")
            ))

        # 自动生成策略变异候选
        self._generate_strategy_mutations(result)

        self.review_status_var.set(f"复盘完成，{len(ai_result.get('param_suggestions', []))}个参数建议")

        # 自动应用参数建议
        try:
            auto_result = self.ai_review_engine.apply_all_param_changes(config_path=str(CONFIG_PATH))
            if auto_result.get('applied'):
                applied_count = len(auto_result['applied'])
                self.review_status_var.set(f"✅ 复盘完成，已自动应用 {applied_count} 项参数优化")
                logger.info(f"自动应用 {applied_count} 项参数: {[a['param'] for a in auto_result['applied']]}")
        except Exception as e:
            logger.error(f"自动应用失败: {e}")

        self._refresh_review_history()
        if hasattr(self, "_refresh_learning_evolution_views"):
            self._refresh_learning_evolution_views()
        elif hasattr(self, "_refresh_evolution_timeline"):
            self._refresh_evolution_timeline()

    def _show_pending_params(self):
        """显示待确认的参数变更"""
        if not self.ai_review_engine:
            return

        pending = self.ai_review_engine.get_pending_param_changes()

        self.param_changes_tree.delete(*self.param_changes_tree.get_children())
        for c in pending:
            self.param_changes_tree.insert("", tk.END, values=(
                c["param"],
                c["old_value"],
                c["new_value"],
                c["reason"]
            ))

        self.review_status_var.set(f"待确认参数: {len(pending)}个")

    def _apply_all_params(self):
        """应用所有待确认的参数建议（自动模式）"""
        if not self.ai_review_engine:
            return

        pending = self.ai_review_engine.get_pending_param_changes()
        if not pending:
            messagebox.showinfo("提示", "没有待确认的参数变更")
            return

        # 显示预览
        msg = f"即将自动应用{len(pending)}个参数变更:\n\n"
        for c in pending:
            msg += f"{c['param']}: {c['old_value']} → {c['new_value']}\n"
        msg += "\n变更会自动创建备份，可随时回滚。是否继续?"

        if not messagebox.askyesno("确认参数变更", msg):
            return

        # 直接使用 ai_review_engine 自动应用
        result = self.ai_review_engine.apply_all_param_changes(config_path=str(CONFIG_PATH))

        if result.get('status') == 'success':
            applied = result.get('applied', [])
            failed = result.get('failed', [])
            skipped = result.get('skipped', [])

            msg = f"✅ 应用完成!\n\n成功: {len(applied)}\n失败: {len(failed)}\n跳过: {len(skipped)}"

            if applied:
                msg += "\n\n已应用的参数:\n"
                for ap in applied:
                    msg += f"  • {ap['param']} = {ap['new_value']}\n"

            self.review_status_var.set(f"已自动应用 {len(applied)} 项参数优化")
            messagebox.showinfo("成功", msg)

            # 清空已应用的建议
            self.param_changes_tree.delete(*self.param_changes_tree.get_children())
        else:
            messagebox.showerror("失败", result.get('error', '应用失败'))

    def _clear_review_history(self):
        """清空复盘历史"""
        if not messagebox.askyesno("确认", "是否清空所有 AI 复盘历史记录?"):
            return
        try:
            conn = sqlite3.connect(self.learner.db_path, timeout=10)
            conn.execute("DELETE FROM learning_log WHERE event_type='AI_REVIEW'")
            conn.commit()
            conn.close()
        except Exception as e:
            messagebox.showerror("错误", f"清空失败: {e}")
            return
        self.review_history_tree.delete(*self.review_history_tree.get_children())
        self._review_history_cache = []
        self.review_status_var.set("已清空复盘历史")

    def _refresh_review_history(self):
        """从 learning_log 加载 AI_REVIEW 历史"""
        if not hasattr(self, "review_history_tree"):
            return
        self.review_history_tree.delete(*self.review_history_tree.get_children())
        self._review_history_cache = []
        try:
            conn = sqlite3.connect(self.learner.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT timestamp, message, details, improvement_score
                FROM learning_log
                WHERE event_type = 'AI_REVIEW'
                ORDER BY timestamp DESC
                LIMIT 30
                """
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            self.review_status_var.set(f"历史加载失败: {e}")
            return

        if not rows:
            self.review_history_tree.insert("", tk.END, values=(
                "—", "—", "—", "暂无复盘记录"
            ))
            return

        import re
        for idx, row in enumerate(rows):
            trades = "—"
            winrate = "—"
            msg = row["message"] or ""
            m = re.search(r"分析了(\d+)笔", msg)
            if m:
                trades = m.group(1)
            details = {}
            if row["details"]:
                try:
                    details = json.loads(row["details"])
                except (json.JSONDecodeError, TypeError):
                    details = {}
            if details.get("win_rate") is not None:
                winrate = f"{float(details['win_rate']):.1%}"
            elif row["improvement_score"] is not None:
                winrate = f"置信 {float(row['improvement_score']):.0%}"
            sug_count = len(details.get("param_suggestions") or [])
            status = f"完成 · {sug_count}项参数建议" if sug_count else "完成"
            self._review_history_cache.append({
                "timestamp": row["timestamp"],
                "message": msg,
                "details": details,
                "trades": trades,
                "winrate": winrate,
                "status": status,
            })
            self.review_history_tree.insert("", tk.END, iid=str(idx), values=(
                (row["timestamp"] or "")[:19],
                trades,
                winrate,
                status,
            ))
        if hasattr(self, "_refresh_review_timeline_preview"):
            self._refresh_review_timeline_preview()

    def _refresh_review_timeline_preview(self):
        """在 AI 复盘 Tab 展示近期进化事件快照。"""
        if not hasattr(self, "_review_timeline_text"):
            return
        try:
            from bnb_quant_tool.learning_timeline import format_timeline_text

            collector = (
                self._learning_timeline_collector()
                if hasattr(self, "_learning_timeline_collector")
                else None
            )
            if collector is None:
                from bnb_quant_tool.learning_timeline import LearningTimelineCollector
                collector = LearningTimelineCollector(self.learner.db_path)
            events = collector.collect(limit=6)
            text = format_timeline_text(events, limit=6)
            self._review_timeline_text.delete(1.0, tk.END)
            self._review_timeline_text.insert(1.0, text)
        except Exception as e:
            self._review_timeline_text.delete(1.0, tk.END)
            self._review_timeline_text.insert(1.0, f"进化快照加载失败: {e}")

    def _on_review_history_select(self, _event=None):
        sel = self.review_history_tree.selection()
        if not sel or not self._review_history_cache:
            return
        try:
            idx = int(sel[0])
        except (ValueError, TypeError):
            return
        if idx < 0 or idx >= len(self._review_history_cache):
            return
        item = self._review_history_cache[idx]
        details = item.get("details") or {}
        lines = [
            f"【历史复盘】{item.get('timestamp', '')[:19]}",
            item.get("message", ""),
            f"胜率/置信: {item.get('winrate', '—')}",
            "",
            "=== 亏损特征 ===",
        ]
        for p in details.get("loss_patterns") or []:
            lines.append(f"  - {p}")
        lines.append("\n=== 盈利特征 ===")
        for p in details.get("win_patterns") or []:
            lines.append(f"  - {p}")
        lines.append("\n=== 参数建议 ===")
        for sug in details.get("param_suggestions") or []:
            lines.append(
                f"  - {sug.get('param')}: {sug.get('old')} → {sug.get('new')} ({sug.get('reason', '')})"
            )
        if details.get("new_strategy_idea"):
            lines.append(f"\n新策略思路: {details['new_strategy_idea']}")
        self.review_result_text.delete(1.0, tk.END)
        self.review_result_text.insert(tk.END, "\n".join(lines))

    def _save_config(self):
        """保存配置到 config.yaml"""
        import yaml
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
            logger.info("配置已保存到 config.yaml")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _show_param_history(self):
        """显示参数变更历史"""
        from bnb_quant_tool.param_manager import ParamManager

        if not hasattr(self, '_param_manager'):
            self._param_manager = ParamManager(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        # 创建新窗口
        history_win = tk.Toplevel(self.root)
        history_win.title("参数变更历史")
        history_win.geometry("900x500")

        # 版本列表
        ttk.Label(history_win, text="历史版本 (双击查看详情)").pack(pady=5)

        version_frame = ttk.Frame(history_win)
        version_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        version_tree = ttk.Treeview(version_frame, columns=("id", "time", "reason", "trades", "wr"), show="headings", height=6)
        version_tree.heading("id", text="版本ID")
        version_tree.heading("time", text="创建时间")
        version_tree.heading("reason", text="原因")
        version_tree.heading("trades", text="交易数")
        version_tree.heading("wr", text="胜率")
        version_tree.column("id", width=120)
        version_tree.column("time", width=150)
        version_tree.column("reason", width=200)
        version_tree.column("trades", width=80)
        version_tree.column("wr", width=80)
        version_tree.pack(fill=tk.BOTH, expand=True)

        # 加载版本
        versions = self._param_manager.list_versions()
        for v in versions:
            version_tree.insert("", tk.END, values=(
                v["version_id"],
                v["created_at"],
                v["reason"],
                v.get("trades_before", "-"),
                f"{v.get('winrate_before', 0):.1%}" if v.get("winrate_before") else "-"
            ))

        # 变更历史
        ttk.Label(history_win, text="参数变更记录").pack(pady=5)

        change_frame = ttk.Frame(history_win)
        change_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        change_tree = ttk.Treeview(change_frame, columns=("time", "param", "old", "new", "source"), show="headings", height=10)
        change_tree.heading("time", text="时间")
        change_tree.heading("param", text="参数")
        change_tree.heading("old", text="原值")
        change_tree.heading("new", text="新值")
        change_tree.heading("source", text="来源")
        change_tree.column("time", width=150)
        change_tree.column("param", width=120)
        change_tree.column("old", width=80)
        change_tree.column("new", width=80)
        change_tree.column("source", width=100)
        change_tree.pack(fill=tk.BOTH, expand=True)

        # 加载变更
        changes = self._param_manager.get_change_history(limit=20)
        for c in changes:
            change_tree.insert("", tk.END, values=(
                c["timestamp"],
                c["param"],
                c["old"],
                c["new"],
                c["source"]
            ))

        # 双击版本对比性能
        def on_version_double_click(event):
            selected = version_tree.selection()
            if selected:
                item = version_tree.item(selected[0])
                version_id = item['values'][0]
                result = self._param_manager.compare_performance(version_id, self.paper_engine)
                if 'error' not in result:
                    detail_win = tk.Toplevel(history_win)
                    detail_win.title(f"性能对比 - {version_id}")
                    detail_win.geometry("400x200")

                    ttk.Label(detail_win, text=f"版本: {version_id}").pack(pady=10)

                    before = result['before']
                    after = result['after']
                    delta = result['delta']

                    text = f"""变更前: {before['trades']}笔, 胜率{before['winrate']:.1%}, PnL {before['pnl_pct']:.2f}%
变更后: {after['trades']}笔, 胜率{after['winrate']:.1%}, PnL {after['pnl_pct']:.2f}%

Δ 胜率: {delta['winrate']:+.1%}
Δ PnL: {delta['pnl_pct']:+.2f}%"""

                    ttk.Label(detail_win, text=text, justify=tk.LEFT).pack(pady=20)

        version_tree.bind('<Double-1>', on_version_double_click)

    def _rollback_version(self):
        """回滚到指定版本"""
        from bnb_quant_tool.param_manager import ParamManager

        if not hasattr(self, '_param_manager'):
            self._param_manager = ParamManager(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        versions = self._param_manager.list_versions(limit=10)
        if not versions:
            messagebox.showinfo("提示", "没有可回滚的历史版本")
            return

        # 创建选择窗口
        select_win = tk.Toplevel(self.root)
        select_win.title("选择回滚版本")
        select_win.geometry("500x350")

        ttk.Label(select_win, text="选择要回滚的版本:").pack(pady=10)

        version_tree = ttk.Treeview(select_win, columns=("id", "time", "reason"), show="headings", height=8)
        version_tree.heading("id", text="版本ID")
        version_tree.heading("time", text="创建时间")
        version_tree.heading("reason", text="原因")
        version_tree.column("id", width=150)
        version_tree.column("time", width=150)
        version_tree.column("reason", width=200)
        version_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        for v in versions:
            version_tree.insert("", tk.END, values=(
                v["version_id"],
                v["created_at"],
                v["reason"]
            ))

        def do_rollback():
            selected = version_tree.selection()
            if not selected:
                return

            item = version_tree.item(selected[0])
            version_id = item['values'][0]

            if messagebox.askyesno("确认回滚", 
                f"是否回滚到 {version_id}?\n\n当前配置会被备份"):
                success, msg = self._param_manager.rollback_to_version(version_id)
                if success:
                    messagebox.showinfo("成功", msg)
                    self.config = self._param_manager.load_config()
                    select_win.destroy()
                else:
                    messagebox.showerror("失败", msg)

        ttk.Button(select_win, text="确认回滚", command=do_rollback).pack(pady=10)

    def _show_shadow_strategies(self):
        """显示影子策略测试队列"""
        from bnb_quant_tool.strategy_mutator import StrategyMutator

        if not hasattr(self, '_strategy_mutator'):
            self._strategy_mutator = StrategyMutator(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        # 创建窗口
        shadow_win = tk.Toplevel(self.root)
        shadow_win.title("影子策略测试队列")
        shadow_win.geometry("1000x600")

        # 顶部控制区
        control_frame = ttk.Frame(shadow_win)
        control_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(control_frame, text="🔄 刷新", 
                   command=lambda: self._refresh_shadow_list(shadow_tree, "active")).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="✅ 已升级策略", 
                   command=lambda: self._refresh_shadow_list(shadow_tree, "promoted")).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="❌ 已淘汰策略", 
                   command=lambda: self._refresh_shadow_list(shadow_tree, "retired")).pack(side=tk.LEFT, padx=5)

        ttk.Button(control_frame, text="🧬 生成新变异", 
                   command=self._generate_new_mutation).pack(side=tk.RIGHT, padx=5)

        # 主列表
        list_frame = ttk.Frame(shadow_win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("id", "created", "reason", "trades", "wins", "wr", "pnl", "status")
        shadow_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)

        shadow_tree.heading("id", text="策略ID")
        shadow_tree.heading("created", text="创建时间")
        shadow_tree.heading("reason", text="变异原因")
        shadow_tree.heading("trades", text="影子交易")
        shadow_tree.heading("wins", text="盈利")
        shadow_tree.heading("wr", text="胜率")
        shadow_tree.heading("pnl", text="累计PnL")
        shadow_tree.heading("status", text="状态")

        shadow_tree.column("id", width=200)
        shadow_tree.column("created", width=150)
        shadow_tree.column("reason", width=250)
        shadow_tree.column("trades", width=80)
        shadow_tree.column("wins", width=60)
        shadow_tree.column("wr", width=70)
        shadow_tree.column("pnl", width=100)
        shadow_tree.column("status", width=80)

        shadow_tree.pack(fill=tk.BOTH, expand=True)

        # 详情区
        detail_frame = ttk.LabelFrame(shadow_win, text="策略详情")
        detail_frame.pack(fill=tk.X, padx=10, pady=5)

        detail_text = scrolledtext.ScrolledText(detail_frame, width=100, height=8, wrap=tk.WORD)
        detail_text.pack(fill=tk.X, padx=5, pady=5)

        # 双击查看详情
        def on_shadow_double_click(event):
            selected = shadow_tree.selection()
            if not selected:
                return

            item = shadow_tree.item(selected[0])
            strategy_id = item['values'][0]

            # 加载策略详情
            strategies = self._strategy_mutator.list_shadow_strategies(status="all")
            strategy = next((s for s in strategies if s["strategy_id"] == strategy_id), None)

            if strategy:
                detail_text.delete(1.0, tk.END)
                detail_text.insert(tk.END, f"策略ID: {strategy['strategy_id']}\n")
                detail_text.insert(tk.END, f"创建时间: {strategy['created_at']}\n")
                detail_text.insert(tk.END, f"变异原因: {strategy['mutation_reason']}\n")
                detail_text.insert(tk.END, f"影子交易: {strategy['total_trades']}笔\n")
                detail_text.insert(tk.END, f"胜率: {strategy['win_rate']:.1%}\n")
                detail_text.insert(tk.END, f"累计PnL: {strategy['total_pnl']:.2f}%\n")
                detail_text.insert(tk.END, f"状态: {strategy['status']}\n")

        shadow_tree.bind('<Double-1>', on_shadow_double_click)

        # 初始加载
        self._refresh_shadow_list(shadow_tree, "active")

    def _refresh_shadow_list(self, tree, status):
        """刷新影子策略列表"""
        from bnb_quant_tool.strategy_mutator import StrategyMutator

        if not hasattr(self, '_strategy_mutator'):
            self._strategy_mutator = StrategyMutator(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        strategies = self._strategy_mutator.list_shadow_strategies(status=status)

        tree.delete(*tree.get_children())
        for s in strategies:
            tree.insert("", tk.END, values=(
                s["strategy_id"],
                s["created_at"],
                s["mutation_reason"][:40] + "..." if len(s["mutation_reason"]) > 40 else s["mutation_reason"],
                s["total_trades"],
                s["wins"],
                f"{s['win_rate']:.1%}",
                f"{s['total_pnl']:.2f}%",
                s["status"]
            ))

    def _generate_new_mutation(self):
        """基于最近复盘生成新变异"""
        from bnb_quant_tool.strategy_mutator import StrategyMutator

        if not hasattr(self, '_strategy_mutator'):
            self._strategy_mutator = StrategyMutator(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        # 获取最近一次复盘结果
        # TODO: 从数据库加载最近复盘

        messagebox.showinfo("提示", "请先运行AI复盘生成变异建议")

    def _generate_strategy_mutations(self, review_result: Dict):
        """基于AI复盘结果自动生成策略变异"""
        from bnb_quant_tool.strategy_mutator import StrategyMutator

        if not hasattr(self, '_strategy_mutator'):
            self._strategy_mutator = StrategyMutator(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        # 生成变异候选
        candidates = self._strategy_mutator.generate_mutations(review_result)

        if not candidates:
            return

        # 添加到影子测试队列
        added = 0
        for c in candidates:
            if self._strategy_mutator.add_to_shadow_queue(c):
                added += 1

        if added > 0:
            self.update_status(f"已生成 {added} 个影子策略候选")

            # 更新影子策略显示
            if hasattr(self, 'shadow_strategies_text'):
                self.shadow_strategies_text.delete(1.0, tk.END)
                for c in candidates:
                    self.shadow_strategies_text.insert(tk.END, 
                        f"[{c.strategy_id}]\n")
                    self.shadow_strategies_text.insert(tk.END, 
                        f"  类型: {c.strategy_def.get('type')}\n")
                    self.shadow_strategies_text.insert(tk.END, 
                        f"  原因: {c.mutation_reason[:60]}...\n\n")

    def _run_indicator_exploration(self):
        """运行指标探索（遗传算法）"""
        from bnb_quant_tool.indicator_explorer import IndicatorExplorer

        if not hasattr(self, '_indicator_explorer'):
            self._indicator_explorer = IndicatorExplorer(
                config_path=str(CONFIG_PATH),
                learning_db_path=self.learner.db_path
            )

        # 创建进度窗口
        progress_win = tk.Toplevel(self.root)
        progress_win.title("指标探索进行中")
        progress_win.geometry("500x200")

        ttk.Label(progress_win, text="正在运行遗传算法优化...", 
                  font=('Microsoft YaHei', 12)).pack(pady=20)

        progress_var = tk.StringVar(value="初始化种群...")
        ttk.Label(progress_win, textvariable=progress_var).pack(pady=10)

        progress_bar = ttk.Progressbar(progress_win, mode='indeterminate')
        progress_bar.pack(fill=tk.X, padx=20, pady=10)
        progress_bar.start()

        result_var = {'best': None}

        def run_evolution_thread():
            try:
                best = self._indicator_explorer.run_evolution(
                    generations=20,
                    target_fitness=0.85
                )
                result_var['best'] = best
            except Exception as e:
                logger.error(f"进化失败: {e}")
                result_var['error'] = str(e)

        import threading
        thread = threading.Thread(target=run_evolution_thread, daemon=True)
        thread.start()

        def check_thread():
            if thread.is_alive():
                progress_var.set("进化中... (可能需要1-2分钟)")
                self.root.after(500, check_thread)
            else:
                progress_bar.stop()
                progress_win.destroy()

                if 'error' in result_var:
                    messagebox.showerror("错误", f"探索失败: {result_var['error']}")
                elif result_var['best']:
                    best = result_var['best']
                    messagebox.showinfo("探索完成", 
                        f"发现最优策略!\n\n"
                        f"策略ID: {best.strategy_id}\n"
                        f"适应度: {best.fitness:.3f}\n"
                        f"指标数: {len(best.genes)}\n"
                        f"表达式: {best.evaluate_expression()[:50]}...\n\n"
                        f"已保存到数据库，可查看已发现策略列表")
                else:
                    messagebox.showinfo("完成", "探索完成，未找到理想策略")

        self.root.after(500, check_thread)

    def _run_deep_learning(self):
        """运行深度学习训练和预测"""
        from bnb_quant_tool.deep_learning_engine import DeepLearningEngine

        if not hasattr(self, '_dl_engine'):
            self._dl_engine = DeepLearningEngine(
                db_path=self.learner.db_path
            )

        # 创建进度窗口
        progress_win = tk.Toplevel(self.root)
        progress_win.title("深度学习训练")
        progress_win.geometry("600x400")

        ttk.Label(progress_win, text="正在训练深度学习模型...", 
                  font=('Microsoft YaHei', 12)).pack(pady=20)

        progress_var = tk.StringVar(value="初始化...")
        ttk.Label(progress_win, textvariable=progress_var).pack(pady=10)

        progress_bar = ttk.Progressbar(progress_win, mode='indeterminate')
        progress_bar.pack(fill=tk.X, padx=20, pady=10)
        progress_bar.start()

        result_var = {'result': None}

        def run_training_thread():
            try:
                # 训练模型
                stats = self._dl_engine.train(epochs=50)

                # 获取当前市场数据预测
                market_data = {
                    'indicators': getattr(self, '_last_indicators', {}),
                    'sentiment': {}
                }
                result = self._dl_engine.predict(market_data)
                result['training_stats'] = stats
                result_var['result'] = result

            except Exception as e:
                logger.error(f"Deep learning failed: {e}")
                result_var['error'] = str(e)

        import threading
        thread = threading.Thread(target=run_training_thread, daemon=True)
        thread.start()

        def check_thread():
            if thread.is_alive():
                progress_var.set("训练中... (可能需要1-2分钟)")
                self.root.after(500, check_thread)
            else:
                progress_bar.stop()
                progress_win.destroy()

                if 'error' in result_var:
                    messagebox.showerror("错误", f"训练失败: {result_var['error']}")
                elif result_var['result']:
                    result = result_var['result']
                    stats = result.get('training_stats') or {}

                    if not stats or stats.get('final_loss') is None:
                        # 训练数据不足或训练未实际执行
                        self.update_status("⚠️ 深度学习训练跳过：训练数据不足")
                        messagebox.showwarning(
                            "训练跳过",
                            "训练数据不足，深度学习模型未更新。\n"
                            "需要更多交易记录后重试。"
                        )
                    else:
                        # v2.2: 训练成功后，将 DL 引擎连接到 TradeAdvisor
                        self.trade_advisor.set_dl_engine(self._dl_engine)
                        self.update_status(f"🧠 深度学习已接入 TradeAdvisor (权重={self.trade_advisor.dl_weight:.0%})")
                        _epochs = stats.get('epochs') or 'N/A'
                        _loss = stats.get('final_loss', 0)
                        _samples = stats.get('samples') or 0
                        _signal = result.get('signal') or 'N/A'
                        _confidence = result.get('confidence') or 0
                        _model_type = result.get('model_type') or 'N/A'
                        msg = f"""深度学习训练完成!

训练统计:
- Epochs: {_epochs}
- 最终损失: {_loss:.4f}
- 样本数: {_samples}

预测结果:
- 信号: {_signal}
- 置信度: {_confidence:.1%}
- 模型类型: {_model_type}

Top 5 关键特征:"""

                        for i, f in enumerate((result.get('feature_importance') or [])[:5], 1):
                            msg += f"\n{i}. {f['feature']}: {f['value']:.3f}"

                        msg += "\n\n模型已保存到 ai_learning.db"
                        messagebox.showinfo("训练完成", msg)

        self.root.after(500, check_thread)

