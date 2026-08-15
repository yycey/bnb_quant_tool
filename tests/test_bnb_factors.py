"""BNB 专属因子模块单元测试（可离线运行 NLP 部分）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.binance_announcement_nlp import BinanceAnnouncementNLP
from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors, clamp


def test_nlp_sec_lawsuit_bearish():
    nlp = BinanceAnnouncementNLP()
    r = nlp.analyze_text("SEC files lawsuit against Binance CEO")
    assert r["score"] < 0
    assert r["impact_level"] in ("critical", "high", "medium")


def test_nlp_launchpool_bullish():
    nlp = BinanceAnnouncementNLP()
    items = [{
        "title": "Binance Launchpool: Stake BNB to earn 15% APY",
        "summary": "",
        "source": "BinanceAnnouncement",
    }]
    r = nlp.analyze_items(items)
    assert r["score"] > 0
    assert r["dominant_category"] == "launchpool"


def test_alpha_ols():
    import numpy as np
    bf = BNBSpecificFactors(fetcher=None, config={"enabled": True})
    y = np.array([0.01, 0.02, -0.01, 0.015])
    x1 = np.array([0.008, 0.015, -0.012, 0.01])
    x2 = np.array([0.005, 0.01, -0.008, 0.007])
    b1, b2 = bf._ols_betas(y, x1, x2)
    assert 0.5 < b1 < 2.0


def test_clamp():
    assert clamp(1.5) == 1.0
    assert clamp(-2) == -1.0
