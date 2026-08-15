"""
OrderflowSignalLayer — 主力大单 / 吃单比 / 多空比软信号层。

借鉴：AICoin（大单+资金流）、Freqtrade 市场微观结构、Real-time_stock_analysis 实时监控。
不直接下单；输出 soft vote 供 institutional_conviction / scanner 使用。

数据源（公开 REST，无需 API Key）：
- Binance Futures aggTrades → 大单买卖失衡
- takerlongshortRatio → 主动买卖比
- topLongShortAccountRatio → 大户多空比
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from bnb_quant_tool.disk_ttl_cache import DiskTTLCache

logger = logging.getLogger(__name__)

_FAPI_MIRRORS = (
    "https://fapi.binance.com",
    "https://fapi.binance.me",
)


class OrderflowSignalLayer:
    """订单流 / 衍生品微观结构信号聚合。"""

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        cache_seconds: int = 120,
        cache_dir: str = "data/orderflow_cache",
        large_trade_usd: float = 50_000.0,
        agg_limit: int = 200,
        config: Optional[Dict[str, Any]] = None,
    ):
        cfg = (config or {}).get("orderflow") or config or {}
        self.timeout = float(cfg.get("timeout", timeout))
        self.cache_seconds = max(0, int(cfg.get("cache_seconds", cache_seconds)))
        self.large_trade_usd = float(cfg.get("large_trade_usd", large_trade_usd))
        self.agg_limit = max(50, min(1000, int(cfg.get("agg_limit", agg_limit))))
        self.enabled = bool(cfg.get("enabled", True))
        self._mem: Dict[str, Tuple[float, Dict]] = {}
        self._disk = DiskTTLCache(
            str(cfg.get("cache_dir") or cache_dir), prefix="orderflow"
        )

    def fetch_all(self, symbol: str = "BNBUSDT") -> Dict[str, Any]:
        """拉取并聚合订单流信号。失败时返回可用=False 的空壳，不抛异常。"""
        symbol = (symbol or "BNBUSDT").upper().replace("/", "")
        if not self.enabled:
            return {"symbol": symbol, "available": False, "reason": "disabled"}

        cache_key = f"all:{symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        large = self._fetch_large_trade_imbalance(symbol)
        taker = self._fetch_taker_ratio(symbol)
        top = self._fetch_top_trader_ratio(symbol)

        score, parts = self._aggregate(large, taker, top)
        direction = "NEUTRAL"
        if score >= 0.25:
            direction = "BULLISH"
        elif score <= -0.25:
            direction = "BEARISH"

        result: Dict[str, Any] = {
            "symbol": symbol,
            "available": any(x.get("available") for x in (large, taker, top)),
            "orderflow_score": round(score, 4),
            "direction": direction,
            "large_trades": large,
            "taker_ratio": taker,
            "top_trader": top,
            "interpretation": "; ".join(parts) if parts else "订单流数据不足",
            "soft_vote": self._soft_vote(score, direction),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if result["available"]:
            self._set_cache(cache_key, result)
        return result

    def _soft_vote(self, score: float, direction: str) -> Dict[str, Any]:
        """机构投票池可消费的软票。"""
        if abs(score) < 0.15:
            sig = "HOLD"
            conf = 0.45
        elif score > 0:
            sig = "BUY"
            conf = min(0.85, 0.55 + abs(score) * 0.35)
        else:
            sig = "SELL"
            conf = min(0.85, 0.55 + abs(score) * 0.35)
        return {
            "strategy": "orderflow_microstructure",
            "signal": sig,
            "confidence": round(conf, 3),
            "score": round(score, 4),
            "direction": direction,
            "description": f"订单流软票 score={score:.2f}",
        }

    def _aggregate(
        self,
        large: Dict,
        taker: Dict,
        top: Dict,
    ) -> Tuple[float, List[str]]:
        score = 0.0
        wsum = 0.0
        parts: List[str] = []

        if large.get("available"):
            s = float(large.get("imbalance") or 0.0)
            w = 40.0
            score += s * w
            wsum += w
            parts.append(
                f"大单失衡 {s:+.2f} "
                f"(买${large.get('buy_usd', 0):.0f}/卖${large.get('sell_usd', 0):.0f})"
            )

        if taker.get("available"):
            s = float(taker.get("score") or 0.0)
            w = 35.0
            score += s * w
            wsum += w
            parts.append(f"主动买卖比 {taker.get('buy_sell_ratio', 0):.3f}→{s:+.2f}")

        if top.get("available"):
            s = float(top.get("score") or 0.0)
            w = 25.0
            score += s * w
            wsum += w
            parts.append(
                f"大户多空比 {top.get('long_short_ratio', 0):.3f}→{s:+.2f}"
            )

        if wsum <= 0:
            return 0.0, parts
        return max(-1.0, min(1.0, score / wsum)), parts

    # ── data fetchers ──────────────────────────────────────────

    def _request_json(self, path: str, params: Dict[str, Any]) -> Optional[Any]:
        last_err: Optional[Exception] = None
        for base in _FAPI_MIRRORS:
            try:
                r = requests.get(
                    f"{base}{path}",
                    params=params,
                    timeout=self.timeout,
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                continue
        if last_err:
            logger.debug("orderflow request %s failed: %s", path, last_err)
        return None

    def _fetch_large_trade_imbalance(self, symbol: str) -> Dict[str, Any]:
        data = self._request_json(
            "/fapi/v1/aggTrades",
            {"symbol": symbol, "limit": self.agg_limit},
        )
        empty = {"available": False, "imbalance": 0.0, "buy_usd": 0.0, "sell_usd": 0.0}
        if not isinstance(data, list) or not data:
            return empty

        buy_usd = 0.0
        sell_usd = 0.0
        large_n = 0
        for t in data:
            try:
                price = float(t.get("p") or 0)
                qty = float(t.get("q") or 0)
                notional = price * qty
                if notional < self.large_trade_usd:
                    continue
                large_n += 1
                # m=true → buyer is maker → 主动卖
                if t.get("m"):
                    sell_usd += notional
                else:
                    buy_usd += notional
            except (TypeError, ValueError):
                continue

        total = buy_usd + sell_usd
        if total <= 0 or large_n == 0:
            return {
                "available": True,
                "imbalance": 0.0,
                "buy_usd": 0.0,
                "sell_usd": 0.0,
                "large_count": 0,
                "note": "无达到阈值的大单",
            }
        imbalance = (buy_usd - sell_usd) / total  # -1~+1
        return {
            "available": True,
            "imbalance": round(imbalance, 4),
            "buy_usd": round(buy_usd, 2),
            "sell_usd": round(sell_usd, 2),
            "large_count": large_n,
            "threshold_usd": self.large_trade_usd,
        }

    def _fetch_taker_ratio(self, symbol: str) -> Dict[str, Any]:
        data = self._request_json(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": "1h", "limit": 2},
        )
        empty = {"available": False, "score": 0.0, "buy_sell_ratio": 0.0}
        if not isinstance(data, list) or not data:
            return empty
        row = data[-1]
        try:
            ratio = float(row.get("buySellRatio") or 0)
        except (TypeError, ValueError):
            return empty
        # ratio>1 主动买多 → 偏多；映射到 [-1,1]
        score = max(-1.0, min(1.0, (ratio - 1.0) * 2.0))
        return {
            "available": True,
            "buy_sell_ratio": round(ratio, 4),
            "score": round(score, 4),
            "timestamp": row.get("timestamp"),
        }

    def _fetch_top_trader_ratio(self, symbol: str) -> Dict[str, Any]:
        data = self._request_json(
            "/futures/data/topLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 2},
        )
        empty = {"available": False, "score": 0.0, "long_short_ratio": 0.0}
        if not isinstance(data, list) or not data:
            return empty
        row = data[-1]
        try:
            ratio = float(row.get("longShortRatio") or 0)
        except (TypeError, ValueError):
            return empty
        # >1 大户偏多；极端拥挤反向（>2.5 或 <0.4）给反向分
        if ratio >= 2.5:
            score = -0.35  # 拥挤多
            note = "大户多头拥挤"
        elif ratio <= 0.4:
            score = 0.35  # 拥挤空 → 反转偏多
            note = "大户空头拥挤"
        else:
            score = max(-1.0, min(1.0, (ratio - 1.0)))
            note = "大户仓位中性偏"
        return {
            "available": True,
            "long_short_ratio": round(ratio, 4),
            "long_account": row.get("longAccount"),
            "short_account": row.get("shortAccount"),
            "score": round(score, 4),
            "note": note,
            "timestamp": row.get("timestamp"),
        }

    def _get_cache(self, key: str) -> Optional[Dict]:
        now = time.time()
        hit = self._mem.get(key)
        if hit and now - hit[0] < self.cache_seconds:
            return hit[1]
        disk = self._disk.get(key, float(self.cache_seconds))
        if isinstance(disk, dict):
            self._mem[key] = (now, disk)
            return disk
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._mem[key] = (time.time(), data)
        try:
            self._disk.set(key, data)
        except Exception:
            pass


def orderflow_conviction_score(
    orderflow: Optional[Dict[str, Any]],
) -> Tuple[Optional[float], str]:
    """供 institutional_conviction 使用：返回 (-1~1, text) 或 (None, '')."""
    if not orderflow or not orderflow.get("available"):
        return None, ""
    score = float(orderflow.get("orderflow_score") or 0.0)
    text = str(orderflow.get("interpretation") or f"订单流 {score:+.2f}")
    return max(-1.0, min(1.0, score)), text


def fetch_orderflow(
    symbol: str = "BNBUSDT",
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """便捷入口：按 config.orderflow 拉取。"""
    cfg = config or {}
    of_cfg = cfg.get("orderflow") if isinstance(cfg.get("orderflow"), dict) else None
    if of_cfg is None and any(
        k in cfg for k in ("enabled", "large_trade_usd", "cache_seconds", "agg_limit")
    ):
        # 已是 orderflow 小节本身
        of_cfg = cfg
    layer = OrderflowSignalLayer(config={"orderflow": of_cfg or {}})
    return layer.fetch_all(symbol)
