"""
BNB量化交易工具
使用 DeepSeek AI 和币安 API 分析 BNB 交易机会。

公开符号按需延迟导入，避免 GUI/CLI 冷启动时拖入全部重依赖。
"""

from __future__ import annotations

from typing import Any

__version__ = "3.0.0"
__author__ = "OpenClaw AI Assistant"

# name -> (module, attr)
_LAZY_EXPORTS = {
    "BinanceDataFetcher": (".data_fetcher", "BinanceDataFetcher"),
    "BitgetDataFetcher": (".bitget_fetcher", "BitgetDataFetcher"),
    "DeepSeekAnalyzer": (".ai_analyzer", "DeepSeekAnalyzer"),
    "TechnicalIndicators": (".technical_indicators", "TechnicalIndicators"),
    "TradingSignals": (".trading_signals", "TradingSignals"),
    "RiskManager": (".risk_manager", "RiskManager"),
    "TradeAdvisor": (".trade_advisor", "TradeAdvisor"),
    "BacktestEngine": (".backtest_engine", "BacktestEngine"),
    "BacktestResult": (".backtest_engine", "BacktestResult"),
    "MultiTimeframeAnalyzer": (".multi_timeframe", "MultiTimeframeAnalyzer"),
    "MarketSentiment": (".market_sentiment", "MarketSentiment"),
    "OnChainAnalyzer": (".onchain_analysis", "OnChainAnalyzer"),
    "EtherscanV2Client": (".etherscan_v2", "EtherscanV2Client"),
    "BSC_CHAIN_ID": (".etherscan_v2", "BSC_CHAIN_ID"),
    "MacroDataLayer": (".macro_data", "MacroDataLayer"),
    "BNBSpecificFactors": (".bnb_specific_factors", "BNBSpecificFactors"),
    "BNBEventCalendar": (".bnb_event_calendar", "BNBEventCalendar"),
    "EventPhase": (".bnb_event_calendar", "EventPhase"),
    "BNBRiskSentry": (".bnb_risk_sentry", "BNBRiskSentry"),
    "BNBBurnPredictor": (".bnb_burn_predictor", "BNBBurnPredictor"),
    "BNBChainHealthFactor": (".bnb_chain_health", "BNBChainHealthFactor"),
    "BinanceVolumeShareFactor": (".binance_volume_share_factor", "BinanceVolumeShareFactor"),
    "is_bnb_trading_pair": (".bnb_symbol", "is_bnb_trading_pair"),
    "LaunchpoolMiningFactor": (".launchpool_mining_factor", "LaunchpoolMiningFactor"),
    "AIGuardrail": (".ai_guardrail", "AIGuardrail"),
    "NewsCredibilityFilter": (".news_credibility", "NewsCredibilityFilter"),
    "BinanceAnnouncementNLP": (".binance_announcement_nlp", "BinanceAnnouncementNLP"),
    "PriceAlertEngine": (".price_alert", "PriceAlertEngine"),
    "PriceRule": (".price_alert", "PriceRule"),
    "NewsCollector": (".news_collector", "NewsCollector"),
    "TikHubTwitterClient": (".tikhub_twitter", "TikHubTwitterClient"),
    "PaperTradingEngine": (".paper_trading", "PaperTradingEngine"),
    "PaperPosition": (".paper_trading", "PaperPosition"),
    "DecisionExplainer": (".decision_explainer", "DecisionExplainer"),
    "PatternMemory": (".pattern_memory", "PatternMemory"),
    "CounterfactualAnalyzer": (".counterfactual_analyzer", "CounterfactualAnalyzer"),
    "StrategyLab": (".strategy_lab", "StrategyLab"),
    "DiscoveryConfig": (".strategy_lab", "DiscoveryConfig"),
    "MarketRegimeDetector": (".market_regime", "MarketRegimeDetector"),
    "DynamicPositionSizer": (".dynamic_position", "DynamicPositionSizer"),
    "score_closed_trade": (".trade_quality", "score_closed_trade"),
    "LearningEvolutionCoordinator": (".learning_evolution", "LearningEvolutionCoordinator"),
    "CapabilityMemory": (".capability_memory", "CapabilityMemory"),
    "load_analysis_record": (".capability_memory", "load_analysis_record"),
    "extract_knowledge_async": (".capability_memory", "extract_knowledge_async"),
    "KnowledgeExtractor": (".knowledge_extractor", "KnowledgeExtractor"),
    "KnowledgeVectorStore": (".knowledge_vector_store", "KnowledgeVectorStore"),
    "MultiAgentOrchestrator": (".agents", "MultiAgentOrchestrator"),
    "MarketContext": (".agents", "MarketContext"),
    "DeliberationResult": (".agents", "DeliberationResult"),
}

__all__ = [
    "__version__",
    *_LAZY_EXPORTS.keys(),
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    mod = import_module(module_name, __name__)
    value = getattr(mod, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
