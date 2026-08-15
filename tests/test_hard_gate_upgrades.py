"""硬门升级：Platt 校准 / 全成本净 RR / 跨模态冲突。"""

from bnb_quant_tool.confidence_calibration import fit_platt, apply_platt, calibrate_confidence
from bnb_quant_tool.cross_modal_conflict import score_cross_modal, apply_cross_modal_conflict_gate
from bnb_quant_tool.kelly_sizing import net_reward_risk_ratio
from bnb_quant_tool.trade_cost_model import expected_funding_cost_pct
from bnb_quant_tool.learning_gates import apply_confidence_hard_gate, apply_net_rr_gate


def test_platt_fit_and_apply_shrinks_overconfidence():
    # 高置信但一半失败 → 校准应压低高置信
    xs = [0.9] * 10 + [0.6] * 10
    ys = [1, 0] * 5 + [1] * 5 + [0] * 5
    model = fit_platt(xs, ys)
    assert model["n"] == 20
    cal_hi = apply_platt(0.9, model)
    # 不应仍接近 0.9（过度自信被压）
    assert cal_hi < 0.9


def test_calibrate_without_model_mild_shrink():
    cal, meta = calibrate_confidence(0.9, regime="TRENDING", config={
        "confidence_calibration": {"enabled": True, "auto_refit": False}
    }, auto_refit=False)
    assert meta["raw"] == 0.9
    assert 0.0 < cal <= 0.9


def test_confidence_hard_uses_calibrated():
    advice = {
        "action": "LONG",
        "confidence": 0.9,
        "calibrated_confidence": 0.40,
        "raw_confidence": 0.9,
        "position": {"quantity": 1.0},
    }
    out = apply_confidence_hard_gate(
        advice,
        config={"ai_trading": {
            "confidence_hard_gate": True,
            "probe_confidence_floor": 0.55,
            "min_open_confidence": 0.70,
        }},
    )
    assert out["action"] == "WAIT"
    assert out.get("confidence_hard_blocked") is True


def test_net_rr_with_full_costs_lower_than_gross():
    prices = {"entry_mid": 100.0, "stop_loss": 97.0, "tp1": 106.0}
    # risk=3%, reward=6% → gross RR=2
    ratio_cheap, _ = net_reward_risk_ratio(
        prices, fee_rate=0.0, slippage_pct=0.0, funding_pct=0.0, liquidity_premium_pct=0.0
    )
    ratio_full, detail = net_reward_risk_ratio(
        prices,
        fee_rate=0.0004,
        slippage_pct=0.003,
        funding_pct=0.001,
        hold_periods=1,
        liquidity_premium_pct=0.0005,
    )
    assert ratio_cheap > ratio_full
    assert detail["liquidity_premium_pct"] > 0
    # 更高成本应把净 RR 压到门槛以下
    ratio_harsh, _ = net_reward_risk_ratio(
        prices,
        fee_rate=0.001,
        slippage_pct=0.005,
        funding_pct=0.003,
        hold_periods=1,
        liquidity_premium_pct=0.001,
    )
    assert ratio_harsh < 1.5
    out = apply_net_rr_gate(
        {"action": "LONG", "prices": prices, "hold_hours": 48},
        config={"ai_trading": {
            "net_rr_gate_enabled": True,
            "min_net_rr": 1.5,
            "fee_rate": 0.001,
            "slippage_pct": 0.005,
            "liquidity_premium_pct": 0.001,
            "default_funding_abs_rate": 0.001,
            "funding_period_hours": 8,
        }},
    )
    assert out["action"] == "WAIT"
    assert out.get("net_rr_blocked") is True


def test_funding_cost_long_pays_positive_rate():
    cost, meta = expected_funding_cost_pct(
        action="LONG", funding_rate=0.001, hold_hours=16, config={
            "ai_trading": {"funding_period_hours": 8}
        }
    )
    assert abs(cost - 0.002) < 1e-9  # 2 periods * 0.001
    assert meta["periods"] == 2.0


def test_cross_modal_conflict_blocks():
    advice = {"action": "LONG", "confidence": 0.8}
    out = apply_cross_modal_conflict_gate(
        advice,
        config={"cross_modal_conflict": {
            "enabled": True, "conflict_ratio": 3.0, "min_reverse_weight": 0.5
        }},
        onchain={"onchain_score": -0.9},
        news_summary={"polarity": "bearish", "confidence": 0.9},
        macro={"risk_regime": "risk-off"},
    )
    assert out["action"] == "WAIT"
    assert out.get("cross_modal_blocked") is True


def test_cross_modal_aligned_passes():
    advice = {"action": "LONG", "confidence": 0.8}
    out = apply_cross_modal_conflict_gate(
        advice,
        config={"cross_modal_conflict": {"enabled": True, "conflict_ratio": 3.0}},
        onchain={"onchain_score": 0.8},
        news_summary={"polarity": "bullish", "confidence": 0.7},
        macro={"risk_regime": "risk-on"},
    )
    assert out["action"] == "LONG"
    scored = score_cross_modal(
        "LONG",
        onchain={"onchain_score": 0.8},
        news_summary={"polarity": "bullish", "confidence": 0.7},
    )
    assert scored["same_weight"] > scored["reverse_weight"]
