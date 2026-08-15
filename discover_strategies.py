"""
discover_strategies.py - StrategyLab 一键发现脚本
================================================================

用法（在项目根目录执行）：

    python discover_strategies.py                # 用 BNBUSDT 1h 最近 1500 根 K 线
    python discover_strategies.py --bars 3000    # 加大样本
    python discover_strategies.py --candidates 200 --topk 8
    python discover_strategies.py --demo         # 用合成数据离线跑

跑完会：
1. 在控制台输出 Top-K 策略 + 关键回测指标
2. 写入 ai_learning.db（discovered_strategies 表）
3. bump data/strategy_pool_signal.json；已运行的 GUI/headless
   下一轮分析会热加载进投票池（无需整进程重启）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np
import pandas as pd

# 让 src/ 内的包可被导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from bnb_quant_tool.strategy_lab import StrategyLab, DiscoveryConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# 回测会反复算指标，压住该模块的 INFO 日志避免刷屏
logging.getLogger("bnb_quant_tool.technical_indicators").setLevel(logging.WARNING)
logger = logging.getLogger("discover_strategies")


def _make_demo_df(n: int = 1500, seed: int = 7) -> pd.DataFrame:
    """生成带趋势和周期性的合成 K 线（加中期上涨趋势 + 正弦振荡）"""
    rng = np.random.default_rng(seed)
    drift = np.linspace(0, 80, n)               # 总趋势 +80
    cycle = 15 * np.sin(np.linspace(0, 8 * np.pi, n))   # 振荡
    noise = np.cumsum(rng.normal(0, 1.0, n))    # 随机游走
    prices = 600 + drift + cycle + noise * 0.5
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="1h"),
        "open": prices + rng.normal(0, 0.3, n),
        "high": prices + np.abs(rng.normal(0, 1.0, n)),
        "low": prices - np.abs(rng.normal(0, 1.0, n)),
        "close": prices,
        "volume": rng.uniform(800, 2000, n),
    })


def _fetch_real_df(symbol: str, interval: str, bars: int) -> pd.DataFrame:
    from bnb_quant_tool.data_fetcher import BinanceDataFetcher
    fetcher = BinanceDataFetcher()
    df = fetcher.get_klines(symbol=symbol, interval=interval, limit=min(bars, 1000))
    # 不足时滚动多次取
    if len(df) < bars and "timestamp" in df.columns:
        oldest = df["timestamp"].iloc[0]
        while len(df) < bars:
            try:
                end_ms = int(pd.Timestamp(oldest).timestamp() * 1000)
                more = fetcher.get_klines(
                    symbol=symbol, interval=interval, limit=1000, end_time=end_ms
                )
                if more is None or len(more) == 0:
                    break
                df = pd.concat([more, df], ignore_index=True).drop_duplicates(
                    subset=["timestamp"]
                )
                oldest = df["timestamp"].iloc[0]
            except Exception as e:
                logger.warning(f"补充历史 K 线失败，使用现有 {len(df)} 根: {e}")
                break
    return df.reset_index(drop=True)


def main():
    p = argparse.ArgumentParser(description="StrategyLab - AI 自动发现新策略")
    p.add_argument("--symbol", default="BNBUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--bars", type=int, default=1500, help="历史 K 线数量")
    p.add_argument("--candidates", type=int, default=120, help="候选策略数量")
    p.add_argument("--topk", type=int, default=5, help="保留 Top-K")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--demo", action="store_true", help="使用合成数据离线测试")
    p.add_argument("--no-merge", action="store_true",
                   help="不与已有策略库合并，直接覆盖 ai_learning.db 中的记录")
    p.add_argument(
        "--hyperopt",
        action="store_true",
        help="使用 Hyperopt（Optuna TPE 或随机+爬山）替代纯随机发现",
    )
    p.add_argument("--trials", type=int, default=0, help="Hyperopt trials（默认=candidates）")
    args = p.parse_args()

    if args.demo:
        logger.info(f"[DEMO] 生成合成数据 {args.bars} 根 K 线")
        df = _make_demo_df(args.bars)
    else:
        logger.info(f"拉取真实 K 线 {args.symbol} {args.interval} x{args.bars}")
        try:
            df = _fetch_real_df(args.symbol, args.interval, args.bars)
        except Exception as e:
            logger.error(f"拉取真实 K 线失败，回退 DEMO: {e}")
            df = _make_demo_df(args.bars)

    n_search = args.trials or args.candidates
    logger.info(
        f"开始发现：mode={'hyperopt' if args.hyperopt else 'random'} "
        f"search={n_search} TopK={args.topk} bars={len(df)}"
    )

    if args.hyperopt:
        from bnb_quant_tool.strategy_hyperopt import HyperoptConfig, StrategyLabHyperopt

        hcfg = HyperoptConfig(
            n_trials=n_search,
            top_k=args.topk,
            seed=args.seed,
        )
        eng = StrategyLabHyperopt(hcfg)
        top = eng.run_and_save(df, merge_with_existing=not args.no_merge)
    else:
        cfg = DiscoveryConfig(
            n_candidates=args.candidates,
            top_k=args.topk,
            seed=args.seed,
        )
        lab = StrategyLab(cfg)
        top = lab.discover_and_save(df, merge_with_existing=not args.no_merge)

    print()
    print(StrategyLab.format_report(top))
    print()
    print(f"已保存到: ai_learning.db (discovered_strategies 表, {len(top)} 条)")
    try:
        from bnb_quant_tool.strategy_pool import bump_reload_signal, reload_discovered_strategies
        from pathlib import Path

        root = Path(__file__).resolve().parent
        reload_discovered_strategies(reason="cli_discover", project_root=root, bump=True)
        print("已发出策略池热加载信号 (data/strategy_pool_signal.json)")
    except Exception as e:
        logger.debug("strategy pool signal: %s", e)
        print("提示: 运行中的 GUI/服务器下一轮分析会热加载；或重启进程。")
    if args.hyperopt:
        print("提示: pip install optuna 可启用 TPE 采样；未安装时自动回退随机+爬山。")

    # 刷新晋升漏斗看板
    try:
        from bnb_quant_tool.promotion_funnel import PromotionFunnel
        funnel = PromotionFunnel()
        report = funnel.evaluate_all(top)
        print(f"晋升漏斗: {report.get('counts')}")
    except Exception as e:
        logger.debug("promotion funnel refresh: %s", e)


if __name__ == "__main__":
    main()
