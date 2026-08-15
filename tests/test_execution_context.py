"""执行层解读：分析方向 vs 风控结论 vs 跟单资格."""

from bnb_quant_tool.ai_trading_context import (
    get_effective_follow_direction,
    needs_relaxed_open,
    resolve_execution_context,
    should_open_from_advice,
)


def _advice(action="WAIT", raw="LONG", passed=False):
    return {
        "action": action,
        "raw_action": raw,
        "passed_gate": passed,
        "gate_reasons": ["测试门控"],
        "prices": {"entry_mid": 600.0, "stop_loss": 590.0, "tp2": 620.0},
    }


def test_effective_direction_falls_back_to_raw():
    assert get_effective_follow_direction(_advice()) == "LONG"


def test_should_open_without_gate_when_configured():
    cfg = {"ai_trading": {"require_gate_pass": False}}
    assert should_open_from_advice(_advice(), cfg) is True


def test_should_not_open_when_gate_required_and_failed():
    cfg = {"ai_trading": {"require_gate_pass": True}}
    assert should_open_from_advice(_advice(), cfg) is False


def test_resolve_shows_follow_when_gate_relaxed():
    cfg = {"ai_trading": {"require_gate_pass": False}}
    ctx = resolve_execution_context(_advice(), cfg, auto_follow_enabled=True)
    assert ctx["analysis_direction"] == "LONG"
    assert ctx["final_action"] == "WAIT"
    assert ctx["will_follow"] is True
    assert "LONG" in ctx["follow_label"]


def test_needs_relaxed_when_action_wait_but_follow_long():
    cfg = {"ai_trading": {"require_gate_pass": False}}
    assert needs_relaxed_open(_advice(), cfg) is True
