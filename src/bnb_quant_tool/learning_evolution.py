"""
学习进化协调器 — 每次分析/交易后闭环强化 AI 决策能力。

职责：
1. 记录并验证注入的知识卡片
2. 反事实结果入库
3. 因子归因学习
4. 平仓后 AI 反思 / 周期元学习
5. 参数影子 A/B、Walk-Forward / Monte Carlo 守门
6. StrategyLab 策略晋升流水线
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUALITY_TIER_MULTIPLIER: Dict[str, float] = {
    "A": 1.0,
    "B": 0.85,
    "C": 0.6,
    "D": 0.3,
}

# 决策解释器展示名 → 归因键
FACTOR_NAME_TO_KEY: Dict[str, str] = {
    "AI 方向一致": "ai_direction",
    "AI 方向冲突": "ai_direction",
    "AI 方向中立": "ai_direction",
    "机构策略共识": "institutional_consensus",
    "机构投票比": "institutional_vote_ratio",
    "RSI 信号": "rsi_signal",
    "MACD 信号": "macd_signal",
    "多周期共振": "multi_timeframe",
    "新闻情绪": "news_sentiment",
    "市场情绪": "market_sentiment",
    "Launchpool 质押": "bnb_launchpool",
    "BNB Alpha": "bnb_alpha",
    "监管 NLP": "bnb_regulatory_nlp",
    "事件周期": "bnb_event_cycle",
    "资金费率极值": "bnb_funding_extreme",
    "BNB/BTC 弱势": "bnb_btc_weakness",
    "学习成熟度": "learning_maturity",
    "历史胜率": "historical_accuracy",
    "风险回报比": "risk_reward",
    "波动率": "volatility",
}


class LearningEvolutionCoordinator:
    """统一编排分析后 / 平仓后的进化步骤。"""

    def __init__(
        self,
        learner,
        capability_memory=None,
        counterfactual=None,
        config: Optional[Dict] = None,
    ):
        self.learner = learner
        self.memory = capability_memory or getattr(learner, "capability_memory", None)
        self.counterfactual = counterfactual
        self.config = config or getattr(learner, "config", {}) or {}
        self.ev_cfg = self.config.get("learning_evolution") or {}

    def _should_use_ai_extract(self) -> bool:
        """平仓/反思是否调用大模型提炼（对齐 capability_memory.extract_and_save_from_trade）。"""
        if self.memory is None:
            return False
        if bool(getattr(self.memory, "use_ai_extract", False)):
            return True
        learn_cfg = (self.config or {}).get("learning") or {}
        return bool(learn_cfg.get("use_ai_extract_on_close", False))

    def on_analysis_recorded(
        self,
        record_id: int,
        learning_context: Optional[Dict[str, Any]] = None,
        trade_advice: Optional[Dict[str, Any]] = None,
        ai_analysis: Optional[Dict[str, Any]] = None,
    ) -> None:
        """分析入库后记录本次注入的知识卡片 ID + 影子参数 gate 对比。"""
        if record_id and learning_context and self.memory:
            cards = learning_context.get("capability_cards") or []
            card_ids = [int(c["id"]) for c in cards if c.get("id")]
            if card_ids:
                self.memory.record_injected_cards(int(record_id), card_ids)

        if (
            self.ev_cfg.get("shadow_param_ab", True)
            and trade_advice
            and ai_analysis
            and record_id
        ):
            try:
                from bnb_quant_tool.shadow_param_evaluator import ShadowParamEvaluator
                spe = ShadowParamEvaluator(self.learner.db_path, self.config)
                spe.evaluate_analysis(
                    int(record_id), trade_advice, ai_analysis, learning_context
                )
            except Exception as e:
                logger.debug("shadow gate evaluate on analysis: %s", e)

    def on_trade_closed(
        self,
        *,
        position_id: int,
        trade_row: Dict[str, Any],
        record_id: int,
        outcome: str,
        quality: Optional[Dict[str, Any]] = None,
        cf_result: Optional[Dict[str, Any]] = None,
        decision_explanation: Optional[Dict[str, Any]] = None,
        regime: Optional[str] = None,
    ) -> Dict[str, Any]:
        """平仓后完整进化流水线（同步部分）。"""
        summary: Dict[str, Any] = {"record_id": record_id, "position_id": position_id}
        if not self.learner or not record_id:
            return summary

        if cf_result and self.memory:
            try:
                from bnb_quant_tool.capability_memory import load_analysis_record

                analysis_rec = load_analysis_record(self.learner, int(record_id))
                n = self.memory.save_counterfactual_lesson(
                    cf_result, trade_row, analysis_rec
                )
                summary["cf_cards"] = n
            except Exception as e:
                logger.warning("counterfactual lesson failed: %s", e)

        try:
            self._maybe_run_meta_consolidation()
        except Exception as e:
            logger.warning("meta consolidation check failed: %s", e)

        try:
            self._evaluate_shadow_param_trials()
        except Exception as e:
            logger.warning("shadow param evaluation failed: %s", e)

        try:
            self._maybe_process_batch_reflection(
                record_id=int(record_id),
                position_id=int(position_id),
                outcome=outcome,
            )
        except Exception as e:
            logger.warning("batch reflection failed: %s", e)

        return summary

    def on_trade_closed_async(
        self,
        *,
        position_id: int,
        trade_row: Dict[str, Any],
        record_id: int,
        outcome: str,
        quality: Optional[Dict[str, Any]] = None,
        decision_explanation: Optional[Dict[str, Any]] = None,
        regime: Optional[str] = None,
    ) -> None:
        """后台：平仓后 AI 结构化反思。"""
        if not self.memory or not self.ev_cfg.get("post_trade_reflection", True):
            return
        if not self._should_use_ai_extract() or not self.memory.extractor.available:
            return

        from bnb_quant_tool.capability_memory import (
            extract_knowledge_async,
            load_analysis_record,
        )

        analysis_rec = load_analysis_record(self.learner, int(record_id))
        extract_knowledge_async(
            self.memory,
            "extract_post_trade_reflection",
            trade_row={**trade_row, "id": position_id, "learning_record_id": record_id},
            analysis_record=analysis_rec,
            outcome=outcome,
            quality=quality,
        )

    def queue_shadow_param_change(
        self,
        param_name: str,
        baseline_value: float,
        shadow_value: float,
        reason: str = "",
        source: str = "AI_REVIEW",
    ) -> Optional[int]:
        """参数变更先入影子队列，积累样本后再晋升。"""
        if not self.ev_cfg.get("shadow_param_ab", True):
            return None
        try:
            conn = self.learner._get_conn()
            cur = conn.cursor()
            now = datetime.now().isoformat()
            cur.execute(
                """
                INSERT INTO shadow_param_trials
                (timestamp, param_name, baseline_value, shadow_value,
                 status, trades_observed, baseline_wins, shadow_wins, source, reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now, param_name, baseline_value, shadow_value,
                    "active", 0, 0, 0, source, reason[:500],
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception as e:
            logger.warning("shadow param queue failed: %s", e)
            return None

    def walk_forward_gate(
        self,
        trades: List[Dict[str, Any]],
        min_samples: Optional[int] = None,
    ) -> Dict[str, Any]:
        """简易 Walk-Forward：前半训练胜率 vs 后半测试胜率。"""
        min_n = int(min_samples or self.ev_cfg.get("walk_forward_min_samples", 50))
        if len(trades) < min_n:
            return {
                "passed": False,
                "reason": f"样本不足 ({len(trades)}/{min_n})",
                "train_wr": None,
                "test_wr": None,
            }

        mid = len(trades) // 2
        train = trades[:mid]
        test = trades[mid:]

        def _wr(batch: List[Dict]) -> float:
            wins = sum(1 for t in batch if float(t.get("realized_pnl_usdt") or 0) > 0)
            return wins / len(batch) if batch else 0.0

        train_wr = _wr(train)
        test_wr = _wr(test)
        passed = test_wr >= train_wr * 0.75 - 0.05
        return {
            "passed": passed,
            "train_wr": round(train_wr, 4),
            "test_wr": round(test_wr, 4),
            "train_n": len(train),
            "test_n": len(test),
            "reason": "通过" if passed else "测试段胜率显著低于训练段",
        }

    def monte_carlo_gate(
        self,
        trades: List[Dict[str, Any]],
        counterfactual=None,
    ) -> Dict[str, Any]:
        """蒙特卡洛压力测试守门。"""
        cf = counterfactual or self.counterfactual
        if cf is None:
            return {"passed": True, "reason": "no_counterfactual_analyzer"}
        mc_cfg = (self.config.get("counterfactual") or {}).get("monte_carlo") or {}
        return cf.monte_carlo_stress_test(
            trades=trades,
            n_simulations=int(mc_cfg.get("n_simulations", 100)),
            price_jitter_pct=float(mc_cfg.get("price_jitter_pct", 0.01)),
            min_pass_rate=float(mc_cfg.get("min_pass_rate", 0.65)),
        )

    def promotion_gate(
        self,
        trades: List[Dict[str, Any]],
        counterfactual=None,
    ) -> Dict[str, Any]:
        """Walk-Forward + Monte Carlo 组合守门。"""
        wf = self.walk_forward_gate(trades)
        mc = self.monte_carlo_gate(trades, counterfactual=counterfactual)
        passed = bool(wf.get("passed")) and bool(mc.get("passed"))
        return {
            "passed": passed,
            "walk_forward": wf,
            "monte_carlo": mc,
        }

    def learning_writeback_gate(
        self,
        param_name: str = "",
        *,
        new_value: Any = None,
        old_value: Any = None,
    ) -> Dict[str, Any]:
        """改参/收紧前联合守门：滚动 E[R] + 开仓密度。

        开仓饥饿时禁止继续抬高门槛类参数。
        """
        ev = self.ev_cfg or {}
        result: Dict[str, Any] = {"passed": True, "reasons": []}
        lookback = int(ev.get("open_density_lookback_days", 7) or 7)
        try:
            from bnb_quant_tool.trading_profile import auto_expectancy_stats

            stats = auto_expectancy_stats(
                lookback_days=lookback,
                auto_only=True,
            )
        except Exception as e:
            result["warning"] = str(e)
            return result

        result["stats"] = stats
        n = int(stats.get("n") or 0)
        er = float(stats.get("expectancy_r") or 0.0)
        dens = float(stats.get("opens_per_day") or 0.0)
        min_dens = float(ev.get("min_open_density_per_day", 0.3) or 0.3)
        require_pos = bool(ev.get("require_positive_expectancy_to_tighten", True))
        block_starve = bool(ev.get("block_tighten_when_open_starved", True))
        tighten_params = {
            str(x) for x in (ev.get("tighten_params") or [
                "min_open_confidence",
                "long_min_confidence",
                "min_net_rr",
                "gate_tightening",
                "confidence_threshold",
            ])
        }
        is_tighten = str(param_name) in tighten_params
        try:
            if old_value is not None and new_value is not None:
                if float(new_value) > float(old_value):
                    is_tighten = is_tighten or str(param_name) in tighten_params
        except (TypeError, ValueError):
            pass

        if block_starve and is_tighten and dens < min_dens:
            result["passed"] = False
            result["reasons"].append(
                f"open_starved dens={dens:.3f}/day < {min_dens} (n={n})"
            )
        if require_pos and is_tighten and n >= 8 and er <= 0:
            result["passed"] = False
            result["reasons"].append(
                f"expectancy_r={er:.3f}<=0 with n={n}; refuse further tighten"
            )
        return result

    def promote_strategy_lab_candidates(
        self,
        paper_db_path: Optional[str] = None,
        min_live_trades: int = 15,
    ) -> Dict[str, Any]:
        """根据模拟盘表现 + PromotionFunnel 阶段机晋升 StrategyLab 策略。"""
        if not self.ev_cfg.get("strategy_lab_auto_promote", True):
            return {"status": "disabled"}

        try:
            from bnb_quant_tool.strategy_lab import StrategyLab
            from bnb_quant_tool.promotion_funnel import (
                PromotionFunnel,
                load_paper_stats_for_funnel,
            )

            db_path = self.learner.db_path
            specs = StrategyLab.load_discovered(db_path=db_path)
            if not specs:
                return {"status": "empty", "promoted": []}

            trades = self._load_closed_paper_trades(paper_db_path, limit=80)
            gate = self.promotion_gate(trades)
            if not gate.get("passed") and len(trades) >= int(
                self.ev_cfg.get("walk_forward_min_samples", 50)
            ):
                return {"status": "blocked", "gate": gate, "promoted": []}

            paper_stats = load_paper_stats_for_funnel(
                paper_db_path, config=self.config
            )
            # 二次验证：纸面胜率相对回测不可崩塌
            second_val = bool(self.ev_cfg.get("strategy_lab_second_validation", True))
            live_wr = self._paper_live_win_rate(paper_db_path)
            if second_val and live_wr is not None and paper_stats:
                paper_stats = dict(paper_stats)
                paper_stats["win_rate"] = live_wr

            funnel = PromotionFunnel(self.config, db_path=db_path)
            result = funnel.promote_eligible_to_voting_pool(
                specs,
                paper_stats=paper_stats,
                db_path=db_path,
            )
            result["gate"] = gate
            if live_wr is not None:
                result["live_win_rate"] = round(live_wr, 4)
            return result
        except Exception as e:
            logger.warning("strategy lab promote failed: %s", e)
            return {"status": "error", "error": str(e), "promoted": []}

    def _maybe_run_meta_consolidation(self) -> Optional[Dict[str, Any]]:
        if not self.memory or not self.ev_cfg.get("meta_consolidation", True):
            return None
        min_cards = int(self.ev_cfg.get("meta_consolidation_min_cards", 40))
        interval_days = int(self.ev_cfg.get("meta_consolidation_interval_days", 7))
        count = self.memory.count_active_cards()
        if count < min_cards:
            return None

        conn = self.learner._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT timestamp FROM meta_learning_log ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                last = datetime.fromisoformat(str(row[0]))
                if datetime.now() - last < timedelta(days=interval_days):
                    return None
            except ValueError:
                pass

        result = self.memory.consolidate_knowledge_cards()
        if result.get("merged_count", 0) > 0:
            cur.execute(
                """
                INSERT INTO meta_learning_log
                (timestamp, cards_before, cards_after, merged_count, summary)
                VALUES (?,?,?,?,?)
                """,
                (
                    datetime.now().isoformat(),
                    result.get("cards_before", 0),
                    result.get("cards_after", 0),
                    result.get("merged_count", 0),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.commit()
        return result

    def _evaluate_shadow_param_trials(self) -> None:
        """用真 gate 重算结果晋升/拒绝影子参数。"""
        if not self.ev_cfg.get("shadow_param_ab", True):
            return
        try:
            from bnb_quant_tool.shadow_param_evaluator import ShadowParamEvaluator
            spe = ShadowParamEvaluator(self.learner.db_path, self.config)
            min_obs = int(self.ev_cfg.get("shadow_param_min_trades", 8))
            result = spe.finalize_trials(min_observations=min_obs)
            if result.get("promoted") or result.get("rejected"):
                logger.info("Shadow param finalize: %s", result)
        except Exception as e:
            logger.warning("shadow finalize failed: %s", e)

    def _maybe_process_batch_reflection(
        self,
        *,
        record_id: int,
        position_id: int,
        outcome: str,
    ) -> Optional[Dict[str, Any]]:
        """积累足够样本后批量反思（规则优先，可选 AI）。"""
        if not self.ev_cfg.get("batch_reflection", True):
            return None
        if not self.memory:
            return None

        batch_size = int(self.ev_cfg.get("batch_reflection_size", 5))
        self.learner.queue_reflection(record_id, position_id, outcome)
        return self.drain_pending_reflections(force=False, batch_size=batch_size)

    def drain_pending_reflections(
        self,
        *,
        force: bool = False,
        batch_size: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """消化积压反思队列：满 batch 或 force 时处理；无 AI 时用规则提炼。"""
        if not self.ev_cfg.get("batch_reflection", True):
            return None
        if not self.memory:
            return None

        size = int(batch_size or self.ev_cfg.get("batch_reflection_size", 5) or 5)
        pending = self.learner.get_pending_reflection_count()
        if pending <= 0:
            return None
        if not force and pending < size:
            return None

        take = pending if force else size
        take = max(1, min(int(take), 20))

        conn = self.learner._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, record_id, position_id, outcome FROM reflection_queue "
            "WHERE status='pending' ORDER BY id ASC LIMIT ?",
            (take,),
        )
        raw_rows = cur.fetchall()
        rows = []
        for r in raw_rows:
            if isinstance(r, dict):
                rows.append(r)
            elif hasattr(r, "keys"):
                rows.append({k: r[k] for k in r.keys()})
            else:
                rows.append({
                    "id": r[0],
                    "record_id": r[1],
                    "position_id": r[2],
                    "outcome": r[3],
                })
        if not rows:
            return None
        if not force and len(rows) < size:
            return None

        from bnb_quant_tool.capability_memory import (
            extract_knowledge_async,
            load_analysis_record,
        )

        use_ai = self._should_use_ai_extract()
        processed = 0
        for row in rows:
            rid = int(row["record_id"] or 0)
            pid = int(row["position_id"] or 0)
            oc = str(row.get("outcome") or "")
            if not rid:
                cur.execute(
                    "UPDATE reflection_queue SET status='processed', processed_at=? WHERE id=?",
                    (datetime.now().isoformat(), int(row["id"])),
                )
                continue
            analysis_rec = load_analysis_record(self.learner, rid)
            trade_row = {"id": pid, "learning_record_id": rid, "outcome": oc}
            method = (
                "extract_post_trade_reflection"
                if use_ai
                else "extract_and_save_from_trade"
            )
            kwargs = {
                "trade_row": trade_row,
                "analysis_record": analysis_rec,
                "outcome": oc,
                "quality": None,
            }
            try:
                extract_knowledge_async(self.memory, method, **kwargs)
            except Exception as e:
                logger.debug("drain reflection extract failed: %s", e)
                # 同步规则兜底，保证「每笔必学」
                try:
                    if method == "extract_and_save_from_trade":
                        self.memory.extract_and_save_from_trade(
                            trade_row, analysis_rec, oc, None
                        )
                    else:
                        self.memory.extract_post_trade_reflection(
                            trade_row, analysis_rec, oc, None
                        )
                except Exception as e2:
                    logger.debug("drain reflection sync fallback: %s", e2)
            cur.execute(
                "UPDATE reflection_queue SET status='processed', processed_at=? "
                "WHERE id=?",
                (datetime.now().isoformat(), int(row["id"])),
            )
            processed += 1
        conn.commit()
        if processed:
            logger.info(
                "drained reflection_queue: processed=%s pending_was=%s ai=%s",
                processed, pending, use_ai,
            )
        return {"processed": processed, "batch_size": size, "forced": force}

    def _paper_live_win_rate(self, paper_db_path: Optional[str]) -> Optional[float]:
        trades = self._load_closed_paper_trades(paper_db_path, limit=40)
        if len(trades) < int(self.ev_cfg.get("strategy_lab_min_live_trades", 15)):
            return None
        wins = sum(1 for t in trades if float(t.get("realized_pnl_usdt") or 0) > 0)
        return wins / len(trades) if trades else None

    def _apply_shadow_param(
        self,
        param_name: str,
        baseline: float,
        shadow: float,
        shadow_wr: float,
        baseline_wr: float,
    ) -> None:
        try:
            gate = self.learning_writeback_gate(
                param_name, new_value=shadow, old_value=baseline
            )
            if not gate.get("passed"):
                logger.warning(
                    "Shadow param blocked by writeback gate: %s %s",
                    param_name,
                    gate.get("reasons"),
                )
                return
            from bnb_quant_tool.param_manager import ParamManager

            pm = ParamManager(
                config_path=str(
                    self.config.get("_config_path")
                    or ParamManager.resolve_config_path()
                ),
                learning_db_path=self.learner.db_path,
            )
            ok, msg = pm.set_param_value(param_name, shadow)
            if ok:
                logger.info(
                    "Shadow param promoted: %s %.4f→%.4f (wr %.1f%% vs %.1f%%)",
                    param_name, baseline, shadow, shadow_wr * 100, baseline_wr * 100,
                )
        except Exception as e:
            logger.warning("apply shadow param failed: %s", e)

    @staticmethod
    def _load_closed_paper_trades(
        paper_db_path: Optional[str],
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        if not paper_db_path:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                paper_db_path = str(get_localized_db_path("paper_trading"))
            except ImportError:
                return []
        try:
            conn = sqlite3.connect(paper_db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM paper_positions
                WHERE status='CLOSED'
                ORDER BY closed_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []


def extract_factor_scores(explanation: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """从 decision_explainer 输出提取因子分数字典。"""
    if not explanation:
        return {}
    scores: Dict[str, int] = {}
    for factor in explanation.get("factors") or []:
        name = str(factor.get("name") or "")
        key = FACTOR_NAME_TO_KEY.get(name)
        if not key:
            for prefix, k in FACTOR_NAME_TO_KEY.items():
                if name.startswith(prefix.split(" ")[0]):
                    key = k
                    break
        if not key:
            continue
        score = int(factor.get("score") or 0)
        scores[key] = scores.get(key, 0) + score
    return scores
