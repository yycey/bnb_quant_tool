"""
BNB量化交易工具 - 宏观数据层 (Macro Data Layer)
================================================
2026 加密市场与宏观资产高度联动，本模块拉取并量化：

1. 美股（尤其科技股 QQQ）— 风险 appetite 代理
2. 美债 10Y 收益率 (^TNX) — 无风险利率 / 流动性
3. 美元指数 (DXY) — 全球流动性
4. BTC 与上述资产的滚动相关性
5. 美联储政策预期代理 — 收益率曲线与利率动量非线性信号

数据源：Yahoo Finance 公开 Chart API（无需 API Key）
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class MacroDataLayer:
    """宏观因子层 — 为 AI 与 TradeAdvisor 提供跨市场上下文。"""

    YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    STOOQ_DAILY = "https://stooq.com/q/d/l/"

    DEFAULT_SYMBOLS = {
        "tech": "QQQ",
        "treasury_10y": "^TNX",
        "treasury_2y": "2YY=F",
        "usd_index": "DX-Y.NYB",
        "btc": "BTC-USD",
        "vix": "^VIX",
    }

    # Stooq CSV 符号回退（Yahoo 403 时使用）
    STOOQ_SYMBOLS = {
        "tech": "qqq.us",
        "treasury_10y": "10usy.b",
        "treasury_2y": "2usy.b",
        "usd_index": "dx.f",
        "vix": "vix.f",
    }

    def __init__(
        self,
        symbols: Optional[Dict[str, str]] = None,
        correlation_lookback_days: int = 30,
        timeout: int = 15,
        cache_seconds: int = 900,
    ):
        self.symbols = {**self.DEFAULT_SYMBOLS, **(symbols or {})}
        self.correlation_lookback_days = max(10, correlation_lookback_days)
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        from bnb_quant_tool.disk_ttl_cache import DiskTTLCache
        self._disk = DiskTTLCache("data/macro_cache", prefix="macro")

    # ============================================================
    # 主入口
    # ============================================================
    def fetch_all(self) -> Dict[str, Any]:
        cache_key = "macro:all"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        series_map: Dict[str, List[float]] = {}
        snapshots: Dict[str, Dict] = {}

        for name, yahoo_sym in self.symbols.items():
            closes = self._fetch_closes(name, yahoo_sym)
            if closes:
                series_map[name] = closes
                snapshots[name] = self._snapshot_from_closes(closes, label=name)

        correlations = self._compute_correlations(series_map)
        fed_signal = self._fed_policy_proxy(snapshots, series_map)
        macro_score = self._aggregate_macro_score(snapshots, correlations, fed_signal)
        crypto_vol = self._crypto_volatility_context(series_map.get("btc", []))

        result = {
            "snapshots": snapshots,
            "correlations": correlations,
            "fed_policy_proxy": fed_signal,
            "crypto_volatility": crypto_vol,
            "macro_score": macro_score,
            "interpretation": "",
            "data_sources": self._detect_sources(snapshots),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result["interpretation"] = self._interpret(result)
        self._set_cache(cache_key, result)
        return result

    # ============================================================
    # 价格序列（Yahoo → Stooq → Binance[BTC]）
    # ============================================================
    def _fetch_closes(self, name: str, yahoo_symbol: str) -> List[float]:
        closes = self._fetch_yahoo_closes(yahoo_symbol)
        if closes:
            return closes
        stooq_sym = self.STOOQ_SYMBOLS.get(name)
        if stooq_sym:
            closes = self._fetch_stooq_closes(stooq_sym)
            if closes:
                logger.info("宏观 %s 使用 Stooq 回退", name)
                return closes
        if name == "btc":
            closes = self._fetch_binance_btc_closes()
            if closes:
                logger.info("宏观 %s 使用 Binance 回退", name)
                return closes
        binance_sym = self.BINANCE_MACRO_FALLBACK.get(name)
        if binance_sym and name != "btc":
            closes = self._fetch_binance_closes(binance_sym)
            if closes:
                logger.info("宏观 %s 使用 Binance 代理 (%s)", name, binance_sym)
                return closes
        return []

    def _fetch_yahoo_closes(self, symbol: str, range_days: int = 90) -> List[float]:
        url = self.YAHOO_CHART.format(symbol=symbol)
        range_str = "3mo" if range_days >= 60 else "2mo"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://finance.yahoo.com/",
        }
        try:
            r = requests.get(
                url,
                params={"interval": "1d", "range": range_str},
                headers=headers,
                timeout=self.timeout,
            )
            r.raise_for_status()
            chart = r.json().get("chart", {}).get("result") or []
            if not chart:
                return []
            closes = chart[0].get("indicators", {}).get("quote", [{}])[0].get("close") or []
            return [float(c) for c in closes if c is not None]
        except Exception as e:
            logger.warning("Yahoo %s 失败: %s", symbol, e)
            return []

    def _fetch_stooq_closes(self, stooq_symbol: str, limit: int = 90) -> List[float]:
        """Stooq 日 K CSV — 免费、无需 API Key。"""
        try:
            r = requests.get(
                self.STOOQ_DAILY,
                params={"s": stooq_symbol, "i": "d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            lines = [ln.strip() for ln in r.text.strip().splitlines() if ln.strip()]
            if len(lines) < 2:
                return []
            closes: List[float] = []
            for row in lines[1:]:
                parts = row.split(",")
                if len(parts) >= 5:
                    try:
                        closes.append(float(parts[4]))
                    except ValueError:
                        continue
            return closes[-limit:]
        except Exception as e:
            logger.warning("Stooq %s 失败: %s", stooq_symbol, e)
            return []

    @staticmethod
    def _fetch_binance_btc_closes(limit: int = 90) -> List[float]:
        return MacroDataLayer._fetch_binance_closes("BTCUSDT", limit)

    @staticmethod
    def _fetch_binance_closes(symbol: str, limit: int = 90) -> List[float]:
        try:
            from bnb_quant_tool.data_fetcher import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            df = fetcher.get_klines(symbol=symbol, interval="1d", limit=min(limit, 1000))
            if df is None or df.empty:
                return []
            return [float(x) for x in df["close"].tolist()]
        except Exception as e:
            logger.warning("Binance %s 回退失败: %s", symbol, e)
            return []

    # Binance 宏观代理（Yahoo/Stooq 不可达时）
    BINANCE_MACRO_FALLBACK = {
        "tech": "ETHUSDT",       # 风险资产代理：ETH 弹性
        "treasury_10y": "BNBUSDT",  # 利率敏感型 alt 代理
        "usd_index": "BNBUSDT",
        "btc": "BTCUSDT",
    }

    # ============================================================
    # Yahoo Finance (legacy alias kept for tests)
    # ============================================================

    @staticmethod
    def _snapshot_from_closes(closes: List[float], label: str = "") -> Dict[str, Any]:
        if len(closes) < 2:
            return {"error": "数据不足"}
        latest = closes[-1]
        prev = closes[-2]
        ch_1d = (latest - prev) / prev * 100 if prev else 0.0
        ch_7d = None
        ch_30d = None
        if len(closes) >= 8:
            base = closes[-8]
            ch_7d = (latest - base) / base * 100 if base else None
        if len(closes) >= 31:
            base = closes[-31]
            ch_30d = (latest - base) / base * 100 if base else None
        return {
            "label": label,
            "latest": round(latest, 4),
            "change_1d_pct": round(ch_1d, 3),
            "change_7d_pct": round(ch_7d, 3) if ch_7d is not None else None,
            "change_30d_pct": round(ch_30d, 3) if ch_30d is not None else None,
        }

    # ============================================================
    # 相关性
    # ============================================================
    def _compute_correlations(self, series_map: Dict[str, List[float]]) -> Dict[str, Any]:
        btc = series_map.get("btc") or []
        if len(btc) < self.correlation_lookback_days + 1:
            return {"error": "BTC 数据不足", "lookback_days": self.correlation_lookback_days}

        n = self.correlation_lookback_days
        btc_rets = self._daily_returns(btc[-(n + 1):])
        out: Dict[str, Any] = {"lookback_days": n, "pairs": {}}

        pair_labels = {
            "tech": "BTC vs 风险资产代理(ETH)",
            "treasury_10y": "BTC vs Alt代理(BNB)",
            "usd_index": "BTC vs Alt代理(BNB)",
            "vix": "BTC vs VIX",
        }
        for key, label in pair_labels.items():
            other = series_map.get(key) or []
            if len(other) < n + 1:
                continue
            other_rets = self._daily_returns(other[-(n + 1):])
            corr = self._pearson(btc_rets, other_rets)
            if corr is not None:
                out["pairs"][key] = {
                    "label": label,
                    "correlation": round(corr, 3),
                    "strength": self._corr_strength(corr),
                }

        # 综合：2026 典型高相关环境
        tech_corr = (out["pairs"].get("tech") or {}).get("correlation")
        if tech_corr is not None:
            if abs(tech_corr) >= 0.6:
                out["regime"] = "高联动（随美股波动）"
            elif abs(tech_corr) >= 0.35:
                out["regime"] = "中等联动"
            else:
                out["regime"] = "低联动/脱钩"
        return out

    @staticmethod
    def _daily_returns(prices: List[float]) -> List[float]:
        rets = []
        for i in range(1, len(prices)):
            if prices[i - 1] != 0:
                rets.append((prices[i] - prices[i - 1]) / prices[i - 1])
        return rets

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> Optional[float]:
        n = min(len(x), len(y))
        if n < 5:
            return None
        x, y = x[:n], y[:n]
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((a - mx) * (b - my) for a, b in zip(x, y))
        den_x = math.sqrt(sum((a - mx) ** 2 for a in x))
        den_y = math.sqrt(sum((b - my) ** 2 for b in y))
        if den_x == 0 or den_y == 0:
            return None
        return num / (den_x * den_y)

    @staticmethod
    def _corr_strength(corr: float) -> str:
        a = abs(corr)
        if a >= 0.7:
            return "强"
        if a >= 0.4:
            return "中"
        return "弱"

    # ============================================================
    # 美联储政策预期代理（非线性）
    # ============================================================
    def _fed_policy_proxy(
        self,
        snapshots: Dict[str, Dict],
        series_map: Dict[str, List[float]],
    ) -> Dict[str, Any]:
        """用收益率动量 + 曲线形态近似 Fed 预期对 crypto 的非线性影响。"""
        t10 = snapshots.get("treasury_10y") or {}
        t2 = snapshots.get("treasury_2y") or {}
        dxy = snapshots.get("usd_index") or {}
        vix = snapshots.get("vix") or {}

        y10 = t10.get("latest")
        y2 = t2.get("latest")
        spread = (y10 - y2) if y10 is not None and y2 is not None else None

        ch10_30d = t10.get("change_30d_pct")
        dxy_30d = dxy.get("change_30d_pct")

        # 非线性评分：hawkish 压制 crypto，dovish pivot 支撑
        hawkish_score = 0.0
        notes: List[str] = []

        if ch10_30d is not None:
            if ch10_30d > 5:
                hawkish_score += 0.5
                notes.append("10Y收益率30d急升→紧缩预期压制风险资产")
            elif ch10_30d > 2:
                hawkish_score += 0.25
            elif ch10_30d < -3:
                hawkish_score -= 0.35
                notes.append("10Y收益率回落→降息/宽松预期利好crypto")

        if spread is not None:
            if spread < 0:
                hawkish_score += 0.2
                notes.append("收益率曲线倒挂→衰退担忧")
            elif spread > 1.5:
                hawkish_score -= 0.15
                notes.append("曲线陡峭化→增长预期改善")

        if dxy_30d is not None:
            if dxy_30d > 2:
                hawkish_score += 0.25
                notes.append("美元走强→全球流动性收紧")
            elif dxy_30d < -2:
                hawkish_score -= 0.2
                notes.append("美元走弱→流动性宽松")

        vix_level = vix.get("latest")
        if vix_level is not None:
            if vix_level > 25:
                hawkish_score += 0.15
                notes.append(f"VIX={vix_level:.1f} 高波动风险环境")
            elif vix_level < 15:
                hawkish_score -= 0.1

        # 映射到 crypto 友好度 [-1, 1]，负 hawkish = 利好 crypto
        crypto_friendly = round(max(-1.0, min(1.0, -hawkish_score)), 3)

        if crypto_friendly >= 0.3:
            stance = "偏鸽/利好风险资产"
        elif crypto_friendly <= -0.3:
            stance = "偏鹰/压制crypto波动率"
        else:
            stance = "宏观中性"

        return {
            "yield_10y": y10,
            "yield_2y": y2,
            "yield_spread_10y_2y": round(spread, 3) if spread is not None else None,
            "hawkish_score": round(hawkish_score, 3),
            "crypto_friendly_score": crypto_friendly,
            "stance": stance,
            "notes": notes,
        }

    # ============================================================
    # Crypto 波动率上下文
    # ============================================================
    @staticmethod
    def _crypto_volatility_context(btc_closes: List[float]) -> Dict[str, Any]:
        if len(btc_closes) < 31:
            return {"error": "BTC 数据不足"}
        rets = MacroDataLayer._daily_returns(btc_closes[-31:])
        if not rets:
            return {"error": "无法计算波动率"}
        vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(365) * 100
        recent = math.sqrt(sum(r * r for r in rets[-7:]) / min(7, len(rets))) * math.sqrt(365) * 100

        if vol >= 80:
            level = "极高"
        elif vol >= 55:
            level = "偏高"
        elif vol >= 35:
            level = "中等"
        else:
            level = "偏低"

        return {
            "annualized_vol_pct": round(vol, 2),
            "recent_7d_vol_pct": round(recent, 2),
            "level": level,
        }

    # ============================================================
    # 综合宏观分 [-1, +1]
    # ============================================================
    def _aggregate_macro_score(
        self,
        snapshots: Dict[str, Dict],
        correlations: Dict,
        fed: Dict,
    ) -> float:
        score = 0.0
        n = 0

        tech = snapshots.get("tech") or {}
        ch30 = tech.get("change_30d_pct")
        if ch30 is not None:
            if ch30 > 5:
                score += 0.35
            elif ch30 > 2:
                score += 0.15
            elif ch30 < -5:
                score -= 0.35
            elif ch30 < -2:
                score -= 0.15
            n += 1

        fed_score = fed.get("crypto_friendly_score")
        if fed_score is not None:
            score += fed_score * 0.5
            n += 1

        btc = snapshots.get("btc") or {}
        btc30 = btc.get("change_30d_pct")
        if btc30 is not None:
            if btc30 > 10:
                score += 0.1
            elif btc30 < -10:
                score -= 0.1
            n += 1

        if n == 0:
            return 0.0
        return round(max(-1.0, min(1.0, score)), 3)

    @staticmethod
    def _detect_sources(snapshots: Dict[str, Dict]) -> str:
        if not snapshots:
            return "none"
        if len(snapshots) >= 4:
            return "mixed(yahoo/stooq/binance)"
        if "btc" in snapshots and len(snapshots) <= 2:
            return "binance_proxy"
        return "partial"

    def _interpret(self, data: Dict) -> str:
        score = data.get("macro_score", 0.0)
        fed = data.get("fed_policy_proxy") or {}
        corr = data.get("correlations") or {}
        vol = data.get("crypto_volatility") or {}

        parts = []
        tech = (data.get("snapshots") or {}).get("tech") or {}
        if tech.get("change_30d_pct") is not None:
            parts.append(f"QQQ 30d {tech['change_30d_pct']:+.1f}%")
        if fed.get("stance"):
            parts.append(f"Fed代理: {fed['stance']}")
        regime = corr.get("regime")
        if regime:
            parts.append(regime)
        if vol.get("level"):
            parts.append(f"BTC波动率{vol['level']}({vol.get('annualized_vol_pct', '?')}%)")

        if score >= 0.35:
            tag = "宏观偏多（risk-on）"
        elif score <= -0.35:
            tag = "宏观偏空（risk-off）"
        elif score >= 0.12:
            tag = "宏观略偏多"
        elif score <= -0.12:
            tag = "宏观略偏空"
        else:
            tag = "宏观中性"
        return f"[{tag}]  " + "  ".join(parts)

    # ============================================================
    # AI 提示词格式化
    # ============================================================
    @staticmethod
    def format_for_prompt(data: Dict) -> str:
        if not data:
            return ""
        lines = ["", "【宏观因子层 — 跨市场联动与Fed政策预期】", "=" * 50]
        lines.append(f"宏观综合分: {data.get('macro_score', 0):+.3f} (-1~+1)")
        lines.append(f"解读: {data.get('interpretation', '')}")

        for name, snap in (data.get("snapshots") or {}).items():
            if snap.get("error"):
                continue
            ch7 = snap.get("change_7d_pct")
            ch30 = snap.get("change_30d_pct")
            lines.append(
                f"  {name}: {snap.get('latest')} | 7d={ch7}% | 30d={ch30}%"
            )

        fed = data.get("fed_policy_proxy") or {}
        if fed:
            lines.append(f"Fed代理: {fed.get('stance')} (crypto友好度 {fed.get('crypto_friendly_score', 0):+.2f})")
            for note in fed.get("notes") or []:
                lines.append(f"  · {note}")

        corr = data.get("correlations") or {}
        for key, pair in (corr.get("pairs") or {}).items():
            lines.append(f"  相关性 {pair.get('label')}: {pair.get('correlation')} ({pair.get('strength')})")

        vol = data.get("crypto_volatility") or {}
        if vol.get("annualized_vol_pct"):
            lines.append(
                f"BTC年化波动率: {vol['annualized_vol_pct']}% ({vol.get('level')})"
            )
        lines.append("=" * 50)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def format_report(data: Dict) -> str:
        sep = "=" * 60
        lines = [sep, "  宏观数据层", sep]
        lines.append(f"宏观综合分: {data.get('macro_score', 0):+.3f}")
        lines.append(f"解读: {data.get('interpretation', '')}")
        lines.append("")
        lines.append("--- 资产快照 ---")
        for name, snap in (data.get("snapshots") or {}).items():
            if snap.get("error"):
                lines.append(f"  {name}: 无数据")
                continue
            lines.append(
                f"  {name}: {snap.get('latest')}  "
                f"1d={snap.get('change_1d_pct')}%  "
                f"7d={snap.get('change_7d_pct')}%  "
                f"30d={snap.get('change_30d_pct')}%"
            )
        lines.append("")
        lines.append("--- BTC 相关性 ---")
        corr = data.get("correlations") or {}
        for pair in (corr.get("pairs") or {}).values():
            lines.append(f"  {pair.get('label')}: r={pair.get('correlation')} ({pair.get('strength')})")
        if corr.get("regime"):
            lines.append(f"  联动状态: {corr['regime']}")
        lines.append("")
        fed = data.get("fed_policy_proxy") or {}
        lines.append("--- 美联储政策预期代理 ---")
        lines.append(f"  立场: {fed.get('stance')}")
        lines.append(f"  10Y-2Y利差: {fed.get('yield_spread_10y_2y')}")
        lines.append(f"  Crypto友好度: {fed.get('crypto_friendly_score', 0):+.3f}")
        for note in fed.get("notes") or []:
            lines.append(f"  · {note}")
        vol = data.get("crypto_volatility") or {}
        if vol.get("annualized_vol_pct"):
            lines.append("")
            lines.append(f"BTC波动率: {vol['annualized_vol_pct']}% ({vol.get('level')})")
        lines.append(sep)
        return "\n".join(lines)

    # ============================================================
    # 缓存
    # ============================================================
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


if __name__ == "__main__":
    layer = MacroDataLayer()
    result = layer.fetch_all()
    print(MacroDataLayer.format_report(result))
