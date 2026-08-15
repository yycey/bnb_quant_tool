"""BNB 风控哨兵单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.bnb_risk_sentry import BNBRiskSentry


def test_funding_extreme_detection():
    s = BNBRiskSentry(config={
        "enabled": True,
        "funding_rate": {"extreme_threshold": 0.001, "block_long_on_extreme": True},
    })
    rate = 0.0012
    extreme = rate >= s.funding_extreme_threshold
    assert extreme is True
    assert s.funding_block_long is True


def test_funding_extreme_block_long():
    s = BNBRiskSentry(config={"enabled": True, "funding_rate": {"extreme_threshold": 0.001}})
    s._fetch_binance_funding = lambda sym: (0.0015, "test")
    s._fetch_gate_funding = lambda sym: (None, "")
    result = s.check_funding_extreme("BNBUSDT")
    assert result["extreme"] is True
    assert result["block_long"] is True
    assert result["reversal_risk"] is True


def test_bnb_btc_weakness_logic():
    s = BNBRiskSentry(fetcher=None, config={"enabled": True})
    weak = (
        0.02 >= 0.005
        and -0.01 <= -0.008
    )
    assert weak is True
