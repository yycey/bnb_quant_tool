"""持仓软/硬超时：未止盈尽快平，最长不超过 48h；认错用当前逆向。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bnb_quant_tool.paper_trading import PaperTradingEngine, _parse_opened_at


@pytest.fixture()
def engine(tmp_path: Path):
    db = tmp_path / "paper_timeout.db"
    cfg = {
        "risk_management": {"max_open_positions": 3},
        "paper_trading": {
            "slippage_enabled": False,
            "pin_filter_enabled": False,
            "max_position_age_hours": 48,
            "soft_exit": {
                "enabled": True,
                "min_hours": 2,
                "hours": 10,
                "require_tp1_not_hit": True,
            },
            "admit_wrong": {
                "enabled": True,
                "min_age_minutes": 30,
                "adverse_r": 0.35,
                "skip_if_tp1_hit": True,
            },
        },
        "trade_advisor": {"tp_split": {"tp1": "40%", "tp2": "35%", "tp3": "25%"}},
    }
    eng = PaperTradingEngine(db_path=str(db), config=cfg)
    eng.pin_filter_enabled = False
    eng.slippage_enabled = False
    eng.set_price_provider(lambda _sym: 100.0)
    return eng


def _advice(side: str = "LONG", entry: float = 100.0, sl: float = 98.0):
    risk = abs(entry - sl)
    if side == "LONG":
        tp1, tp2, tp3 = entry + 3 * risk, entry + 5 * risk, entry + 8 * risk
    else:
        tp1, tp2, tp3 = entry - 3 * risk, entry - 5 * risk, entry - 8 * risk
    return {
        "action": side,
        "symbol": "BNBUSDT",
        "prices": {
            "entry_mid": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
        },
        "position": {"quantity": 1.0, "usdt_amount": entry, "leverage_suggest": 1},
    }


def _backdate(engine: PaperTradingEngine, pid: int, hours_ago: float) -> None:
    opened = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(
        timespec="seconds"
    )
    with engine._lock:
        conn = engine._conn()
        conn.execute(
            "UPDATE paper_positions SET opened_at=? WHERE id=?",
            (opened, pid),
        )
        conn.commit()


def test_parse_opened_at_aware_and_naive():
    aware = _parse_opened_at("2026-08-13T12:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None
    naive = _parse_opened_at("2026-08-13T12:00:00")
    assert naive is not None and naive.tzinfo is not None


def test_soft_exit_closes_when_no_tp1_after_10h(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice(), equity_usdt=10_000)
    assert pid is not None
    _backdate(engine, pid, 10.5)
    opens = engine.get_open_positions()
    engine._check_position_timeout(opens)
    row = engine._get_position_row(pid)
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "TIMEOUT_NO_TP"


def test_soft_exit_works_with_real_utc_opened_at(engine: PaperTradingEngine):
    """回归：opened_at 带 +00:00 时，不可用本地 naive now 导致超时永不触发。"""
    pid = engine.open_from_advice(_advice(), equity_usdt=10_000)
    row = engine._get_position_row(pid)
    assert "+" in str(row["opened_at"]) or "Z" in str(row["opened_at"]).upper()
    _backdate(engine, pid, 10.5)
    engine._check_position_timeout(engine.get_open_positions())
    assert engine._get_position_row(pid)["close_reason"] == "TIMEOUT_NO_TP"


def test_soft_exit_skips_if_tp1_already_hit(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice(), equity_usdt=10_000)
    assert pid is not None
    with engine._lock:
        conn = engine._conn()
        conn.execute(
            "UPDATE paper_positions SET tp1_hit=1 WHERE id=?",
            (pid,),
        )
        conn.commit()
    _backdate(engine, pid, 12.0)
    engine._check_position_timeout(engine.get_open_positions())
    row = engine._get_position_row(pid)
    assert row["status"] == "OPEN"


def test_hard_timeout_closes_even_if_tp1_hit(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice(), equity_usdt=10_000)
    assert pid is not None
    with engine._lock:
        conn = engine._conn()
        conn.execute(
            "UPDATE paper_positions SET tp1_hit=1 WHERE id=?",
            (pid,),
        )
        conn.commit()
    _backdate(engine, pid, 49.0)
    engine._check_position_timeout(engine.get_open_positions())
    row = engine._get_position_row(pid)
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "TIMEOUT"


def test_no_soft_exit_before_window(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice(), equity_usdt=10_000)
    assert pid is not None
    _backdate(engine, pid, 5.0)
    engine._check_position_timeout(engine.get_open_positions())
    row = engine._get_position_row(pid)
    assert row["status"] == "OPEN"


def test_resolve_timeout_policy_defaults(engine: PaperTradingEngine):
    policy = engine._resolve_timeout_policy()
    assert policy["hard_hours"] == 48.0
    assert policy["soft_hours"] == 10.0
    assert policy["soft_min_hours"] == 2.0
    assert policy["soft_enabled"] == 1.0


def test_admit_wrong_closes_on_live_adverse_r(engine: PaperTradingEngine):
    engine.config["paper_trading"]["admit_wrong"] = {
        "enabled": True,
        "min_age_minutes": 30,
        "adverse_r": 0.35,
        "skip_if_tp1_hit": True,
    }
    engine.set_price_provider(lambda _s: 99.0)  # -0.5R if risk=2
    pid = engine.open_from_advice(_advice(entry=100.0, sl=98.0), equity_usdt=10_000)
    assert pid is not None
    _backdate(engine, pid, hours_ago=1.0)
    engine._check_admit_wrong(engine.get_open_positions())
    row = engine._get_position_row(pid)
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "ADMIT_WRONG"


def test_admit_wrong_skips_recovered_after_mae(engine: PaperTradingEngine):
    """历史 MAE 很大但价格已回本 → 不应认错砍仓。"""
    engine.config["paper_trading"]["admit_wrong"] = {
        "enabled": True,
        "min_age_minutes": 30,
        "adverse_r": 0.35,
    }
    engine.set_price_provider(lambda _s: 100.2)  # 略盈
    pid = engine.open_from_advice(_advice(entry=100.0, sl=98.0), equity_usdt=10_000)
    _backdate(engine, pid, hours_ago=1.0)
    with engine._lock:
        conn = engine._conn()
        # DB 里 mae_r 常为负
        conn.execute("UPDATE paper_positions SET mae_r=-0.8 WHERE id=?", (pid,))
        conn.commit()
    engine._check_admit_wrong(engine.get_open_positions())
    assert engine._get_position_row(pid)["status"] == "OPEN"


def test_admit_wrong_skips_young_position(engine: PaperTradingEngine):
    engine.config["paper_trading"]["admit_wrong"] = {
        "enabled": True,
        "min_age_minutes": 30,
        "adverse_r": 0.35,
    }
    engine.set_price_provider(lambda _s: 99.0)
    pid = engine.open_from_advice(_advice(entry=100.0, sl=98.0), equity_usdt=10_000)
    engine._check_admit_wrong(engine.get_open_positions())
    assert engine._get_position_row(pid)["status"] == "OPEN"
