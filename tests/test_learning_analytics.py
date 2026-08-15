"""学习成效分析：分桶权重、盈利曲线、亏损模式门控"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bnb_quant_tool.learning_analytics import (
    LearningAnalytics,
    format_regime_bucket_text,
    get_session_gate_boost,
    load_gate_state,
    normalize_regime_bucket,
    save_gate_state,
    tick_session_gate,
)


def test_normalize_regime_bucket():
    assert normalize_regime_bucket("TRENDING") == "TREND"
    assert normalize_regime_bucket("RANGING") == "RANGE"
    assert normalize_regime_bucket("PANIC") == "VOLATILE"
    assert normalize_regime_bucket(None) == "GLOBAL"


def test_gate_state_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        save_gate_state({"gate_tightening_boost": 0.08, "trades_remaining": 5, "patterns": []}, root)
        assert load_gate_state(root)["gate_tightening_boost"] == 0.08
        assert get_session_gate_boost(root) == 0.08


def _make_learner_db(path: str):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE analysis_records (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            actual_result TEXT,
            pnl_percent REAL,
            market_regime TEXT,
            trading_action TEXT,
            final_signal TEXT
        );
        CREATE TABLE strategy_regime_performance (
            regime TEXT,
            strategy_name TEXT,
            total_predictions INTEGER,
            correct_predictions INTEGER,
            win_rate REAL,
            weight REAL
        );
        CREATE TABLE strategy_performance (
            strategy_name TEXT PRIMARY KEY,
            total_predictions INTEGER,
            correct_predictions INTEGER,
            win_rate REAL,
            weight REAL,
            is_active INTEGER DEFAULT 1
        );
    """)
    rows = [
        ("2025-01-01T10:00:00", "LOSS", -1.2, '{"regime":"RANGING"}', "LONG", "BUY"),
        ("2025-01-02T10:00:00", "LOSS", -0.8, '{"regime":"RANGING"}', "LONG", "BUY"),
        ("2025-01-03T10:00:00", "LOSS", -1.0, '{"regime":"RANGING"}', "LONG", "BUY"),
        ("2025-01-04T10:00:00", "WIN", 2.0, '{"regime":"TRENDING"}', "LONG", "BUY"),
        ("2025-01-05T10:00:00", "WIN", 1.5, '{"regime":"TRENDING"}', "LONG", "BUY"),
        ("2025-01-06T10:00:00", "WIN", 1.0, '{"regime":"TRENDING"}', "LONG", "BUY"),
        ("2025-01-07T10:00:00", "WIN", 0.5, '{"regime":"TRENDING"}', "LONG", "BUY"),
        ("2025-01-08T10:00:00", "LOSS", -0.3, '{"regime":"RANGING"}', "SHORT", "SELL"),
    ]
    conn.executemany(
        "INSERT INTO analysis_records "
        "(timestamp, actual_result, pnl_percent, market_regime, trading_action, final_signal) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO strategy_regime_performance VALUES (?,?,?,?,?,?)",
        ("RANGE", "Bollinger Bands", 10, 4, 0.4, 0.15),
    )
    conn.commit()
    conn.close()


class _ConnLearner:
    """测试用 learner：每次 _get_conn 返回新连接，避免 Windows 文件锁。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.capability_memory = MagicMock()
        self.capability_memory.get_recent_cards.return_value = []

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _load_strategy_weights(self):
        return {"SMA": 1.0}


@pytest.fixture
def analytics_db(tmp_path):
    db = str(tmp_path / "ai_learning.db")
    _make_learner_db(db)
    return LearningAnalytics(_ConnLearner(db))


def test_profit_curve_comparison(analytics_db):
    curve = analytics_db.get_profit_curve_comparison()
    assert curve["feedback_n"] == 8
    assert curve["feedback_early_wr"] is not None
    assert curve["feedback_late_wr"] is not None
    assert curve["curve_text"]
    assert "累计胜率" in curve["curve_text"] or "反馈" in curve["curve_text"]


def test_detect_repeated_loss_patterns(analytics_db):
    patterns = analytics_db.detect_repeated_loss_patterns(min_occurrences=3)
    ids = [p["id"] for p in patterns]
    assert any("regime_RANGE_LONG" == i for i in ids)


def test_format_regime_bucket_text():
    text = format_regime_bucket_text([
        {
            "bucket_label": "震荡市",
            "regime": "RANGE",
            "strategy": "Bollinger Bands",
            "win_rate": 0.4,
            "weight": 0.15,
            "total": 10,
        }
    ])
    assert "震荡市" in text
    assert "Bollinger" in text


def test_apply_gate_tightening_no_patterns():
    analytics = LearningAnalytics(MagicMock())
    result = analytics.apply_loss_pattern_gate_tightening(patterns=[])
    assert result["ok"] is False


def test_tick_session_gate_clears_when_zero(tmp_path):
    save_gate_state({"gate_tightening_boost": 0.08, "trades_remaining": 1}, tmp_path)
    tick_session_gate(tmp_path)
    assert load_gate_state(tmp_path) == {}


def test_apply_counterfactual_gate_blocks_overtrading():
    from bnb_quant_tool.ai_trading_context import apply_counterfactual_gate

    advice = apply_counterfactual_gate(
        {"action": "LONG", "passed_gate": True, "gate_reasons": []},
        {
            "counterfactual_stats": {
                "total_analyzed": 10,
                "should_have_waited": 5,
                "should_have_reversed": 1,
            }
        },
        {"ai_trading": {"counterfactual_gate": {"enabled": True, "min_samples": 8}}},
    )
    assert advice["action"] == "WAIT"
    assert advice.get("counterfactual_blocked")
