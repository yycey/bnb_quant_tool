"""平仓学习闭环：缺记录不断链、议会回写、幂等。"""

from __future__ import annotations

from pathlib import Path

from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.agents.trader_memory import TraderMemoryStore
from bnb_quant_tool.trade_close_learning import (
    TradeCloseLearningDeps,
    process_trade_close,
)


def _make_learner(tmp_path: Path) -> AILearningSystem:
    return AILearningSystem(
        db_path=str(tmp_path / "ai_learning.db"),
        config={"learning": {"min_samples": 3, "create_stub_record_if_missing": True}},
    )


def test_close_without_record_creates_stub_and_learns(tmp_path: Path):
    learner = _make_learner(tmp_path)
    positions = {
        1: {
            "id": 1,
            "side": "LONG",
            "symbol": "BNBUSDT",
            "entry_price": 600.0,
            "close_avg_price": 620.0,
            "realized_pnl_usdt": 20.0,
            "close_reason": "TAKE_PROFIT_FULL",
            "learning_record_id": None,
            "advice_snapshot": "{}",
            "sl_initial": 580.0,
            "tp1": 620.0,
        }
    }

    deps = TradeCloseLearningDeps(
        learner=learner,
        config={"learning": {"create_stub_record_if_missing": True, "update_council_memory": False}},
        get_position_row=lambda pid: positions.get(int(pid)),
    )
    result = process_trade_close(1, deps)
    assert result.feedback_ok is True
    assert result.stub_record is True
    assert result.record_id is not None
    assert result.outcome == "WIN"
    assert result.progressed is True

    # 幂等：第二次不重复学习
    result2 = process_trade_close(1, deps)
    assert result2.skipped_duplicate is True


def test_council_outcomes_update_trader_weights(tmp_path: Path):
    learner = _make_learner(tmp_path)
    # 先建一条正式分析记录，带议会投票
    rid = learner.ensure_stub_analysis_record_for_position(
        {
            "id": 9,
            "side": "LONG",
            "symbol": "BNBUSDT",
            "entry_price": 100.0,
            "close_avg_price": 110.0,
            "advice_snapshot": {},
        }
    )
    assert rid
    import json
    conn = learner._get_conn()
    cur = conn.cursor()
    delib = {
        "council": {
            "votes": [
                {"trader_id": "momentum__deepseek", "action": "LONG", "confidence": 0.7},
                {"trader_id": "momentum__qianwen", "action": "SHORT", "confidence": 0.6},
                {"trader_id": "macro__deepseek", "action": "WAIT", "confidence": 0.4},
            ]
        }
    }
    cur.execute(
        "UPDATE analysis_records SET multi_agent_deliberation=? WHERE id=?",
        (json.dumps(delib), int(rid)),
    )
    conn.commit()

    tm = TraderMemoryStore(str(tmp_path / "trader_memory.db"))
    positions = {
        2: {
            "id": 2,
            "side": "LONG",
            "symbol": "BNBUSDT",
            "entry_price": 100.0,
            "close_avg_price": 110.0,
            "realized_pnl_usdt": 15.0,
            "close_reason": "TAKE_PROFIT_FULL",
            "learning_record_id": rid,
            "advice_snapshot": "{}",
            "sl_initial": 90.0,
            "tp1": 110.0,
        }
    }
    deps = TradeCloseLearningDeps(
        learner=learner,
        config={"learning": {"update_council_memory": True}},
        get_position_row=lambda pid: positions.get(int(pid)),
        trader_memory=tm,
    )
    result = process_trade_close(2, deps)
    assert result.feedback_ok
    assert result.council_updated == 3
    # LONG 投对 → 权重应高于 SHORT 投错（样本<5 时仍为 1.0，但 outcomes 已写入）
    acc_long = tm.get_accuracy("momentum__deepseek")
    acc_short = tm.get_accuracy("momentum__qianwen")
    assert acc_long["total"] == 1 and acc_long["correct"] == 1
    assert acc_short["total"] == 1 and acc_short["correct"] == 0


def test_paper_close_learned_no_prefix_collision(tmp_path: Path):
    """#1 已学习时，#10/#12/#100 不得被 LIKE 前缀误判。"""
    learner = _make_learner(tmp_path)
    learner.mark_paper_close_learned(1, {"outcome": "WIN"})
    assert learner.has_paper_close_learned(1) is True
    assert learner.has_paper_close_learned(10) is False
    assert learner.has_paper_close_learned(12) is False
    assert learner.has_paper_close_learned(100) is False
    learner.mark_paper_close_learned(10, {"outcome": "LOSS"})
    assert learner.has_paper_close_learned(10) is True
    assert learner.has_paper_close_learned(1) is True
    assert learner.has_paper_close_learned(100) is False
