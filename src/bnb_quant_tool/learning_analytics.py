"""
学习成效分析 — 市场状态分桶权重、盈利曲线对比、重复亏损模式识别与门控收紧。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 细粒度 regime → 三大桶（趋势 / 震荡 / 高波动）
REGIME_BUCKETS: Dict[str, str] = {
    "TRENDING": "TREND",
    "EUPHORIA": "TREND",
    "RANGING": "RANGE",
    "LOW_VOLATILITY": "RANGE",
    "HIGH_VOLATILITY": "VOLATILE",
    "PANIC": "VOLATILE",
    "NEWS_DRIVEN": "VOLATILE",
}

BUCKET_LABELS = {
    "TREND": "趋势市",
    "RANGE": "震荡市",
    "VOLATILE": "高波动",
    "GLOBAL": "全局",
}

GATE_STATE_FILENAME = "learning_gate_state.json"


def normalize_regime_bucket(regime: Optional[str]) -> str:
    if not regime:
        return "GLOBAL"
    r = str(regime).upper().strip()
    return REGIME_BUCKETS.get(r, r if r in BUCKET_LABELS else "GLOBAL")


def _gate_state_path(project_root: Optional[Path] = None) -> Path:
    if project_root is None:
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            return get_localized_db_path("ai_learning").parent / GATE_STATE_FILENAME
        except ImportError:
            return Path("data") / GATE_STATE_FILENAME
    return Path(project_root) / "data" / GATE_STATE_FILENAME


def get_session_gate_boost(project_root: Optional[Path] = None) -> float:
    state = load_gate_state(project_root)
    if not state:
        return 0.0
    remaining = int(state.get("trades_remaining") or 0)
    if remaining <= 0:
        return 0.0
    try:
        return float(state.get("gate_tightening_boost") or 0.0)
    except Exception:
        return 0.0


def tick_session_gate(project_root: Optional[Path] = None) -> None:
    """每次分析后递减 session 门控剩余笔数，归零则自动解除。"""
    state = load_gate_state(project_root)
    if not state or "trades_remaining" not in state:
        return
    remaining = int(state.get("trades_remaining") or 0) - 1
    if remaining <= 0:
        clear_gate_state(project_root)
        return
    state["trades_remaining"] = remaining
    save_gate_state(state, project_root)


def clear_gate_state(project_root: Optional[Path] = None) -> None:
    path = _gate_state_path(project_root)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def load_gate_state(project_root: Optional[Path] = None) -> Dict[str, Any]:
    path = _gate_state_path(project_root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_gate_state(state: Dict[str, Any], project_root: Optional[Path] = None) -> Path:
    path = _gate_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class LearningAnalytics:
    """学习仪表盘数据聚合。"""

    def __init__(self, learner, paper_engine=None, pattern_memory=None):
        self.learner = learner
        self.paper_engine = paper_engine
        self.pattern_memory = pattern_memory

    def get_regime_bucket_weights(self) -> List[Dict[str, Any]]:
        """按趋势/震荡/高波动桶展示策略权重。"""
        conn = self.learner._get_conn()
        cur = conn.cursor()
        rows: List[Dict[str, Any]] = []

        cur.execute(
            """
            SELECT regime, strategy_name, total_predictions, correct_predictions,
                   win_rate, weight
            FROM strategy_regime_performance
            WHERE total_predictions >= 1
            ORDER BY regime, win_rate DESC
            """
        )
        for r in cur.fetchall():
            regime = r[0]
            bucket = normalize_regime_bucket(regime)
            rows.append({
                "regime": regime,
                "bucket": bucket,
                "bucket_label": BUCKET_LABELS.get(bucket, bucket),
                "strategy": r[1],
                "total": int(r[2] or 0),
                "correct": int(r[3] or 0),
                "win_rate": float(r[4] or 0),
                "weight": float(r[5] or 0),
            })

        if rows:
            return rows

        global_w = self.learner._load_strategy_weights()
        for name, w in sorted(global_w.items(), key=lambda x: -x[1])[:13]:
            rows.append({
                "regime": "GLOBAL",
                "bucket": "GLOBAL",
                "bucket_label": "全局(待分桶样本)",
                "strategy": name,
                "total": 0,
                "correct": 0,
                "win_rate": 0.0,
                "weight": w,
            })
        return rows

    def get_profit_curve_comparison(self) -> Dict[str, Any]:
        """学习前后胜率对比 + 累计盈利曲线点。"""
        result: Dict[str, Any] = {
            "feedback_early_wr": None,
            "feedback_late_wr": None,
            "feedback_delta": None,
            "feedback_n": 0,
            "paper_early_wr": None,
            "paper_late_wr": None,
            "paper_delta": None,
            "paper_n": 0,
            "curve_points": [],
            "curve_text": "",
        }

        conn = self.learner._get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp, actual_result, pnl_percent
            FROM analysis_records
            WHERE actual_result IS NOT NULL
            ORDER BY timestamp ASC
            """
        )
        feedback_rows = cur.fetchall()
        if len(feedback_rows) >= 4:
            mid = len(feedback_rows) // 2
            early = feedback_rows[:mid]
            late = feedback_rows[mid:]
            e_wr = sum(1 for r in early if r[1] == "WIN") / len(early)
            l_wr = sum(1 for r in late if r[1] == "WIN") / len(late)
            result["feedback_early_wr"] = round(e_wr, 4)
            result["feedback_late_wr"] = round(l_wr, 4)
            result["feedback_delta"] = round(l_wr - e_wr, 4)
            result["feedback_n"] = len(feedback_rows)

            cum_wins = 0
            points = []
            for i, row in enumerate(feedback_rows, 1):
                if row[1] == "WIN":
                    cum_wins += 1
                points.append({
                    "i": i,
                    "cum_wr": round(cum_wins / i, 4),
                    "pnl_pct": row[2],
                })
            result["curve_points"] = points
            result["curve_text"] = _format_wr_sparkline(points, label="反馈样本累计胜率")

        paper_path = None
        if self.paper_engine and getattr(self.paper_engine, "db_path", None):
            paper_path = self.paper_engine.db_path
        else:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                paper_path = str(get_localized_db_path("paper_trading"))
            except ImportError:
                paper_path = None

        if paper_path and Path(paper_path).is_file():
            try:
                pconn = sqlite3.connect(paper_path, timeout=5)
                pconn.row_factory = sqlite3.Row
                prows = pconn.execute(
                    """
                    SELECT closed_at, realized_pnl_usdt
                    FROM paper_positions
                    WHERE status='CLOSED' AND closed_at IS NOT NULL
                    ORDER BY closed_at ASC
                    """
                ).fetchall()
                pconn.close()
                if len(prows) >= 4:
                    mid = len(prows) // 2
                    early_p = prows[:mid]
                    late_p = prows[mid:]
                    e_wr = sum(1 for r in early_p if float(r["realized_pnl_usdt"] or 0) > 0) / len(early_p)
                    l_wr = sum(1 for r in late_p if float(r["realized_pnl_usdt"] or 0) > 0) / len(late_p)
                    result["paper_early_wr"] = round(e_wr, 4)
                    result["paper_late_wr"] = round(l_wr, 4)
                    result["paper_delta"] = round(l_wr - e_wr, 4)
                    result["paper_n"] = len(prows)

                    if not result["curve_points"]:
                        cum_wins = 0
                        points = []
                        for i, row in enumerate(prows, 1):
                            if float(row["realized_pnl_usdt"] or 0) > 0:
                                cum_wins += 1
                            points.append({"i": i, "cum_wr": round(cum_wins / i, 4)})
                        result["curve_points"] = points
                        result["curve_text"] = _format_wr_sparkline(
                            points, label="模拟盘累计胜率"
                        )
            except Exception as e:
                logger.debug("paper profit curve: %s", e)

        if not result["curve_text"] and result["curve_points"]:
            result["curve_text"] = _format_wr_sparkline(
                result["curve_points"], label="累计胜率"
            )
        return result

    def detect_repeated_loss_patterns(self, min_occurrences: int = 3) -> List[Dict[str, Any]]:
        """识别重复亏损模式。

        注意：不用 SQLite json_extract/json_valid —— 库内 market_regime 常为纯字符串
        （如 RANGING），部分 SQLite 版本会对坏 JSON 直接抛 OperationalError。
        """
        patterns: List[Dict[str, Any]] = []
        try:
            regime_rows = self._fallback_regime_loss_rows()
        except Exception as e:
            logger.debug("detect_repeated_loss_patterns regime rows: %s", e)
            regime_rows = []

        try:
            for row in regime_rows:
                regime, direction, n, losses = row[0], row[1], int(row[2]), int(row[3])
                if losses < min_occurrences:
                    continue
                loss_rate = losses / max(n, 1)
                if loss_rate < 0.55:
                    continue
                bucket = normalize_regime_bucket(regime)
                patterns.append({
                    "id": f"regime_{bucket}_{direction}",
                    "type": "regime_direction",
                    "title": f"{BUCKET_LABELS.get(bucket, bucket or '?')} + {direction} 反复亏损",
                    "detail": f"{n} 笔样本中 {losses} 笔亏损 (胜率 {1-loss_rate:.0%})",
                    "severity": min(1.0, loss_rate * (losses / min_occurrences)),
                    "loss_count": losses,
                    "sample_count": n,
                    "suggested_tightening": round(0.03 + loss_rate * 0.05, 3),
                })
        except Exception as e:
            logger.debug("regime pattern build: %s", e)

        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            paper_path = str(get_localized_db_path("paper_trading"))
            if Path(paper_path).is_file():
                pconn = sqlite3.connect(paper_path, timeout=5)
                try:
                    for row in pconn.execute(
                        """
                        SELECT close_reason, side, COUNT(*) AS n,
                               SUM(CASE WHEN realized_pnl_usdt < -0.5 THEN 1 ELSE 0 END) AS losses
                        FROM paper_positions
                        WHERE status='CLOSED'
                        GROUP BY close_reason, side
                        HAVING n >= ?
                        """,
                        (min_occurrences,),
                    ):
                        reason, side, n, losses = row[0], row[1], int(row[2]), int(row[3])
                        if losses < min_occurrences:
                            continue
                        patterns.append({
                            "id": f"paper_{reason}_{side}",
                            "type": "paper_close",
                            "title": f"{side} {reason or 'CLOSE'} 连续止损模式",
                            "detail": f"{n} 笔中 {losses} 笔亏损",
                            "severity": min(1.0, losses / n + 0.2),
                            "loss_count": losses,
                            "sample_count": n,
                            "suggested_tightening": 0.05,
                        })
                finally:
                    pconn.close()
        except Exception as e:
            logger.debug("paper loss patterns: %s", e)

        try:
            mem = self.learner.capability_memory
            for card in mem.get_recent_cards(limit=20):
                if card.get("category") not in ("error_lesson", "stop_loss_rule"):
                    continue
                validated = int(card.get("times_validated") or 0)
                conf = float(card.get("confidence") or 0)
                if validated < 2 and conf < 0.75:
                    continue
                patterns.append({
                    "id": f"card_{card.get('id')}",
                    "type": "knowledge_card",
                    "title": card.get("title") or "历史亏损教训",
                    "detail": (card.get("lesson") or "")[:120],
                    "severity": conf,
                    "loss_count": validated,
                    "sample_count": validated,
                    "suggested_tightening": 0.04,
                })
        except Exception:
            pass

        if self.pattern_memory:
            try:
                insight = self.pattern_memory.get_insight({
                    "indicators": {},
                    "ai_analysis": {"confidence": 0.5},
                    "institutional_strategies": {"consensus_confidence": 0.5},
                    "current_price": 0,
                    "symbol": "BNBUSDT",
                })
                matched = int(insight.get("matched") or 0)
                wr = float(insight.get("win_rate") or 0)
                if matched >= min_occurrences and wr < 0.4:
                    patterns.append({
                        "id": "pattern_memory_current",
                        "type": "pattern_memory",
                        "title": "相似历史局面胜率偏低",
                        "detail": f"匹配 {matched} 条，胜率 {wr:.0%}",
                        "severity": min(1.0, 0.5 + (0.4 - wr)),
                        "loss_count": int(matched * (1 - wr)),
                        "sample_count": matched,
                        "suggested_tightening": 0.04,
                    })
            except Exception:
                pass

        patterns.sort(key=lambda p: float(p.get("severity") or 0), reverse=True)
        return patterns[:12]

    def _fallback_regime_loss_rows(self) -> List[Tuple]:
        """Python 侧聚合：兼容 market_regime 存纯字符串或坏 JSON。"""
        from collections import defaultdict

        buckets: Dict[Tuple[Any, Any], List[int]] = defaultdict(lambda: [0, 0])
        try:
            conn = self.learner._get_conn()
            rows = conn.execute(
                """
                SELECT market_regime, COALESCE(trading_action, final_signal), actual_result
                FROM analysis_records
                WHERE actual_result IN ('WIN', 'LOSS')
                """
            ).fetchall()
        except Exception as e:
            logger.debug("fallback regime rows: %s", e)
            return []

        for regime_raw, direction, result in rows:
            regime = None
            if regime_raw:
                text = str(regime_raw).strip()
                if text.startswith("{") or text.startswith("["):
                    try:
                        obj = json.loads(text)
                        if isinstance(obj, dict):
                            regime = obj.get("regime") or obj.get("market_regime")
                    except Exception:
                        regime = text
                else:
                    regime = text
            key = (regime, direction)
            buckets[key][0] += 1
            if result == "LOSS":
                buckets[key][1] += 1
        return [(k[0], k[1], v[0], v[1]) for k, v in buckets.items()]

    def apply_loss_pattern_gate_tightening(
        self,
        patterns: Optional[List[Dict[str, Any]]] = None,
        config_path: Optional[str] = None,
        project_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """一键收紧门控：写入 session boost + 可选提高 min_confidence。"""
        if patterns is None:
            patterns = self.detect_repeated_loss_patterns()
        if not patterns:
            return {"ok": False, "reason": "未检测到重复亏损模式"}

        boost = min(0.15, sum(float(p.get("suggested_tightening") or 0.03) for p in patterns[:3]))
        trades_n = 15
        if config_path and Path(config_path).is_file():
            try:
                import yaml
                with open(config_path, encoding="utf-8") as f:
                    full_cfg = yaml.safe_load(f) or {}
                trades_n = int((full_cfg.get("learning_gate") or {}).get("trades_per_session", 15))
            except Exception:
                pass
        state = {
            "gate_tightening_boost": boost,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "patterns": [{"id": p["id"], "title": p["title"]} for p in patterns[:5]],
            "trades_remaining": trades_n,
        }
        save_gate_state(state, project_root)

        applied_params = []
        if config_path and Path(config_path).is_file():
            try:
                from bnb_quant_tool.param_manager import ParamManager
                pm = ParamManager(config_path=config_path, learning_db_path=self.learner.db_path)
                old_conf = pm.get_param_value("confidence_threshold") or pm.get_param_value("min_confidence")
                if old_conf is not None:
                    new_conf = min(0.85, float(old_conf) + 0.03)
                    ok, msg = pm.set_param_value("confidence_threshold", new_conf)
                    if ok:
                        applied_params.append(f"confidence_threshold → {new_conf:.2f}")
            except Exception as e:
                logger.warning("apply gate param: %s", e)

        if hasattr(self.learner, "trade_advisor_ref"):
            try:
                advisor = self.learner.trade_advisor_ref
                if advisor is not None and hasattr(advisor, "min_confidence"):
                    advisor.min_confidence = min(
                        0.85,
                        float(advisor.min_confidence) + boost * 0.5,
                    )
            except Exception as e:
                logger.debug("trade_advisor gate sync: %s", e)

        return {
            "ok": True,
            "gate_tightening_boost": boost,
            "patterns_applied": len(patterns[:5]),
            "params": applied_params,
            "message": f"已收紧门控 +{boost:.0%}，影响后续 {state['trades_remaining']} 笔分析",
        }

    def clear_gate_tightening(self, project_root: Optional[Path] = None) -> None:
        clear_gate_state(project_root)

    def enrich_analysis_context(
        self,
        ctx: Dict[str, Any],
        *,
        regime: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        min_loss_occurrences: int = 3,
    ) -> Dict[str, Any]:
        """合并亏损模式检测 + 胜率优化上下文（分析流水线统一入口）。"""
        enriched = dict(ctx)
        enriched["loss_patterns"] = self.detect_repeated_loss_patterns(
            min_occurrences=min_loss_occurrences,
        )
        if self.learner is not None:
            try:
                from bnb_quant_tool.win_rate_strategy import load_strategy_performance_map

                enriched["strategy_performance"] = load_strategy_performance_map(
                    self.learner, regime=regime
                )
            except ImportError:
                enriched["strategy_performance"] = {}
        wrc_cfg = resolve_win_rate_config(app_config=config or {})
        enriched["win_rate_context"] = build_win_rate_context(
            enriched,
            regime=regime,
            config=wrc_cfg,
        )
        return enriched


# ---------------------------------------------------------------------------
# 胜率学习优化（原 win_rate_optimizer，已合并至此模块）
# ---------------------------------------------------------------------------

WIN_RATE_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "paper_min_trades": 15,
    "paper_high_wr": 0.55,
    "paper_low_wr": 0.42,
    "pattern_boost_wr": 0.65,
    "pattern_penalty_wr": 0.35,
    "pattern_min_samples": 5,
    "regime_loss_min_rate": 0.65,
    "regime_loss_min_samples": 4,
    "consec_loss_tighten_per": 0.02,
    "max_gate_tightening": 0.12,
    "max_gate_relaxation": 0.05,
    "worst_strategy_wr": 0.35,
    "worst_strategy_min_total": 5,
    "inst_penalty_scale": 0.06,
}

