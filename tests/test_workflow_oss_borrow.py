"""订单流 / 晋升漏斗 / Scoreboard 单元测试（离线，不依赖实盘网络）。"""

from __future__ import annotations

from bnb_quant_tool.institutional_conviction import compute_institutional_conviction
from bnb_quant_tool.orderflow_signal import (
    OrderflowSignalLayer,
    orderflow_conviction_score,
)
from bnb_quant_tool.promotion_funnel import PromotionFunnel, PromotionStage
from bnb_quant_tool.scoreboard import build_scoreboard
from bnb_quant_tool.workflow_pipeline import WorkflowPipeline


def test_orderflow_conviction_score_empty():
    assert orderflow_conviction_score(None)[0] is None
    assert orderflow_conviction_score({"available": False})[0] is None


def test_orderflow_conviction_score_bullish():
    of = {
        "available": True,
        "orderflow_score": 0.6,
        "interpretation": "大单偏多",
    }
    score, text = orderflow_conviction_score(of)
    assert score == 0.6
    assert "大单" in text or "偏多" in text


def test_orderflow_soft_vote_from_aggregate():
    layer = OrderflowSignalLayer(config={"orderflow": {"enabled": True}})
    large = {"available": True, "imbalance": 0.8, "buy_usd": 80000, "sell_usd": 20000}
    taker = {"available": True, "score": 0.5, "buy_sell_ratio": 1.3}
    top = {"available": True, "score": 0.2, "long_short_ratio": 1.2}
    score, parts = layer._aggregate(large, taker, top)
    assert score > 0.3
    assert parts
    vote = layer._soft_vote(score, "BULLISH")
    assert vote["signal"] == "BUY"


def test_conviction_includes_orderflow_factor():
    result = compute_institutional_conviction(
        inst_results={"strategy_details": {}, "buy_signals": 0, "sell_signals": 0},
        market_regime={"regime": "RANGING"},
        indicators={"RSI": 50, "MACD": 0, "BB_Position": 50},
        orderflow={
            "available": True,
            "orderflow_score": 0.7,
            "interpretation": "订单流偏多",
        },
    )
    names = [f["name"] for f in result.get("factors") or []]
    assert "订单流微观结构" in names


def test_promotion_funnel_stages():
    funnel = PromotionFunnel({"promotion_funnel": {"require_oos": True}})
    weak = {
        "id": "auto_0001",
        "name": "Weak",
        "metrics": {"total_trades": 5, "win_rate": 0.4, "profit_factor": 0.8, "total_return_pct": -1},
    }
    rec = funnel.evaluate_spec(weak)
    assert rec.stage in (
        PromotionStage.REJECTED.value,
        PromotionStage.DISCOVERED.value,
    )

    strong = {
        "id": "auto_0002",
        "name": "Strong",
        "metrics": {
            "total_trades": 40,
            "win_rate": 0.58,
            "profit_factor": 1.6,
            "total_return_pct": 12.0,
            "sharpe_ratio": 1.2,
        },
        "walk_forward": {"passed": True, "reason": "OOS 通过"},
    }
    rec2 = funnel.evaluate_spec(
        strong,
        paper_stats={"total_trades": 20, "win_rate": 0.55, "expectancy_r": 0.12},
    )
    assert rec2.stage == PromotionStage.PAPER_VALIDATED.value


def test_scoreboard_build_without_engine():
    board = build_scoreboard(config={})
    assert "paper" in board
    assert "decision_funnel" in board
    assert board["health"] in ("insufficient", "healthy", "watch", "unhealthy")


def test_workflow_pipeline_mark():
    wp = WorkflowPipeline({})
    wp.mark("signal", ok=True, n=1)
    wp.mark("proposal", ok=True)
    d = wp.to_dict()
    assert "signal" in d["snapshots"]
    assert d["stages"][0] == "signal"
