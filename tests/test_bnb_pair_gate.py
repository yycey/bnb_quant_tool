"""BNB 交易对门控单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.bnb_symbol import is_bnb_trading_pair, normalize_symbol
from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors


def test_normalize_symbol():
    assert normalize_symbol("bnb/usdt") == "BNBUSDT"
    assert normalize_symbol("BNB-USDT") == "BNBUSDT"


def test_is_bnb_trading_pair():
    assert is_bnb_trading_pair("BNBUSDT") is True
    assert is_bnb_trading_pair("bnb/usdt") is True
    assert is_bnb_trading_pair("BNBBTC") is True
    assert is_bnb_trading_pair("BTCUSDT") is False
    assert is_bnb_trading_pair("ETHUSDT") is False
    assert is_bnb_trading_pair("SOLUSDT") is False


def test_fetch_all_skips_non_bnb():
    bf = BNBSpecificFactors(
        fetcher=None,
        config={"enabled": True, "apply_only_to_bnb_pairs": True},
    )
    r = bf.fetch_all(symbol="ETHUSDT")
    assert r["enabled"] is False
    assert r["skipped"] is True
    assert r["skip_reason"] == "non_bnb_pair"
    assert r["position_boost"] == 1.0
    assert r["bnb_score"] == 0.0
    assert BNBSpecificFactors.format_for_prompt(r) == ""


def test_fetch_all_allows_bnb_when_disabled_gate():
    bf = BNBSpecificFactors(
        fetcher=None,
        config={
            "enabled": True,
            "apply_only_to_bnb_pairs": False,
            "volume_share": {"enabled": False},
            "chain_health": {"enabled": False},
            "mining_factor": {"enabled": False},
            "news_credibility": {"enabled": False},
            "risk_sentry": {"enabled": False},
            "event_calendar": {"enabled": False},
        },
    )
    # 非 BNB 也会跑，但无网络时应返回结构（可能 alpha 为空）
    r = bf.fetch_all(symbol="ETHUSDT")
    assert r.get("skipped") is not True
    assert "bnb_score" in r
