"""回归：学习期试探不可绕过熔断/风控；SHORT pnl 方向。"""

from bnb_quant_tool.ai_trading_context import (
    _learning_probe_hard_blocked,
    apply_learning_phase_probe,
    should_open_from_advice,
)

_CFG = {
    "ai_trading": {
        "learning_phase": True,
        "learning_phase_probe_open": True,
        "learning_phase_use_relaxed": True,
        "require_gate_pass": True,
    }
}


def test_learning_probe_blocked_by_circuit_breaker_flag():
    advice = {
        "action": "WAIT",
        "raw_action": "LONG",
        "passed_gate": False,
        "gate_reasons": ["🛑 连续亏损 5 笔 ≥ 5，强制停手"],
        "circuit_breaker_blocked": True,
        "confidence": 0.7,
        "prices": {"entry_mid": 600, "stop_loss": 590, "tp1": 610},
        "current_price": 600,
    }
    assert _learning_probe_hard_blocked(advice) == "熔断硬拦"
    out = apply_learning_phase_probe(advice, _CFG)
    assert out.get("action") == "WAIT"
    assert not out.get("learning_phase_probe")
    assert not should_open_from_advice(out, _CFG)


def test_learning_probe_blocked_by_risk_veto():
    advice = {
        "action": "WAIT",
        "raw_action": "SHORT",
        "passed_gate": False,
        "gate_reasons": ["🛑 风控否决: 波动过大"],
        "risk_vetoed": True,
        "confidence": 0.8,
        "prices": {"entry_mid": 600, "stop_loss": 610, "tp1": 590},
        "current_price": 600,
    }
    assert _learning_probe_hard_blocked(advice) == "风控否决硬拦"
    out = apply_learning_phase_probe(advice, _CFG)
    assert out.get("action") == "WAIT"
    assert not should_open_from_advice(out, _CFG)


def test_learning_probe_blocked_by_consec_loss_keyword():
    advice = {
        "action": "WAIT",
        "raw_action": "LONG",
        "passed_gate": False,
        "gate_reasons": ["🛑 连续亏损 5 笔 ≥ 5，强制停手"],
        "confidence": 0.7,
        "prices": {"entry_mid": 600, "stop_loss": 590, "tp1": 610},
        "current_price": 600,
    }
    reason = _learning_probe_hard_blocked(advice)
    assert reason is not None
    assert "连续亏损" in reason or "强制停手" in reason


def test_short_feedback_pnl_percent_sign(tmp_path):
    from bnb_quant_tool.ai_learning_system import AILearningSystem

    db = tmp_path / "learn.db"
    learner = AILearningSystem(db_path=str(db), config={})
    conn = learner._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO analysis_records
        (timestamp, symbol, timeframe, current_price, final_signal,
         trading_action, entry_price, stop_loss, take_profit)
        VALUES ('2026-01-01T00:00:00', 'BNBUSDT', '1h', 100.0, '卖出',
                'SHORT', 100.0, 105.0, 90.0)
        """
    )
    rid = cur.lastrowid
    conn.commit()
    # 价格跌到 90 → SHORT 应盈利 +10%
    ok = learner.submit_feedback(rid, "WIN", actual_price=90.0, notes="test")
    assert ok
    cur.execute("SELECT pnl_percent FROM analysis_records WHERE id=?", (rid,))
    pnl = float(cur.fetchone()[0])
    assert pnl > 0
    assert abs(pnl - 10.0) < 0.01
