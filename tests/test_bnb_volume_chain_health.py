"""币安量能份额 + BNB Chain 健康因子单元测试（离线）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.binance_volume_share_factor import BinanceVolumeShareFactor
from bnb_quant_tool.bnb_chain_health import BNBChainHealthFactor
from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors


def test_volume_share_attention_scoring():
    f = BinanceVolumeShareFactor(config={"enabled": True, "baseline_bnb_btc_ratio": 0.08})
    # 强制注入量能：高注意力
    f._fetch_spot_quote_volumes = lambda symbol: (16_000_000.0, 100_000_000.0, "mock")
    f._fetch_futures_quote_volume = lambda symbol: (None, "none")
    f._compute_volume_momentum = lambda symbol: {
        "vol_ratio": 1.4,
        "momentum_score": 0.5,
        "source": "mock",
    }
    r = f.fetch("BNBUSDT")
    assert r["enabled"] is True
    assert r["attention_ratio"] == 0.16
    assert r["volume_share_score"] > 0.2
    assert r["rising"] is True


def test_volume_share_fading():
    f = BinanceVolumeShareFactor(config={"enabled": True, "baseline_bnb_btc_ratio": 0.08})
    f._fetch_spot_quote_volumes = lambda symbol: (4_000_000.0, 100_000_000.0, "mock")
    f._fetch_futures_quote_volume = lambda symbol: (None, "none")
    f._compute_volume_momentum = lambda symbol: {
        "vol_ratio": 0.6,
        "momentum_score": -0.5,
        "source": "mock",
    }
    r = f.fetch("BNBUSDT")
    assert r["volume_share_score"] < -0.2
    assert r["fading"] is True


def test_chain_health_security_bridge_hack_blocks():
    ch = BNBChainHealthFactor(config={"enabled": True})
    ch._fetch_tvl = lambda: {
        "tvl_usd": 5e9,
        "tvl_change_pct": 2.0,
        "tvl_score": 0.1,
        "source": "mock",
    }
    ch._fetch_stablecoins = lambda: {
        "stable_usd": 1e9,
        "stable_change_pct": 1.0,
        "stable_score": 0.05,
        "source": "mock",
    }
    news = [{
        "title": "Major BSC bridge hacked, $50M drained",
        "summary": "Attackers exploited the bridge contract",
    }]
    r = ch.fetch(news_items=news)
    assert r["block_long"] is True
    assert r["chain_health_score"] <= -0.55
    assert r["security"]["hit_count"] >= 1


def test_chain_health_no_false_positive_on_normal_news():
    ch = BNBChainHealthFactor(config={"enabled": True})
    ch._fetch_tvl = lambda: {
        "tvl_usd": 5e9,
        "tvl_change_pct": 6.0,
        "tvl_score": 0.4,
        "source": "mock",
    }
    ch._fetch_stablecoins = lambda: {
        "stable_usd": 1e9,
        "stable_change_pct": 4.0,
        "stable_score": 0.35,
        "source": "mock",
    }
    r = ch.fetch(news_items=[{"title": "BNB price rises on market rally", "summary": ""}])
    assert r["block_long"] is False
    assert r["healthy"] is True
    assert r["chain_health_score"] > 0.2


def test_aggregate_includes_new_factors():
    bf = BNBSpecificFactors(
        fetcher=None,
        config={
            "enabled": True,
            "volume_share": {"enabled": False},
            "chain_health": {"enabled": False},
            "mining_factor": {"enabled": False},
            "news_credibility": {"enabled": False},
            "risk_sentry": {"enabled": False},
            "event_calendar": {"enabled": False},
        },
    )
    score = bf._aggregate_score(
        {"launchpool_score": 0.0},
        {"alpha_score": 0.0},
        {"score": 0.0},
        volume_share={"volume_share_score": 1.0},
        chain_health={"chain_health_score": 1.0},
    )
    assert score > 0.1

    blocked = bf._aggregate_score(
        {"launchpool_score": 0.5},
        {"alpha_score": 0.5},
        {"score": 0.5},
        chain_health={"chain_health_score": -0.2, "block_long": True},
    )
    assert blocked <= -0.45

    bias = bf._trade_bias(
        0.5, {}, {}, {}, chain_health={"block_long": True},
    )
    assert bias == "WAIT"
