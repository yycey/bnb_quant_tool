"""持续开平验证模式测试。"""

from __future__ import annotations

import json
from pathlib import Path

from bnb_quant_tool.ai_trading_context import (
    apply_learning_phase_probe,
    should_open_from_advice,
    use_relaxed_follow,
)
from bnb_quant_tool.validation_trading import (
    get_validation_stats,
    is_validation_trading_enabled,
    record_validation_close,
    record_validation_open,
    should_encourage_validation_opens,
)


def test_validation_mode_enabled_by_profile():
    cfg = {"_trading_profile_applied": "validation", "validation_trading": {"enabled": True}}
    assert is_validation_trading_enabled(cfg) is True


def test_validation_probe_bypasses_soft_gate():
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {"enabled": True, "probe_open": True},
        "ai_trading": {
            "learning_phase": False,
            "require_gate_pass": True,
            "probe_confidence_floor": 0.48,
            "probe_position_scale": 0.35,
        },
    }
    advice = {
        "action": "WAIT",
        "raw_action": "LONG",
        "passed_gate": False,
        "confidence": 0.62,
        "confidence_hard_blocked": True,
        "prices": {"entry_mid": 600.0, "stop_loss": 590.0, "tp1": 610.0},
        "position": {"usdt_amount": 200.0},
        "votes": {"ai_direction": "LONG"},
    }
    out = apply_learning_phase_probe(advice, cfg)
    assert out["action"] == "LONG"
    assert out.get("validation_probe") is True
    assert out.get("passed_gate") is True
    assert should_open_from_advice(out, cfg) is True


def test_validation_rejects_full_size_without_probe():
    """软门控未过且未打 probe 旗标时，禁止全仓旁路开仓。"""
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {"enabled": True},
        "ai_trading": {"require_gate_pass": False},  # 即使旧配置误开也要挡
    }
    advice = {
        "action": "WAIT",
        "raw_action": "LONG",
        "passed_gate": False,
        "confidence": 0.30,  # 低于 probe floor
        "votes": {"ai_direction": "LONG"},
    }
    assert should_open_from_advice(advice, cfg) is False
    from bnb_quant_tool.ai_trading_context import needs_relaxed_open
    assert needs_relaxed_open(advice, cfg) is False


def test_validation_profile_merge_applies_probe_knobs():
    from bnb_quant_tool.trading_profile import apply_trading_profile
    raw = {
        "trading_profile": "validation",
        "trading_profiles": {
            "validation": {
                "validation_trading": {"enabled": True, "probe_open": True},
                "ai_trading": {
                    "require_gate_pass": True,
                    "min_net_rr": 1.2,
                    "min_open_confidence": 0.58,
                },
            }
        },
        "ai_trading": {"require_gate_pass": True, "min_net_rr": 1.5},
        "validation_trading": {"enabled": False},
    }
    cfg = apply_trading_profile(raw)
    assert cfg.get("_trading_profile_applied") == "validation"
    assert cfg["validation_trading"]["enabled"] is True
    assert float(cfg["ai_trading"]["min_net_rr"]) == 1.2


def test_validation_open_close_log(tmp_path, monkeypatch):
    log = tmp_path / "val.jsonl"
    cfg = {
        "validation_trading": {
            "enabled": True,
            "log_path": str(log),
        }
    }
    advice = {
        "symbol": "BNBUSDT",
        "action": "LONG",
        "confidence": 0.7,
        "votes": {"ai_direction": "LONG", "institutional_consensus": "BUY"},
        "validation_probe": True,
    }
    record_validation_open(position_id=1, advice=advice, record_id=99, config=cfg)
    record_validation_close(
        position_id=1,
        trade_row={"side": "LONG", "r_multiple": 1.2, "mfe_r": 0.8, "close_reason": "TP1"},
        outcome="WIN",
        pnl=15.0,
        config=cfg,
    )
    stats = get_validation_stats(cfg)
    assert stats["opens"] == 1
    assert stats["closed"] == 1
    assert stats["correct"] == 1
    assert stats["accuracy_pct"] == 100.0
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    close_row = json.loads(lines[1])
    assert close_row["validated_correct"] is True


def test_should_encourage_when_density_low():
    cfg = {"validation_trading": {"enabled": True, "target_opens_per_day": 0.5}}
    assert should_encourage_validation_opens(
        cfg,
        paper_stats={"total_trades": 2},
        funnel={"analysis_triggered": 20, "opened": 0},
    ) is True


