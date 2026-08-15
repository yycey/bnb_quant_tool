"""熔断器测试"""

from unittest.mock import MagicMock

from bnb_quant_tool.circuit_breaker import CircuitBreaker


def test_circuit_breaker_stops_on_consecutive_losses():
    engine = MagicMock()
    engine.get_closed_positions.return_value = [
        {"realized_pnl_usdt": -10, "closed_at": "2026-07-10T10:00:00"},
        {"realized_pnl_usdt": -5, "closed_at": "2026-07-10T09:00:00"},
        {"realized_pnl_usdt": -3, "closed_at": "2026-07-10T08:00:00"},
        {"realized_pnl_usdt": -2, "closed_at": "2026-07-10T07:00:00"},
        {"realized_pnl_usdt": -1, "closed_at": "2026-07-10T06:00:00"},
        {"realized_pnl_usdt": 20, "closed_at": "2026-07-09T10:00:00"},
    ]
    breaker = CircuitBreaker(
        paper_engine=engine,
        config={"consec_loss_stop": 5, "consec_loss_half": 3, "cooldown_hours": 0},
    )
    breaker.reset_cooldown()
    result = breaker.check()
    assert result["allowed"] is False
    assert result["level"] == "STOPPED"
    assert result["consec_losses"] >= 5


def test_circuit_breaker_reduces_on_three_losses():
    engine = MagicMock()
    engine.get_closed_positions.return_value = [
        {"realized_pnl_usdt": -1, "closed_at": "2026-07-10T10:00:00"},
        {"realized_pnl_usdt": -1, "closed_at": "2026-07-10T09:00:00"},
        {"realized_pnl_usdt": -1, "closed_at": "2026-07-10T08:00:00"},
        {"realized_pnl_usdt": 10, "closed_at": "2026-07-09T10:00:00"},
    ]
    breaker = CircuitBreaker(
        paper_engine=engine,
        config={"consec_loss_stop": 5, "consec_loss_half": 3, "cooldown_hours": 0},
    )
    breaker.reset_cooldown()
    result = breaker.check()
    assert result["allowed"] is True
    assert result["level"] == "REDUCED"
    assert result["position_factor"] <= 0.5