DEFAULT_CFG = WIN_RATE_DEFAULT_CFG  # backward compat alias


def _win_rate_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(WIN_RATE_DEFAULT_CFG)
    if config:
        merged.update(config)
    return merged


def _direction_from_loss_pattern(pattern: Dict[str, Any]) -> Optional[str]:
    pid = str(pattern.get("id") or "")
    if pid.startswith("regime_") or pid.startswith("paper_"):
        tail = pid.split("_")[-1].upper()
        if tail in ("LONG", "BUY"):
            return "LONG"
        if tail in ("SHORT", "SELL"):
            return "SHORT"
    title = str(pattern.get("title") or "").upper()
    if any(x in title for x in ("LONG", "BUY", "做多")):
        return "LONG"
    if any(x in title for x in ("SHORT", "SELL", "做空")):
        return "SHORT"
    return None


def _pattern_regime_bucket(pattern: Dict[str, Any]) -> Optional[str]:
    pid = str(pattern.get("id") or "")
    if pid.startswith("regime_") and pid.count("_") >= 2:
        return pid.split("_")[1]
    return None


def build_win_rate_context(
    insights: Dict[str, Any],
    *,
    regime: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """根据学习洞察构建胜率优化上下文（投票加减分 + 门控 + 方向拦截）。"""
    cfg = _win_rate_cfg(config)
    ctx: Dict[str, Any] = {
        "enabled": bool(cfg.get("enabled", True)),
        "long_boost": 0.0,
        "short_boost": 0.0,
        "long_penalty": 0.0,
        "short_penalty": 0.0,
        "gate_tightening": 0.0,
        "gate_relaxation": 0.0,
        "block_long": False,
        "block_short": False,
        "reasons": [],
        "hints": [],
    }
    if not ctx["enabled"]:
        return ctx

    regime_bucket = normalize_regime_bucket(
        regime or insights.get("regime_bucket") or insights.get("regime")
    )
    reasons: List[str] = ctx["reasons"]
    hints: List[str] = ctx["hints"]

    paper = insights.get("paper_trading") or {}
    closed = int(paper.get("closed_trades") or 0)
    wr = float(paper.get("win_rate") or 0)
    min_trades = int(cfg["paper_min_trades"])
    if closed >= min_trades:
        if wr >= float(cfg["paper_high_wr"]):
            ctx["gate_relaxation"] += 0.02
            hints.append(f"模拟盘胜率 {wr:.0%}（{closed} 笔）表现良好，可适度放宽门槛")
        elif wr < float(cfg["paper_low_wr"]):
            tighten = min(0.06, 0.03 + (float(cfg["paper_low_wr"]) - wr))
            ctx["gate_tightening"] += tighten
            reasons.append(f"模拟盘胜率仅 {wr:.0%}（{closed} 笔）")
            hints.append(f"模拟盘胜率偏低 {wr:.0%}，优先 WAIT、提高开仓门槛")

    consec = int(paper.get("consecutive_losses") or 0)
    if consec >= 3:
        per = float(cfg["consec_loss_tighten_per"])
        ctx["gate_tightening"] += min(0.08, per * consec)
        reasons.append(f"连亏 {consec} 笔")
        hints.append(f"当前连亏 {consec} 笔，勿强行开仓")

    pm = insights.get("pattern_memory") or {}
    matched = int(pm.get("matched") or 0)
    pm_wr = float(pm.get("win_rate") or pm.get("historical_win_rate") or 0)
    pm_min = int(cfg["pattern_min_samples"])
    if matched >= pm_min:
        avg_pnl = float(pm.get("avg_pnl_usdt") or pm.get("avg_pnl") or 0)
        boost_wr = float(cfg["pattern_boost_wr"])
        penalty_wr = float(cfg["pattern_penalty_wr"])
        if pm_wr >= boost_wr:
            boost = 0.14 * pm_wr
            if avg_pnl >= 0:
                ctx["long_boost"] += boost
            else:
                ctx["short_boost"] += boost
            hints.append(f"相似局面历史胜率 {pm_wr:.0%}（{matched} 次），倾向顺势加分")
        elif pm_wr < penalty_wr:
            penalty = 0.16 * (penalty_wr - pm_wr)
            ctx["gate_tightening"] += 0.03
            if avg_pnl > 0:
                ctx["long_penalty"] += penalty
            elif avg_pnl < 0:
                ctx["short_penalty"] += penalty
            else:
                ctx["long_penalty"] += penalty * 0.5
                ctx["short_penalty"] += penalty * 0.5
            reasons.append(f"模式记忆胜率 {pm_wr:.0%}（{matched} 次）")
            hints.append(
                f"相似历史局面胜率仅 {pm_wr:.0%}（{matched} 次），应谨慎或 WAIT"
            )

    loss_patterns = insights.get("loss_patterns") or []
    min_loss_rate = float(cfg["regime_loss_min_rate"])
    min_samples = int(cfg["regime_loss_min_samples"])
    for p in loss_patterns:
        ptype = p.get("type")
        n = int(p.get("sample_count") or 0)
        losses = int(p.get("loss_count") or 0)
        if n < min_samples:
            continue
        loss_rate = losses / max(n, 1)

        if ptype == "regime_direction":
            if losses < min_samples - 1 or loss_rate < min_loss_rate:
                continue
            pb = _pattern_regime_bucket(p)
            if pb and pb != regime_bucket:
                continue
            direction = _direction_from_loss_pattern(p)
            if direction == "LONG":
                ctx["block_long"] = True
                ctx["gate_tightening"] += float(p.get("suggested_tightening") or 0.04)
                reasons.append(p.get("title") or "regime+LONG 反复亏损")
            elif direction == "SHORT":
                ctx["block_short"] = True
                ctx["gate_tightening"] += float(p.get("suggested_tightening") or 0.04)
                reasons.append(p.get("title") or "regime+SHORT 反复亏损")
        elif ptype == "paper_close" and loss_rate >= 0.55:
            direction = _direction_from_loss_pattern(p)
            pen = 0.05 * loss_rate
            if direction == "LONG":
                ctx["long_penalty"] += pen
            elif direction == "SHORT":
                ctx["short_penalty"] += pen
            ctx["gate_tightening"] += float(p.get("suggested_tightening") or 0.03)
            reasons.append(p.get("title") or "模拟盘止损模式")

    worst_wr = float(cfg["worst_strategy_wr"])
    worst_min = int(cfg["worst_strategy_min_total"])
    inst_scale = float(cfg["inst_penalty_scale"])
    for s in insights.get("worst_strategies") or []:
        s_wr = float(s.get("win_rate") or 0)
        total = int(s.get("total") or 0)
        if total < worst_min or s_wr >= worst_wr:
            continue
        pen = inst_scale * (worst_wr - s_wr)
        name = str(s.get("name") or "").upper()
        if any(k in name for k in ("BUY", "LONG", "BULL", "多")):
            ctx["long_penalty"] += pen
        elif any(k in name for k in ("SELL", "SHORT", "BEAR", "空")):
            ctx["short_penalty"] += pen
        else:
            ctx["long_penalty"] += pen * 0.3
            ctx["short_penalty"] += pen * 0.3

    for row in insights.get("factor_attribution") or []:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        total = wins + losses
        if total < 6:
            continue
        f_wr = float(row.get("win_rate") or (wins / total))
        key = str(row.get("factor_key") or "").lower()
        if f_wr >= 0.62:
            if "long" in key or "buy" in key or "bull" in key:
                ctx["long_boost"] += 0.06 * f_wr
            elif "short" in key or "sell" in key or "bear" in key:
                ctx["short_boost"] += 0.06 * f_wr
        elif f_wr < 0.35:
            pen = 0.08 * (0.35 - f_wr)
            if "long" in key or "buy" in key or "bull" in key:
                ctx["long_penalty"] += pen
            elif "short" in key or "sell" in key or "bear" in key:
                ctx["short_penalty"] += pen

    ctx["gate_tightening"] = min(float(cfg["max_gate_tightening"]), ctx["gate_tightening"])
    ctx["gate_relaxation"] = min(float(cfg["max_gate_relaxation"]), ctx["gate_relaxation"])
    return ctx


def apply_vote_adjustments(
    long_score: float,
    short_score: float,
    ctx: Optional[Dict[str, Any]],
) -> Tuple[float, float]:
    """将胜率上下文应用到多空投票得分。"""
    if not ctx or not ctx.get("enabled", True):
        return long_score, short_score
    ls = long_score + float(ctx.get("long_boost") or 0) - float(ctx.get("long_penalty") or 0)
    ss = short_score + float(ctx.get("short_boost") or 0) - float(ctx.get("short_penalty") or 0)
    return max(0.0, ls), max(0.0, ss)


def apply_direction_blocks(
    action: str,
    ctx: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """历史 regime+方向反复亏损时拦截开仓。"""
    if not ctx or not ctx.get("enabled", True) or action not in ("LONG", "SHORT"):
        return action, ""
    if action == "LONG" and ctx.get("block_long"):
        return "WAIT", "胜率学习: 当前市场状态做多历史反复亏损，改 WAIT"
    if action == "SHORT" and ctx.get("block_short"):
        return "WAIT", "胜率学习: 当前市场状态做空历史反复亏损，改 WAIT"
    return action, ""


def gate_adjustments_from_context(
    ctx: Optional[Dict[str, Any]],
) -> Tuple[float, float]:
    if not ctx or not ctx.get("enabled", True):
        return 0.0, 0.0
    return float(ctx.get("gate_tightening") or 0), float(ctx.get("gate_relaxation") or 0)


def format_win_rate_for_prompt(ctx: Optional[Dict[str, Any]]) -> str:
    """格式化胜率优化提示，注入 DeepSeek。"""
    if not ctx or not ctx.get("enabled", True):
        return ""
    lines = ["--- 胜率学习优化（必须参考，避免重复亏损） ---"]
    for h in ctx.get("hints") or []:
        lines.append(f"  • {h}")
    for r in ctx.get("reasons") or []:
        lines.append(f"  ⚠ {r}")
    if ctx.get("block_long"):
        lines.append("  🚫 当前 regime 下做多已被学习系统标记为高风险")
    if ctx.get("block_short"):
        lines.append("  🚫 当前 regime 下做空已被学习系统标记为高风险")
    lb = float(ctx.get("long_boost") or 0) - float(ctx.get("long_penalty") or 0)
    sb = float(ctx.get("short_boost") or 0) - float(ctx.get("short_penalty") or 0)
    if abs(lb) > 0.01 or abs(sb) > 0.01:
        lines.append(f"  投票修正: 多 {lb:+.2f} / 空 {sb:+.2f}")
    if len(lines) <= 1:
        return ""
    lines.append("  要求: 若与历史亏损模式一致，优先 WAIT 或降低置信度")
    lines.append("")
    return "\n".join(lines)


def format_win_rate_cockpit_lines(ctx: Optional[Dict[str, Any]]) -> List[str]:
    """GUI / Web 驾驶舱展示用。"""
    if not ctx or not ctx.get("enabled", True):
        return []
    lines = ["[胜率学习]"]
    paper_hint = next((h for h in (ctx.get("hints") or []) if "模拟盘" in h), "")
    if paper_hint:
        lines.append(f"  {paper_hint}")
    for r in (ctx.get("reasons") or [])[:3]:
        lines.append(f"  ⚠ {r}")
    lb = float(ctx.get("long_boost") or 0) - float(ctx.get("long_penalty") or 0)
    sb = float(ctx.get("short_boost") or 0) - float(ctx.get("short_penalty") or 0)
    if abs(lb) > 0.01 or abs(sb) > 0.01:
        lines.append(f"  投票: 多{lb:+.2f} / 空{sb:+.2f}")
    if ctx.get("block_long"):
        lines.append("  🚫 做多拦截")
    if ctx.get("block_short"):
        lines.append("  🚫 做空拦截")
    gt = float(ctx.get("gate_tightening") or 0)
    if gt > 0:
        lines.append(f"  门控收紧 +{gt:.0%}")
    return lines


def resolve_win_rate_config(
    extra_market: Optional[Dict[str, Any]] = None,
    app_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从 extra_market 或 app_config 解析 win_rate_optimizer 配置。"""
    em = extra_market or {}
    cfg = em.get("win_rate_optimizer")
    if cfg is not None:
        return dict(cfg) if isinstance(cfg, dict) else {}
    full = app_config or em.get("app_config") or {}
    return dict(full.get("win_rate_optimizer") or {})


def build_learning_dashboard_snapshot(
    learner,
    *,
    paper_engine=None,
    pattern_memory=None,
    config: Optional[Dict[str, Any]] = None,
    regime: Optional[str] = None,
) -> Dict[str, Any]:
    """学习 Tab / Web API 统一仪表盘快照（亏损模式 + 胜率上下文 + 曲线）。"""
    analytics = LearningAnalytics(
        learner,
        paper_engine=paper_engine,
        pattern_memory=pattern_memory,
    )
    try:
        from bnb_quant_tool.ai_trading_context import enrich_learning_insights

        insights = learner.get_learning_insights(market_context={"regime": regime})
        insights = enrich_learning_insights(insights, paper_engine=paper_engine)
        if hasattr(learner, "get_factor_attribution_summary"):
            insights["factor_attribution"] = learner.get_factor_attribution_summary(
                regime=regime
            )
        enriched = analytics.enrich_analysis_context(
            insights,
            regime=regime,
            config=config or {},
        )
    except Exception as e:
        logger.debug("dashboard snapshot insights: %s", e)
        enriched = {"win_rate_context": {}, "loss_patterns": []}

    wrc = enriched.get("win_rate_context") or {}
    curve = analytics.get_profit_curve_comparison()
    patterns = enriched.get("loss_patterns") or analytics.detect_repeated_loss_patterns()

    try:
        from bnb_quant_tool.data_localization import get_localization_manager

        gate = load_gate_state(get_localization_manager().workspace)
    except Exception:
        gate = load_gate_state()

    lb = float(wrc.get("long_boost") or 0) - float(wrc.get("long_penalty") or 0)
    sb = float(wrc.get("short_boost") or 0) - float(wrc.get("short_penalty") or 0)

    return {
        "win_rate_context": wrc,
        "loss_patterns": patterns[:12],
        "profit_curve": curve,
        "paper_trading": enriched.get("paper_trading") or {},
        "gate_state": gate,
        "vote_adj_long": round(lb, 4),
        "vote_adj_short": round(sb, 4),
        "cockpit_lines": format_win_rate_cockpit_lines(wrc),
        "block_long": bool(wrc.get("block_long")),
        "block_short": bool(wrc.get("block_short")),
        "gate_tightening": float(wrc.get("gate_tightening") or 0),
        "gate_relaxation": float(wrc.get("gate_relaxation") or 0),
    }

def maybe_auto_tighten_gate_after_loss(
    learner,
    config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
    outcome: str = "",
) -> Optional[Dict[str, Any]]:
    """平仓亏损后若检测到重复亏损模式，自动收紧门控（可配置）。"""
    if outcome != "LOSS":
        return None
    cfg = (config or {}).get("learning_gate") or {}
    if not bool(cfg.get("auto_tighten_on_loss", True)):
        return None
    if load_gate_state(project_root):
        return None
    analytics = LearningAnalytics(learner)
    patterns = analytics.detect_repeated_loss_patterns(
        min_occurrences=int(cfg.get("min_pattern_occurrences", 3)),
    )
    if not patterns:
        return None
    top = patterns[0]
    if float(top.get("severity") or 0) < float(cfg.get("min_severity", 0.65)):
        return None
    config_path = cfg.get("config_path")
    if config_path is None and project_root:
        config_path = str(Path(project_root) / "config.yaml")
    return analytics.apply_loss_pattern_gate_tightening(
        patterns=patterns[:3],
        config_path=config_path,
        project_root=project_root,
    )


def _format_wr_sparkline(points: List[Dict], label: str = "累计胜率", width: int = 40) -> str:
    if not points:
        return f"{label}: 样本不足"
    lines = [f"[{label}]", ""]
    if len(points) >= 4:
        mid = len(points) // 2
        early_wr = points[mid - 1]["cum_wr"] if mid > 0 else points[0]["cum_wr"]
        late_wr = points[-1]["cum_wr"]
        delta = late_wr - early_wr
        arrow = "↑ 学习有效" if delta > 0.03 else ("↓ 需复盘" if delta < -0.03 else "→ 持平")
        lines.append(
            f"前半段胜率 ~{early_wr:.1%}  →  最新累计 {late_wr:.1%}  ({delta:+.1%}) {arrow}"
        )
        lines.append("")

    step = max(1, len(points) // width)
    sampled = points[::step][-width:]
    chars = []
    for p in sampled:
        wr = float(p["cum_wr"])
        if wr >= 0.6:
            chars.append("█")
        elif wr >= 0.5:
            chars.append("▓")
        elif wr >= 0.4:
            chars.append("▒")
        else:
            chars.append("░")
    lines.append("".join(chars))
    lines.append(f"0% {'─' * (width - 10)} 100%  (█≥60% ▓≥50% ▒≥40% ░<40%)")
    lines.append(f"样本数: {len(points)}")
    return "\n".join(lines)


def format_regime_bucket_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "暂无分桶权重数据。完成带 market_regime 的反馈后会自动分桶学习。"
    lines = ["[市场状态分桶策略权重]", ""]
    current_bucket = None
    for r in rows:
        b = r.get("bucket_label") or r.get("bucket")
        if b != current_bucket:
            current_bucket = b
            lines.append(f"── {b} ({r.get('regime', '')}) ──")
        wr = r.get("win_rate") or 0
        wr_s = f"{wr:.0%}" if r.get("total", 0) >= 3 else "样本不足"
        lines.append(
            f"  {r.get('strategy', '?')[:28]:28s}  "
            f"WR {wr_s:>6s}  W {(r.get('weight') or 0)*100:5.1f}%  "
            f"n={r.get('total', 0)}"
        )
    lines.append("")
    lines.append("说明: 趋势/震荡/高波动分桶权重会在对应 market_regime 下自动融合到机构策略投票。")
    return "\n".join(lines)
