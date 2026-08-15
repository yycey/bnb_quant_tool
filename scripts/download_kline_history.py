#!/usr/bin/env python3
"""下载 BNB/USDT 历史 K 线到本地归档（默认 18 个月 × 1h，按月分片）。

数据源优先级（--source auto）: MEXC → Binance → Bitget
保存目录: data/klines/BNBUSDT/1h/chunks/YYYY-MM.csv
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd
import yaml

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from bnb_quant_tool.bitget_fetcher import BitgetDataFetcher
from bnb_quant_tool.config_access import build_data_fetcher
from bnb_quant_tool.kline_archive import KlineArchive
from bnb_quant_tool.mexc_fetcher import MexcDataFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [download] %(levelname)s %(message)s",
)
logger = logging.getLogger("download_kline_history")


class KlineProvider(Protocol):
    last_data_source: str

    def fetch_month(
        self,
        symbol: str,
        interval: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> pd.DataFrame: ...


class MexcProvider:
    def __init__(self, base_url: str | None = None):
        self._fetcher = MexcDataFetcher(base_url=base_url)
        self.last_data_source = "mexc"

    def fetch_month(self, symbol, interval, start_dt, end_dt):
        df = self._fetcher.get_range_klines(symbol, interval, start_dt, end_dt)
        self.last_data_source = self._fetcher.last_data_source
        return df


class BinanceProvider:
    """下载专用：关闭本地归档优先，避免读到半成品。"""

    def __init__(self, cfg: dict):
        dl_cfg = copy.deepcopy(cfg)
        dl_cfg["kline_archive"] = {
            **(dl_cfg.get("kline_archive") or {}),
            "enabled": False,
            "prefer_local": False,
        }
        self._fetcher = build_data_fetcher(dl_cfg)
        self.last_data_source = "binance"

    def fetch_month(self, symbol, interval, start_dt, end_dt):
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000) - 1
        df = self._fetcher.get_klines(
            symbol=symbol,
            interval=interval,
            limit=1000,
            start_time=start_ms,
            end_time=end_ms,
            allow_fallback=False,
        )
        if df.empty:
            df = self._fetcher.get_historical_klines(
                symbol=symbol,
                interval=interval,
                start_str=start_dt.strftime("%Y-%m-%d"),
                end_str=end_dt.strftime("%Y-%m-%d"),
            )
        if not df.empty:
            df = df[(df["open_time"] >= start_dt) & (df["open_time"] < end_dt)]
        self.last_data_source = getattr(self._fetcher, "last_data_source", "binance")
        return df


class BitgetProvider:
    def __init__(self, base_url: str | None = None):
        self._fetcher = BitgetDataFetcher(base_url=base_url)
        self.last_data_source = "bitget"

    def fetch_month(self, symbol, interval, start_dt, end_dt):
        df = self._fetcher.get_historical_klines(
            symbol=symbol,
            interval=interval,
            start_str=start_dt.strftime("%Y-%m-%d"),
            end_str=end_dt.strftime("%Y-%m-%d"),
        )
        if not df.empty:
            df = df[(df["open_time"] >= start_dt) & (df["open_time"] < end_dt)]
        self.last_data_source = "bitget"
        return df


class AutoProvider:
    """MEXC → Binance(+Bitget fallback) → Bitget 直连。"""

    def __init__(self, cfg: dict):
        mexc_cfg = cfg.get("mexc") or {}
        bitget_cfg = cfg.get("bitget") or {}
        self._chain: list[KlineProvider] = [
            MexcProvider(base_url=mexc_cfg.get("base_url")),
            BinanceProvider(cfg),
            BitgetProvider(base_url=bitget_cfg.get("base_url")),
        ]
        self.last_data_source = "auto"

    def fetch_month(self, symbol, interval, start_dt, end_dt):
        errors: list[str] = []
        for provider in self._chain:
            name = type(provider).__name__
            try:
                df = provider.fetch_month(symbol, interval, start_dt, end_dt)
                if df is not None and not df.empty:
                    self.last_data_source = provider.last_data_source
                    logger.info("  数据源: %s (%d 条)", name, len(df))
                    return df
                errors.append(f"{name}: 空数据")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.warning("  %s 失败: %s", name, exc)
        raise RuntimeError("所有数据源均失败 — " + "; ".join(errors))


def load_config() -> dict:
    path = PROJECT_DIR / "config.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_provider(source: str, cfg: dict) -> KlineProvider:
    source = (source or "auto").lower()
    mexc_cfg = cfg.get("mexc") or {}
    bitget_cfg = cfg.get("bitget") or {}
    if source == "mexc":
        return MexcProvider(base_url=mexc_cfg.get("base_url"))
    if source == "binance":
        return BinanceProvider(cfg)
    if source == "bitget":
        return BitgetProvider(base_url=bitget_cfg.get("base_url"))
    return AutoProvider(cfg)


def download_month(provider: KlineProvider, archive: KlineArchive, year: int, month: int) -> int:
    start_dt, end_dt = archive.month_range(year, month)
    df = provider.fetch_month(archive.symbol, archive.interval, start_dt, end_dt)
    archive.save_chunk(year, month, df, source=provider.last_data_source)
    return len(df)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 K 线历史到 data/klines/")
    parser.add_argument("--symbol", default=None, help="交易对，默认 BNBUSDT")
    parser.add_argument("--interval", default="1h", help="K 线周期")
    parser.add_argument("--months", type=int, default=None, help="回溯月数，默认 config kline_archive.months 或 18")
    parser.add_argument("--source", default=None, choices=["auto", "mexc", "binance", "bitget"], help="数据源")
    parser.add_argument("--force", action="store_true", help="覆盖已有 chunk")
    args = parser.parse_args()

    cfg = load_config()
    ka_cfg = cfg.get("kline_archive") or {}
    symbol = (args.symbol or cfg.get("trading", {}).get("symbol", "BNBUSDT")).upper()
    months_count = args.months or int(ka_cfg.get("months", 18))
    source = args.source or ka_cfg.get("download_source", "auto")

    provider = build_provider(source, cfg)
    archive = KlineArchive(PROJECT_DIR, symbol=symbol, interval=args.interval)
    month_list = KlineArchive.iter_months_back(months_count)

    logger.info("=" * 60)
    logger.info("BNB/USDT 历史 K 线下载")
    logger.info("交易对: %s | 周期: %s | 月数: %d | 数据源: %s", symbol, args.interval, len(month_list), source)
    logger.info("范围: %04d-%02d ~ %04d-%02d", month_list[0][0], month_list[0][1], month_list[-1][0], month_list[-1][1])
    logger.info("保存目录: %s", archive.chunks_dir)
    logger.info("=" * 60)

    sources: set[str] = set()
    total_rows = 0
    failed: list[str] = []

    for i, (year, month) in enumerate(month_list, 1):
        chunk_file = archive.chunk_path(year, month)
        now = datetime.now()
        is_current_month = year == now.year and month == now.month
        # 历史月可跳过；当前月必须续写，否则 tip 会卡在月中旧收盘
        if chunk_file.exists() and not args.force and not is_current_month:
            rows = len(archive.load_chunk(year, month))
            logger.info("[%d/%d] 跳过已有 %s (%d 条)", i, len(month_list), chunk_file.name, rows)
            total_rows += rows
            continue

        logger.info(
            "[%d/%d] 下载 %04d-%02d%s ...",
            i,
            len(month_list),
            year,
            month,
            " (当前月强制刷新)" if is_current_month and chunk_file.exists() else "",
        )
        try:
            rows = download_month(provider, archive, year, month)
            total_rows += rows
            sources.add(provider.last_data_source)
            if rows == 0:
                failed.append(f"{year:04d}-{month:02d}")
        except Exception as exc:
            failed.append(f"{year:04d}-{month:02d}")
            logger.error("[%d/%d] %04d-%02d 失败: %s", i, len(month_list), year, month, exc)
        time.sleep(0.25)

    merged = archive.rebuild_merged()
    manifest = archive.write_manifest(sources=list(sources))

    logger.info("=" * 60)
    logger.info("完成: %d 个月 | chunk 合计 %d 条 | 合并 %d 条", len(manifest.get("months", [])), total_rows, len(merged))
    logger.info("时间范围: %s ~ %s", manifest.get("range_start"), manifest.get("range_end"))
    logger.info("manifest: %s", archive.manifest_path)
    if archive.merged_csv_path.exists():
        logger.info("合并 CSV: %s", archive.merged_csv_path)
    if failed:
        logger.warning("失败/空数据月份: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
