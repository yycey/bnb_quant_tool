"""v3.0 硬化：探针翻边价、熔断冷却恢复、连亏 ignore、软超时保浮盈。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from bnb_quant_tool.ai_trading_context import (
    _count_consecutive_losses,
    _ensure_probe_prices,
)
from bnb_quant_tool.circuit_breaker import CircuitBreaker
from bnb_quant_tool.position_exit_policy import evaluate_timeout, resolve_timeout_policy


def test_probe_prices_rewrite_wrong_side_on_direction_flip():
    advice = {
        "current_price": 600.0,
        "prices": {
            "entry_mid": 600.0,
            "atr": 6.0,
            # 原 LONG 书，探针翻 SHORT 时必须重写
            "stop_loss": 591.0,
            "tp1": 609.0,
            "tp2": 618.0,
            "tp3": 627.0,
        },
    }
    out = _ensure_probe_prices(advice, "SHORT")
    prices = out["prices"]
    assert prices["stop_loss"] > 600.0
    assert prices["tp1"] < 600.0
    assert prices["tp2"] < prices["tp1"]


def test_probe_prices_keep_valid_side():
    advice = {
        "prices": {
            "entry_mid": 600.0,
            "atr": 6.0,
            "stop_loss": 591.0,
            "tp1": 609.0,
        }
    }
    out = _ensure_probe_prices(advice, "LONG")
    assert out["prices"]["stop_loss"] == 591.0
    assert out["prices"]["tp1"] == 609.0


def test_circuit_breaker_recovers_after_cooldown():
    engine = MagicMock()
    engine.get_closed_positions.return_value = [
        {"realized_pnl_usdt": -10, "close_reason": "STOP_LOSS"},
        {"realized_pnl_usdt": -5, "close_reason": "STOP_LOSS"},
        {"realized_pnl_usdt": -3, "close_reason": "STOP_LOSS"},
        {"realized_pnl_usdt": -2, "close_reason": "STOP_LOSS"},
        {"realized_pnl_usdt": -1, "close_reason": "STOP_LOSS"},
    ]
    breaker = CircuitBreaker(
        paper_engine=engine,
        config={"consec_loss_stop": 5, "consec_loss_half": 3, "cooldown_hours": 0.001},
    )
    breaker.reset_cooldown()
    first = breaker.check()
    assert first["allowed"] is False
    assert first["level"] == "STOPPED"
    # 模拟冷却结束
    breaker._last_stop_time = time.time() - 10
    second = breaker.check()
    assert second["allowed"] is True
    assert second["position_factor"] <= 0.5
    assert second["level"] == "REDUCED"


def test_circuit_breaker_does_not_refresh_cooldown_while_stopped():
    engine = MagicMock()
    engine.get_closed_positions.return_value = [
        {"realized_pnl_usdt": -1, "close_reason": "STOP_LOSS"},
    ] * 5
    breaker = CircuitBreaker(
        paper_engine=engine,
        config={"consec_loss_stop": 5, "cooldown_hours": 4},
    )
    breaker.reset_cooldown()
    breaker.check()
    t0 = breaker._last_stop_time
    assert t0 is not None
    time.sleep(0.02)
    breaker.check()
    assert breaker._last_stop_time == t0


def test_consec_losses_skip_admit_and_timeout():
    recent = [
        {"realized_pnl_usdt": -2.0, "close_reason": "ADMIT_WRONG"},
        {"realized_pnl_usdt": -1.0, "close_reason": "TIMEOUT_NO_TP"},
        {"realized_pnl_usdt": -3.0, "close_reason": "STOP_LOSS"},
        {"realized_pnl_usdt": 5.0, "close_reason": "TP1"},
    ]
    assert _count_consecutive_losses(recent) == 1


def test_soft_timeout_skips_healthy_runner():
    cfg = {
        "paper_trading": {
            "max_position_age_hours": 48,
            "soft_exit": {
                "enabled": True,
                "min_hours": 2,
                "hours": 6,
                "require_tp1_not_hit": True,
                "max_live_r": 0.25,
            },
        }
    }
    policy = resolve_timeout_policy(cfg)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    pos = {
        "opened_at": (now - timedelta(hours=7)).isoformat(),
        "tp1_hit": 0,
        "side": "LONG",
        "entry_price": 100.0,
        "sl_initial": 98.0,
    }
    # +0.5R 浮盈 → 软超时跳过
    assert evaluate_timeout(pos, policy, now=now, price=101.0) is None
    # 接近持平/小亏 → 软超时平
    hit = evaluate_timeout(pos, policy, now=now, price=100.2)
    assert hit is not None and hit.reason == "TIMEOUT_NO_TP"


def test_version_is_3():
    from bnb_quant_tool import __version__

    assert __version__.startswith("3.")
