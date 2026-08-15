#!/usr/bin/env python3
"""BNB Quant Tool — GUI application entry."""

from __future__ import annotations

import tkinter as tk

from gui.bootstrap import BootstrapMixin
from gui.layout import LayoutMixin
from gui.tab_advice import AdviceTabMixin
from gui.analysis import AnalysisMixin
from gui.tab_learning import LearningTabMixin
from gui.tab_evolution import EvolutionTimelineMixin
from gui.tab_grid import GridTabMixin
from gui.tab_backtest import BacktestTabMixin
from gui.tab_market import MarketTabMixin
from gui.tab_alert import AlertTabMixin
from gui.tab_news import NewsTabMixin
from gui.tab_paper import PaperTabMixin
from gui.tab_review import ReviewTabMixin
from gui.tab_signal import SignalTrackingTabMixin
from gui.tab_scanner import ScannerTabMixin
from gui.tab_traders import TradersTabMixin
from gui.learning_loop import LearningLoopMixin
from gui.brain_io import BrainIOMixin
from gui.automation import AutomationMixin


class BNBQuantGUI(
    BootstrapMixin,
    LayoutMixin,
    AdviceTabMixin,
    AnalysisMixin,
    LearningTabMixin,
    EvolutionTimelineMixin,
    GridTabMixin,
    BacktestTabMixin,
    MarketTabMixin,
    AlertTabMixin,
    NewsTabMixin,
    PaperTabMixin,
    ReviewTabMixin,
    SignalTrackingTabMixin,
    ScannerTabMixin,
    TradersTabMixin,
    LearningLoopMixin,
    BrainIOMixin,
    AutomationMixin,
):
    """BNB AI 交易分析 GUI — 以 AI 为核心决策引擎，融合多因子研判、风控门控与复盘学习"""


def main() -> None:
    root = tk.Tk()
    BNBQuantGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
