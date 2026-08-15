"""
BNB 量化工具 - BNB 专属风控哨兵 (Risk Sentry)
================================================
两项 BNB 特异性拦截：

1. 资金费率极值 — Binance 合约 Funding Rate 过高 → 拦截做多
2. BNB/BTC 汇率弱势 — 大盘涨 BNB 不涨 → 降低仓位
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

class BNBRiskSentry:
    """BNB 专属风控哨兵。"""

    FAPI_MIRRORS = (
        "https://fapi.binance.com",
        "https://fapi.binance.me",
    )
    SPOT_MIRRORS = (
        "https://api.binance.com",
        "https://api.binance.me",
    )
    GATE_TICKER = "https://api.gateio.ws/api/v4/futures/usdt/tickers"

    def __init__(self, fetcher=None, config: Optional[Dict] = None):
        cfg = config or {}
        self.fetcher = fetcher
        self.enabled = bool(cfg.get("enabled", True))
        self.cache_seconds = int(cfg.get("cache_seconds", 300))
        self.timeout = int(cfg.get("timeout", 10))

        fr_cfg = cfg.get("funding_rate") or {}
        self.funding_extreme_threshold = float(fr_cfg.get("extreme_threshold", 0.001))
        self.funding_elevated_threshold = float(fr_cfg.get("elevated_threshold", 0.0005))
        self.funding_block_long = bool(fr_cfg.get("block_long_on_extreme", True))

        ratio_cfg = cfg.get("bnb_btc_ratio") or {}
        self.ratio_lookback_bars = int(ratio_cfg.get("lookback_bars", 24))
        self.ratio_interval = str(ratio_cfg.get("interval", "1h"))
        self.btc_min_rise_pct = float(ratio_cfg.get("btc_min_rise_pct", 0.005))
        self.ratio_max_drop_pct = float(ratio_cfg.get("ratio_max_drop_pct", -0.008))
        self.weak_position_scale = float(ratio_cfg.get("weak_position_scale", 0.55))

        liq_cfg = cfg.get("liquidity_guard") or {}
        self.liquidity_enabled = bool(liq_cfg.get("enabled", True))
        self.depth_drop_threshold = float(liq_cfg.get("depth_drop_threshold", 0.50))
        self.funding_spike_threshold = float(liq_cfg.get("funding_spike_threshold", 0.0003))
        self.tighten_sl_factor = float(liq_cfg.get("tighten_sl_factor", 0.75))
        self._prev_funding_rate: Optional[float] = None
        self._prev_depth_usdt: Optional[float] = None

        self._cache: Dict[str, Tuple[float, Dict]] = {}

    def fetch_all(
        self,
        symbol: str = "BNBUSDT",
        news_items: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled_result()

        cache_key = f"risk_sentry:{symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        funding = self.check_funding_extreme(symbol)
        ratio = self.check_bnb_btc_weakness(symbol)
        liquidity = self.check_liquidity_void(symbol, funding)

        block_long = False
        position_scale = 1.0
        reasons: List[str] = []

        if funding.get("block_long"):
            block_long = True
            reasons.append(funding.get("interpretation", "资金费率极值"))
        if liquidity.get("block_long"):
            block_long = True
            reasons.append(liquidity.get("interpretation", "流动性空洞"))
        if ratio.get("weak"):
            position_scale = min(position_scale, float(ratio.get("position_scale", 0.55)))

        risk_score = self._aggregate_risk(funding, ratio)
        if liquidity.get("liquidity_void"):
            risk_score = min(1.0, risk_score + 0.35)

        result = {
            "enabled": True,
            "funding_extreme": funding,
            "bnb_btc_weakness": ratio,
            "liquidity_guard": liquidity,
            "block_long": block_long,
            "position_scale": round(position_scale, 3),
            "sl_tighten_factor": float(liquidity.get("sl_tighten_factor") or 1.0),
            "risk_score": round(risk_score, 3),
            "interpretation": " | ".join(reasons) if reasons else "BNB风控哨兵：均未触发",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._set_cache(cache_key, result)
        return result

    # ---- 1. 资金费率极值 ----
    def check_funding_extreme(self, symbol: str = "BNBUSDT") -> Dict:
        rate, source = self._fetch_binance_funding(symbol)
        if rate is None:
            rate, source = self._fetch_gate_funding(symbol)

        if rate is None:
            return {
                "rate": None,
                "rate_pct": None,
                "extreme": False,
                "elevated": False,
                "block_long": False,
                "source": "none",
                "interpretation": "资金费率不可用",
            }

        rate_pct = rate * 100
        extreme = rate >= self.funding_extreme_threshold
        elevated = rate >= self.funding_elevated_threshold
        block = extreme and self.funding_block_long

        interp = f"Funding {rate_pct:.4f}%/8h ({source})"
        if extreme:
            interp += " — 散户极度狂热做多，插针爆仓前兆，拦截做多"
        elif elevated:
            interp += " — 费率偏高，谨慎追多"

        return {
            "rate": round(rate, 6),
            "rate_pct": round(rate_pct, 4),
            "extreme": extreme,
            "elevated": elevated,
            "block_long": block,
            "reversal_risk": extreme or elevated,
            "source": source,
            "threshold_pct": self.funding_extreme_threshold * 100,
            "interpretation": interp,
        }

    def _fetch_binance_funding(self, symbol: str) -> Tuple[Optional[float], str]:
        for base in self.FAPI_MIRRORS:
            try:
                resp = requests.get(
                    f"{base}/fapi/v1/premiumIndex",
                    params={"symbol": symbol},
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                rate = float(data.get("lastFundingRate") or data.get("lastFundingRate", 0))
                return rate, "binance_fapi"
            except Exception as e:
                logger.debug(f"Binance FAPI funding {base}: {e}")
        return None, ""

    def _fetch_gate_funding(self, symbol: str) -> Tuple[Optional[float], str]:
        try:
            contract = symbol.replace("USDT", "_USDT")
            resp = requests.get(
                self.GATE_TICKER,
                params={"contract": contract},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            rate = float(data.get("funding_rate", 0))
            return rate, "gate_io_fallback"
        except Exception as e:
            logger.debug(f"Gate funding fallback: {e}")
            return None, ""

            return None, ""

    # ---- 4. 插针 / 流动性空洞 ----
    def check_liquidity_void(
        self, symbol: str = "BNBUSDT", funding: Optional[Dict] = None,
    ) -> Dict:
        if not self.liquidity_enabled:
            return self._liquidity_clear()

        depth_usdt, depth_source = self._fetch_orderbook_depth(symbol)
        funding = funding or self.check_funding_extreme(symbol)
        rate = funding.get("rate")

        depth_drop = False
        funding_spike = False
        if depth_usdt is not None and self._prev_depth_usdt is not None and self._prev_depth_usdt > 0:
            drop_pct = 1.0 - (depth_usdt / self._prev_depth_usdt)
            depth_drop = drop_pct >= self.depth_drop_threshold

        if rate is not None and self._prev_funding_rate is not None:
            delta = abs(rate - self._prev_funding_rate)
            funding_spike = delta >= self.funding_spike_threshold

        if depth_usdt is not None:
            self._prev_depth_usdt = depth_usdt
        if rate is not None:
            self._prev_funding_rate = rate

        liquidity_void = depth_drop or (funding_spike and (depth_drop or (depth_usdt or 0) < 50000))
        block_long = liquidity_void
        sl_factor = self.tighten_sl_factor if liquidity_void else 1.0

        parts = []
        if depth_usdt is not None:
            parts.append(f"盘口深度 ~${depth_usdt:,.0f} ({depth_source})")
        if depth_drop:
            parts.append(f"深度骤降 ≥{self.depth_drop_threshold:.0%}")
        if funding_spike:
            parts.append("资金费率突变")
        if liquidity_void:
            parts.append("⚠ 防插针保护：暂停 LONG + 收紧 SL")

        return {
            "depth_usdt": depth_usdt,
            "depth_drop": depth_drop,
            "funding_spike": funding_spike,
            "liquidity_void": liquidity_void,
            "block_long": block_long,
            "sl_tighten_factor": sl_factor,
            "interpretation": " | ".join(parts) if parts else "流动性正常",
        }

    def _fetch_orderbook_depth(self, symbol: str, pct: float = 0.01) -> Tuple[Optional[float], str]:
        for base in self.SPOT_MIRRORS:
            try:
                resp = requests.get(
                    f"{base}/api/v3/depth",
                    params={"symbol": symbol, "limit": 100},
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                bids = data.get("bids") or []
                asks = data.get("asks") or []
                if not bids or not asks:
                    continue
                mid = (float(bids[0][0]) + float(asks[0][0])) / 2
                band = mid * pct
                bid_depth = sum(float(p) * float(q) for p, q in bids if float(p) >= mid - band)
                ask_depth = sum(float(p) * float(q) for p, q in asks if float(p) <= mid + band)
                return bid_depth + ask_depth, "binance_spot"
            except Exception as e:
                logger.debug("Orderbook depth %s: %s", base, e)
        return None, ""

    @staticmethod
    def _liquidity_clear() -> Dict:
        return {
            "depth_usdt": None,
            "depth_drop": False,
            "funding_spike": False,
            "liquidity_void": False,
            "block_long": False,
            "sl_tighten_factor": 1.0,
            "interpretation": "",
        }

    # ---- 2. BNB/BTC 汇率弱势 ----
    def check_bnb_btc_weakness(self, symbol: str = "BNBUSDT") -> Dict:
        if self.fetcher is None:
            return self._ratio_empty("no_fetcher")

        n = max(12, self.ratio_lookback_bars)
        try:
            bnb_df = self.fetcher.get_klines(symbol=symbol, interval=self.ratio_interval, limit=n)
            btc_df = self.fetcher.get_klines(symbol="BTCUSDT", interval=self.ratio_interval, limit=n)
        except Exception as e:
            return self._ratio_empty(str(e))

        if bnb_df is None or btc_df is None or len(bnb_df) < 2 or len(btc_df) < 2:
            return self._ratio_empty("insufficient_data")

        bnb_close = float(bnb_df["close"].iloc[-1])
        btc_close = float(btc_df["close"].iloc[-1])
        if btc_close <= 0 or bnb_close <= 0:
            return self._ratio_empty("invalid_price")

        ratio_now = bnb_close / btc_close
        bnb_start = float(bnb_df["close"].iloc[0])
        btc_start = float(btc_df["close"].iloc[0])
        ratio_start = bnb_start / btc_start if btc_start > 0 else ratio_now
        ratio_change = (ratio_now - ratio_start) / ratio_start if ratio_start > 0 else 0.0
        btc_change = (btc_close - btc_start) / btc_start if btc_start > 0 else 0.0
        bnb_change = (bnb_close - bnb_start) / bnb_start if bnb_start > 0 else 0.0

        weak = (
            btc_change >= self.btc_min_rise_pct
            and ratio_change <= self.ratio_max_drop_pct
        ) or (
            btc_change > 0.003 and bnb_change < 0 and ratio_change < -0.004
        )

        scale = self.weak_position_scale if weak else 1.0
        interp = (
            f"BNB/BTC 汇率 {ratio_change:+.2%} (BTC {btc_change:+.2%}, BNB {bnb_change:+.2%})"
        )
        if weak:
            interp += " — 资金不在币安生态，做多胜率低，降仓"

        return {
            "ratio": round(ratio_now, 8),
            "ratio_change_pct": round(ratio_change * 100, 3),
            "btc_change_pct": round(btc_change * 100, 3),
            "bnb_change_pct": round(bnb_change * 100, 3),
            "weak": weak,
            "position_scale": scale,
            "lookback_bars": n,
            "interpretation": interp,
        }

    @staticmethod
    def _ratio_empty(reason: str) -> Dict:
        return {
            "ratio": None,
            "ratio_change_pct": 0.0,
            "btc_change_pct": 0.0,
            "bnb_change_pct": 0.0,
            "weak": False,
            "position_scale": 1.0,
            "interpretation": f"BNB/BTC 相对强度不可用 ({reason})",
        }

    @staticmethod
    def _aggregate_risk(funding: Dict, ratio: Dict) -> float:
        score = 0.0
        if funding.get("extreme"):
            score += 0.45
        elif funding.get("elevated"):
            score += 0.2
        if ratio.get("weak"):
            score += 0.25
        return min(1.0, score)

    @classmethod
    def format_for_prompt(cls, sentry: Dict) -> str:
        if not sentry or not sentry.get("enabled"):
            return ""
        lines = ["\n【BNB 风控哨兵】"]
        fr = sentry.get("funding_extreme") or {}
        if fr.get("extreme") or fr.get("elevated"):
            lines.append(f"- 资金费率: {fr.get('interpretation', '')}")
        ratio = sentry.get("bnb_btc_weakness") or {}
        if ratio.get("weak"):
            lines.append(f"- 汇率弱势: {ratio.get('interpretation', '')}")
        if sentry.get("block_long"):
            lines.append("- ⛔ 风控哨兵：当前禁止做多")
        liq = sentry.get("liquidity_guard") or {}
        if liq.get("liquidity_void"):
            lines.append(f"- 插针保护: {liq.get('interpretation', '')}")
        lines.append("")
        return "\n".join(lines)

    def _get_cache(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self.cache_seconds:
                return data
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)

    @staticmethod
    def _disabled_result() -> Dict:
        return {
            "enabled": False,
            "block_long": False,
            "position_scale": 1.0,
            "risk_score": 0.0,
            "interpretation": "BNB 风控哨兵已禁用",
        }
