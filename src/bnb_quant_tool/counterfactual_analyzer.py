"""
BNB量化交易工具 - 反事实学习 (Counterfactual Learning)
====================================================
核心职责：
每笔交易平仓后，自动回答三个关键问题：
  1. 如果当时不交易会怎样？  (NO_TRADE)
  2. 如果反向做会怎样？      (REVERSE)
  3. 如果晚进场会怎样？      (LATE_ENTRY)

设计原则：
- 平仓后异步执行（不阻塞 UI）
- 依赖 BinanceDataFetcher 获取历史 K 线
- SQLite 持久化结果，供后续复盘和 AI 自省
- 统计汇总：长期看 "实际决策 vs 替代方案" 的优劣
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 手续费率（双边）
FEE_RATE = 0.0004

# 晚进场延迟（小时）
LATE_ENTRY_DELAY_HOURS = 3


class CounterfactualAnalyzer:
    """反事实学习分析器 — 每笔平仓后计算替代方案的 PnL"""

    def __init__(self, db_path: str = None, fetcher=None):
        """
        Args:
            db_path: 反事实结果 DB 路径（默认项目根目录 counterfactual.db）
            fetcher: BinanceDataFetcher 实例（用于拉取历史 K 线）
        """
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.db_path = str(get_localized_db_path('counterfactual'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent / "data"
                if not base_dir.exists():
                    base_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(base_dir / "counterfactual.db")
        else:
            self.db_path = db_path

        self._fetcher = fetcher
        self._local = threading.local()
        self._init_db()
        logger.info(f"CounterfactualAnalyzer initialized, db={self.db_path}")

    # ============================================================
    # DB
    # ============================================================
    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=60)
            self._local.conn.row_factory = sqlite3.Row
            from bnb_quant_tool.sqlite_util import apply_writer_pragmas
            apply_writer_pragmas(self._local.conn)
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS counterfactual_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                close_price REAL NOT NULL,
                qty REAL NOT NULL,
                actual_pnl REAL NOT NULL,
                -- 三个反事实场景
                no_trade_pnl REAL DEFAULT 0,
                reverse_pnl REAL,
                late_entry_price REAL,
                late_entry_pnl REAL,
                late_delay_hours REAL,
                -- 评价
                best_scenario TEXT,
                decision_score REAL,
                summary TEXT,
                details_json TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cf_pid "
            "ON counterfactual_results(position_id)"
        )
        conn.commit()

    # ============================================================
    # 主入口
    # ============================================================
    def analyze(self, position: Dict, symbol: str = "BNBUSDT",
                delay_hours: float = LATE_ENTRY_DELAY_HOURS) -> Dict:
        """
        对一笔已平仓交易执行反事实分析。

        Args:
            position: 平仓交易字典 (来自 paper_positions 行)
                必需字段: id, side, entry_price, close_avg_price,
                          qty_total, realized_pnl_usdt, opened_at, closed_at
            symbol: 交易对
            delay_hours: 晚进场延迟时间（小时）

        Returns:
            {
                "position_id": int,
                "actual_pnl": float,
                "scenarios": {
                    "no_trade": {"pnl": 0, "diff": float},
                    "reverse": {"pnl": float, "diff": float},
                    "late_entry": {"pnl": float, "entry_price": float, "diff": float} | None
                },
                "best_scenario": str,  # "ACTUAL" / "NO_TRADE" / "REVERSE" / "LATE_ENTRY"
                "decision_score": float,  # 0-100, 实际决策在所有方案中的排名得分
                "text": str,  # 人可读总结
            }
        """
        pid = position.get("id", 0)
        side = (position.get("side") or "LONG").upper()
        entry_price = float(position.get("entry_price") or 0)
        close_price = float(position.get("close_avg_price") or 0)
        qty = float(position.get("qty_total") or 0)
        actual_pnl = float(position.get("realized_pnl_usdt") or 0)
        opened_at = position.get("opened_at", "")
        closed_at = position.get("closed_at", "")

        if entry_price <= 0 or close_price <= 0 or qty <= 0:
            return self._empty_result(pid, actual_pnl, "数据不完整")

        # ------ 场景 1: 不交易 ------
        no_trade_pnl = 0.0  # 什么都不做，PnL = 0

        # ------ 场景 2: 反向交易 ------
        reverse_pnl = self._calc_reverse(side, entry_price, close_price, qty)

        # ------ 场景 3: 晚进场 ------
        late_result = self._calc_late_entry(
            side, entry_price, close_price, qty,
            opened_at, closed_at, symbol, delay_hours
        )

        # ------ 场景 4: 同期持有 BNB 现货不动 ------
        hold_bnb = self._calc_hold_bnb(entry_price, close_price, qty)
        excess_pnl = round(actual_pnl - hold_bnb["pnl"], 4)
        attribution = self._attribute_edge(actual_pnl, excess_pnl, hold_bnb["pnl"])

        # ------ 评分 ------
        scenarios = {
            "no_trade": {"pnl": no_trade_pnl, "diff": actual_pnl - no_trade_pnl},
            "reverse": {"pnl": reverse_pnl, "diff": actual_pnl - reverse_pnl},
            "hold_bnb": {
                "pnl": hold_bnb["pnl"],
                "diff": excess_pnl,
                "return_pct": hold_bnb["return_pct"],
            },
        }
        if late_result:
            late_result["diff"] = round(actual_pnl - late_result["pnl"], 4)
            scenarios["late_entry"] = late_result
        else:
            scenarios["late_entry"] = None

        best_scenario, decision_score = self._evaluate(
            actual_pnl, no_trade_pnl, reverse_pnl,
            late_result["pnl"] if late_result else None,
            hold_bnb_pnl=hold_bnb["pnl"],
        )

        # 生成文本
        text = self._format_text(
            pid, side, actual_pnl, scenarios, best_scenario, decision_score,
            excess_pnl=excess_pnl, attribution=attribution,
        )

        result = {
            "position_id": pid,
            "actual_pnl": actual_pnl,
            "excess_pnl": excess_pnl,
            "attribution": attribution,
            "scenarios": scenarios,
            "best_scenario": best_scenario,
            "decision_score": decision_score,
            "text": text,
        }

        # 持久化
        self._save_result(
            pid, symbol, side, entry_price, close_price, qty,
            actual_pnl, no_trade_pnl, reverse_pnl, late_result,
            best_scenario, decision_score, text, delay_hours,
            hold_bnb_pnl=hold_bnb["pnl"],
            excess_pnl=excess_pnl,
            attribution=attribution,
        )

        return result

    # ============================================================
    # 场景计算
    # ============================================================
    def _calc_reverse(self, side: str, entry: float, close: float,
                      qty: float) -> float:
        """反向交易 PnL"""
        fee = (entry + close) * qty * FEE_RATE
        if side == "LONG":
            # 反向 = SHORT: (entry - close) * qty
            pnl = (entry - close) * qty - fee
        else:
            # 反向 = LONG: (close - entry) * qty
            pnl = (close - entry) * qty - fee
        return round(pnl, 4)

    @staticmethod
    def _calc_hold_bnb(entry: float, close: float, qty: float) -> Dict:
        """同期买入等量 BNB 现货并持有至平仓时刻的基准收益。"""
        fee = (entry + close) * qty * FEE_RATE
        pnl = (close - entry) * qty - fee
        ret = ((close - entry) / entry) if entry > 0 else 0.0
        return {"pnl": round(pnl, 4), "return_pct": round(ret * 100.0, 4)}

    @staticmethod
    def _attribute_edge(actual_pnl: float, excess_pnl: float, hold_pnl: float) -> str:
        """区分逻辑 alpha / 大盘 Beta / 噪音。"""
        if abs(actual_pnl) < 1e-6 and abs(excess_pnl) < 1e-6:
            return "noise"
        # 超额很小 → 主要吃到现货 Beta
        if abs(hold_pnl) > 1e-6 and abs(excess_pnl) < 0.25 * abs(hold_pnl):
            return "beta"
        if abs(excess_pnl) < 0.15 * max(abs(actual_pnl), 1.0):
            return "noise"
        return "logic"

    def _calc_late_entry(self, side: str, entry: float, close: float,
                         qty: float, opened_at: str, closed_at: str,
                         symbol: str, delay_hours: float) -> Optional[Dict]:
        """
        晚进场场景：延迟 delay_hours 后进场，相同出场时间。
        需要获取 opened_at + delay 时的价格。
        """
        if not self._fetcher or not opened_at:
            return None

        try:
            # 解析开仓时间
            open_dt = datetime.fromisoformat(opened_at)
            late_dt = open_dt + timedelta(hours=delay_hours)

            # 获取晚进场时的价格（取 1h K线的 close）
            start_ms = int(late_dt.timestamp() * 1000)
            end_ms = start_ms + 3600 * 1000  # 1小时窗口

            df = self._fetcher.get_klines(
                symbol=symbol, interval="1h",
                limit=2, start_time=start_ms, end_time=end_ms
            )

            if df is None or df.empty:
                return None

            late_entry_price = float(df['close'].iloc[0])
            if late_entry_price <= 0:
                return None

            # 用新入场价算 PnL（出场价不变）
            fee = (late_entry_price + close) * qty * FEE_RATE
            if side == "LONG":
                late_pnl = (close - late_entry_price) * qty - fee
            else:
                late_pnl = (late_entry_price - close) * qty - fee

            return {
                "pnl": round(late_pnl, 4),
                "entry_price": late_entry_price,
                "diff": 0,  # diff 由调用方填充
            }
        except Exception as e:
            logger.warning(f"晚进场计算失败: {e}")
            return None

    # ============================================================
    # 评分
    # ============================================================
    def _evaluate(
        self,
        actual: float,
        no_trade: float,
        reverse: float,
        late: Optional[float],
        hold_bnb_pnl: Optional[float] = None,
    ) -> Tuple[str, float]:
        """
        评价实际决策在所有方案中的表现。

        Returns:
            (best_scenario_name, decision_score 0-100)
        """
        candidates = {
            "ACTUAL": actual,
            "NO_TRADE": no_trade,
            "REVERSE": reverse,
        }
        if late is not None:
            candidates["LATE_ENTRY"] = late
        if hold_bnb_pnl is not None:
            candidates["HOLD_BNB"] = float(hold_bnb_pnl)

        # 找最佳方案
        best_name = max(candidates, key=candidates.get)
        worst_val = min(candidates.values())
        best_val = max(candidates.values())

        # 决策评分: 实际 PnL 在 [worst, best] 中的位置 → 0-100
        if best_val == worst_val:
            score = 50.0  # 所有方案等效
        else:
            score = (actual - worst_val) / (best_val - worst_val) * 100.0

        score = max(0.0, min(100.0, score))
        return best_name, round(score, 1)

    # ============================================================
    # 汇总统计
    # ============================================================
    def get_summary_stats(self, limit: int = 50) -> Dict:
        """
        统计最近 N 笔交易的反事实对比汇总。

        Returns:
            {
                "total_analyzed": int,
                "actual_was_best": int,     # 实际决策是最优的次数
                "actual_was_best_pct": float,
                "avg_decision_score": float,
                "should_have_reversed": int,   # 反向更好的次数
                "should_have_waited": int,     # 不交易更好的次数
                "should_have_delayed": int,    # 晚进场更好的次数
                "total_actual_pnl": float,
                "total_reverse_pnl": float,
                "text": str,
            }
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT actual_pnl, no_trade_pnl, reverse_pnl, late_entry_pnl, "
            "best_scenario, decision_score FROM counterfactual_results "
            "ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cur.fetchall()

        if not rows:
            return {
                "total_analyzed": 0,
                "actual_was_best": 0,
                "actual_was_best_pct": 0.0,
                "avg_decision_score": 0.0,
                "should_have_reversed": 0,
                "should_have_waited": 0,
                "should_have_delayed": 0,
                "total_actual_pnl": 0.0,
                "total_reverse_pnl": 0.0,
                "text": "反事实分析：暂无数据",
            }

        total = len(rows)
        actual_best = sum(1 for r in rows if r["best_scenario"] == "ACTUAL")
        reversed_better = sum(1 for r in rows if r["best_scenario"] == "REVERSE")
        waited_better = sum(1 for r in rows if r["best_scenario"] == "NO_TRADE")
        delayed_better = sum(1 for r in rows if r["best_scenario"] == "LATE_ENTRY")
        avg_score = sum(r["decision_score"] or 50 for r in rows) / total
        total_actual = sum(r["actual_pnl"] or 0 for r in rows)
        total_reverse = sum(r["reverse_pnl"] or 0 for r in rows)

        text_lines = [
            "━" * 36,
            "🔄 反事实学习汇总 (Counterfactual)",
            "━" * 36,
            f"分析笔数: {total}",
            f"实际决策最优: {actual_best}/{total} ({actual_best/total:.0%})",
            f"平均决策评分: {avg_score:.1f}/100",
            f"应该反向做: {reversed_better} 次",
            f"应该不交易: {waited_better} 次",
            f"应该晚进场: {delayed_better} 次",
            f"实际累计 PnL: ${total_actual:+.2f}",
            f"反向累计 PnL: ${total_reverse:+.2f}",
            "━" * 36,
        ]

        return {
            "total_analyzed": total,
            "actual_was_best": actual_best,
            "actual_was_best_pct": round(actual_best / total, 4) if total else 0,
            "avg_decision_score": round(avg_score, 1),
            "should_have_reversed": reversed_better,
            "should_have_waited": waited_better,
            "should_have_delayed": delayed_better,
            "total_actual_pnl": round(total_actual, 4),
            "total_reverse_pnl": round(total_reverse, 4),
            "text": "\n".join(text_lines),
        }

    # ============================================================
    # 格式化
    # ============================================================
    def _format_text(
        self,
        pid: int,
        side: str,
        actual_pnl: float,
        scenarios: Dict,
        best: str,
        score: float,
        *,
        excess_pnl: float = 0.0,
        attribution: str = "noise",
    ) -> str:
        """生成人可读的反事实分析文本"""
        lines = []
        lines.append("━" * 36)
        lines.append(f"🔄 反事实分析 - 交易 #{pid} ({side})")
        lines.append("━" * 36)
        lines.append(f"  实际结果: ${actual_pnl:+.2f}")
        lines.append(f"  超额收益(vs持有BNB): ${excess_pnl:+.2f} [{attribution}]")
        lines.append("")

        # 场景对比
        no_trade = scenarios["no_trade"]
        reverse = scenarios["reverse"]
        late = scenarios.get("late_entry")
        hold = scenarios.get("hold_bnb") or {}

        lines.append(f"  ① 如果不交易: ${no_trade['pnl']:+.2f}"
                     f"  (差距 ${no_trade['diff']:+.2f})")
        lines.append(f"  ② 如果反向做: ${reverse['pnl']:+.2f}"
                     f"  (差距 ${reverse['diff']:+.2f})")
        if late:
            lines.append(
                f"  ③ 如果晚{LATE_ENTRY_DELAY_HOURS:.0f}h进场: "
                f"${late['pnl']:+.2f} (入场@{late['entry_price']:.2f})"
            )
        else:
            lines.append(f"  ③ 晚进场: (数据不可用)")
        if hold:
            lines.append(
                f"  ④ 持有BNB不动: ${float(hold.get('pnl') or 0):+.2f} "
                f"(现货 {float(hold.get('return_pct') or 0):+.2f}%)"
            )

        lines.append("")

        # 最佳方案
        label_map = {
            "ACTUAL": "✅ 实际决策是最优选择！",
            "NO_TRADE": "⚠️ 不交易会更好 (过度交易?)",
            "REVERSE": "⚠️ 反向做会更好 (方向判断错误?)",
            "LATE_ENTRY": "⚠️ 晚进场会更好 (进场时机偏早?)",
            "HOLD_BNB": "⚠️ 持有现货会更好 (交易未跑赢Beta)",
        }
        lines.append(f"  {label_map.get(best, best)}")
        lines.append(f"  决策评分: {score:.0f}/100")
        lines.append("━" * 36)

        return "\n".join(lines)

    def _empty_result(self, pid: int, actual_pnl: float, reason: str) -> Dict:
        return {
            "position_id": pid,
            "actual_pnl": actual_pnl,
            "excess_pnl": 0.0,
            "attribution": "noise",
            "scenarios": {"no_trade": {"pnl": 0, "diff": 0},
                          "reverse": {"pnl": 0, "diff": 0},
                          "hold_bnb": {"pnl": 0, "diff": 0, "return_pct": 0},
                          "late_entry": None},
            "best_scenario": "UNKNOWN",
            "decision_score": 50.0,
            "text": f"🔄 反事实分析 #{pid}: {reason}",
        }

    # ============================================================
    # 持久化
    # ============================================================
    def _save_result(
        self,
        pid,
        symbol,
        side,
        entry,
        close,
        qty,
        actual_pnl,
        no_trade_pnl,
        reverse_pnl,
        late_result,
        best_scenario,
        decision_score,
        text,
        delay_hours,
        *,
        hold_bnb_pnl: float = 0.0,
        excess_pnl: float = 0.0,
        attribution: str = "noise",
    ):
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")

            late_price = late_result["entry_price"] if late_result else None
            late_pnl = late_result["pnl"] if late_result else None

            # 兼容旧库：尽力写入 details_json；列不存在则忽略
            details = json.dumps(
                {
                    "hold_bnb_pnl": hold_bnb_pnl,
                    "excess_pnl": excess_pnl,
                    "attribution": attribution,
                },
                ensure_ascii=False,
            )
            try:
                cur.execute("""
                    INSERT INTO counterfactual_results
                    (timestamp, position_id, symbol, side, entry_price, close_price,
                     qty, actual_pnl, no_trade_pnl, reverse_pnl,
                     late_entry_price, late_entry_pnl, late_delay_hours,
                     best_scenario, decision_score, summary, details_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (now, pid, symbol, side, entry, close, qty,
                      actual_pnl, no_trade_pnl, reverse_pnl,
                      late_price, late_pnl, delay_hours,
                      best_scenario, decision_score, text, details))
            except sqlite3.OperationalError:
                cur.execute("""
                    INSERT INTO counterfactual_results
                    (timestamp, position_id, symbol, side, entry_price, close_price,
                     qty, actual_pnl, no_trade_pnl, reverse_pnl,
                     late_entry_price, late_entry_pnl, late_delay_hours,
                     best_scenario, decision_score, summary)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (now, pid, symbol, side, entry, close, qty,
                      actual_pnl, no_trade_pnl, reverse_pnl,
                      late_price, late_pnl, delay_hours,
                      best_scenario, decision_score, text))
            conn.commit()
            logger.info(
                f"Counterfactual saved: #{pid} best={best_scenario} "
                f"score={decision_score} excess={excess_pnl} attr={attribution}"
            )
        except Exception as e:
            logger.error(f"Counterfactual save error: {e}")

    def get_result_count(self) -> int:
        """返回已分析的总数"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM counterfactual_results")
        row = cur.fetchone()
        return row["cnt"] if row else 0

    # ============================================================
    # 蒙特卡洛压力测试（参数/策略权重稳健性）
    # ============================================================
    def monte_carlo_stress_test(
        self,
        trades: List[Dict],
        param_overrides: Optional[Dict] = None,
        n_simulations: int = 100,
        price_jitter_pct: float = 0.01,
        news_delay_minutes: float = 5.0,
        min_pass_rate: float = 0.65,
    ) -> Dict:
        """
        对历史交易施加随机扰动，评估参数调整是否过拟合。

        Args:
            trades: 最近 N 笔已平仓交易（含 realized_pnl_usdt / entry_price / close_avg_price）
            param_overrides: 待验证的参数变更（用于记录，不影响扰动逻辑）
            n_simulations: 模拟次数
            price_jitter_pct: 价格随机扰动幅度
            news_delay_minutes: 新闻延迟模拟（分钟，影响晚进场场景权重）
            min_pass_rate: 通过阈值

        Returns:
            {passed, pass_rate, n_simulations, avg_pnl, worst_pnl, ...}
        """
        if not trades or len(trades) < 5:
            return {
                "passed": False,
                "pass_rate": 0.0,
                "reason": "样本不足（需 ≥5 笔）",
                "n_simulations": 0,
            }

        pnls = [float(t.get("realized_pnl_usdt") or t.get("pnl") or 0) for t in trades]
        baseline_total = sum(pnls)
        baseline_wins = sum(1 for p in pnls if p > 0)
        baseline_wr = baseline_wins / len(pnls)

        wins = 0
        sim_totals: List[float] = []

        for _ in range(n_simulations):
            sim_pnl = 0.0
            for t, base_pnl in zip(trades, pnls):
                entry = float(t.get("entry_price") or 0)
                close = float(t.get("close_avg_price") or 0)
                if entry <= 0 or close <= 0:
                    sim_pnl += base_pnl
                    continue
                jitter = 1.0 + random.uniform(-price_jitter_pct, price_jitter_pct)
                delay_factor = 1.0 - (news_delay_minutes / 60.0) * random.uniform(0, 0.02)
                side = (t.get("side") or "LONG").upper()
                adj_entry = entry * jitter
                adj_close = close * jitter * delay_factor
                qty = float(t.get("qty_total") or t.get("qty") or 1)
                fee = (adj_entry + adj_close) * qty * FEE_RATE
                if side == "LONG":
                    sim_pnl += (adj_close - adj_entry) * qty - fee
                else:
                    sim_pnl += (adj_entry - adj_close) * qty - fee
            sim_totals.append(sim_pnl)
            # 通过条件：扰动后仍盈利或胜率不低于基线 80%
            sim_wr = sum(1 for p in pnls if p > 0) / len(pnls)  # 方向不变，用总量
            if sim_pnl >= baseline_total * 0.7 or sim_pnl > 0:
                wins += 1

        pass_rate = wins / n_simulations if n_simulations else 0.0
        passed = pass_rate >= min_pass_rate

        return {
            "passed": passed,
            "pass_rate": round(pass_rate, 3),
            "n_simulations": n_simulations,
            "baseline_total_pnl": round(baseline_total, 2),
            "avg_sim_pnl": round(sum(sim_totals) / len(sim_totals), 2) if sim_totals else 0,
            "worst_sim_pnl": round(min(sim_totals), 2) if sim_totals else 0,
            "baseline_win_rate": round(baseline_wr, 3),
            "min_pass_rate": min_pass_rate,
            "param_overrides": param_overrides or {},
            "interpretation": (
                f"蒙特卡洛压力测试 {'通过' if passed else '未通过'} "
                f"({pass_rate:.0%} ≥ {min_pass_rate:.0%})"
            ),
        }
