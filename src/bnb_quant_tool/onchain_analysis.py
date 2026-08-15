"""
BNB量化交易工具 - 链上筹码分析 (On-Chain Analysis)
====================================================
主流币 (BTC/ETH) 长周期链上指标，补充交易所情绪：

1. MVRV 比率 — 市场价值 / 已实现价值，识别高估/低估区间
2. Exchange Netflow — 交易所净流入/流出，抛压/囤币信号
3. 巨鲸地址异动 — 大额地址余额变化

数据源优先级：
- Glassnode API（需 api_key，指标最全）
- CoinMetrics Community API（免费回退，无需 key）
- CoinGecko（市值占比等辅助）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .etherscan_v2 import BSC_CHAIN_ID, EtherscanV2Client

logger = logging.getLogger(__name__)

# BNB 等 alt 无完整链上指标时，用 BTC+ETH 作为大盘锚
LEADER_ASSETS = ("BTC", "ETH")
SYMBOL_TO_LEADERS = {
    "BNBUSDT": ("BTC", "ETH"),
    "BTCUSDT": ("BTC",),
    "ETHUSDT": ("ETH",),
}


class OnChainAnalyzer:
    """链上筹码分析器 — Glassnode 优先，CoinMetrics 回退。"""

    GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"
    COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

    GLASSNODE_METRICS = {
        "mvrv": "indicators/mvrv",
        "exchange_netflow": "transactions/transfers_volume_exchanges_net",
        "whale_balance": "entities/supply_balance_more_100k",
    }

    COINMETRICS_METRICS = (
        "CapMrktCurUSD,CapRealUSD,FlowNetExInclUSD,AdrBalUSD1MilCnt"
    )

    def __init__(
        self,
        glassnode_api_key: Optional[str] = None,
        timeout: int = 15,
        use_coinmetrics_fallback: bool = True,
        cache_seconds: int = 900,
        etherscan_api_key: Optional[str] = None,
        bscscan_api_key: Optional[str] = None,
        bsc_chain_id: int = BSC_CHAIN_ID,
        bsc_enabled: bool = True,
    ):
        self.glassnode_api_key = (glassnode_api_key or "").strip() or None
        self.timeout = timeout
        self.use_coinmetrics_fallback = use_coinmetrics_fallback
        self.cache_seconds = cache_seconds
        api_key = (etherscan_api_key or bscscan_api_key or "").strip() or None
        self.etherscan_api_key = api_key
        self.bsc_chain_id = int(bsc_chain_id)
        self.bsc_enabled = bsc_enabled
        self._etherscan_v2 = (
            EtherscanV2Client(api_key=api_key, chain_id=self.bsc_chain_id, timeout=timeout)
            if api_key else None
        )
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        from bnb_quant_tool.disk_ttl_cache import DiskTTLCache
        self._disk = DiskTTLCache("data/onchain_cache", prefix="onchain")

    # ============================================================
    # 主入口
    # ============================================================
    def fetch_all(self, symbol: str = "BNBUSDT") -> Dict[str, Any]:
        """拉取与交易对相关的主流币链上面板。"""
        cache_key = f"all:{symbol.upper()}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        leaders = self._resolve_leaders(symbol)
        assets_data: Dict[str, Dict] = {}
        for asset in leaders:
            assets_data[asset] = self.fetch_asset(asset)

        coingecko = self._fetch_coingecko_global()
        from bnb_quant_tool.bnb_symbol import is_bnb_trading_pair
        use_bsc = bool(self.bsc_enabled and is_bnb_trading_pair(symbol))
        bsc = self._fetch_bsc_activity() if use_bsc else {}
        leader_score = self._aggregate_score(assets_data)
        bsc_score = float(bsc.get("bsc_score") or 0) if use_bsc else 0.0
        if use_bsc:
            blended = round(max(-1.0, min(1.0, leader_score * 0.65 + bsc_score * 0.35)), 3)
        else:
            blended = round(max(-1.0, min(1.0, leader_score)), 3)
        result = {
            "symbol": symbol.upper(),
            "leader_assets": list(leaders),
            "assets": assets_data,
            "bsc": bsc if use_bsc else {"skipped": True, "skip_reason": "non_bnb_pair"},
            "coingecko": coingecko,
            "onchain_score": blended,
            "leader_score": leader_score,
            "bsc_score": bsc_score,
            "bsc_applied": use_bsc,
            "interpretation": "",
            "data_source": self._primary_source(assets_data),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result["interpretation"] = self._interpret(result)
        self._set_cache(cache_key, result)
        return result

    def fetch_asset(self, asset: str) -> Dict[str, Any]:
        """单资产链上指标。"""
        asset = asset.upper()
        cache_key = f"asset:{asset}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        data: Dict[str, Any] = {"asset": asset, "source": "none"}

        if self.glassnode_api_key:
            gn = self._fetch_glassnode_asset(asset)
            if gn and not gn.get("error"):
                data = gn
                data["source"] = "glassnode"
                self._set_cache(cache_key, data)
                return data

        if self.use_coinmetrics_fallback:
            cm = self._fetch_coinmetrics_asset(asset)
            if cm and not cm.get("error"):
                data = cm
                data["source"] = "coinmetrics"
                self._set_cache(cache_key, data)
                return data

        proxy = self._fetch_price_proxy_asset(asset)
        if proxy and not proxy.get("error"):
            data = proxy
            data["source"] = "price_proxy"
            self._set_cache(cache_key, data)
            return data

        data["error"] = "无可用链上数据源（请配置 glassnode_api_key 或检查网络）"
        self._set_cache(cache_key, data)
        return data

    # ============================================================
    # Glassnode
    # ============================================================
    def _fetch_glassnode_asset(self, asset: str) -> Dict[str, Any]:
        since = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        asset_lower = asset.lower()

        mvrv_series = self._glassnode_series(
            self.GLASSNODE_METRICS["mvrv"], asset_lower, since
        )
        netflow_series = self._glassnode_series(
            self.GLASSNODE_METRICS["exchange_netflow"], asset_lower, since
        )
        whale_series = self._glassnode_series(
            self.GLASSNODE_METRICS["whale_balance"], asset_lower, since
        )

        if not mvrv_series and not netflow_series:
            return {"asset": asset, "error": "Glassnode 无数据"}

        mvrv = self._series_latest(mvrv_series)
        netflow = self._series_latest(netflow_series)
        netflow_7d = self._series_change(netflow_series, days=7)
        whale = self._series_latest(whale_series)
        whale_7d_pct = self._series_pct_change(whale_series, days=7)

        return {
            "asset": asset,
            "mvrv": self._build_mvrv_block(mvrv, mvrv_series),
            "exchange_netflow": self._build_netflow_block(netflow, netflow_7d, netflow_series),
            "whale_activity": self._build_whale_block(whale, whale_7d_pct),
        }

    def _glassnode_series(
        self, metric_path: str, asset: str, since: int
    ) -> List[Tuple[int, float]]:
        if not self.glassnode_api_key:
            return []
        url = f"{self.GLASSNODE_BASE}/{metric_path}"
        params = {
            "a": self.glassnode_api_key,
            "i": "24h",
            "s": since,
            "c": "native",
        }
        try:
            r = requests.get(url, params={**params, "asset": asset}, timeout=self.timeout)
            if r.status_code == 402:
                logger.warning("Glassnode 需付费套餐: %s", metric_path)
                return []
            r.raise_for_status()
            raw = r.json()
            if not isinstance(raw, list):
                return []
            return [(int(row[0]), float(row[1])) for row in raw if len(row) >= 2]
        except Exception as e:
            logger.warning("Glassnode %s/%s 失败: %s", asset, metric_path, e)
            return []

    # ============================================================
    # CoinMetrics Community (免费)
    # ============================================================
    def _fetch_coinmetrics_asset(self, asset: str) -> Dict[str, Any]:
        params = {
            "assets": asset.lower(),
            "metrics": self.COINMETRICS_METRICS,
            "frequency": "1d",
            "page_size": 30,
            "sort": "time",
        }
        try:
            r = requests.get(
                self.COINMETRICS_BASE,
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("data") or []
            if not rows:
                return {"asset": asset, "error": "CoinMetrics 无数据"}

            parsed = []
            for row in rows:
                t = row.get("time")
                mkt = self._safe_float(row.get("CapMrktCurUSD"))
                real = self._safe_float(row.get("CapRealUSD"))
                netflow = self._safe_float(row.get("FlowNetExInclUSD"))
                whales = self._safe_float(row.get("AdrBalUSD1MilCnt"))
                mvrv = (mkt / real) if mkt and real and real > 0 else None
                parsed.append({
                    "time": t,
                    "mvrv": mvrv,
                    "netflow_usd": netflow,
                    "whale_addresses": whales,
                })

            latest = parsed[-1]
            mvrv_val = latest.get("mvrv")
            netflow_val = latest.get("netflow_usd")
            whale_val = latest.get("whale_addresses")

            netflow_7d = None
            whale_7d_pct = None
            if len(parsed) >= 8:
                prev = parsed[-8]
                if netflow_val is not None and prev.get("netflow_usd") is not None:
                    netflow_7d = netflow_val - prev["netflow_usd"]
                w0 = prev.get("whale_addresses")
                if whale_val and w0 and w0 > 0:
                    whale_7d_pct = (whale_val - w0) / w0 * 100

            mvrv_series = [(i, p["mvrv"]) for i, p in enumerate(parsed) if p.get("mvrv")]

            return {
                "asset": asset,
                "mvrv": self._build_mvrv_block(mvrv_val, mvrv_series),
                "exchange_netflow": self._build_netflow_block(
                    netflow_val, netflow_7d, None, unit="USD"
                ),
                "whale_activity": self._build_whale_block(whale_val, whale_7d_pct, unit="addresses"),
            }
        except Exception as e:
            logger.warning("CoinMetrics %s 失败: %s", asset, e)
            return {"asset": asset, "error": str(e)}

    # ============================================================
    # 价格代理（Mayer Multiple 等 — 无 Glassnode 时的免费回退）
    # ============================================================
    def _fetch_price_proxy_asset(self, asset: str) -> Dict[str, Any]:
        """用 Binance 日 K 估算长周期估值与资金流代理（非真实链上，但可运行）。"""
        symbol = f"{asset}USDT"
        try:
            from bnb_quant_tool.data_fetcher import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            df = fetcher.get_klines(symbol=symbol, interval="1d", limit=200)
            if df is None or len(df) < 50:
                return {"asset": asset, "error": "K线数据不足"}

            closes = df["close"].astype(float)
            volumes = df["volume"].astype(float)
            price = float(closes.iloc[-1])
            ma200 = float(closes.tail(200).mean())
            mayer = price / ma200 if ma200 > 0 else 1.0

            vol_7 = float(volumes.tail(7).mean())
            vol_30 = float(volumes.tail(30).mean())
            vol_ratio = vol_7 / vol_30 if vol_30 > 0 else 1.0

            # Mayer Multiple 映射到类 MVRV 区间
            level, signal = self._mayer_to_mvrv_signal(mayer)

            # 成交量放大 + 价格下跌 ≈ 潜在抛压；缩量上涨 ≈ 囤币
            ch_7d = (price - float(closes.iloc[-8])) / float(closes.iloc[-8]) * 100 if len(closes) >= 8 else 0
            if ch_7d < -3 and vol_ratio > 1.2:
                nf_trend, nf_signal = "净流入", "偏空"
                nf_latest = vol_7 * 0.01
            elif ch_7d > 3 and vol_ratio < 0.9:
                nf_trend, nf_signal = "净流出", "偏多"
                nf_latest = -vol_7 * 0.01
            else:
                nf_trend, nf_signal = "平衡", "中性"
                nf_latest = 0.0

            return {
                "asset": asset,
                "mvrv": {
                    "value": round(mayer, 4),
                    "level": level,
                    "signal": signal,
                    "change_30d_pct": round(
                        (mayer - price / float(closes.iloc[-31].mean())) / max(mayer, 0.01) * 100, 2
                    ) if len(closes) >= 31 else None,
                    "note": "Mayer Multiple 代理 (Price/MA200)",
                },
                "exchange_netflow": {
                    "latest": round(nf_latest, 4),
                    "unit": "volume_proxy",
                    "trend": nf_trend,
                    "signal": nf_signal,
                    "change_7d": round(vol_ratio - 1, 4),
                    "note": "成交量趋势代理",
                },
                "whale_activity": {
                    "value": round(vol_7, 2),
                    "unit": "avg_volume_7d",
                    "change_7d_pct": round((vol_ratio - 1) * 100, 2),
                    "activity": "增仓" if vol_ratio > 1.15 else ("减仓" if vol_ratio < 0.85 else "稳定"),
                    "signal": "偏多" if vol_ratio > 1.15 and ch_7d > 0 else (
                        "偏空" if vol_ratio > 1.15 and ch_7d < 0 else "中性"
                    ),
                    "note": "大额成交活跃度代理",
                },
            }
        except Exception as e:
            logger.warning("价格代理 %s 失败: %s", asset, e)
            return {"asset": asset, "error": str(e)}

    @staticmethod
    def _mayer_to_mvrv_signal(mayer: float) -> Tuple[str, str]:
        if mayer >= 2.4:
            return "严重高估", "强烈偏空"
        if mayer >= 1.8:
            return "高估", "偏空"
        if mayer >= 1.2:
            return "合理偏高", "中性"
        if mayer >= 0.9:
            return "合理", "中性偏多"
        if mayer >= 0.7:
            return "低估", "偏多"
        return "严重低估", "强烈偏多"

    # ============================================================
    # BSC (BNB Chain) 热度雷达 — Etherscan API V2 (chainid=56)
    # ============================================================
    DEFILLAMA_CHAINS = "https://api.llama.fi/v2/chains"

    def _fetch_bsc_activity(self) -> Dict[str, Any]:
        """BSC 活跃地址 / Gas 费突增 — BNB 原生需求热度。"""
        cache_key = "bsc:activity"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        gas_gwei: Optional[float] = None
        daily_tx: Optional[int] = None
        daily_new_addresses: Optional[int] = None
        gas_source = ""
        tx_source = ""
        api_label = "none"
        upgrade_hint = ""

        if self._etherscan_v2 and self._etherscan_v2.enabled:
            snap = self._etherscan_v2.fetch_bsc_activity_snapshot()
            gas_gwei = snap.get("gas_gwei")
            daily_tx = snap.get("daily_tx")
            daily_new_addresses = snap.get("daily_new_addresses")
            gas_source = tx_source = snap.get("source") or "etherscan_v2"
            api_label = "etherscan_v2"
            upgrade_hint = snap.get("upgrade_hint") or ""
            if snap.get("upgrade_required"):
                logger.info("Etherscan V2 BSC: %s", upgrade_hint)

        if gas_gwei is None and daily_tx is None:
            daily_tx, tx_source = self._fetch_bsc_daily_tx_fallback()
            if daily_tx is not None and not gas_source:
                gas_source = tx_source

        gas_spike = False
        tx_spike = False
        addr_spike = False
        bsc_score = 0.0

        if gas_gwei is not None:
            if gas_gwei >= 8.0:
                gas_spike = True
                bsc_score += 0.35
            elif gas_gwei >= 5.0:
                bsc_score += 0.15
            elif gas_gwei <= 3.0:
                bsc_score -= 0.05

        if daily_tx is not None:
            if daily_tx >= 4_000_000:
                tx_spike = True
                bsc_score += 0.30
            elif daily_tx >= 3_000_000:
                bsc_score += 0.12

        if daily_new_addresses is not None:
            if daily_new_addresses >= 500_000:
                addr_spike = True
                bsc_score += 0.20
            elif daily_new_addresses >= 300_000:
                bsc_score += 0.08

        if gas_gwei is None and daily_tx is None:
            proxy = self._fetch_bnb_volume_activity_proxy()
            if proxy:
                bsc_score = float(proxy.get("bsc_score") or 0)
                gas_spike = bool(proxy.get("gas_spike"))
                tx_spike = bool(proxy.get("tx_spike"))
                data = {
                    **proxy,
                    "api": api_label,
                    "upgrade_hint": upgrade_hint,
                    "source": "bnb_volume_proxy",
                    "bsc_score": round(bsc_score, 3),
                }
                self._set_cache(cache_key, data)
                return data

        bsc_score = max(-1.0, min(1.0, bsc_score))
        interp_parts = []
        if gas_gwei is not None:
            interp_parts.append(f"Gas {gas_gwei:.1f} Gwei ({api_label})")
        if daily_tx is not None:
            interp_parts.append(f"日交易 {daily_tx/1e6:.2f}M ({tx_source or api_label})")
        if daily_new_addresses is not None:
            interp_parts.append(f"日新增地址 {daily_new_addresses/1e3:.0f}K")
        if gas_spike or tx_spike or addr_spike:
            interp_parts.append("链上异常活跃→BNB原生需求增加")
        elif bsc_score > 0.1:
            interp_parts.append("BSC热度偏高")
        else:
            interp_parts.append("BSC热度正常")
        if upgrade_hint:
            interp_parts.append(upgrade_hint)

        data = {
            "gas_gwei": gas_gwei,
            "daily_tx": daily_tx,
            "daily_new_addresses": daily_new_addresses,
            "gas_spike": gas_spike,
            "tx_spike": tx_spike,
            "addr_spike": addr_spike,
            "bsc_score": round(bsc_score, 3),
            "chain_id": self.bsc_chain_id,
            "api": api_label,
            "upgrade_hint": upgrade_hint,
            "interpretation": " | ".join(interp_parts),
            "source": f"{gas_source}+{tx_source}".strip("+") or api_label,
        }
        self._set_cache(cache_key, data)
        return data

    def _fetch_bsc_daily_tx_fallback(self) -> Tuple[Optional[int], str]:
        """Etherscan V2 不可用时的 DefiLlama 回退。"""
        try:
            r = requests.get(self.DEFILLAMA_CHAINS, timeout=self.timeout)
            r.raise_for_status()
            for chain in r.json():
                if (chain.get("name") or "").lower() in ("bsc", "bnb chain", "binance"):
                    txs = chain.get("txs") or chain.get("transactions")
                    if txs:
                        return int(txs), "defillama"
        except Exception as e:
            logger.debug("DefiLlama BSC 失败: %s", e)
        return None, ""

    def _fetch_bnb_volume_activity_proxy(self) -> Optional[Dict]:
        try:
            from bnb_quant_tool.data_fetcher import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            df = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=168)
            if df is None or len(df) < 48:
                return None
            vol = df["volume"].astype(float)
            vol_24 = float(vol.tail(24).mean())
            vol_7d = float(vol.mean())
            ratio = vol_24 / vol_7d if vol_7d > 0 else 1.0
            gas_spike = ratio >= 2.0
            tx_spike = ratio >= 1.5
            score = 0.0
            if ratio >= 2.0:
                score = 0.40
            elif ratio >= 1.5:
                score = 0.20
            return {
                "gas_gwei": None,
                "daily_tx": None,
                "volume_ratio_24h_7d": round(ratio, 3),
                "gas_spike": gas_spike,
                "tx_spike": tx_spike,
                "bsc_score": score,
                "interpretation": f"BNB 24h/7d 量比 {ratio:.2f}x (Meme活跃代理)",
            }
        except Exception as e:
            logger.debug("BNB volume proxy 失败: %s", e)
            return None

    # ============================================================
    # CoinGecko 辅助
    # ============================================================
    def _fetch_coingecko_global(self) -> Dict[str, Any]:
        try:
            r = requests.get(self.COINGECKO_GLOBAL, timeout=self.timeout)
            r.raise_for_status()
            d = r.json().get("data") or {}
            mcp = d.get("market_cap_percentage") or {}
            return {
                "btc_dominance": round(float(mcp.get("btc", 0)), 2),
                "eth_dominance": round(float(mcp.get("eth", 0)), 2),
                "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
            }
        except Exception as e:
            logger.warning("CoinGecko global 失败: %s", e)
            return {"error": str(e)}

    # ============================================================
    # 指标块构建
    # ============================================================
    def _build_mvrv_block(
        self, value: Optional[float], series: Any
    ) -> Dict[str, Any]:
        if value is None:
            return {"error": "无 MVRV 数据"}
        level, signal = self._mvrv_level(value)
        change_30d = self._series_pct_change(series, days=30) if series else None
        return {
            "value": round(value, 4),
            "level": level,
            "signal": signal,
            "change_30d_pct": round(change_30d, 2) if change_30d is not None else None,
        }

    def _build_netflow_block(
        self,
        latest: Optional[float],
        change_7d: Optional[float],
        series: Any,
        unit: str = "native",
    ) -> Dict[str, Any]:
        if latest is None:
            return {"error": "无 Netflow 数据"}
        # 正值 = 净流入交易所（潜在抛压）；负值 = 流出（囤币）
        if latest > 0:
            trend = "净流入"
            signal = "偏空"
        elif latest < 0:
            trend = "净流出"
            signal = "偏多"
        else:
            trend = "平衡"
            signal = "中性"
        return {
            "latest": round(latest, 4),
            "unit": unit,
            "trend": trend,
            "signal": signal,
            "change_7d": round(change_7d, 4) if change_7d is not None else None,
        }

    def _build_whale_block(
        self,
        latest: Optional[float],
        change_7d_pct: Optional[float],
        unit: str = "supply",
    ) -> Dict[str, Any]:
        if latest is None:
            return {"error": "无巨鲸数据"}
        if change_7d_pct is None:
            activity = "稳定"
            signal = "中性"
        elif change_7d_pct > 2:
            activity = "增仓"
            signal = "偏多"
        elif change_7d_pct < -2:
            activity = "减仓"
            signal = "偏空"
        else:
            activity = "稳定"
            signal = "中性"
        return {
            "value": round(latest, 4),
            "unit": unit,
            "change_7d_pct": round(change_7d_pct, 2) if change_7d_pct is not None else None,
            "activity": activity,
            "signal": signal,
        }

    @staticmethod
    def _mvrv_level(value: float) -> Tuple[str, str]:
        if value >= 3.5:
            return "严重高估", "强烈偏空"
        if value >= 2.5:
            return "高估", "偏空"
        if value >= 1.5:
            return "合理偏高", "中性"
        if value >= 1.0:
            return "合理", "中性偏多"
        if value >= 0.8:
            return "低估", "偏多"
        return "严重低估", "强烈偏多"

    # ============================================================
    # 综合打分 [-1, +1]
    # ============================================================
    def _aggregate_score(self, assets_data: Dict[str, Dict]) -> float:
        scores: List[float] = []
        weights: List[float] = []

        for asset, data in assets_data.items():
            if data.get("error"):
                continue
            w = 0.6 if asset == "BTC" else 0.4
            sub = 0.0
            n = 0

            mvrv = data.get("mvrv") or {}
            if "value" in mvrv:
                v = mvrv["value"]
                if v >= 3.0:
                    sub -= 0.6
                elif v >= 2.5:
                    sub -= 0.35
                elif v <= 0.9:
                    sub += 0.5
                elif v <= 1.2:
                    sub += 0.2
                n += 1

            nf = data.get("exchange_netflow") or {}
            if "latest" in nf:
                if nf["latest"] > 0:
                    sub -= 0.25
                elif nf["latest"] < 0:
                    sub += 0.25
                n += 1

            whale = data.get("whale_activity") or {}
            ch = whale.get("change_7d_pct")
            if ch is not None:
                if ch > 3:
                    sub += 0.2
                elif ch < -3:
                    sub -= 0.2
                n += 1

            if n > 0:
                scores.append(max(-1.0, min(1.0, sub)))
                weights.append(w)

        if not scores:
            return 0.0
        total_w = sum(weights)
        blended = sum(s * w for s, w in zip(scores, weights)) / total_w
        return round(max(-1.0, min(1.0, blended)), 3)

    def _interpret(self, data: Dict) -> str:
        score = data.get("onchain_score", 0.0)
        parts = []
        for asset, block in (data.get("assets") or {}).items():
            mvrv = (block.get("mvrv") or {}).get("value")
            nf = (block.get("exchange_netflow") or {}).get("trend")
            whale = (block.get("whale_activity") or {}).get("activity")
            if mvrv is not None:
                parts.append(f"{asset} MVRV={mvrv:.2f}")
            if nf:
                parts.append(f"{asset} 交易所{nf}")
            if whale:
                parts.append(f"{asset} 巨鲸{whale}")

        cg = data.get("coingecko") or {}
        if cg.get("btc_dominance"):
            parts.append(f"BTC市占 {cg['btc_dominance']:.1f}%")

        if score >= 0.35:
            tag = "链上偏多（长周期支撑）"
        elif score <= -0.35:
            tag = "链上偏空（长周期压力）"
        elif score >= 0.12:
            tag = "链上略偏多"
        elif score <= -0.12:
            tag = "链上略偏空"
        else:
            tag = "链上中性"
        bsc = data.get("bsc") or {}
        if bsc.get("interpretation"):
            parts.append(f"BSC: {bsc['interpretation']}")
        src = data.get("data_source", "?")
        return f"[{tag}|{src}]  " + "  ".join(parts)

    @staticmethod
    def _primary_source(assets_data: Dict[str, Dict]) -> str:
        sources = {d.get("source") for d in assets_data.values() if d.get("source")}
        if "glassnode" in sources:
            return "glassnode"
        if "coinmetrics" in sources:
            return "coinmetrics"
        if "price_proxy" in sources:
            return "price_proxy(binance)"
        return "none"

    # ============================================================
    # 工具
    # ============================================================
    @staticmethod
    def _resolve_leaders(symbol: str) -> Tuple[str, ...]:
        sym = symbol.upper().replace("/", "")
        return SYMBOL_TO_LEADERS.get(sym, LEADER_ASSETS)

    @staticmethod
    def _safe_float(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _series_latest(series: List) -> Optional[float]:
        if not series:
            return None
        return float(series[-1][1])

    @staticmethod
    def _series_change(series: List, days: int = 7) -> Optional[float]:
        if len(series) < days + 1:
            return None
        return float(series[-1][1]) - float(series[-1 - days][1])

    @staticmethod
    def _series_pct_change(series: List, days: int = 7) -> Optional[float]:
        if len(series) < days + 1:
            return None
        old = float(series[-1 - days][1])
        new = float(series[-1][1])
        if old == 0:
            return None
        return (new - old) / abs(old) * 100

    def _get_cache(self, key: str) -> Optional[Dict]:
        entry = self._cache.get(key)
        if entry:
            ts, data = entry
            if time.time() - ts <= self.cache_seconds:
                return data
        disk = self._disk.get(key, self.cache_seconds)
        if isinstance(disk, dict):
            self._cache[key] = (time.time(), disk)
            return disk
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)
        self._disk.set(key, data)

    @staticmethod
    def format_report(data: Dict) -> str:
        sep = "=" * 60
        lines = [sep, f"  链上筹码分析  -  {data.get('symbol', 'N/A')}", sep]
        lines.append(f"数据源: {data.get('data_source', '?')}  |  锚定: {', '.join(data.get('leader_assets', []))}")
        lines.append(f"综合链上分: {data.get('onchain_score', 0):+.3f}  (范围 -1~+1)")
        lines.append(f"解读: {data.get('interpretation', '')}")
        lines.append("")

        for asset, block in (data.get("assets") or {}).items():
            lines.append(f"--- {asset} ---")
            if block.get("error"):
                lines.append(f"  错误: {block['error']}")
                continue
            mvrv = block.get("mvrv") or {}
            if "value" in mvrv:
                lines.append(
                    f"  MVRV: {mvrv['value']:.4f} ({mvrv.get('level')}) → {mvrv.get('signal')}"
                )
            nf = block.get("exchange_netflow") or {}
            if "latest" in nf:
                ch = nf.get("change_7d")
                ch_str = f"  7d变化={ch}" if ch is not None else ""
                lines.append(
                    f"  交易所Netflow: {nf['latest']} {nf.get('unit', '')} ({nf.get('trend')}){ch_str}"
                )
            whale = block.get("whale_activity") or {}
            if "value" in whale:
                lines.append(
                    f"  巨鲸: {whale['value']} ({whale.get('unit')}) "
                    f"7d={whale.get('change_7d_pct', 'N/A')}% → {whale.get('activity')}"
                )
            lines.append("")

        cg = data.get("coingecko") or {}
        if cg.get("btc_dominance"):
            lines.append(
                f"CoinGecko: BTC市占 {cg['btc_dominance']}% | ETH {cg.get('eth_dominance', 0)}%"
            )
        lines.append(sep)
        return "\n".join(lines)

    @staticmethod
    def format_for_prompt(data: Dict) -> str:
        if not data:
            return ""
        lines = ["", "【链上筹码分析 — BTC/ETH 长周期锚定】", "=" * 50]
        lines.append(f"链上综合分: {data.get('onchain_score', 0):+.3f} (-1~+1)")
        lines.append(f"解读: {data.get('interpretation', '')}")
        for asset, block in (data.get("assets") or {}).items():
            mvrv = (block.get("mvrv") or {}).get("value")
            nf = (block.get("exchange_netflow") or {})
            whale = (block.get("whale_activity") or {})
            if mvrv is not None:
                lines.append(f"  {asset} MVRV={mvrv:.2f} ({(block.get('mvrv') or {}).get('level')})")
            if nf.get("trend"):
                lines.append(f"  {asset} 交易所{nf['trend']} netflow={nf.get('latest')}")
            if whale.get("activity"):
                lines.append(f"  {asset} 巨鲸{whale['activity']} 7d={whale.get('change_7d_pct')}%")
        cg = data.get("coingecko") or {}
        if cg.get("btc_dominance"):
            lines.append(f"  BTC市占 {cg['btc_dominance']}%")
        bsc = data.get("bsc") or {}
        if bsc.get("interpretation"):
            lines.append(f"  BSC热度: {bsc['interpretation']} (分 {bsc.get('bsc_score', 0):+.2f})")
        lines.append("=" * 50)
        lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    analyzer = OnChainAnalyzer(use_coinmetrics_fallback=True)
    result = analyzer.fetch_all("BNBUSDT")
    print(OnChainAnalyzer.format_report(result))