def test_validation_probe_uses_vote_when_ai_hold():
    """AI HOLD 但投票 LONG → 验证小仓应能开。"""
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {
            "enabled": True,
            "probe_open": True,
            "allow_vote_direction_probe": True,
            "bypass_circuit_for_probe": True,
            "probe_confidence_floor": 0.32,
            "vote_direction_confidence_floor": 0.50,
            "probe_position_scale": 0.30,
        },
        "ai_trading": {"require_gate_pass": True},
    }
    advice = {
        "action": "WAIT",
        "raw_action": "WAIT",
        "passed_gate": False,
        "confidence": 0.35,
        "current_price": 600.0,
        "prices": {},
        "votes": {"ai_direction": "WAIT", "vote_action": "LONG"},
        "gate_reasons": ["AI 建议持有 (block_reason=ai_hold)"],
    }
    from bnb_quant_tool.ai_trading_context import (
        apply_learning_phase_probe,
        get_validation_probe_direction,
        should_open_from_advice,
    )
    assert get_validation_probe_direction(advice) == "LONG"
    out = apply_learning_phase_probe(advice, cfg)
    assert out["action"] == "LONG"
    assert out.get("validation_probe") is True
    assert should_open_from_advice(out, cfg) is True


def test_validation_probe_uses_scanner_when_ai_hold():
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {
            "enabled": True,
            "probe_open": True,
            "allow_vote_direction_probe": True,
            "bypass_circuit_for_probe": True,
            "probe_confidence_floor": 0.32,
            "vote_direction_confidence_floor": 0.50,
            "probe_position_scale": 0.30,
        },
        "ai_trading": {"require_gate_pass": True},
    }
    advice = {
        "action": "WAIT",
        "raw_action": "WAIT",
        "passed_gate": False,
        "confidence": 0.30,
        "current_price": 600.0,
        "prices": {},
        "votes": {"ai_direction": "WAIT", "vote_action": "WAIT"},
        "scanner_signal": {
            "direction": "BULLISH",
            "trade_direction": "LONG",
            "signal_type": "PRICE_SHOCK",
            "strength": 0.8,
        },
        # 软门控 / 连亏类可探针；不含 ATR/黑天鹅硬危险
        "gate_reasons": ["AI HOLD", "置信度不足"],
    }
    from bnb_quant_tool.ai_trading_context import (
        apply_learning_phase_probe,
        get_validation_probe_direction,
        should_open_from_advice,
    )
    assert get_validation_probe_direction(advice) == "LONG"
    out = apply_learning_phase_probe(advice, cfg)
    assert out["action"] == "LONG"
    assert out.get("validation_probe") is True
    assert should_open_from_advice(out, cfg) is True


def test_validation_probe_cannot_bypass_atr_hard():
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {
            "enabled": True,
            "probe_open": True,
            "bypass_circuit_for_probe": True,
            "allow_vote_direction_probe": True,
            "probe_confidence_floor": 0.32,
            "vote_direction_confidence_floor": 0.50,
        },
    }
    # 使用熔断器真实文案（含空格）：「ATR 突变」
    advice = {
        "action": "WAIT",
        "raw_action": "WAIT",
        "passed_gate": False,
        "confidence": 0.55,
        "current_price": 600.0,
        "prices": {},
        "votes": {"vote_action": "LONG"},
        "circuit_breaker_blocked": True,
        "circuit_breaker": {
            "allowed": False,
            "level": "STOPPED",
            "reasons": ["⚡ ATR 突变 3.2x ≥ 3x，只平不开"],
        },
        "gate_reasons": ["⚡ ATR 突变 3.2x ≥ 3x，只平不开"],
    }
    from bnb_quant_tool.ai_trading_context import (
        apply_learning_phase_probe,
        should_open_from_advice,
    )
    out = apply_learning_phase_probe(advice, cfg)
    assert should_open_from_advice(out, cfg) is False
    assert out.get("validation_probe") is not True or should_open_from_advice(out, cfg) is False


def test_validation_probe_bypasses_consec_loss_circuit():
    cfg = {
        "_trading_profile_applied": "validation",
        "validation_trading": {
            "enabled": True,
            "probe_open": True,
            "bypass_circuit_for_probe": True,
            "circuit_bypass_scale": 0.20,
            "probe_confidence_floor": 0.32,
            "allow_vote_direction_probe": True,
            "vote_direction_confidence_floor": 0.50,
        },
    }
    advice = {
        "action": "WAIT",
        "raw_action": "LONG",
        "passed_gate": False,
        "confidence": 0.55,
        "circuit_breaker_blocked": True,
        "current_price": 600.0,
        "prices": {"entry_mid": 600.0, "stop_loss": 590.0, "tp1": 615.0},
        "position": {"usdt_amount": 200.0},
        "votes": {"ai_direction": "LONG"},
        "gate_reasons": ["🛑 连续亏损 6 笔 ≥ 5，强制停手", "冷却中"],
    }
    from bnb_quant_tool.ai_trading_context import apply_learning_phase_probe, should_open_from_advice
    out = apply_learning_phase_probe(advice, cfg)
    assert out.get("validation_probe") is True
    assert out["action"] == "LONG"
    assert out.get("circuit_breaker_blocked") is False
    assert should_open_from_advice(out, cfg) is True
    # 仓位应被 circuit_bypass_scale 缩小
    assert float(out["position"]["usdt_amount"]) <= 200.0 * 0.20 + 1e-6
