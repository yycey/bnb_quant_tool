"""MFE 阶梯锁盈 + 同向叠仓拒开。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bnb_quant_tool.paper_trading import PaperTradingEngine, SIDE_LONG, SIDE_SHORT


@pytest.fixture()
def engine(tmp_path: Path):
    db = tmp_path / "paper_test.db"
    cfg = {
        "risk_management": {"max_open_positions": 1},
        "paper_trading": {
            "slippage_enabled": False,
            "pin_filter_enabled": False,
            "reject_same_side_stack": True,
            "stats_auto_only_default": True,
            "mfe_lock": {
                "enabled": True,
                "fee_buffer_pct": 0.0015,
                "tiers": [
                    {"mfe_r": 0.6, "lock_r": 0.0},
                    {"mfe_r": 1.0, "lock_r": 0.3},
                    {"mfe_r": 1.5, "lock_r": 0.7},
                ],
            },
        },
        "ai_trading": {"stats_auto_only_default": True},
        "trade_advisor": {"tp_split": {"tp1": "40%", "tp2": "35%", "tp3": "25%"}},
    }
    eng = PaperTradingEngine(db_path=str(db), config=cfg)
    # 关闭插针确认，便于单测瞬时触发
    eng.pin_filter_enabled = False
    eng.slippage_enabled = False
    return eng


def _advice(side: str = "LONG", entry: float = 100.0, sl: float = 98.0, **extra):
    risk = abs(entry - sl)
    if side == "LONG":
        tp1, tp2, tp3 = entry + 3 * risk, entry + 5 * risk, entry + 8 * risk
    else:
        tp1, tp2, tp3 = entry - 3 * risk, entry - 5 * risk, entry - 8 * risk
    a = {
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
    a.update(extra)
    return a


def test_mfe_lock_moves_sl_to_breakeven_then_survives_pullback(engine: PaperTradingEngine):
    """浮盈 ≥0.6R 后移保本；回撤到原 SL 不应全损，应触发新 SL 小盈/保本。"""
    pid = engine.open_from_advice(_advice("LONG", 100.0, 98.0), equity_usdt=10_000)
    assert pid is not None
    row = engine._get_position_row(pid)
    assert row["sl"] == pytest.approx(98.0)

    # 推到 0.6R：risk=2，0.6R → price=101.2
    events = engine.tick("BNBUSDT", 101.2)
    types = [e["type"] for e in events]
    assert "MFE_LOCK" in types
    row = engine._get_position_row(pid)
    # 保本 + fee buffer
    assert row["sl"] > 100.0
    assert row["mfe_r"] >= 0.6

    # 回撤到锁盈 SL 附近（约 entry+fee），应小亏/保本，而非旧 SL=98 的 -1R
    events2 = engine.tick("BNBUSDT", 100.0)
    assert any(e["type"] == "SL" for e in events2)
    closed = engine._get_position_row(pid)
    assert closed["status"] == "CLOSED"
    # 锁盈后出场应接近保本（允许手续费）
    assert closed["realized_pnl_usdt"] > -0.5
    assert float(closed.get("r_multiple") or 0) > -0.4


def test_mfe_lock_short_tier(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice("SHORT", 100.0, 102.0), equity_usdt=10_000)
    assert pid is not None
    # 0.6R：price = 100 - 0.6*2 = 98.8
    engine.tick("BNBUSDT", 98.8)
    row = engine._get_position_row(pid)
    assert row["sl"] < 100.0  # 空头保本在 entry 下方缓冲


def test_reject_same_side_stack(engine: PaperTradingEngine):
    pid1 = engine.open_from_advice(_advice("LONG"), equity_usdt=10_000)
    assert pid1 is not None
    pid2 = engine.open_from_advice(_advice("LONG"), equity_usdt=10_000)
    assert pid2 is None
    # 异向也拒（同品种已有仓）
    pid3 = engine.open_from_advice(_advice("SHORT", 100.0, 102.0), equity_usdt=10_000)
    assert pid3 is None


def test_get_stats_auto_only_excludes_manual(engine: PaperTradingEngine):
    pid = engine.open_from_advice(_advice("LONG"), equity_usdt=10_000)
    # 手动平掉盈利
    engine.close_manual(pid, 105.0, reason="MANUAL")
    # 再开一笔自动止损亏
    pid2 = engine.open_from_advice(_advice("LONG", 100.0, 98.0), equity_usdt=10_000)
    engine.pin_filter_enabled = False
    engine.tick("BNBUSDT", 97.5)  # hit SL

    stats_auto = engine.get_stats(auto_only=True)
    stats_all = engine.get_stats(auto_only=False)
    assert stats_auto["manual_excluded"] >= 1
    assert stats_auto["total_trades"] < stats_all["total_trades"]
    assert "expectancy_r" in stats_auto
    assert stats_auto["auto_only"] is True


def test_long_strict_gate():
    from bnb_quant_tool.learning_gates import apply_long_strict_gate

    cfg = {
        "ai_trading": {
            "long_strict_gate_enabled": True,
            "long_min_confidence": 0.72,
            "long_min_net_rr": 2.0,
        }
    }
    blocked = apply_long_strict_gate(
        {
            "action": "LONG",
            "confidence": 0.60,
            "net_rr": {"net_rr": 1.5},
            "passed_gate": True,
        },
        config=cfg,
    )
    assert blocked["action"] == "WAIT"
    assert blocked.get("long_strict_blocked")

    ok = apply_long_strict_gate(
        {
            "action": "LONG",
            "confidence": 0.80,
            "net_rr": {"net_rr": 2.5},
            "passed_gate": True,
        },
        config=cfg,
    )
    assert ok["action"] == "LONG"

    short_ok = apply_long_strict_gate(
        {"action": "SHORT", "confidence": 0.5, "passed_gate": True},
        config=cfg,
    )
    assert short_ok["action"] == "SHORT"
