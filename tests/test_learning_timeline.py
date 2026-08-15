"""学习进化时间线聚合测试"""

import json
import os
import sqlite3
import tempfile

import pytest

from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.learning_timeline import (
    LearningTimelineCollector,
    format_timeline_text,
)


@pytest.fixture
def timeline_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    learner = AILearningSystem(db_path=path, config={})
    conn = learner._get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO learning_log (timestamp, event_type, message, details, improvement_score)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "2026-07-09T10:00:00",
            "AI_REVIEW",
            "AI复盘完成，分析了5笔交易",
            json.dumps({"confidence": 0.8, "param_suggestions": [{"param": "min_confidence"}]}),
            0.8,
        ),
    )
    cur.execute(
        """
        INSERT INTO analysis_records
        (timestamp, symbol, timeframe, current_price, final_signal, actual_result, pnl_percent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-07-09T09:00:00", "BNBUSDT", "1h", 600.0, "BUY", "WIN", 1.5),
    )
    conn.commit()

    from bnb_quant_tool.capability_memory import CapabilityMemory

    cm = CapabilityMemory(path, config={})
    cm.save_knowledge_card(
        {
            "title": "止损过紧",
            "lesson": "震荡市不宜贴价止损",
            "category": "stop_loss_rule",
            "confidence": 0.75,
        },
        source="AI_REVIEW",
    )
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_collect_merges_review_knowledge_feedback(timeline_db):
    collector = LearningTimelineCollector(timeline_db)
    events = collector.collect(limit=20)
    stages = {e.stage for e in events}
    assert "review" in stages
    assert "knowledge" in stages
    assert "feedback" in stages
    assert events[0].timestamp >= events[-1].timestamp


def test_format_timeline_text_nonempty(timeline_db):
    collector = LearningTimelineCollector(timeline_db)
    events = collector.collect(limit=5)
    text = format_timeline_text(events)
    assert "Learning Evolution Timeline" in text
    assert "AI 复盘" in text or "复盘" in text


def test_format_timeline_text_empty():
    text = format_timeline_text([])
    assert "暂无进化事件" in text


def test_collect_includes_paper_trades(timeline_db):
    fd, paper_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(paper_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE paper_positions (
                id INTEGER PRIMARY KEY,
                symbol TEXT, side TEXT, status TEXT,
                opened_at TEXT, closed_at TEXT,
                entry_price REAL, qty_total REAL, qty_remaining REAL,
                sl REAL, sl_initial REAL,
                realized_pnl_usdt REAL, close_reason TEXT, learning_record_id INTEGER,
                r_multiple REAL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO paper_positions
            (id, symbol, side, status, opened_at, closed_at, entry_price, qty_total,
             qty_remaining, sl, sl_initial, realized_pnl_usdt, close_reason,
             learning_record_id, r_multiple)
            VALUES (1, 'BNBUSDT', 'LONG', 'CLOSED', '2026-07-09T08:00:00',
                    '2026-07-09T11:00:00', 600, 1, 0, 590, 590, 12.5, 'TP3', 1, 1.2)
            """
        )
        conn.commit()
        conn.close()

        collector = LearningTimelineCollector(timeline_db, paper_db_path=paper_path)
        events = collector.collect(limit=20)
        stages = {e.stage for e in events}
        assert "trade" in stages
        trade_ev = next(e for e in events if e.stage == "trade")
        assert "模拟盘" in trade_ev.title
        assert trade_ev.payload.get("position_id") == 1
    finally:
        try:
            os.unlink(paper_path)
        except OSError:
            pass
