"""Headless runner smoke test"""

from pathlib import Path

from bnb_quant_tool.headless_runner import HeadlessAnalysisRunner


def test_headless_runner_init():
    root = Path(__file__).resolve().parents[1]
    runner = HeadlessAnalysisRunner(str(root))
    assert runner.trade_advisor is not None
    assert runner.learner is not None
    assert runner.paper_engine is not None
