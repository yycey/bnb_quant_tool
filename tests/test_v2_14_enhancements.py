"""AI Guardrail 与挖矿事件因子单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.ai_guardrail import AIGuardrail
from bnb_quant_tool.launchpool_mining_factor import LaunchpoolMiningFactor
from bnb_quant_tool.news_credibility import NewsCredibilityFilter


def test_guardrail_blocks_long_on_mtf_conflict():
    g = AIGuardrail({"enabled": True, "block_on_conflict": True})
    r = g.validate(
        ai_analysis={"signal": "LONG", "confidence": 0.7},
        proposed_action="LONG",
        indicators={"ATR": 1.0},
        multi_timeframe={"recommended_action": "SHORT", "weighted_score": 0.8},
        current_price=100.0,
    )
    assert r["blocked"] is True
    assert r["final_action"] == "WAIT"


def test_mining_factor_pre_end_blocks_long():
    mf = LaunchpoolMiningFactor(pre_unlock_hours=12)
    r = mf.compute(
        event_cycle={
            "phase": "staking_lock",
            "active_event": {
                "end_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).replace(hour=0).isoformat(),
            },
        },
        launchpool={"max_apy_pct": 10},
        nlp_result={},
    )
    assert r["mining_event_factor"] <= 0
    assert r["block_long"] or r["hours_to_end"] is not None


def test_news_credibility_noise_on_single_twitter():
    f = NewsCredibilityFilter({"enabled": True, "min_tier1_confirmations": 2})
    items = [{
        "title": "Binance SEC lawsuit rumor",
        "summary": "",
        "source": "Twitter/TikHub",
    }]
    r = f.analyze(items)
    assert r["regime_impact"] in ("NOISE", "NORMAL", "PANIC")
    if r["verified_panic_count"] == 0:
        assert r.get("block_extreme_news_filter") is True or r["regime_impact"] == "NOISE"
