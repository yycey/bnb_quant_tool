"""StrategyLab walk-forward OOS 守门测试"""

import numpy as np
import pandas as pd

from bnb_quant_tool.strategy_lab import DiscoveryConfig, StrategyLab


def _sample_df(n=400):
    rng = np.random.default_rng(99)
    rets = rng.normal(0.0002, 0.008, n)
    prices = 600 * np.cumprod(1 + rets)
    return pd.DataFrame({
        "open": prices,
        "high": prices * 1.01,
        "low": prices * 0.99,
        "close": prices,
        "volume": rng.integers(800, 2000, n).astype(float),
    })


def test_walk_forward_validate_structure():
    lab = StrategyLab(config=DiscoveryConfig(walk_forward_oos=True, oos_train_ratio=0.7))
    df = _sample_df(350)
    spec = {
        "id": "auto_test",
        "rules": [
            {"feature": "RSI", "op": "<", "threshold": 35, "action": 1},
            {"feature": "RSI", "op": ">", "threshold": 65, "action": -1},
        ],
        "buy_score": 1,
        "sell_score": -1,
    }
    wf = lab.walk_forward_validate(df, spec)
    assert "passed" in wf
    assert "train_wr" in wf
    assert "test_wr" in wf
    assert "reason" in wf
