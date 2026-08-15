"""Mixin: LearningLoopMixin"""

from gui._imports import *


class LearningLoopMixin:
    def _get_learning_evolution(self):
        if getattr(self, "learning_evolution", None) is not None:
            return self.learning_evolution
        try:
            from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
            self.learning_evolution = LearningEvolutionCoordinator(
                self.learner,
                capability_memory=self.learner.capability_memory,
                counterfactual=getattr(self, "counterfactual", None),
                config=self.config,
            )
            return self.learning_evolution
        except Exception as e:
            logger.warning(f"LearningEvolutionCoordinator init failed: {e}")
            return None

    def _auto_submit_learning_feedback(self, pid):
        """模拟盘平仓后自动将结果回填到 AI 学习系统（统一管道）。"""
        try:
            if pid is None:
                return
            from bnb_quant_tool.trade_close_learning import (
                TradeCloseLearningDeps,
                process_trade_close,
            )

            evolution = self._get_learning_evolution()
            deps = TradeCloseLearningDeps(
                learner=self.learner,
                config=self.config,
                get_position_row=self.paper_engine._get_position_row,
                counterfactual=getattr(self, "counterfactual", None),
                pattern_memory=getattr(self, "pattern_memory", None),
                evolution=evolution,
                paper_engine=self.paper_engine,
                on_status=self.update_status,
            )
            result = process_trade_close(int(pid), deps)
            if result.feedback_ok:
                try:
                    from bnb_quant_tool.intelligence_loop import get_or_create_loop
                    loop = get_or_create_loop(self, learner=self.learner, config=self.config)
                    loop.mark_reflect(
                        loop.last_report,
                        position_id=int(pid),
                        outcome=result.outcome,
                        progressed=result.progressed,
                    )
                except Exception:
                    pass
            if result.feedback_ok and hasattr(self, "_refresh_learning_evolution_views"):
                self.root.after(0, self._refresh_learning_evolution_views)
            if result.feedback_ok and hasattr(self, "_refresh_learning_dashboard"):
                self.root.after(0, self._refresh_learning_dashboard)
            if result.knowledge_queued:
                self.root.after(12000, self._refresh_knowledge_cards)
                if hasattr(self, "_refresh_learning_evolution_views"):
                    self.root.after(12000, self._refresh_learning_evolution_views)
        except Exception as e:
            logger.warning(f"_auto_submit_learning_feedback 异常: {e}")

    def _run_counterfactual(self, pid):
        """平仓后执行反事实分析（主流程已在 feedback 中统一处理）"""
        try:
            if pid is None:
                return
            row = self.paper_engine._get_position_row(int(pid))
            if row is None:
                return
            symbol = self.config.get('trading', {}).get('symbol', 'BNBUSDT')
            cf_result = self.counterfactual.analyze(row, symbol=symbol)
            if cf_result and cf_result.get("best_scenario"):
                best = cf_result["best_scenario"]
                score = cf_result["decision_score"]
                self.update_status(
                    f"🔄 反事实分析 #{pid}: 最优={best} 评分={score:.0f}/100"
                )
        except Exception as e:
            logger.warning(f"_run_counterfactual 异常: {e}")

    def _run_unified_auto_review(self):
        """统一自动复盘：优先富数据 payload，写入 param_change_log 后可 auto_apply。"""
        engine = getattr(self, "ai_review_engine", None)
        if not engine:
            return {"status": "skipped", "reason": "no_review_engine"}

        symbol = (
            self.config.get("trading", {}).get("symbol", "BNBUSDT")
            .replace("USDT", "")
        )
        if hasattr(self, "_build_enriched_review_payload"):
            try:
                payload = self._build_enriched_review_payload(max_trades=50)
                return engine.run_enriched_review(payload, symbol=symbol)
            except Exception as e:
                logger.warning(f"enriched review failed, fallback to run_review: {e}")
        return engine.run_review()

    def _maybe_trigger_auto_review(self):
        """平仓后检查是否达到自动复盘阈值"""
        try:
            if not self.ai_review_engine:
                return

            should, reason = self.ai_review_engine.should_trigger_review(self.paper_engine)
            if not should:
                return

            if self._auto_review_running:
                self.update_status(f"自动复盘: 上一轮未结束, 跳过")
                return
            if hasattr(self.ai_review_engine, "try_begin_review"):
                if not self.ai_review_engine.try_begin_review():
                    self.update_status("自动复盘: 引擎侧上一轮未结束, 跳过")
                    return

            self._auto_review_running = True
            self.update_status(f"✨ {reason}, 触发自动 AI 复盘...")

            def _worker():
                try:
                    result = self._run_unified_auto_review()
                    if result.get("status") == "success":
                        trades_n = result.get('trades_analyzed', 0)
                        stats = self.paper_engine.get_stats()
                        closed_n = stats.get('total_closed_trades', 0)
                        if hasattr(self.ai_review_engine, "mark_review_triggered"):
                            self.ai_review_engine.mark_review_triggered(
                                total_closed=closed_n,
                                streak=("连续" in str(reason)),
                            )
                        else:
                            self.ai_review_engine._last_review_at_count = closed_n

                        applied_list = []
                        if self.ai_review_engine.auto_apply:
                            apply_result = self.ai_review_engine.auto_apply_after_review(result)
                            applied_list = apply_result.get('applied', [])

                            if applied_list:
                                try:
                                    from bnb_quant_tool.config_access import load_app_config
                                    self.config = load_app_config(CONFIG_PATH)
                                except Exception:
                                    pass

                        try:
                            from bnb_quant_tool.training_loop import after_successful_review

                            after_successful_review(
                                paper_engine=self.paper_engine,
                                learner=self.learner,
                                evolution=self._get_learning_evolution(),
                                review_result=result,
                                config=self.config,
                                project_root=PROJECT_ROOT,
                                config_path=str(CONFIG_PATH),
                            )
                        except Exception as pe:
                            logger.debug(f"training_loop after review: {pe}")

                        def _ui_update():
                            ai_result = result.get('ai_result', {})
                            msg = f"📊 AI自动复盘完成\n"
                            msg += f"分析交易: {trades_n}笔\n"
                            msg += f"胜率: {result.get('win_rate', 0):.1%}\n"
                            msg += f"\n🔍 亏损规律:\n"
                            for p in (ai_result.get('loss_patterns') or ai_result.get('mistakes') or [])[:3]:
                                msg += f"  - {p}\n"
                            msg += f"\n✅ 盈利规律:\n"
                            for p in (ai_result.get('win_patterns') or ai_result.get('what_works') or [])[:3]:
                                msg += f"  - {p}\n"
                            sl_hint = ai_result.get('sl_diagnosis')
                            if not sl_hint:
                                sl_diag = ai_result.get('sl_tp_diagnosis') or {}
                                sl_hint = sl_diag.get('hint') if isinstance(sl_diag, dict) else sl_diag
                            msg += f"\nSL诊断: {sl_hint or 'N/A'}\n"

                            if applied_list:
                                msg += f"\n🧠 自动学习: 应用了{len(applied_list)}项参数优化\n"
                                for a in applied_list:
                                    msg += f"  ✅ {a['param']} = {a['new_value']} ({a['reason']})\n"
                            else:
                                msg += "\n无参数调整建议\n"

                            msg += f"\n新策略思路: {ai_result.get('new_strategy_idea') or ai_result.get('next_focus', 'N/A')}\n"
                            msg += f"AI置信度: {ai_result.get('confidence', 0):.0%}"

                            if hasattr(self, 'review_result_text'):
                                self.review_result_text.delete('1.0', tk.END)
                                self.review_result_text.insert(tk.END, msg)

                            status = f"✅ 自动复盘完成 ({trades_n}笔"
                            if applied_list:
                                status += f", 自学习{len(applied_list)}项)"
                            else:
                                status += ")"
                            self.update_status(status)
                            self.review_status_var.set(status)
                            if hasattr(self, "_refresh_learning_evolution_views"):
                                self._refresh_learning_evolution_views()

                        self.root.after(0, _ui_update)

                        try:
                            import json as _json
                            from datetime import datetime as _dt
                            log_path = str(PROJECT_ROOT / "auto_review.log")
                            with open(log_path, "a", encoding="utf-8") as _f:
                                _f.write("=" * 70 + "\n")
                                _f.write(f"[{_dt.now().isoformat(timespec='seconds')}] 自动复盘 {trades_n}笔, 应用{len(applied_list)}项\n")
                                _f.write(_json.dumps(result, ensure_ascii=False, indent=2))
                                _f.write("\n\n")
                        except Exception:
                            pass

                    elif result.get("status") == "skipped":
                        self.root.after(0, lambda: self.update_status(f"AI复盘跳过: {result.get('reason', '数据不足')}"))
                except Exception as e:
                    self.root.after(0, lambda: self.update_status(f"自动复盘失败: {e}"))
                finally:
                    self._auto_review_running = False
                    try:
                        if hasattr(self.ai_review_engine, "end_review"):
                            self.ai_review_engine.end_review()
                    except Exception:
                        pass

            threading.Thread(target=_worker, daemon=True).start()
        except Exception as e:
            self.update_status(f"自动复盘检查异常: {e}")
            self._auto_review_running = False
            try:
                if self.ai_review_engine and hasattr(self.ai_review_engine, "end_review"):
                    self.ai_review_engine.end_review()
            except Exception:
                pass
