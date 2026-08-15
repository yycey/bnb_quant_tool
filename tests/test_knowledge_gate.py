"""知识门控与平仓学习管道测试"""

import os
import sqlite3
import tempfile

import pytest

from bnb_quant_tool.knowledge_gate import apply_knowledge_gates


def test_knowledge_gate_blocks_high_conf_error_lesson():
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
    assert tightening >= 0


def test_knowledge_gate_tightens_stop_loss_rule():
    action, reasons, tightening = apply_knowledge_gates(
        "LONG",
        {
            "capability_cards": [
                {
                    "title": "RSI高位不做多",
                    "category": "stop_loss_rule",
                    "confidence": 0.8,
                    "times_validated": 2,
                    "trigger_condition": "RSI>65",
                    "action_rule": "RSI>65 时提高开仓门槛",
                    "lesson": "高位追多易亏",
                }
            ]
        },
        indicators={"RSI": 70},
    )
    assert action == "LONG"
    assert tightening > 0


def test_knowledge_gate_no_cards_passthrough():
    action, reasons, tightening = apply_knowledge_gates("SHORT", {})
    assert action == "SHORT"
    assert not reasons
    assert tightening == 0.0
