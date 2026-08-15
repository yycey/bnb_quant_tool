"""决策解释 — WAIT 状态应展示投票明细而非笼统文案。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.decision_explainer import DecisionExplainer


def test_wait_zero_scores_shows_neutral_not_close():
    exp = DecisionExplainer().explain(
        action="WAIT",
        indicators={},
        ai_analysis={"signal": "持有", "confidence": 0.45, "trend": "震荡"},
        institutional={
            "consensus_signal": "HOLD",
            "consensus_confidence": 0.5,
            "buy_signals": 3,
            "sell_signals": 3,
            "hold_signals": 7,
        },
        votes={
            "decided_action": "WAIT",
            "long_score": 0.0,
            "short_score": 0.0,
            "vote_threshold": 0.10,
            "decision_reason": "vote_tie",
            "ai_direction": "WAIT",
            "ai_confidence": 0.45,
            "institutional_direction": "WAIT",
            "institutional_confidence": 0.5,
            "institutional_distribution": {"buy": 3, "sell": 3, "hold": 7, "total": 13},
            "dl_direction": "WAIT",
            "dl_confidence": 0.0,
        },
        gate_reasons=[
            "综合投票未分出方向（多 0.00 / 空 0.00，差 0.00 < 阈值 0.10）",
        ],
    )
    details = " ".join(f["detail"] for f in exp["factors"])
    assert "各方均未给出方向" in details
    assert "多空力量接近" not in details
    assert any(f["name"] == "AI 信号" for f in exp["factors"])
    assert any(f["name"] == "机构策略" for f in exp["factors"])
    assert "建议观望" in exp["text"]


def test_wait_close_scores_shows_threshold_message():
    exp = DecisionExplainer().explain(
        action="WAIT",
        indicators={},
        ai_analysis={"signal": "买入", "confidence": 0.55},
        votes={
            "decided_action": "WAIT",
            "long_score": 0.12,
            "short_score": 0.10,
            "vote_threshold": 0.10,
            "decision_reason": "vote_tie",
            "ai_direction": "LONG",
            "ai_confidence": 0.55,
            "institutional_direction": "WAIT",
            "institutional_confidence": 0.5,
            "dl_direction": "WAIT",
            "dl_confidence": 0.0,
        },
    )
    vote_factor = next(f for f in exp["factors"] if f["name"] == "综合投票")
    assert "差 0.02" in vote_factor["detail"]
    assert "未达到开仓阈值" in exp["text"] or "接近" in exp["text"]
