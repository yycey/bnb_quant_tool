#!/usr/bin/env python3
"""独立模拟盘监控进程 — 不依赖 GUI 也能触发 SL/TP/超时平仓

系统版本: 3.0
- BNB 事件周期日历自动切换风控参数
- BNB 风控哨兵（资金费率极值）
- 与 autopilot 内嵌 watcher 共用 paper_watcher.lock，防双写
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from bnb_quant_tool.process_runtime import (  # noqa: E402
    ensure_singleton,
    load_project_dotenv,
    setup_logging,
)

load_project_dotenv(PROJECT_DIR)
setup_logging("paper_watcher", log_dir=PROJECT_DIR / "data" / "logs")
logger = logging.getLogger("paper_watcher")

from bnb_quant_tool.data_localization import init_workspace

init_workspace(str(PROJECT_DIR))

from bnb_quant_tool.ai_learning_system import AILearningSystem
from bnb_quant_tool.ai_review_engine import AIReviewEngine
from bnb_quant_tool.advisor_risk_sync import apply_event_cycle_risk, apply_risk_sentry
from bnb_quant_tool.bnb_event_calendar import BNBEventCalendar
from bnb_quant_tool.bnb_risk_sentry import BNBRiskSentry
from bnb_quant_tool.config_access import (
    build_data_fetcher,
    build_trade_advisor_config,
)
from bnb_quant_tool.counterfactual_analyzer import CounterfactualAnalyzer
from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
from bnb_quant_tool.llm_provider import get_llm_credentials
from bnb_quant_tool.paper_trading import PaperTradingEngine
from bnb_quant_tool.pattern_memory import PatternMemory
from bnb_quant_tool.trade_advisor import TradeAdvisor


def _load_config() -> dict:
    from bnb_quant_tool.config_access import load_app_config
    return load_app_config(PROJECT_DIR / "config.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description="BNB Quant Paper Watcher")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="跳过单实例锁（仅调试）",
    )
    args = parser.parse_args()
    if not args.no_lock:
        ensure_singleton(
            PROJECT_DIR / "data" / "locks" / "paper_watcher.lock",
            logger=logger,
        )

    cfg = _load_config()
    pt_cfg = cfg.get("paper_trading", {}) or {}
    interval = float(pt_cfg.get("poll_interval", 15))

    event_cfg = cfg.get("bnb_event_calendar") or {}
    sentry_cfg = cfg.get("bnb_risk_sentry") or {}
    refresh_sec = max(60, int(event_cfg.get("refresh_minutes", 30)) * 60)
    sentry_refresh_sec = max(60, int(sentry_cfg.get("cache_seconds", 300)))

    fetcher = build_data_fetcher(cfg)
    learner = AILearningSystem(config=cfg)
    advisor = TradeAdvisor(build_trade_advisor_config(cfg))
    symbol = (cfg.get("trading") or {}).get("symbol", "BNBUSDT")

    llm_creds = get_llm_credentials(cfg)
    review = AIReviewEngine(
        config=cfg,
        deepseek_api_key=llm_creds["api_key"],
        deepseek_model=llm_creds["model"],
        deepseek_base_url=llm_creds["base_url"],
    )

    calendar = BNBEventCalendar(
        config=event_cfg,
        state_path=event_cfg.get("state_path"),
        fetcher=fetcher,
    ) if event_cfg.get("enabled", True) else None

    sentry = BNBRiskSentry(fetcher=fetcher, config=sentry_cfg) if sentry_cfg.get("enabled", True) else None

    if calendar:
        cycle = apply_event_cycle_risk(cfg, advisor, calendar, symbol=symbol)
        if cycle.get("skipped"):
            logger.info("非 BNB 交易对(%s)，跳过事件周期风控", symbol)
        else:
            logger.info(
                "BNB 事件周期已加载: %s | %s",
                cycle.get("phase_label"),
                cycle.get("interpretation", "")[:80],
            )

    engine = PaperTradingEngine(
        db_path=pt_cfg.get("db_path"),
        config=cfg,
        ai_review_engine=review,
    )
    engine.set_learner(learner)
    engine.set_trade_advisor(advisor)

    counterfactual = CounterfactualAnalyzer(fetcher=fetcher)
    pattern_memory = PatternMemory(
        paper_db_path=engine.db_path,
        learning_db_path=learner.db_path,
    )
    evolution = LearningEvolutionCoordinator(
        learner,
        capability_memory=learner.capability_memory,
        counterfactual=counterfactual,
        config=cfg,
    )
    engine.set_learning_pipeline_deps(
        counterfactual=counterfactual,
        pattern_memory=pattern_memory,
        evolution=evolution,
        on_status=lambda msg: logger.info(msg),
    )

    def price_provider(symbol: str) -> float:
        return fetcher.get_price_with_fallback(symbol)

    engine.set_price_provider(price_provider)
    engine.start_watcher(interval=interval)

    if sentry:
        try:
            rs = apply_risk_sentry(cfg, advisor, sentry, symbol)
            if rs.get("skipped"):
                logger.info("非 BNB 交易对(%s)，跳过风控哨兵", symbol)
            else:
                logger.info("BNB 风控哨兵: %s", rs.get("interpretation", "")[:100])
        except Exception as e:
            logger.warning("风控哨兵初始化失败: %s", e)

    logger.info("模拟盘 watcher 已启动, interval=%ss, db=%s", interval, engine.db_path)

    running = True
    last_event_refresh = time.time()
    last_sentry_refresh = time.time()

    def _stop(_sig=None, _frame=None):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while running:
        if calendar and (time.time() - last_event_refresh) >= refresh_sec:
            try:
                cycle = apply_event_cycle_risk(cfg, advisor, calendar, symbol=symbol)
                if not cycle.get("skipped"):
                    logger.info(
                        "事件周期刷新: %s | mode=%s | max_pos=%.1f%%",
                        cycle.get("phase_label"),
                        cycle.get("strategy_mode"),
                        advisor.max_position_pct * 100,
                    )
            except Exception as e:
                logger.warning("事件周期刷新失败: %s", e)
            last_event_refresh = time.time()
        if sentry and (time.time() - last_sentry_refresh) >= sentry_refresh_sec:
            try:
                rs = apply_risk_sentry(cfg, advisor, sentry, symbol)
                if rs.get("block_long"):
                    logger.warning("风控哨兵刷新: %s", rs.get("interpretation", ""))
            except Exception as e:
                logger.warning("风控哨兵刷新失败: %s", e)
            last_sentry_refresh = time.time()
        time.sleep(1)

    engine.stop_watcher()
    logger.info("模拟盘 watcher 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
