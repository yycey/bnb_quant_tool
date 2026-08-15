"""机构信念引擎测试"""

from bnb_quant_tool.institutional_conviction import compute_institutional_conviction


def test_conviction_trend_regime_favors_momentum():
    inst = {
        "strategy_details": {
            "citadel_momentum": {"signal": "BUY", "confidence": 0.8, "vote_weight": 1.5},
            "turtle_trading": {"signal": "BUY", "confidence": 0.75, "vote_weight": 1.2},
            "bollinger_bands": {"signal": "SELL", "confidence": 0.6, "vote_weight": 0.5},
        },
        "consensus_signal": "BUY",
        "buy_signals": 2,
        "sell_signals": 1,
    }
    result = compute_institutional_conviction(
        inst_results=inst,
        market_regime={"regime": "TRENDING"},
        indicators={"RSI": 58, "MACD": 1.2, "BB_Position": 55},
    )
    assert result["direction"] in ("LONG", "WAIT")
    assert result["conviction"] > 0
    assert result["regime_bucket"] == "TREND"


def test_conviction_funding_crowding_bearish():
    result = compute_institutional_conviction(
        inst_results={"strategy_details": {}, "buy_signals": 1, "sell_signals": 0},
        market_regime={"regime": "RANGING"},
        bnb_factors={
            "risk_sentry": {
                "funding_extreme": {
                    "rate": 0.0012,
                    "interpretation": "Funding 极正",
                }
            }
        },
        indicators={"RSI": 50, "MACD": 0, "BB_Position": 50},
    )
    funding_factors = [f for f in result["factors"] if "Funding" in f.get("name", "")]
    assert funding_factors
    assert funding_factors[0]["score"] < 0


def test_conviction_panic_reduces_exposure():
    result = compute_institutional_conviction(
        inst_results={
            "strategy_details": {
                "citadel_momentum": {"signal": "BUY", "confidence": 0.9, "vote_weight": 1.0},
            },
            "buy_signals": 1,
            "sell_signals": 0,
        },
        market_regime={"regime": "PANIC"},
        indicators={"RSI": 20, "MACD": -2, "BB_Position": 10},
    )
    assert abs(result["conviction"]) < 0.5
    assert any("Regime 风险缩放" in f.get("name", "") for f in result["factors"])
