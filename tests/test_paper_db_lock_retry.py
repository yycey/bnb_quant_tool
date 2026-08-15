"""paper_trading：database is locked 时应重试并最终成功。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bnb_quant_tool.paper_trading import PaperTradingEngine


@pytest.fixture()
def engine(tmp_path: Path):
    db = tmp_path / "paper_lock.db"
    cfg = {
        "risk_management": {"max_open_positions": 5},
        "paper_trading": {"slippage_enabled": False, "pin_filter_enabled": False},
        "trade_advisor": {"tp_split": {"tp1": "40%", "tp2": "35%", "tp3": "25%"}},
    }
    eng = PaperTradingEngine(db_path=str(db), config=cfg)
    eng.pin_filter_enabled = False
    eng.slippage_enabled = False
    return eng


def test_run_db_retries_on_locked(engine: PaperTradingEngine):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert engine._run_db(flaky, label="flaky", max_retries=5) == "ok"
    assert calls["n"] == 3


def test_run_db_resets_connection_after_lock(engine: PaperTradingEngine):
    engine._conn()  # warm
    assert getattr(engine._local, "conn", None) is not None
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return True

    assert engine._run_db(flaky, label="reset", max_retries=4) is True
    # 第二次尝试前应复位连接（attempt>=1）
    assert calls["n"] == 2


def test_close_manual_after_open(engine: PaperTradingEngine):
    advice = {
        "action": "LONG",
        "symbol": "BNBUSDT",
        "prices": {
            "entry_mid": 100.0,
            "stop_loss": 98.0,
            "tp1": 103.0,
            "tp2": 105.0,
            "tp3": 108.0,
        },
        "position": {"quantity": 1.0, "usdt_amount": 100.0, "leverage_suggest": 1},
    }
    pid = engine.open_from_advice(advice)
    assert pid
    assert engine.close_manual(int(pid), 102.0, reason="TIMEOUT_NO_TP") is True
    row = engine._get_position_row(int(pid))
    assert row is not None and row["status"] == "CLOSED"
