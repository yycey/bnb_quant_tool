"""
多源新闻可信度评级 (Fake News Filter)
======================================
Twitter 单渠道传闻需经 BlockBeats / CoinDesk / 币安官方等 ≥2 权威源交叉验证，
才触发 NEWS_DRIVEN / PANIC；否则归类为噪音。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# 权威源（不区分大小写子串匹配）
TIER1_SOURCES: Tuple[str, ...] = (
    "binanceannouncement", "binance announcement", "币安公告", "support.binance",
    "blockbeats", "律动", "coindesk", "cointelegraph", "reuters", "bloomberg",
    "secgov", "sec.gov", "cftc", "federalreserve",
)

TIER2_SOURCES: Tuple[str, ...] = (
    "wublockchain", "吴说", "lookonchain", "certik", "odaily", "cryptoslate",
)

NOISE_SOURCES: Tuple[str, ...] = (
    "twitter", "tikhub", "x.com", "unknown",
)

PANIC_KEYWORDS: Tuple[str, ...] = (
    r"\b(sec|doj|cftc)\b.*\b(sue|lawsuit|ban|indict|charge)\b",
    r"binance.*(ban|shutdown|halt|suspend|investigation)",
    r"币安.*(被禁|关停|调查|诉讼)",
    r"\b(hack|exploit|breach)\b.*\bbnb\b",
    r"暂停.*(提币|充值)",
)

NEWS_DRIVEN_KEYWORDS: Tuple[str, ...] = (
    r"launch\s*pool|launchpool|megadrop",
    r"bnb.*burn|销毁",
    r"listing|上币|上线",
    r"partnership|合作",
)


class NewsCredibilityFilter:
    """新闻交叉验证与可信度评级。"""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.min_tier1_confirmations = int(cfg.get("min_tier1_confirmations", 2))
        self.allow_tier1_plus_tier2 = bool(cfg.get("allow_tier1_plus_tier2", True))
        self.twitter_always_noise = bool(cfg.get("twitter_always_noise", True))
        self.similarity_window_hours = float(cfg.get("similarity_window_hours", 6))

    def analyze(self, items: List[Dict]) -> Dict[str, Any]:
        if not self.enabled or not items:
            return self._empty()

        enriched = [self._enrich_item(it) for it in items]
        clusters = self._cluster_by_topic(enriched)

        verified_panic: List[Dict] = []
        verified_news: List[Dict] = []
        noise_items: List[Dict] = []

        for cluster in clusters:
            tier1 = [it for it in cluster if it["tier"] == 1]
            tier2 = [it for it in cluster if it["tier"] == 2]
            noise = [it for it in cluster if it["tier"] >= 3]

            confirmed = len(tier1) >= self.min_tier1_confirmations
            if not confirmed and self.allow_tier1_plus_tier2:
                confirmed = len(tier1) >= 1 and len(tier2) >= 1 and len(tier1) + len(tier2) >= 2

            topic = cluster[0].get("topic", "general")
            if topic == "panic":
                if confirmed:
                    verified_panic.extend(cluster[:3])
                else:
                    noise_items.extend(cluster)
            elif topic == "news_driven":
                if confirmed or len(tier1) >= 1:
                    verified_news.extend(cluster[:3])
                else:
                    noise_items.extend(cluster)
            else:
                if not confirmed and self.twitter_always_noise:
                    noise_items.extend([it for it in cluster if it["tier"] >= 3])
                else:
                    verified_news.extend(cluster[:2])

        regime_impact = "NORMAL"
        block_extreme_filter = False
        if verified_panic:
            regime_impact = "PANIC"
        elif verified_news:
            regime_impact = "NEWS_DRIVEN"
        elif noise_items:
            regime_impact = "NOISE"
            block_extreme_filter = True  # 单渠道传闻不触发极端拦截

        return {
            "regime_impact": regime_impact,
            "block_extreme_news_filter": block_extreme_filter,
            "verified_panic_count": len(verified_panic),
            "verified_news_count": len(verified_news),
            "noise_count": len(noise_items),
            "verified_panic_headlines": [it.get("title", "")[:80] for it in verified_panic[:3]],
            "noise_headlines": [it.get("title", "")[:80] for it in noise_items[:3]],
            "cred_score": self._cred_score(
                regime_impact, len(verified_panic), len(verified_news), len(noise_items)
            ),
            "interpretation": self._interpret(regime_impact, verified_panic, verified_news, noise_items),
        }

    @staticmethod
    def _cred_score(
        regime_impact: str,
        panic_n: int,
        news_n: int,
        noise_n: int,
    ) -> float:
        """0~1 可信度：权威交叉验证高，纯噪音低。"""
        if regime_impact == "PANIC" and panic_n:
            return 0.95
        if regime_impact == "NEWS_DRIVEN" and news_n:
            return 0.82
        if regime_impact == "NOISE" or (noise_n and not news_n and not panic_n):
            return 0.35
        if news_n:
            return 0.70
        return 0.55

    def _enrich_item(self, item: Dict) -> Dict:
        source = (item.get("source") or "").lower()
        title = item.get("title") or ""
        summary = item.get("summary") or ""
        text = f"{title} {summary}".lower()

        tier = 3
        for s in TIER1_SOURCES:
            if s in source or s in text[:200]:
                tier = 1
                break
        if tier > 1:
            for s in TIER2_SOURCES:
                if s in source:
                    tier = 2
                    break
        for s in NOISE_SOURCES:
            if s in source:
                tier = max(tier, 3)

        topic = "general"
        for pat in PANIC_KEYWORDS:
            if re.search(pat, text, re.I):
                topic = "panic"
                break
        if topic == "general":
            for pat in NEWS_DRIVEN_KEYWORDS:
                if re.search(pat, text, re.I):
                    topic = "news_driven"
                    break

        return {**item, "tier": tier, "topic": topic, "text_key": self._text_key(title)}

    @staticmethod
    def _text_key(title: str) -> str:
        words = re.findall(r"[a-zA-Z\u4e00-\u9fff]{2,}", (title or "").lower())
        return " ".join(sorted(set(words))[:8])

    def _cluster_by_topic(self, items: List[Dict]) -> List[List[Dict]]:
        buckets: Dict[str, List[Dict]] = {}
        for it in items:
            key = f"{it.get('topic')}:{it.get('text_key', '')[:40]}"
            buckets.setdefault(key, []).append(it)
        return list(buckets.values())

    @staticmethod
    def _interpret(
        regime: str,
        panic: List[Dict],
        news: List[Dict],
        noise: List[Dict],
    ) -> str:
        if regime == "PANIC":
            return f"多源验证 PANIC 事件 ({len(panic)} 条权威确认)"
        if regime == "NEWS_DRIVEN":
            return f"权威源确认 NEWS_DRIVEN ({len(news)} 条)"
        if regime == "NOISE":
            return f"单渠道/未验证传闻 {len(noise)} 条 → 归类噪音，不触发极端拦截"
        return "新闻可信度：无显著事件"

    @staticmethod
    def _empty() -> Dict:
        return {
            "regime_impact": "NORMAL",
            "block_extreme_news_filter": False,
            "verified_panic_count": 0,
            "verified_news_count": 0,
            "noise_count": 0,
            "cred_score": 0.55,
            "interpretation": "无可分析新闻",
        }

    @classmethod
    def format_for_prompt(cls, result: Dict) -> str:
        if not result or result.get("regime_impact") == "NORMAL":
            return ""
        lines = ["\n【新闻可信度交叉验证】", f"- {result.get('interpretation', '')}"]
        if result.get("block_extreme_news_filter"):
            lines.append("- 单渠道传闻已降级为噪音，不触发 PANIC 门控")
        lines.append("")
        return "\n".join(lines)
