"""K 线归档增量同步 — 分析/更新时拉取最新 K 线并写入本地。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd

from .bitget_fetcher import BitgetDataFetcher
from .kline_archive import KlineArchive
from .mexc_fetcher import MexcDataFetcher

logger = logging.getLogger(__name__)


def fetch_historical_from_api(
    symbol: str,
    interval: str,
    start_str: str,
    end_str: Optional[str] = None,
    *,
    source: str = "auto",
    mexc_base_url: Optional[str] = None,
    bitget_base_url: Optional[str] = None,
    binance_fetcher=None,
) -> Tuple[pd.DataFrame, str]:
    """从 API 拉取历史 K 线（MEXC → Binance → Bitget），不读本地。"""
    errors: list[str] = []
    chain = _resolve_source_chain(source, mexc_base_url, bitget_base_url, binance_fetcher)

    for name, fetch in chain:
        try:
            df = fetch(symbol, interval, start_str, end_str)
            if df is not None and not df.empty:
                return df, name
            errors.append(f"{name}: 空数据")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            logger.debug("fetch_historical_api %s 失败: %s", name, exc)

    logger.warning("全部 API 源失败: %s", "; ".join(errors))
    return pd.DataFrame(), "none"


def fetch_recent_klines(
    symbol: str,
    interval: str,
    lookback_days: int,
    *,
    source: str = "auto",
    mexc_base_url: Optional[str] = None,
    bitget_base_url: Optional[str] = None,
    binance_fetcher=None,
) -> Tuple[pd.DataFrame, str]:
    """
    从 API 拉取最近 N 天 K 线（不读本地归档）。
    返回 (DataFrame, source_name)。
    """
    start_str = f"{max(1, int(lookback_days))} days ago"
    return fetch_historical_from_api(
        symbol, interval, start_str,
        source=source,
        mexc_base_url=mexc_base_url,
        bitget_base_url=bitget_base_url,
        binance_fetcher=binance_fetcher,
    )


def _resolve_source_chain(source, mexc_base_url, bitget_base_url, binance_fetcher):
    source = (source or "auto").lower()

    def _mexc(sym, iv, start_str, end_str=None):
        return MexcDataFetcher(base_url=mexc_base_url).get_historical_klines(
            sym, iv, start_str, end_str,
        )

    def _bitget(sym, iv, start_str, end_str=None):
        return BitgetDataFetcher(base_url=bitget_base_url).get_historical_klines(
            sym, iv, start_str, end_str,
        )

    def _binance(sym, iv, start_str, end_str=None):
        if binance_fetcher is None:
            raise RuntimeError("无 Binance fetcher")
        return binance_fetcher._fetch_binance_api(sym, iv, start_str, end_str)

    if source == "mexc":
        return [("mexc", _mexc)]
    if source == "bitget":
        return [("bitget", _bitget)]
    if source == "binance":
        return [("binance", _binance)]
    chain = [("mexc", _mexc)]
    if binance_fetcher is not None:
        chain.append(("binance", _binance))
    chain.append(("bitget", _bitget))
    return chain


def sync_archive_recent(
    archive: KlineArchive,
    symbol: str,
    interval: str,
    lookback_days: int,
    *,
    source: str = "auto",
    mexc_base_url: Optional[str] = None,
    bitget_base_url: Optional[str] = None,
    binance_fetcher=None,
    fresh_df: Optional[pd.DataFrame] = None,
    data_source: Optional[str] = None,
) -> Dict:
    """拉取或合并最新 K 线到本地归档，返回同步摘要。"""
    if fresh_df is not None and not fresh_df.empty:
        src = data_source or "analysis"
        return archive.merge_bars(fresh_df, source=src)

    df, src = fetch_recent_klines(
        symbol,
        interval,
        lookback_days,
        source=source,
        mexc_base_url=mexc_base_url,
        bitget_base_url=bitget_base_url,
        binance_fetcher=binance_fetcher,
    )
    return archive.merge_bars(df, source=src)
