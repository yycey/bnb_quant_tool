"""策略反馈胜负判定 — 阶段1 修复回归测试。"""

from bnb_quant_tool.ai_learning_system import AILearningSystem


def test_long_win_buy_correct():
    assert AILearningSystem.is_strategy_signal_correct("BUY", "LONG", "WIN") is True


def test_long_win_sell_wrong():
    assert AILearningSystem.is_strategy_signal_correct("SELL", "LONG", "WIN") is False


def test_long_loss_buy_wrong():
    assert AILearningSystem.is_strategy_signal_correct("BUY", "LONG", "LOSS") is False


def test_long_loss_hold_correct():
    assert AILearningSystem.is_strategy_signal_correct("HOLD", "LONG", "LOSS") is True


def test_long_loss_sell_correct():
    assert AILearningSystem.is_strategy_signal_correct("SELL", "LONG", "LOSS") is True


def test_short_win_sell_correct():
    assert AILearningSystem.is_strategy_signal_correct("SELL", "SHORT", "WIN") is True


def test_short_loss_buy_correct():
    assert AILearningSystem.is_strategy_signal_correct("BUY", "SHORT", "LOSS") is True


def test_break_even_neutral():
    assert AILearningSystem.is_strategy_signal_correct("BUY", "LONG", "BREAK_EVEN") is None


def test_resolve_trade_direction():
    assert AILearningSystem._resolve_trade_direction("LONG", "HOLD") == "LONG"
    assert AILearningSystem._resolve_trade_direction("", "BUY") == "LONG"
    assert AILearningSystem._resolve_trade_direction("", "SELL") == "SHORT"
