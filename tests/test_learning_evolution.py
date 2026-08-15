"""学习进化闭环测试"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.capability_memory import CapabilityMemory
from bnb_quant_tool.learning_evolution import (
    LearningEvolutionCoordinator,
    extract_factor_scores,
    QUALITY_TIER_MULTIPLIER,
)


@pytest.fixture
def learning_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    learner = AILearningSystem(db_path=path, config={})
    yield learner, path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_quality_weighted_strategy_update(learning_db):
    learner, _ = learning_db
    conn = learner._get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analysis_records "
        "(timestamp, symbol, timeframe, current_price, final_signal, "
        "institutional_results, trading_action, market_regime) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "2026-06-14T10:00:00",
            "BNBUSDT",
            "1h",
            600.0,
            "BUY",
            '{"ema_crossover": {"strategy": "EMA Crossover", "signal": "BUY"}}',
            "LONG",
            "TRENDING",
        ),
    )
    record_id = cur.lastrowid
    conn.commit()

    quality_a = {"tier": "A", "score": 85}
    quality_d = {"tier": "D", "score": 25}

    learner.submit_feedback(record_id, "WIN", 610.0, "test", quality=quality_a)
    row = conn.execute(
        "SELECT weighted_correct, correct_predictions FROM strategy_performance "
        "WHERE strategy_name='EMA Crossover'"
    ).fetchone()
    assert row is not None
    assert float(row[0]) == QUALITY_TIER_MULTIPLIER["A"]
    assert int(row[1]) == 1

    cur.execute(
        "INSERT INTO analysis_records "
        "(timestamp, symbol, timeframe, current_price, final_signal, "
        "institutional_results, trading_action, market_regime) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "2026-06-14T11:00:00",
            "BNBUSDT",
            "1h",
            600.0,
            "BUY",
            '{"ema_crossover": {"strategy": "EMA Crossover", "signal": "SELL"}}',
            "LONG",
            "TRENDING",
        ),
    )
    rid2 = cur.lastrowid
    conn.commit()
    learner.submit_feedback(rid2, "LOSS", 590.0, "test", quality=quality_d)
    regime_row = conn.execute(
        "SELECT total_predictions, correct_predictions FROM strategy_regime_performance "
        "WHERE strategy_name='EMA Crossover' AND regime='TRENDING'"
    ).fetchone()
    assert regime_row is not None
    assert int(regime_row[0]) >= 2


def test_knowledge_card_validate_and_retire(learning_db):
    learner, db_path = learning_db
    mem = CapabilityMemory(db_path, config={"capability_memory": {"enabled": True}})
    card_id = mem.save_knowledge_card(
        {
            "category": "error_lesson",
            "title": "测试卡片",
            "trigger_condition": "RSI>70",
            "action_rule": "WAIT",
            "lesson": "测试教训",
            "confidence": 0.5,
        },
        source="test",
    )
    assert card_id is not None
    mem.record_injected_cards(1, [card_id])

    result = mem.validate_cards_for_feedback(1, "LOSS", {"tier": "D", "score": 20})
    assert result["contradicted"] == 1

    result2 = mem.validate_cards_for_feedback(1, "LOSS", {"tier": "D", "score": 15})
    assert result2["contradicted"] == 1

    row = mem._get_conn().execute(
        "SELECT is_active, times_contradicted FROM knowledge_cards WHERE id=?",
        (card_id,),
    ).fetchone()
    assert int(row["times_contradicted"]) >= 2
    assert int(row["is_active"]) == 0


def test_counterfactual_lesson_saved(learning_db):
    learner, db_path = learning_db
    mem = CapabilityMemory(db_path, config={"capability_memory": {"enabled": True}})
    cf = {
        "best_scenario": "NO_TRADE",
        "decision_score": 35.0,
        "actual_pnl": -12.0,
        "text": "不交易更好",
    }
    trade = {"id": 1, "side": "LONG", "realized_pnl_usdt": -12.0}
    n = mem.save_counterfactual_lesson(cf, trade)
    assert n == 1
    assert mem.count_active_cards() >= 1


def test_factor_attribution_and_growth_dimensions(learning_db):
    learner, _ = learning_db
    explanation = {
        "factors": [
            {"name": "多周期共振", "score": 22},
            {"name": "RSI 信号", "score": -7},
        ]
    }
    learner.record_factor_attribution(
        explanation, outcome="WIN", regime="RANGING",
        factor_scores=extract_factor_scores(explanation),
    )
    summary = learner.get_factor_attribution_summary(regime="RANGING")
    assert any(s["factor_key"] == "multi_timeframe" for s in summary)

    snap = learner.get_growth_snapshot()
    assert "capability_dimensions" in snap
    assert "prediction_accuracy" in snap["capability_dimensions"]
    assert snap["capability_level"] <= 100


def test_walk_forward_gate():
    trades = [{"realized_pnl_usdt": 10}] * 30 + [{"realized_pnl_usdt": -5}] * 30
    ev = LearningEvolutionCoordinator(learner=None, config={
        "learning_evolution": {"walk_forward_min_samples": 50}
    })
    result = ev.walk_forward_gate(trades)
    assert "passed" in result
    assert result.get("train_wr") is not None


def test_card_rank_score_sorting():
    mem_path = tempfile.mktemp(suffix=".db")
    mem = CapabilityMemory(mem_path, config={"capability_memory": {"enabled": True}})
    high = {"similarity": 0.8, "confidence": 0.9, "times_validated": 3, "times_contradicted": 0}
    low = {"similarity": 0.9, "confidence": 0.2, "times_validated": 0, "times_contradicted": 2}
    assert mem._card_rank_score(high) > mem._card_rank_score(low)
    try:
        os.unlink(mem_path)
    except OSError:
        pass


def test_capability_level_capped_without_feedback(learning_db):
    learner, _ = learning_db
    learner.config = {
        "learning_evolution": {
            "cap_level_min_feedback": 10,
            "cap_level_max_without_feedback": 25,
        }
    }
    conn = learner._get_conn()
    for i in range(5):
        conn.execute(
            "INSERT INTO analysis_records "
            "(timestamp, symbol, timeframe, current_price, final_signal, "
            "institutional_results, trading_action) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"2026-06-14T{i:02d}:00:00", "BNBUSDT", "1h", 600.0,
                "BUY", "{}", "LONG",
            ),
        )
    conn.commit()
    snap = learner.get_growth_snapshot()
    assert snap["feedback_count"] < 10
    assert snap["capability_level"] <= 25


def test_agent_accuracy_recording(learning_db):
    learner, _ = learning_db
    deliberation = {
        "researcher": {"action": "LONG"},
        "quant": {"action": "LONG"},
        "learning": {"action": "WAIT"},
        "risk_verdict": {"action": "LONG"},
    }
    learner.record_agent_accuracy(
        deliberation, trade_side="LONG", outcome="WIN", record_id=1
    )
    summary = learner.get_agent_accuracy_summary()
    roles = {s["agent_role"] for s in summary}
    assert "researcher" in roles
    assert "quant" in roles
    researcher = next(s for s in summary if s["agent_role"] == "researcher")
    assert researcher["correct"] == 1


def test_factor_reliability_in_insights(learning_db):
    learner, _ = learning_db
    conn = learner._get_conn()
    conn.execute(
        "INSERT INTO factor_attribution "
        "(factor_key, regime, wins, losses, last_updated) "
        "VALUES (?,?,?,?,?)",
        ("ai_confidence", "GLOBAL", 8, 2, "2026-06-14"),
    )
    conn.commit()
    insights = learner.get_learning_insights()
    assert "factor_reliability" in insights
    assert insights["factor_reliability"].get("ai_confidence", 1.0) >= 1.0


def test_compute_reliability_multipliers():
    from bnb_quant_tool.factor_attribution_learner import compute_reliability_multipliers
    summary = [{"factor_key": "rsi_signal", "wins": 2, "losses": 8}]
    mult = compute_reliability_multipliers(summary, min_samples=3)
    assert mult["rsi_signal"] < 1.0


def test_shadow_param_evaluator_gate(learning_db):
    from bnb_quant_tool.shadow_param_evaluator import ShadowParamEvaluator
    learner, _ = learning_db
    conn = learner._get_conn()
    conn.execute(
        "INSERT INTO shadow_param_trials "
        "(timestamp, param_name, baseline_value, shadow_value, status) "
        "VALUES (?,?,?,?,?)",
        ("2026-06-14", "confidence_threshold", 0.6, 0.65, "active"),
    )
    conn.commit()
    spe = ShadowParamEvaluator(learner.db_path, {"trading": {"confidence_threshold": 0.6}})
    n = spe.evaluate_analysis(
        1,
        {"action": "LONG", "raw_action": "LONG", "passed_gate": True,
         "confidence": 0.7, "strength": "MODERATE", "risk_reward_ratio": 2.0},
        {"confidence": 0.7},
        {},
    )
    assert n >= 1
    trials = spe.get_trials_summary()
    assert trials[0]["gate_decisions"] >= 1
