"""
BNB Chain 健康因子（TVL + 稳定币沉淀 + 安全哨兵）
================================================
链原生币视角：

1. DefiLlama BSC TVL 水平与 7d 变化
2. BSC 稳定币流通量变化（桥/资金进出免费代理；桥 API 需付费）
3. 安全哨兵：新闻/公告中的黑客、桥被盗、停链等 → 硬拦做多

输出 chain_health_score ∈ [-1, 1] 与 block_long。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class BNBChainHealthFactor:
    """BNB Chain TVL / 资金流 / 安全哨兵。"""

    DEFILLAMA_CHAINS = "https://api.llama.fi/v2/chains"
    DEFILLAMA_HIST_TVL = "https://api.llama.fi/v2/historicalChainTvl/BSC"
    STABLE_CHARTS = "https://stablecoins.llama.fi/stablecoincharts/bsc"
    STABLE_CHAINS = "https://stablecoins.llama.fi/stablecoinchains"

    CHAIN_ALIASES = ("bsc", "bnb", "bnb chain", "binance")

    SECURITY_PATTERNS: Tuple[Tuple[str, float, Tuple[str, ...]], ...] = (
        ("bridge_hack", 1.0, (
            r"\b(bridge).{0,40}\b(hack|hacked|exploit|drained|stolen)\b",
            r"\b(hack|exploit|drained).{0,40}\b(bridge)\b",
            r"(跨链桥|桥).{0,20}(被盗|黑客|漏洞|攻击)",
        )),
        ("chain_halt", 1.0, (
            r"\b(bnb\s*chain|bsc|binance\s*smart\s*chain).{0,40}\b(halt|paused|stopped|outage)\b",
            r"(BNB\s*Chain|BSC).{0,20}(停链|暂停出块|网络中断|宕机)",
        )),
        ("protocol_hack", 0.85, (
            r"\b(hack|hacked|exploit|breach|stolen).{0,40}\b(bsc|bnb\s*chain|pancake|venus)\b",
            r"\b(bsc|bnb\s*chain).{0,40}\b(hack|hacked|exploit)\b",
            r"(BSC|BNB\s*Chain).{0,20}(黑客|被盗|漏洞利用)",
        )),
        ("validator_issue", 0.7, (
            r"\b(validator).{0,30}\b(slash|halt|attack|compromise)\b",
            r"(验证者).{0,20}(异常|被攻击|停摆)",
        )),
    )

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.cache_seconds = int(cfg.get("cache_seconds", 900))
        self.timeout = int(cfg.get("timeout", 15))
        self.tvl_lookback_days = int(cfg.get("tvl_lookback_days", 7))
        self.stable_lookback_days = int(cfg.get("stable_lookback_days", 7))
        self.tvl_bull_pct = float(cfg.get("tvl_bull_pct", 5.0))
        self.tvl_bear_pct = float(cfg.get("tvl_bear_pct", -5.0))
        self.stable_bull_pct = float(cfg.get("stable_bull_pct", 3.0))
        self.stable_bear_pct = float(cfg.get("stable_bear_pct", -3.0))
        self.security_block_threshold = float(cfg.get("security_block_threshold", 0.8))
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BNBQuantTool/2.11)",
            "Accept": "application/json",
        }

    def fetch(
        self,
        news_items: Optional[List[Dict]] = None,
        nlp_result: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self._empty(enabled=False)

        cache_key = "chain_health"
        # 安全事件不走长缓存：有新闻时重新扫描
        if not news_items and not nlp_result:
            cached = self._get_cache(cache_key)
            if cached is not None:
                return cached

        tvl = self._fetch_tvl()
        stables = self._fetch_stablecoins()
        security = self._scan_security(news_items or [], nlp_result or {})

        tvl_score = float(tvl.get("tvl_score") or 0.0)
        stable_score = float(stables.get("stable_score") or 0.0)
        security_penalty = float(security.get("penalty") or 0.0)

        score = tvl_score * 0.50 + stable_score * 0.35 - security_penalty
        score = max(-1.0, min(1.0, score))

        block_long = bool(security.get("critical")) or (
            float(security.get("severity") or 0) >= self.security_block_threshold
        )
        if block_long:
            score = min(score, -0.55)

        parts: List[str] = []
        if tvl.get("tvl_usd") is not None:
            chg = tvl.get("tvl_change_pct")
            chg_s = f"{chg:+.1f}%" if chg is not None else "n/a"
            parts.append(f"BSC TVL ${float(tvl['tvl_usd'])/1e9:.2f}B ({chg_s}/{self.tvl_lookback_days}d)")
        if stables.get("stable_usd") is not None:
            schg = stables.get("stable_change_pct")
            schg_s = f"{schg:+.1f}%" if schg is not None else "n/a"
            parts.append(f"稳定币 ${float(stables['stable_usd'])/1e9:.2f}B ({schg_s})")
        if security.get("hits"):
            parts.append(security.get("interpretation") or "链上安全风险")
        elif score >= 0.25:
            parts.append("链生态资金偏强")
        elif score <= -0.25:
            parts.append("链生态资金偏弱")
        else:
            parts.append("链健康中性")

        result = {
            "enabled": True,
            "chain_health_score": round(score, 3),
            "tvl": tvl,
            "stablecoins": stables,
            "security": security,
            "block_long": block_long,
            "healthy": score >= 0.25 and not block_long,
            "stressed": score <= -0.25 or block_long,
            "interpretation": " | ".join(parts),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        # 无新闻时缓存链上硬数据；有安全命中则短缓存
        if not security.get("hits"):
            self._set_cache(cache_key, result)
        return result

    def _fetch_tvl(self) -> Dict[str, Any]:
        tvl_usd: Optional[float] = None
        source = "none"
        try:
            resp = requests.get(self.DEFILLAMA_CHAINS, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            for chain in resp.json() or []:
                name = (chain.get("name") or "").lower()
                if name in self.CHAIN_ALIASES:
                    tvl_usd = float(chain.get("tvl") or 0) or None
                    if tvl_usd:
                        source = "defillama_chains"
                        break
        except Exception as exc:
            logger.debug("defillama chains: %s", exc)

        change_pct: Optional[float] = None
        try:
            resp = requests.get(self.DEFILLAMA_HIST_TVL, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            series = resp.json() or []
            if isinstance(series, list) and len(series) >= self.tvl_lookback_days + 1:
                latest = float(series[-1].get("tvl") or 0)
                past = float(series[-1 - self.tvl_lookback_days].get("tvl") or 0)
                if tvl_usd is None and latest > 0:
                    tvl_usd = latest
                    source = "defillama_hist"
                if past > 0:
                    change_pct = (latest - past) / past * 100.0
        except Exception as exc:
            logger.debug("defillama hist tvl: %s", exc)

        score = 0.0
        if change_pct is not None:
            if change_pct >= self.tvl_bull_pct:
                score = min(1.0, 0.35 + (change_pct - self.tvl_bull_pct) / 20.0)
            elif change_pct <= self.tvl_bear_pct:
                score = max(-1.0, -0.35 + (change_pct - self.tvl_bear_pct) / 20.0)
            else:
                score = max(-0.25, min(0.25, change_pct / 20.0))

        return {
            "tvl_usd": tvl_usd,
            "tvl_change_pct": round(change_pct, 3) if change_pct is not None else None,
            "tvl_score": round(score, 3),
            "lookback_days": self.tvl_lookback_days,
            "source": source,
        }

    def _fetch_stablecoins(self) -> Dict[str, Any]:
        stable_usd: Optional[float] = None
        change_pct: Optional[float] = None
        source = "none"

        try:
            resp = requests.get(self.STABLE_CHAINS, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            for row in resp.json() or []:
                name = (row.get("name") or "").lower()
                if name in self.CHAIN_ALIASES or name in ("binance", "bnb chain"):
                    circ = (row.get("totalCirculatingUSD") or {})
                    stable_usd = float(circ.get("peggedUSD") or 0) or None
                    if stable_usd:
                        source = "stablecoinchains"
                    break
        except Exception as exc:
            logger.debug("stablecoinchains: %s", exc)

        try:
            resp = requests.get(self.STABLE_CHARTS, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            series = resp.json() or []
            if isinstance(series, list) and len(series) >= self.stable_lookback_days + 1:
                def _usd(pt: Dict) -> float:
                    return float(
                        ((pt.get("totalCirculatingUSD") or {}).get("peggedUSD"))
                        or ((pt.get("totalCirculating") or {}).get("peggedUSD"))
                        or 0
                    )

                latest = _usd(series[-1])
                past = _usd(series[-1 - self.stable_lookback_days])
                if stable_usd is None and latest > 0:
                    stable_usd = latest
                    source = "stablecoincharts"
                if past > 0:
                    change_pct = (latest - past) / past * 100.0
                    if source == "none":
                        source = "stablecoincharts"
        except Exception as exc:
            logger.debug("stablecoincharts: %s", exc)

        score = 0.0
        if change_pct is not None:
            if change_pct >= self.stable_bull_pct:
                score = min(1.0, 0.30 + (change_pct - self.stable_bull_pct) / 15.0)
            elif change_pct <= self.stable_bear_pct:
                score = max(-1.0, -0.30 + (change_pct - self.stable_bear_pct) / 15.0)
            else:
                score = max(-0.2, min(0.2, change_pct / 15.0))

        return {
            "stable_usd": stable_usd,
            "stable_change_pct": round(change_pct, 3) if change_pct is not None else None,
            "stable_score": round(score, 3),
            "lookback_days": self.stable_lookback_days,
            "source": source,
            "note": "稳定币流通变化作为桥/资金进出代理（DefiLlama 桥流量 API 需付费）",
        }

    def _scan_security(
        self,
        news_items: List[Dict],
        nlp_result: Dict,
    ) -> Dict[str, Any]:
        hits: List[Dict[str, Any]] = []
        texts: List[str] = []
        for item in news_items[:40]:
            title = str(item.get("title") or item.get("headline") or "")
            summary = str(item.get("summary") or item.get("content") or item.get("body") or "")
            blob = f"{title} {summary}".strip()
            if blob:
                texts.append(blob)

        for blob in texts:
            low = blob.lower()
            for cat, severity, patterns in self.SECURITY_PATTERNS:
                for pat in patterns:
                    if re.search(pat, blob, flags=re.IGNORECASE) or re.search(pat, low):
                        hits.append({
                            "category": cat,
                            "severity": severity,
                            "snippet": blob[:120],
                        })
                        break

        # NLP 已识别的黑客类
        dom = (nlp_result.get("dominant_category") or "").lower()
        cats = nlp_result.get("categories") or nlp_result.get("matched_categories") or []
        if isinstance(cats, dict):
            cat_keys = [str(k).lower() for k in cats.keys()]
        else:
            cat_keys = [str(c).lower() for c in (cats or [])]
        if dom == "hack_exploit" or "hack_exploit" in cat_keys:
            hits.append({
                "category": "hack_exploit_nlp",
                "severity": 0.9,
                "snippet": nlp_result.get("interpretation") or "NLP hack_exploit",
            })

        max_sev = max((float(h["severity"]) for h in hits), default=0.0)
        critical = max_sev >= self.security_block_threshold
        penalty = min(1.0, max_sev * 1.1) if hits else 0.0
        if hits:
            top = hits[0]
            interp = f"安全哨兵命中 {top['category']} (sev {max_sev:.2f})"
        else:
            interp = "未发现链上安全警报"

        return {
            "hits": hits[:8],
            "hit_count": len(hits),
            "severity": round(max_sev, 3),
            "penalty": round(penalty, 3),
            "critical": critical,
            "interpretation": interp,
        }

    @staticmethod
    def format_for_prompt(data: Optional[Dict]) -> str:
        if not data or data.get("enabled") is False:
            return ""
        score = data.get("chain_health_score")
        if score is None:
            return ""
        flag = " [禁多]" if data.get("block_long") else ""
        return f"- 链健康: {data.get('interpretation', '')} (分 {score:+.2f}){flag}"

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
            "chain_health_score": 0.0,
            "block_long": False,
            "healthy": False,
            "stressed": False,
            "tvl": {},
            "stablecoins": {},
            "security": {},
            "interpretation": "链健康因子已禁用" if not enabled else "链健康数据不足",
        }
