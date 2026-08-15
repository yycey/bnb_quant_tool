"""
BNB量化交易工具 - 市场情绪模块 (Market Sentiment)
====================================================
免费数据源拼装的"加密市场温度计"：

1. 恐惧贪婪指数 (Fear & Greed Index)
   - 来源: https://api.alternative.me/fng/?limit=2
   - 0-100：< 25 极度恐惧（往往是底部，买入机会）
                  > 75 极度贪婪（往往是顶部，警惕回调）
   - 不需要 API Key

2. 永续合约资金费率 (Funding Rate)
   - 来源: https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=BNB_USDT
   - 大于 0：多头给空头付钱（多头过热，可能下跌）
   - 小于 0：空头给多头付钱（空头过热，可能反弹）
   - 不需要 API Key

3. 持仓量 (Open Interest)
   - 来源: https://api.gateio.ws/api/v4/futures/usdt/tickers (total_size 字段)
   - 价格上涨 + OI 上升：趋势加强（追多）
   - 价格上涨 + OI 下降：减仓上涨（不健康，警惕反转）

输出统一打分 sentiment_score：[-1, +1]，正值看涨，负值看跌。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class MarketSentiment:
    """市场情绪聚合器（免费 API）"""

    FNG_URL = "https://api.alternative.me/fng/?limit=2&format=json"
    # Gate.io 合约 API（替代不可达的 fapi.binance.com）
    GATE_FUTURES_TICKER_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    GATE_FUNDING_HIST_URL = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"

    def __init__(self, timeout: int = 10, cache_seconds: int = 1800, cache_dir: str = "data/sentiment_cache"):
        self.timeout = timeout
        self.cache_seconds = max(0, int(cache_seconds))
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        from bnb_quant_tool.disk_ttl_cache import DiskTTLCache
        self._disk = DiskTTLCache(cache_dir, prefix="sentiment")

    # ============================================================
    # 主入口
    # ============================================================
    def fetch_all(self, symbol: str = "BNBUSDT") -> Dict:
        """一次性拉取全部情绪数据（带缓存）。"""
        cache_key = f"all:{symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        gate_ticker = self._fetch_gate_ticker(symbol)
        result: Dict = {
            "symbol": symbol,
            "fear_greed": self.fetch_fear_greed(),
            "funding_rate": self.fetch_funding_rate(symbol, gate_ticker=gate_ticker),
            "open_interest": self.fetch_open_interest(symbol, gate_ticker=gate_ticker),
        }
        result["sentiment_score"] = self._aggregate_score(result)
        result["interpretation"] = self._interpret(result)
        self._set_cache(cache_key, result)
        return result

    def _fetch_gate_ticker(self, symbol: str) -> Optional[Dict]:
        """Gate.io 合约 ticker（资金费率 + 持仓量共用，避免重复请求）。"""
        try:
            contract = symbol.replace("USDT", "_USDT")
            r = requests.get(
                self.GATE_FUTURES_TICKER_URL,
                params={"contract": contract},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data[0] if data else None
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("Gate.io ticker 获取失败: %s", e)
            return None

    # ============================================================
    # 恐惧贪婪指数
    # ============================================================
    def fetch_fear_greed(self) -> Dict:
        try:
            r = requests.get(self.FNG_URL, timeout=self.timeout)
            r.raise_for_status()
            data = r.json().get("data") or []
            if not data:
                return {"error": "无数据"}
            today = data[0]
            yesterday = data[1] if len(data) > 1 else None
            value = int(today.get("value", 50))
            classification = today.get("value_classification", "")
            change = (value - int(yesterday.get("value", value))) if yesterday else 0
            return {
                "value": value,
                "classification": classification,
                "change": change,
                "level": self._fng_level(value),
            }
        except Exception as e:
            logger.warning(f"恐惧贪婪指数获取失败: {e}")
            return {"error": str(e)}

    @staticmethod
    def _fng_level(v: int) -> str:
        if v <= 20:
            return "极度恐惧"
        if v <= 40:
            return "恐惧"
        if v <= 60:
            return "中性"
        if v <= 80:
            return "贪婪"
        return "极度贪婪"

    # ============================================================
    # 资金费率
    # ============================================================
    def fetch_funding_rate(
        self,
        symbol: str = "BNBUSDT",
        gate_ticker: Optional[Dict] = None,
    ) -> Dict:
        try:
            data = gate_ticker if gate_ticker is not None else self._fetch_gate_ticker(symbol)
            if not data:
                return {"error": "无数据"}
            rate = float(data.get("funding_rate", 0))
            mark = float(data.get("mark_price", 0))
            index = float(data.get("index_price", 0))
            return {
                "rate": round(rate, 6),
                "rate_pct": round(rate * 100, 4),
                "annualized_pct": round(rate * 3 * 365 * 100, 2),  # 一天 3 次结算
                "mark_price": mark,
                "index_price": index,
                "level": self._funding_level(rate),
            }
        except Exception as e:
            logger.warning(f"资金费率获取失败: {e}")
            return {"error": str(e)}

    @staticmethod
    def _funding_level(rate: float) -> str:
        # 单期资金费率（每 8 小时）
        if rate > 0.0005:
            return "多头过热"
        if rate < -0.0005:
            return "空头过热"
        if rate > 0.0001:
            return "略偏多"
        if rate < -0.0001:
            return "略偏空"
        return "平衡"

    # ============================================================
    # 持仓量
    # ============================================================
    def fetch_open_interest(
        self,
        symbol: str = "BNBUSDT",
        gate_ticker: Optional[Dict] = None,
    ) -> Dict:
        try:
            data = gate_ticker if gate_ticker is not None else self._fetch_gate_ticker(symbol)
            if not data:
                return {"error": "无数据"}
            contract = symbol.replace("USDT", "_USDT")
            current_size = float(data.get("total_size", 0))

            # 取历史资金费率推断 24h 变化方向（Gate.io 无直接 OI 历史 API）
            try:
                r2 = requests.get(
                    self.GATE_FUNDING_HIST_URL,
                    params={"contract": contract, "limit": 3},
                    timeout=self.timeout,
                )
                r2.raise_for_status()
                hist = r2.json() or []
                # 通过资金费率趋势侧面推断：费率升 → 多头增仓
                if len(hist) >= 2:
                    latest_rate = float(hist[-1].get("r", 0))
                    prev_rate = float(hist[0].get("r", 0))
                    # 费率正向变大 → 多头增仓，反向变大 → 空头增仓
                    rate_trend = latest_rate - prev_rate
                    if rate_trend > 0.0001:
                        change_pct = 2.0   # 估算多头增仓 ~2%
                    elif rate_trend < -0.0001:
                        change_pct = -2.0  # 估算空头增仓 ~-2%
                    else:
                        change_pct = 0.0
                else:
                    change_pct = 0.0
            except Exception:
                change_pct = 0.0

            return {
                "current": round(current_size, 0),
                "change_24h_pct": round(change_pct, 3),
                "trend": "上升" if change_pct > 1 else ("下降" if change_pct < -1 else "稳定"),
            }
        except Exception as e:
            logger.warning(f"持仓量获取失败: {e}")
            return {"error": str(e)}

    # ============================================================
    # 综合打分
    # ============================================================
    def _aggregate_score(self, data: Dict) -> float:
        """综合情绪打分: [-1, +1]，正值看涨"""
        score = 0.0
        n = 0

        fng = data.get("fear_greed") or {}
        if "value" in fng:
            v = fng["value"]
            # 极度恐惧（<=20）→ +0.6（反向看多）；极度贪婪（>=80）→ -0.6
            if v <= 20:
                score += 0.6
            elif v <= 40:
                score += 0.25
            elif v >= 80:
                score -= 0.6
            elif v >= 60:
                score -= 0.25
            n += 1

        fr = data.get("funding_rate") or {}
        if "rate" in fr:
            rate = fr["rate"]
            # 资金费率反向：过热则减分
            if rate > 0.0005:
                score -= 0.5
            elif rate > 0.0001:
                score -= 0.2
            elif rate < -0.0005:
                score += 0.5
            elif rate < -0.0001:
                score += 0.2
            n += 1

        oi = data.get("open_interest") or {}
        if "change_24h_pct" in oi:
            ch = oi["change_24h_pct"]
            # 持仓量是辅助信号，权重小
            if ch > 5:
                score += 0.15
            elif ch < -5:
                score -= 0.15
            n += 1

        if n == 0:
            return 0.0
        # 归一化到 [-1, 1]
        return round(max(-1.0, min(1.0, score)), 3)

    @staticmethod
    def _interpret(data: Dict) -> str:
        s = data.get("sentiment_score", 0.0)
        msgs = []
        fng = data.get("fear_greed") or {}
        if "value" in fng:
            msgs.append(f"恐惧贪婪 {fng['value']}({fng.get('level')})")
        fr = data.get("funding_rate") or {}
        if "rate_pct" in fr:
            msgs.append(f"资金费率 {fr['rate_pct']}%({fr.get('level')})")
        oi = data.get("open_interest") or {}
        if "change_24h_pct" in oi:
            msgs.append(f"持仓量 24h {oi['change_24h_pct']:+.2f}%")

        if s >= 0.4:
            tag = "整体偏多机会"
        elif s <= -0.4:
            tag = "整体偏空风险"
        elif s >= 0.15:
            tag = "略偏多"
        elif s <= -0.15:
            tag = "略偏空"
        else:
            tag = "情绪中性"
        return f"[{tag}]  " + "  ".join(msgs)

    # ============================================================
    # 文本报告
    # ============================================================
    @staticmethod
    def format_report(data: Dict) -> str:
        sep = "=" * 60
        lines = [sep, f"  市场情绪面板  -  {data.get('symbol', 'N/A')}", sep]
        fng = data.get("fear_greed") or {}
        fr = data.get("funding_rate") or {}
        oi = data.get("open_interest") or {}
        lines.append(f"恐惧贪婪指数 : {fng.get('value', 'N/A')} ({fng.get('level', '')})  24h变化 {fng.get('change', 0):+d}")
        lines.append(f"资金费率     : {fr.get('rate_pct', 'N/A')}%  年化≈{fr.get('annualized_pct', 'N/A')}%  ({fr.get('level', '')})")
        lines.append(f"未平仓量     : {oi.get('current', 'N/A')}  24h变化 {oi.get('change_24h_pct', 0):+.2f}%  ({oi.get('trend', '')})")
        lines.append(f"综合情绪分   : {data.get('sentiment_score', 0):+.3f}  (范围 -1~+1)")
        lines.append(f"解读         : {data.get('interpretation', '')}")
        lines.append(sep)
        return "\n".join(lines)

    def _get_cache(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            ts, data = self._cache[key]
            if self.cache_seconds > 0 and time.time() - ts < self.cache_seconds:
                return data
        disk = self._disk.get(key, self.cache_seconds)
        if isinstance(disk, dict):
            self._cache[key] = (time.time(), disk)
            return disk
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)
        self._disk.set(key, data)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._disk.clear()


if __name__ == "__main__":
    s = MarketSentiment()
    d = s.fetch_all("BNBUSDT")
    print(MarketSentiment.format_report(d))
