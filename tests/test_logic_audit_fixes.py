"""逻辑审计修复：WAIT 复用 / 知识门控 / DecisionState / 写回守门。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bnb_quant_tool.analysis_reuse import (
    effective_reuse_actions,
    reuse_enabled,
    _card_is_oversold_rule,
)
from bnb_quant_tool.decision_state import (
    attach_decision_state,
    build_decision_state_from_advice,
    GATE_REGISTRY,
)
from bnb_quant_tool.knowledge_gate import apply_knowledge_gates, _matches_trigger
from bnb_quant_tool.knowledge_hygiene import is_hold_ban_wildcard_card, sanitize_hold_ban_cards
from bnb_quant_tool.trading_profile import apply_trading_profile, auto_expectancy_stats


def test_wait_not_in_effective_reuse_actions_by_default():
    cfg = {
        "capability_memory": {
            "enabled": True,
            "reuse_known_situation": True,
            "skip_llm_on_reuse": True,
            "allow_wait_reuse_skip_llm": False,
            "reuse_actions": ["WAIT", "LONG"],
        },
        "ai_trading": {"learning_phase": False},
    }
    acts = effective_reuse_actions(cfg)
    assert "WAIT" not in acts
    assert "LONG" in acts


def test_reuse_disabled_when_only_wait_and_wait_forbidden():
    cfg = {
        "capability_memory": {
            "enabled": True,
            "reuse_known_situation": True,
            "skip_llm_on_reuse": True,
            "allow_wait_reuse_skip_llm": False,
            "reuse_actions": ["WAIT"],
        },
        "ai_trading": {"learning_phase": False},
    }
    assert reuse_enabled(cfg) is False


def test_oversold_card_not_matched_as_overbought():
    assert _card_is_oversold_rule("极度超卖区禁止逆势做空 RSI < 30")


def test_knowledge_gate_rejects_empty_trigger_wildcard():
    action, reasons, _ = apply_knowledge_gates(
        "LONG",
        {
            "capability_cards": [
                {
                    "title": "空触发教训",
                    "category": "error_lesson",
                    "confidence": 0.95,
                    "times_validated": 500,
                    "trigger_condition": "",
                    "action_rule": "观望等待",
                    "lesson": "亏损",
                }
            ]
        },
    )
    assert action == "LONG"
    assert not reasons


def test_knowledge_gate_skips_hold_ban_wildcard():
    action, reasons, _ = apply_knowledge_gates(
        "SHORT",
        {
            "capability_cards": [
                {
                    "title": "严禁在HOLD信号及低质量评分下开仓",
                    "category": "error_lesson",
                    "confidence": 1.0,
                    "times_validated": 520,
                    "trigger_condition": "HOLD或质量D",
                    "action_rule": "强制空仓观望，绝不因主观臆断开仓",
                    "lesson": "HOLD时开仓必亏",
                }
            ]
        },
        market_regime={"regime": "ranging"},
    )
    assert action == "SHORT"


def test_matches_trigger_no_validated_wildcard():
    card = {
        "category": "error_lesson",
        "trigger_condition": "完全无关条件XYZ",
        "times_validated": 99,
        "lesson": "foo",
    }
    assert _matches_trigger(card, {"action": "LONG", "regime": "trending", "rsi": 50}) is False


def test_decision_state_ai_hold_block_reason():
    advice = {
        "action": "WAIT",
        "raw_action": "WAIT",
        "ai_action": "WAIT",
        "intended_direction": "WAIT",
        "passed_gate": False,
        "follow_ai_direction": True,
        "block_reason": "ai_hold",
        "gate_reasons": [
            "AI 建议持有 (block_reason=ai_hold)：跟单模式下以 AI HOLD 为准，非综合投票否决（参考票 多 0.10 / 空 0.00）"
        ],
        "confidence": 0.35,
    }
    advice = attach_decision_state(advice)
    assert advice["block_reason"] == "ai_hold"
    assert advice["executable"] is False
    ds = advice["decision_state"]
    assert ds["gated"] == "WAIT"
    assert any(b["code"] == "ai_hold" for b in ds["blockers"])


def test_gate_registry_has_post_ta_and_win_rate():
    assert GATE_REGISTRY["ta_playbook"]["phase"] == "post"
    assert GATE_REGISTRY["win_rate"]["skip_in_advisor_when_duplicate"] is True


def test_trading_profile_production_merges():
    cfg = {
        "trading_profile": "production",
        "trading_profiles": {
            "production": {
                "capability_memory": {"allow_wait_reuse_skip_llm": False},
                "ai_trading": {"advisor_skip_duplicate_post_gates": True},
            }
        },
        "capability_memory": {"enabled": True},
        "ai_trading": {"learning_phase": False},
    }
    out = apply_trading_profile(cfg)
    assert out["capability_memory"]["allow_wait_reuse_skip_llm"] is False
    assert out["ai_trading"]["advisor_skip_duplicate_post_gates"] is True
    assert out["_trading_profile_applied"] == "production"


def test_hold_ban_detector():
    assert is_hold_ban_wildcard_card({
        "title": "严禁在HOLD信号及低质量评分下开仓",
        "action_rule": "强制空仓观望",
    })


def test_existing_knowledge_gate_regime_still_blocks():
    action, reasons, tightening = apply_knowledge_gates(
        "LONG",
        {
            "capability_cards": [
                {
                    "title": "震荡市止损过紧",
                    "category": "error_lesson",
                    "confidence": 0.85,
                    "times_validated": 3,
                    "trigger_condition": "震荡市",
                    "action_rule": "震荡市不宜贴价止损，建议观望等待回踩",
                    "lesson": "连续亏损来自止损过紧",
                }
            ]
        },
        ai_confidence=0.7,
        market_regime={"regime": "range"},
        indicators={"RSI": 55},
    )
    assert action == "WAIT"
    assert reasons
