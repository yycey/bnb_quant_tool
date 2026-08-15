"""学习进化时间线 — 汇总复盘、知识卡片、参数演化与反馈事件。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


STAGE_LABELS = {
    "perceive": "感知",
    "decide": "决策",
    "execute": "执行",
    "reflect": "反思",
    "memory": "记忆",
    "analyze": "AI 分析",
    "trade": "模拟交易",
    "feedback": "结果反馈",
    "review": "AI 复盘",
    "knowledge": "知识沉淀",
    "evolve": "参数进化",
    "inject": "知识注入",
    "guard": "风控门控",
}


@dataclass
class TimelineEvent:
    timestamp: str
    stage: str
    title: str
    detail: str = ""
    impact: str = ""
    next_effect: str = ""
    source: str = ""
    ref_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    def sort_key(self) -> str:
        return self.timestamp or ""


class LearningTimelineCollector:
    """从 ai_learning.db（及可选 paper_trading.db）聚合学习进化事件。"""

    def __init__(self, db_path: str, paper_db_path: Optional[str] = None):
        self.db_path = db_path
        self.paper_db_path = paper_db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return cur.fetchone() is not None

    def collect(self, limit: int = 80) -> List[TimelineEvent]:
        events: List[TimelineEvent] = []
        try:
            conn = self._connect()
            cur = conn.cursor()
            events.extend(self._from_learning_log(cur, limit))
            events.extend(self._from_knowledge_cards(cur, limit))
            events.extend(self._from_param_changes(cur, limit))
            events.extend(self._from_injected_knowledge(cur, limit))
            events.extend(self._from_feedback_records(cur, limit))
            conn.close()
            events.extend(self._from_paper_trades(limit))
        except Exception:
            return []

        events.sort(key=lambda e: e.sort_key(), reverse=True)
        # 去重：同秒同标题保留一条
        seen = set()
        deduped: List[TimelineEvent] = []
        for ev in events:
            key = (ev.timestamp[:19], ev.stage, ev.title)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ev)
            if len(deduped) >= limit:
                break
        return deduped

    def _from_learning_log(self, cur: sqlite3.Cursor, limit: int) -> List[TimelineEvent]:
        if not self._table_exists(cur, "learning_log"):
            return []
        cur.execute(
            """
            SELECT timestamp, event_type, message, details, improvement_score
            FROM learning_log
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: List[TimelineEvent] = []
        for row in cur.fetchall():
            et = (row["event_type"] or "").upper()
            stage = self._stage_for_log_type(et)
            impact = ""
            next_effect = self._next_effect_for_stage(stage)
            if row["improvement_score"] is not None:
                impact = f"改进分 {float(row['improvement_score']):.0%}"
            detail = row["message"] or ""
            if row["details"]:
                try:
                    payload = json.loads(row["details"])
                    if isinstance(payload, dict):
                        summary = payload.get("summary") or payload.get("grade")
                        if summary:
                            detail = f"{detail} | {summary}"
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(
                TimelineEvent(
                    timestamp=row["timestamp"] or "",
                    stage=stage,
                    title=self._title_for_log_type(et, row["message"]),
                    detail=detail[:240],
                    impact=impact,
                    next_effect=next_effect,
                    source="learning_log",
                    ref_id=et,
                )
            )
        return out

    def _from_knowledge_cards(self, cur: sqlite3.Cursor, limit: int) -> List[TimelineEvent]:
        if not self._table_exists(cur, "knowledge_cards"):
            return []
        cur.execute(
            """
            SELECT id, timestamp, source, category, title, lesson, confidence, record_id, trade_id
            FROM knowledge_cards
            WHERE is_active = 1
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: List[TimelineEvent] = []
        cat_labels = {
            "trading_logic": "交易逻辑",
            "stop_loss_rule": "止损规则",
            "market_review": "市场复盘",
            "error_lesson": "错误教训",
        }
        for row in cur.fetchall():
            cat = cat_labels.get(row["category"], row["category"] or "知识")
            out.append(
                TimelineEvent(
                    timestamp=row["timestamp"] or "",
                    stage="knowledge",
                    title=f"新增知识卡片: {row['title'] or cat}",
                    detail=(row["lesson"] or "")[:200],
                    impact=f"来源 {row['source']} | 可信度 {float(row['confidence'] or 0):.0%}",
                    next_effect="下次 AI 分析将语义检索并注入相关卡片",
                    source="knowledge_cards",
                    ref_id=str(row["id"]),
                    payload={
                        "card_id": row["id"],
                        "category": row["category"],
                        "record_id": row["record_id"],
                        "trade_id": row["trade_id"],
                    },
                )
            )
        return out

    def _from_param_changes(self, cur: sqlite3.Cursor, limit: int) -> List[TimelineEvent]:
        if not self._table_exists(cur, "param_change_log"):
            return []
        cur.execute(
            """
            SELECT timestamp, param_name, old_value, new_value, source, review_summary
            FROM param_change_log
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: List[TimelineEvent] = []
        for row in cur.fetchall():
            src = row["source"] or "AI_REVIEW"
            applied = "APPLIED" in src.upper()
            out.append(
                TimelineEvent(
                    timestamp=row["timestamp"] or "",
                    stage="evolve",
                    title=f"参数{'已应用' if applied else '建议'}: {row['param_name']}",
                    detail=(row["review_summary"] or "")[:200],
                    impact=f"{row['old_value']} → {row['new_value']}",
                    next_effect="影响后续门控阈值、仓位与止损止盈计算",
                    source="param_change_log",
                    ref_id=row["param_name"],
                    payload={"applied": applied, "source": src},
                )
            )
        return out

    def _from_injected_knowledge(self, cur: sqlite3.Cursor, limit: int) -> List[TimelineEvent]:
        if not self._table_exists(cur, "injected_knowledge_log"):
            return []
        cur.execute(
            """
            SELECT i.injected_at, i.record_id, i.card_id, k.title, k.category, k.lesson
            FROM injected_knowledge_log i
            LEFT JOIN knowledge_cards k ON k.id = i.card_id
            ORDER BY i.injected_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: List[TimelineEvent] = []
        for row in cur.fetchall():
            title = row["title"] or f"卡片#{row['card_id']}"
            out.append(
                TimelineEvent(
                    timestamp=row["injected_at"] or "",
                    stage="inject",
                    title=f"分析 #{row['record_id']} 注入知识: {title}",
                    detail=(row["lesson"] or "")[:180],
                    impact=f"category={row['category'] or '?'}",
                    next_effect="该次 AI 研判使用了这条历史经验",
                    source="injected_knowledge_log",
                    ref_id=str(row["card_id"]),
                    payload={"record_id": row["record_id"], "card_id": row["card_id"]},
                )
            )
        return out

    def _from_feedback_records(self, cur: sqlite3.Cursor, limit: int) -> List[TimelineEvent]:
        if not self._table_exists(cur, "analysis_records"):
            return []
        cur.execute(
            """
            SELECT id, timestamp, symbol, final_signal, actual_result, pnl_percent
            FROM analysis_records
            WHERE actual_result IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: List[TimelineEvent] = []
        for row in cur.fetchall():
            result = row["actual_result"] or "?"
            pnl = row["pnl_percent"]
            pnl_s = f"{float(pnl):+.2f}%" if pnl is not None else "N/A"
            out.append(
                TimelineEvent(
                    timestamp=row["timestamp"] or "",
                    stage="feedback",
                    title=f"分析 #{row['id']} 反馈: {result}",
                    detail=f"{row['symbol']} {row['final_signal']} | PnL {pnl_s}",
                    impact="更新策略权重与历史胜率统计",
                    next_effect="后续 AI 分析会参考此次对错",
                    source="analysis_records",
                    ref_id=str(row["id"]),
                )
            )
        return out

    def _from_paper_trades(self, limit: int) -> List[TimelineEvent]:
        if not self.paper_db_path:
            return []
        try:
            conn = sqlite3.connect(self.paper_db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if not self._table_exists(cur, "paper_positions"):
                conn.close()
                return []
            cur.execute(
                """
                SELECT id, symbol, side, closed_at, realized_pnl_usdt,
                       close_reason, learning_record_id, r_multiple
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            out: List[TimelineEvent] = []
            for row in cur.fetchall():
                pnl = float(row["realized_pnl_usdt"] or 0)
                outcome = "盈利" if pnl > 0.5 else ("亏损" if pnl < -0.5 else "保本")
                r_mult = row["r_multiple"]
                r_s = f" | R={float(r_mult):+.2f}" if r_mult is not None else ""
                rec_id = row["learning_record_id"]
                rec_hint = f" → 分析#{rec_id}" if rec_id else ""
                out.append(
                    TimelineEvent(
                        timestamp=row["closed_at"] or "",
                        stage="trade",
                        title=f"模拟盘 #{row['id']} 平仓: {row['side']} {outcome}",
                        detail=(
                            f"{row['symbol']} | {row['close_reason'] or 'CLOSE'} | "
                            f"PnL ${pnl:+.2f}{r_s}{rec_hint}"
                        ),
                        impact=f"样本结果 {outcome}",
                        next_effect="触发学习反馈、反事实分析与 AI 复盘候选",
                        source="paper_positions",
                        ref_id=str(row["id"]),
                        payload={
                            "position_id": row["id"],
                            "learning_record_id": rec_id,
                            "pnl_usdt": pnl,
                            "close_reason": row["close_reason"],
                        },
                    )
                )
            conn.close()
            return out
        except Exception:
            return []

    @staticmethod
    def _stage_for_log_type(event_type: str) -> str:
        mapping = {
            "ANALYSIS": "analyze",
            "FEEDBACK": "feedback",
            "AI_REVIEW": "review",
            "OPTIMIZATION": "evolve",
            "GROWTH": "evolve",
            "CIRCUIT_BREAKER": "guard",
        }
        return mapping.get(event_type, "analyze")

    @staticmethod
    def _title_for_log_type(event_type: str, message: str) -> str:
        if event_type == "AI_REVIEW":
            return "AI 复盘完成"
        if event_type == "ANALYSIS":
            return "记录 AI 分析"
        if event_type == "FEEDBACK":
            return "收到分析反馈"
        if event_type == "OPTIMIZATION":
            return "策略权重优化"
        if event_type == "GROWTH":
            return "能力成长事件"
        if event_type == "CIRCUIT_BREAKER":
            return "熔断器触发"
        return (message or event_type or "学习事件")[:60]

    @staticmethod
    def _next_effect_for_stage(stage: str) -> str:
        effects = {
            "analyze": "为后续交易决策建立基准记录",
            "trade": "产生真实盈亏样本供复盘学习",
            "feedback": "修正策略权重与历史准确率",
            "review": "触发参数建议并提炼知识卡片",
            "knowledge": "供下次分析语义检索注入",
            "evolve": "改变门控阈值与执行参数",
            "inject": "直接影响当次 AI 判断",
            "guard": "暂停低质量信号，保护账户",
        }
        return effects.get(stage, "影响后续 AI 决策质量")


def format_timeline_text(events: List[TimelineEvent], limit: int = 12) -> str:
    """生成 GUI 可读的紧凑时间线文本。"""
    if not events:
        return (
            "[Learning Evolution Timeline]\n"
            "- 暂无进化事件\n"
            "- 先运行 AI 分析、模拟盘平仓或 AI 复盘，时间线会自动填充\n"
        )
    lines = ["[Learning Evolution Timeline]", ""]
    for ev in events[:limit]:
        ts = (ev.timestamp or "")[:19]
        lines.append(f"{ts} | {ev.stage_label} | {ev.title}")
        if ev.impact:
            lines.append(f"  impact: {ev.impact}")
        if ev.next_effect:
            lines.append(f"  next: {ev.next_effect}")
        lines.append("")
    return "\n".join(lines).rstrip()
