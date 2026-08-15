#!/usr/bin/env python3
"""
BNB量化交易工具 - 图形界面版本 v3.0
集成大机构研究策略 + AI学习成长系统
每次分析自动记录，支持反馈让AI越来越准
"""

import sys
import json
import os
import logging
import threading
import time
import zipfile
import shutil
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
BRAIN_FILES = (
    "config.yaml",
    "data/ai_learning.db",
    "data/paper_trading.db",
    "data/pattern_memory.db",
)
sys.path.insert(0, str(PROJECT_ROOT / "src"))


logger = logging.getLogger(__name__)

from bnb_quant_tool.data_fetcher import BinanceDataFetcher
from bnb_quant_tool.ai_analyzer import DeepSeekAnalyzer
from bnb_quant_tool.technical_indicators import TechnicalIndicators
from bnb_quant_tool.trading_signals import TradingSignals
from bnb_quant_tool.risk_manager import RiskManager
from bnb_quant_tool.institutional_strategies import InstitutionalStrategies
from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.trade_advisor import TradeAdvisor
from bnb_quant_tool.backtest_engine import BacktestEngine
from bnb_quant_tool.decision_explainer import DecisionExplainer
from bnb_quant_tool.pattern_memory import PatternMemory
from bnb_quant_tool.counterfactual_analyzer import CounterfactualAnalyzer
from bnb_quant_tool.multi_timeframe import MultiTimeframeAnalyzer
from bnb_quant_tool.market_sentiment import MarketSentiment
from bnb_quant_tool.onchain_analysis import OnChainAnalyzer
from bnb_quant_tool.macro_data import MacroDataLayer
from bnb_quant_tool.price_alert import PriceAlertEngine
from bnb_quant_tool.news_collector import NewsCollector
from bnb_quant_tool.paper_trading import PaperTradingEngine
from bnb_quant_tool.market_regime import MarketRegimeDetector
from bnb_quant_tool.dynamic_position import DynamicPositionSizer
from bnb_quant_tool.ai_trading_context import (
    enrich_learning_insights,
    build_analysis_learning_context,
    build_full_analysis_learning_context,
    analysis_extra_market,
    apply_pattern_memory_gate,
    should_open_from_advice,
    use_relaxed_follow,
)
from bnb_quant_tool.config_access import get_max_open_positions, is_position_limit_reached
from bnb_quant_tool.trade_quality import score_closed_trade
from bnb_quant_tool.signal_scanner import SignalScanner, ScanSignal
from bnb_quant_tool.agents import MultiAgentOrchestrator, MarketContext
import yaml
from typing import Optional, Dict, List

# 网格策略导入
from bnb_quant_tool.grid_strategy import GridStrategy


