"""规则引擎降级 + LLM 超时路径单元测试。"""

from bnb_quant_tool.llm_rule_fallback import (
    build_rule_engine_analysis,
    force_wait_analysis,
)
from bnb_quant_tool.llm_provider import _llm_request_timeout


def test_llm_request_timeout_default():
    assert _llm_request_timeout({}) == 15.0
    assert _llm_request_timeout({"llm": {"request_timeout_seconds": 20}}) == 20.0
    assert _llm_request_timeout({"llm": {"request_timeout_seconds": 1}}) == 3.0  # floor


def test_rule_engine_long_alignment():
    ind = {
        "close": 100.0,
        "MA_20": 99.0,
        "MA_50": 97.0,
        "MA_200": 90.0,
        "ATR": 1.0,
    }
    mtf = {"recommended_action": "LONG", "resonance_score": 60, "confluence": "强共振"}
    out = build_rule_engine_analysis(ind, multi_timeframe=mtf, reason="LLM超时")
    assert out["trade_suggestion"] == "LONG"
    assert out["_rule_fallback"] is True
    assert out["_llm_timeout"] is True
    assert out["stop_loss"] < out["entry_price"] < out["take_profit"]
    # max(1.5%*100, 1.5*ATR) = max(1.5, 1.5) = 1.5
    assert abs(out["entry_price"] - out["stop_loss"] - 1.5) < 1e-6


def test_rule_engine_conflict_wait():
    ind = {"close": 100.0, "MA_20": 99.0, "MA_50": 97.0}
    mtf = {"recommended_action": "SHORT", "resonance_score": -60}
    out = build_rule_engine_analysis(ind, multi_timeframe=mtf, reason="LLM超时")
    assert out["trade_suggestion"] == "WAIT"
    assert out.get("_rule_forced_wait") is True


def test_rule_engine_no_price_wait():
    out = build_rule_engine_analysis({}, multi_timeframe=None, reason="LLM超时")
    assert out["trade_suggestion"] == "WAIT"
    assert out.get("_rule_forced_wait") is True


def test_force_wait_analysis_flags():
    out = force_wait_analysis(reason="无数据", price=10.0)
    assert out["_provider"] == "rule_fallback"
    assert out["trade_suggestion"] == "WAIT"
