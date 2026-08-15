"""
币安成交量 / BNB 注意力份额因子
================================
平台币视角：交易所热度与 BNB 相对成交强度（无需全市场付费份额数据）。

代理指标：
1. BNB/BTC 24h 报价成交额比（注意力份额）
2. BNB 自身成交额相对近 N 日均量（量能动量）
3. 可选：合约/现货成交额比（杠杆热度，镜像可用时）

输出 volume_share_score ∈ [-1, 1]，供 BNBSpecificFactors 软加权。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class BinanceVolumeShareFactor:
    """币安量能 / BNB 注意力份额因子。"""

    SPOT_MIRRORS = (
        "https://api.binance.me",
        "https://data-api.binance.vision",
        "https://api.binance.com",
    )
    FAPI_MIRRORS = (
        "https://fapi.binance.com",
        "https://fapi.binance.me",
    )

    def __init__(self, fetcher=None, config: Optional[Dict] = None):
        cfg = config or {}
        self.fetcher = fetcher
        self.enabled = bool(cfg.get("enabled", True))
        self.cache_seconds = int(cfg.get("cache_seconds", 300))
        self.timeout = int(cfg.get("timeout", 12))
        self.lookback_days = int(cfg.get("lookback_days", 7))
        self.baseline_bnb_btc_ratio = float(cfg.get("baseline_bnb_btc_ratio", 0.08))
        self.vol_momentum_bull = float(cfg.get("vol_momentum_bull", 1.25))
        self.vol_momentum_bear = float(cfg.get("vol_momentum_bear", 0.75))
        self.attention_bull_mult = float(cfg.get("attention_bull_mult", 1.35))
        self.attention_bear_mult = float(cfg.get("attention_bear_mult", 0.70))
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BNBQuantTool/2.11)",
            "Accept": "application/json",
        }

    def fetch(self, symbol: str = "BNBUSDT") -> Dict[str, Any]:
        if not self.enabled:
            return self._empty(enabled=False)

        cache_key = f"vol_share:{symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        bnb_qv, btc_qv, spot_src = self._fetch_spot_quote_volumes(symbol)
        fut_qv, fut_src = self._fetch_futures_quote_volume(symbol)
        momentum = self._compute_volume_momentum(symbol)

        attention_ratio = None
        attention_score = 0.0
        if bnb_qv and btc_qv and btc_qv > 0:
            attention_ratio = bnb_qv / btc_qv
            baseline = max(1e-9, self.baseline_bnb_btc_ratio)
            rel = attention_ratio / baseline
            if rel >= self.attention_bull_mult:
                attention_score = min(1.0, 0.35 + (rel - self.attention_bull_mult) * 0.8)
            elif rel <= self.attention_bear_mult:
                attention_score = max(-1.0, -0.35 - (self.attention_bear_mult - rel) * 0.8)
            else:
                attention_score = max(-0.3, min(0.3, (rel - 1.0) * 0.5))

        momentum_score = float(momentum.get("momentum_score") or 0.0)

        futures_spot_ratio = None
        futures_heat = 0.0
        if fut_qv and bnb_qv and bnb_qv > 0:
            futures_spot_ratio = fut_qv / bnb_qv
            if futures_spot_ratio >= 3.0:
                futures_heat = 0.15
            elif futures_spot_ratio >= 1.5:
                futures_heat = 0.05
            elif futures_spot_ratio < 0.5:
                futures_heat = -0.05

        # 注意力与量能动量为主，合约热度为辅
        score = attention_score * 0.55 + momentum_score * 0.35 + futures_heat * 0.10
        score = max(-1.0, min(1.0, score))

        rising = score >= 0.25
        fading = score <= -0.25
        parts: List[str] = []
        if attention_ratio is not None:
            parts.append(f"BNB/BTC成交额比 {attention_ratio:.3f}")
        if momentum.get("vol_ratio") is not None:
            parts.append(f"量能动量 {float(momentum['vol_ratio']):.2f}x")
        if futures_spot_ratio is not None:
            parts.append(f"合约/现货 {futures_spot_ratio:.2f}x")
        if rising:
            parts.append("交易所侧量能偏强")
        elif fading:
            parts.append("交易所侧量能偏弱")
        else:
            parts.append("交易所量能中性")

        result = {
            "enabled": True,
            "volume_share_score": round(score, 3),
            "bnb_quote_volume_24h": bnb_qv,
            "btc_quote_volume_24h": btc_qv,
            "attention_ratio": round(attention_ratio, 5) if attention_ratio is not None else None,
            "attention_score": round(attention_score, 3),
            "momentum": momentum,
            "futures_quote_volume_24h": fut_qv,
            "futures_spot_ratio": round(futures_spot_ratio, 3) if futures_spot_ratio is not None else None,
            "rising": rising,
            "fading": fading,
            "source": {
                "spot": spot_src,
                "futures": fut_src,
                "momentum": momentum.get("source") or "none",
            },
            "interpretation": " | ".join(parts),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._set_cache(cache_key, result)
        return result

    def _fetch_spot_quote_volumes(
        self, symbol: str,
    ) -> Tuple[Optional[float], Optional[float], str]:
        symbol = (symbol or "BNBUSDT").upper()
        # 优先走 fetcher（已带镜像/备用）
        if self.fetcher is not None:
            try:
                bnb = self.fetcher.get_ticker(symbol)
                btc = self.fetcher.get_ticker("BTCUSDT")
                bnb_qv = float(bnb.get("quoteVolume") or 0) or None
                btc_qv = float(btc.get("quoteVolume") or 0) or None
                if bnb_qv and btc_qv:
                    return bnb_qv, btc_qv, getattr(self.fetcher, "last_data_source", "fetcher")
            except Exception as exc:
                logger.debug("volume_share fetcher ticker: %s", exc)

        bnb_qv = self._spot_quote_volume(symbol)
        btc_qv = self._spot_quote_volume("BTCUSDT")
        src = "binance_spot" if (bnb_qv or btc_qv) else "none"
        return bnb_qv, btc_qv, src

    def _spot_quote_volume(self, symbol: str) -> Optional[float]:
        for base in self.SPOT_MIRRORS:
            try:
                resp = requests.get(
                    f"{base}/api/v3/ticker/24hr",
                    params={"symbol": symbol},
                    headers=self._headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                qv = float(data.get("quoteVolume") or 0)
                if qv > 0:
                    return qv
            except Exception as exc:
                logger.debug("spot ticker %s %s: %s", base, symbol, exc)
        return None

    def _fetch_futures_quote_volume(self, symbol: str) -> Tuple[Optional[float], str]:
        symbol = (symbol or "BNBUSDT").upper()
        for base in self.FAPI_MIRRORS:
            try:
                resp = requests.get(
                    f"{base}/fapi/v1/ticker/24hr",
                    params={"symbol": symbol},
                    headers=self._headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                qv = float(data.get("quoteVolume") or 0)
                if qv > 0:
                    return qv, base.split("//", 1)[-1]
            except Exception as exc:
                logger.debug("futures ticker %s: %s", base, exc)
        return None, "none"

    def _compute_volume_momentum(self, symbol: str) -> Dict[str, Any]:
        """近 lookback_days 日均量 vs 更早同等窗口。"""
        empty = {
            "vol_ratio": None,
            "momentum_score": 0.0,
            "recent_avg": None,
            "prior_avg": None,
            "source": "none",
        }
        if self.fetcher is None:
            return empty
        days = max(3, self.lookback_days)
        need = days * 2 + 2
        try:
            df = self.fetcher.get_klines(symbol=symbol, interval="1d", limit=need)
        except Exception as exc:
            logger.debug("volume momentum klines: %s", exc)
            return empty
        if df is None or len(df) < days * 2:
            return empty

        col = "quote_volume" if "quote_volume" in df.columns else "volume"
        series = df[col].astype(float)
        recent = float(series.iloc[-days:].mean())
        prior = float(series.iloc[-days * 2 : -days].mean())
        if prior <= 0:
            return empty
        ratio = recent / prior
        if ratio >= self.vol_momentum_bull:
            score = min(1.0, 0.4 + (ratio - self.vol_momentum_bull) * 0.6)
        elif ratio <= self.vol_momentum_bear:
            score = max(-1.0, -0.4 - (self.vol_momentum_bear - ratio) * 0.6)
        else:
            score = max(-0.25, min(0.25, (ratio - 1.0) * 0.5))
        return {
            "vol_ratio": round(ratio, 4),
            "momentum_score": round(score, 3),
            "recent_avg": round(recent, 2),
            "prior_avg": round(prior, 2),
            "source": "klines_1d",
        }

    @staticmethod
    def format_for_prompt(data: Optional[Dict]) -> str:
        if not data or data.get("enabled") is False:
            return ""
        score = data.get("volume_share_score")
        if score is None:
            return ""
        return f"- 交易所量能份额: {data.get('interpretation', '')} (分 {score:+.2f})"

    def _get_cache(self, key: str) -> Optional[Dict]:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)

    @staticmethod
    def _empty(*, enabled: bool = True) -> Dict[str, Any]:
        return {
            "enabled": enabled,
            "volume_share_score": 0.0,
            "rising": False,
            "fading": False,
            "interpretation": "量能份额因子已禁用" if not enabled else "量能数据不足",
        }
