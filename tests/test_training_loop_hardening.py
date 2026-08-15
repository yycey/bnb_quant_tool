from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from bnb_quant_tool.ai_review_engine import AIReviewEngine
from bnb_quant_tool.circuit_breaker import CircuitBreaker
from bnb_quant_tool.strategy_pool import (
    get_strategy_pool,
    register_strategy_pool,
    reload_discovered_strategies,
)
from bnb_quant_tool.validation_trading import open_density_per_day


def test_open_density_uses_recent_opens_not_lifetime(tmp_path: Path):
    # 终身 total_trades 不再参与密度；无 lookback 数据时为 0
    empty_db = tmp_path / "empty_paper.db"
    import sqlite3
    conn = sqlite3.connect(str(empty_db))
    conn.execute(
        "CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, opened_at TEXT)"
    )
    conn.commit()
    conn.close()
    d = open_density_per_day(
        {"total_trades": 570},
        lookback_days=7,
        paper_db_path=str(empty_db),
    )
    assert d == 0.0
    d2 = open_density_per_day({"opens_last_days": 14}, lookback_days=7)
    assert abs(d2 - 2.0) < 1e-9


def test_circuit_breaker_skips_admit_wrong_in_consec():
    engine = MagicMock()
    engine.get_closed_positions.return_value = [
        {"realized_pnl_usdt": -5.0, "close_reason": "ADMIT_WRONG"},
        {"realized_pnl_usdt": -3.0, "close_reason": "TIMEOUT_NO_TP"},
        {"realized_pnl_usdt": -2.0, "close_reason": "SL"},
        {"realized_pnl_usdt": -1.0, "close_reason": "SL"},
        {"realized_pnl_usdt": 4.0, "close_reason": "TP1"},
    ]
    cb = CircuitBreaker(
        paper_engine=engine,
        config={
            "enabled": True,
            "consec_loss_half": 2,
            "consec_loss_stop": 5,
            "consec_ignore_reasons": ["ADMIT_WRONG", "TIMEOUT", "TIMEOUT_NO_TP"],
        },
    )
    assert cb._get_current_consec_losses() == 2


def test_review_streak_cooldown_and_single_flight():
    eng = AIReviewEngine(
        config={"auto_review": {"trigger_on_streak": 3, "streak_review_cooldown_sec": 3600}},
        deepseek_api_key="x",
    )
    paper = MagicMock()
    paper.get_stats.return_value = {"total_closed_trades": 7}
    paper.get_recent_trades.return_value = [
        {"pnl": -1}, {"pnl": -1}, {"pnl": -1},
    ]
    ok, reason = eng.should_trigger_review(paper)
    assert ok is True
    assert "连续" in reason
    eng.mark_review_triggered(streak=True)
    ok2, reason2 = eng.should_trigger_review(paper)
    assert ok2 is False
    assert "冷却" in reason2

    assert eng.try_begin_review() is True
    assert eng.try_begin_review() is False
    eng.end_review()
    assert eng.try_begin_review() is True
    eng.end_review()


def test_strategy_pool_reload_calls_pool(tmp_path: Path):
    pool = MagicMock()
    pool.reload_discovered.return_value = 3
    register_strategy_pool(pool)
    assert get_strategy_pool() is pool
    out = reload_discovered_strategies(reason="test", project_root=tmp_path)
    assert out["ok"] is True
    assert out["reloaded"] == 3
    pool.reload_discovered.assert_called_once()
    sig = tmp_path / "data" / "strategy_pool_signal.json"
    assert sig.is_file()


def test_cross_process_signal_reload(tmp_path: Path):
    from bnb_quant_tool import strategy_pool as sp

    sp._last_applied_version = 0
    pool = MagicMock()
    pool.reload_discovered.return_value = 2
    register_strategy_pool(pool)

    bump = sp.bump_reload_signal(reason="watcher_promote", project_root=tmp_path)
    assert bump["version"] == 1

    # 模拟另一进程已 bump，本进程消费
    out = sp.maybe_reload_from_signal(project_root=tmp_path)
    assert out.get("skipped") is not True
    assert out.get("from_signal") is True
    pool.reload_discovered.assert_called()

    out2 = sp.maybe_reload_from_signal(project_root=tmp_path)
    assert out2.get("skipped") is True
    assert out2.get("reason") == "up_to_date"


def test_shadow_close_hook_records_active(tmp_path: Path):
    from bnb_quant_tool.shadow_close_hooks import record_paper_close_on_shadows

    mutator = MagicMock()
    mutator.list_shadow_strategies.return_value = [
        {"strategy_id": "mut_1"},
        {"strategy_id": "mut_2"},
    ]
    with patch(
        "bnb_quant_tool.strategy_mutator.StrategyMutator",
        return_value=mutator,
    ), patch(
        "bnb_quant_tool.data_localization.get_localized_db_path",
        return_value=str(tmp_path / "ai.db"),
    ):
        out = record_paper_close_on_shadows(
            pnl=-1.5,
            win=False,
            config={"training_loop": {"enabled": True, "shadow_record_on_close": True}},
            config_path=str(tmp_path / "config.yaml"),
        )
    assert out["ok"] is True
    assert out["recorded"] == 2
    assert mutator.record_shadow_trade.call_count == 2
