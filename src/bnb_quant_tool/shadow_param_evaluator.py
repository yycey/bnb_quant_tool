"""
真影子参数 A/B — 用影子参数重算 gate，对比 baseline vs shadow 开仓决策。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PARAM_GATE_MAP = {
    "confidence_threshold": "min_confidence",
    "min_risk_reward_ratio": "min_rr",
}


class ShadowParamEvaluator:
    """记录每次分析在 baseline / shadow 参数下是否会通过门控。"""

    def __init__(self, db_path: str, config: Optional[Dict] = None):
        self.db_path = db_path
        self.config = config or {}
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=60)
            self._local.conn.row_factory = sqlite3.Row
            from bnb_quant_tool.sqlite_util import apply_writer_pragmas
            apply_writer_pragmas(self._local.conn)
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_gate_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trial_id INTEGER NOT NULL,
                record_id INTEGER,
                param_name TEXT NOT NULL,
                baseline_value REAL,
                shadow_value REAL,
                baseline_would_open INTEGER,
                shadow_would_open INTEGER,
                action TEXT,
                ai_confidence REAL,
                passed_gate INTEGER
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sgd_trial
            ON shadow_gate_decisions(trial_id)
        """)
        conn.commit()

    def get_active_trials(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM shadow_param_trials WHERE status='active' ORDER BY id ASC"
        )
        return [dict(r) for r in cur.fetchall()]

    def evaluate_analysis(
        self,
        record_id: int,
        advice: Dict[str, Any],
        ai_analysis: Dict[str, Any],
        learning_insights: Optional[Dict] = None,
    ) -> int:
        """分析完成后记录 baseline/shadow gate 对比。"""
        trials = self.get_active_trials()
        if not trials:
            return 0

        action = str(advice.get("action") or advice.get("raw_action") or "WAIT")
        raw_action = str(advice.get("raw_action") or action)
        passed = bool(advice.get("passed_gate"))
        ai_conf = float(ai_analysis.get("confidence") or advice.get("confidence") or 0)
        strength = str(advice.get("strength") or "MODERATE")
        rr = advice.get("risk_reward_ratio")
        learning_insights = learning_insights or {}

        saved = 0
        conn = self._get_conn()
        cur = conn.cursor()
        now = datetime.now().isoformat()

        for trial in trials:
            pname = trial.get("param_name")
            if pname not in PARAM_GATE_MAP:
                continue
            baseline_val = float(trial.get("baseline_value") or 0)
            shadow_val = float(trial.get("shadow_value") or 0)
            tid = int(trial["id"])

            base_open = self._would_open(
                action, raw_action, passed, ai_conf, strength, rr,
                pname, baseline_val, learning_insights,
            )
            shadow_open = self._would_open(
                action, raw_action, passed, ai_conf, strength, rr,
                pname, shadow_val, learning_insights,
            )

            cur.execute(
                """
                INSERT INTO shadow_gate_decisions
                (timestamp, trial_id, record_id, param_name,
                 baseline_value, shadow_value, baseline_would_open,
                 shadow_would_open, action, ai_confidence, passed_gate)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now, tid, int(record_id), pname,
                    baseline_val, shadow_val,
                    1 if base_open else 0,
                    1 if shadow_open else 0,
                    action, ai_conf, 1 if passed else 0,
                ),
            )
            saved += 1

            cur.execute(
                "UPDATE shadow_param_trials SET trades_observed=trades_observed+1 "
                "WHERE id=?",
                (tid,),
            )
            if base_open:
                cur.execute(
                    "UPDATE shadow_param_trials SET baseline_wins=baseline_wins+1 "
                    "WHERE id=?",
                    (tid,),
                )
            if shadow_open:
                cur.execute(
                    "UPDATE shadow_param_trials SET shadow_wins=shadow_wins+1 "
                    "WHERE id=?",
                    (tid,),
                )

        conn.commit()
        return saved

    def _would_open(
        self,
        action: str,
        raw_action: str,
        passed_gate: bool,
        ai_conf: float,
        strength: str,
        rr: Optional[float],
        param_name: str,
        param_value: float,
        learning_insights: Dict,
    ) -> bool:
        direction = action if action in ("LONG", "SHORT") else raw_action
        if direction not in ("LONG", "SHORT"):
            return False

        gate_key = PARAM_GATE_MAP.get(param_name)
        min_conf = float(
            (self.config.get("trading") or {}).get("confidence_threshold", 0.6)
        )
        min_rr = float(
            (self.config.get("risk_management") or {}).get("min_risk_reward_ratio", 1.8)
        )

        if gate_key == "min_confidence":
            min_conf = param_value
        elif gate_key == "min_rr":
            min_rr = param_value

        if ai_conf < min_conf:
            return False
        if strength == "WEAK":
            return False
        if rr is not None and float(rr) < min_rr:
            return False

        paper = learning_insights.get("paper_trading") or {}
        if int(paper.get("consecutive_losses") or 0) >= 5:
            return False

        return True

    def finalize_trials(self, min_observations: int = 8) -> Dict[str, Any]:
        """根据 gate 对比结果晋升或拒绝影子 trial。"""
        from bnb_quant_tool.param_manager import ParamManager

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM shadow_param_trials WHERE status='active'")
        trials = [dict(r) for r in cur.fetchall()]
        result = {"promoted": [], "rejected": []}

        for trial in trials:
            tid = int(trial["id"])
            obs = int(trial.get("trades_observed") or 0)
            if obs < min_observations:
                continue

            row = cur.execute(
                """
                SELECT
                    SUM(baseline_would_open) AS b_open,
                    SUM(shadow_would_open) AS s_open,
                    COUNT(*) AS n
                FROM shadow_gate_decisions WHERE trial_id=?
                """,
                (tid,),
            ).fetchone()

            b_open = int(row["b_open"] or 0)
            s_open = int(row["s_open"] or 0)
            n = int(row["n"] or 0)
            if n < min_observations:
                continue

            # shadow 更少开仓但质量未验证时：偏好 shadow 若开仓率降低 >15% 且非零
            b_rate = b_open / max(n, 1)
            s_rate = s_open / max(n, 1)

            # 结合真实盈亏：shadow 过滤后的交易质量
            pnl_stats = self._pnl_compare_for_trial(tid)
            shadow_wr = pnl_stats.get("shadow_win_rate")
            baseline_wr = pnl_stats.get("baseline_win_rate")
            shadow_avg_pnl = pnl_stats.get("shadow_avg_pnl")
            baseline_avg_pnl = pnl_stats.get("baseline_avg_pnl")

            promote = False
            if shadow_wr is not None and baseline_wr is not None and pnl_stats.get("samples", 0) >= 5:
                if shadow_wr >= baseline_wr + 0.05 and (shadow_avg_pnl or 0) >= (baseline_avg_pnl or 0):
                    promote = True
                elif shadow_wr < baseline_wr - 0.08:
                    promote = False
                elif s_rate < b_rate * 0.85 and s_rate >= 0.05 and (shadow_avg_pnl or 0) >= 0:
                    promote = True
            elif s_rate < b_rate * 0.85 and s_rate >= 0.05:
                promote = True
            elif s_rate > b_rate * 1.05:
                promote = True

            pname = trial["param_name"]
            shadow_val = float(trial["shadow_value"] or 0)

            if promote:
                cur.execute(
                    "UPDATE shadow_param_trials SET status='promoted' WHERE id=?",
                    (tid,),
                )
                try:
                    pm = ParamManager(
                        config_path=str(ParamManager.resolve_config_path()),
                        learning_db_path=self.db_path,
                    )
                    pm.set_param_value(pname, shadow_val)
                except Exception as e:
                    logger.warning("shadow promote apply failed: %s", e)
                result["promoted"].append({
                    "trial_id": tid,
                    "param": pname,
                    "value": shadow_val,
                    "baseline_open_rate": round(b_rate, 3),
                    "shadow_open_rate": round(s_rate, 3),
                })
            else:
                cur.execute(
                    "UPDATE shadow_param_trials SET status='rejected' WHERE id=?",
                    (tid,),
                )
                result["rejected"].append({
                    "trial_id": tid,
                    "param": pname,
                    "baseline_open_rate": round(b_rate, 3),
                    "shadow_open_rate": round(s_rate, 3),
                })

        conn.commit()
        return result

    def _pnl_compare_for_trial(self, trial_id: int) -> Dict[str, Any]:
        """关联 paper_positions 真实盈亏，评估 shadow 参数质量。"""
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path

            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT record_id, baseline_would_open, shadow_would_open
                FROM shadow_gate_decisions
                WHERE trial_id = ? AND record_id IS NOT NULL
                """,
                (trial_id,),
            )
            gate_rows = cur.fetchall()

            paper_path = str(get_localized_db_path("paper_trading"))
            paper_conn = sqlite3.connect(paper_path, timeout=10)
            paper_conn.row_factory = sqlite3.Row
            pcur = paper_conn.cursor()
        except Exception as e:
            logger.debug("pnl compare skipped: %s", e)
            return {"samples": 0}

        base_pnls: List[float] = []
        shadow_pnls: List[float] = []
        try:
            for r in gate_rows:
                rid = int(r["record_id"])
                pcur.execute(
                    """
                    SELECT realized_pnl_usdt FROM paper_positions
                    WHERE learning_record_id = ? AND status = 'CLOSED'
                    ORDER BY closed_at DESC LIMIT 1
                    """,
                    (rid,),
                )
                prow = pcur.fetchone()
                if prow is None or prow["realized_pnl_usdt"] is None:
                    continue
                pnl = float(prow["realized_pnl_usdt"])
                if int(r["baseline_would_open"] or 0):
                    base_pnls.append(pnl)
                if int(r["shadow_would_open"] or 0):
                    shadow_pnls.append(pnl)
        finally:
            paper_conn.close()

        def _wr(pnls: List[float]) -> Optional[float]:
            if len(pnls) < 3:
                return None
            wins = sum(1 for p in pnls if p > 0.5)
            return wins / len(pnls)

        def _avg(pnls: List[float]) -> Optional[float]:
            return sum(pnls) / len(pnls) if len(pnls) >= 3 else None

        return {
            "samples": len(base_pnls) + len(shadow_pnls),
            "baseline_win_rate": _wr(base_pnls),
            "shadow_win_rate": _wr(shadow_pnls),
            "baseline_avg_pnl": _avg(base_pnls),
            "shadow_avg_pnl": _avg(shadow_pnls),
        }

    def get_trials_summary(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, param_name, baseline_value, shadow_value, status, "
            "trades_observed, baseline_wins, shadow_wins, reason, timestamp "
            "FROM shadow_param_trials ORDER BY id DESC LIMIT 20"
        )
        out = []
        for r in cur.fetchall():
            row = dict(r)
            tid = int(row["id"])
            stats = cur.execute(
                "SELECT SUM(baseline_would_open) AS b, SUM(shadow_would_open) AS s, "
                "COUNT(*) AS n FROM shadow_gate_decisions WHERE trial_id=?",
                (tid,),
            ).fetchone()
            row["gate_baseline_opens"] = int(stats["b"] or 0)
            row["gate_shadow_opens"] = int(stats["s"] or 0)
            row["gate_decisions"] = int(stats["n"] or 0)
            out.append(row)
        return out
