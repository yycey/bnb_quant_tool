"""
BNB 量化工具 - 币安公告 / 监管新闻专属 NLP
================================================
针对 BNB 平台币特性，比通用 BlockBeats 情绪更精准地识别：

- 币安官方公告（Launchpool、销毁、上币、产品更新）
- 针对币安的监管新闻（SEC 诉讼、各国禁令、罚款）
- 平台风险事件（宕机、黑客、下架）

实现方式：轻量级规则 + 关键词加权分类器（无需额外 pip 依赖）。
后续可替换为微调模型，接口保持不变。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PatternRule:
    category: str
    weight: float
    patterns: Tuple[str, ...]


# 高权重监管/平台风险 — 对 BNB 影响最大
_REGULATORY_NEGATIVE: Tuple[_PatternRule, ...] = (
    _PatternRule("sec_lawsuit", 1.0, (
        r"\bsec\b.*\b(sue|lawsuit|charge|settlement|fine|investigation)\b",
        r"\b(sue|lawsuit|charge|fine)\b.*\bbinance\b",
        r"sec.*币安", r"币安.*诉讼", r"美国.*起诉.*币安",
    )),
    _PatternRule("regulatory_ban", 0.95, (
        r"\b(ban|banned|prohibit|crackdown|sanction)\b.*\b(binance|crypto exchange)\b",
        r"禁止.*币安", r"币安.*被禁", r"监管.*打击.*币安",
    )),
    _PatternRule("withdrawal_halt", 0.9, (
        r"\b(halt|suspend|pause|freeze)\b.*\b(withdraw|deposit|trading)\b",
        r"暂停.*(提币|充值|交易)", r"停止.*(提币|充值)",
    )),
    _PatternRule("hack_exploit", 0.85, (
        r"\b(hack|hacked|exploit|breach|stolen)\b.*\b(binance|bnb)\b",
        r"币安.*(黑客|攻击|被盗)", r"bnb.*(被盗|攻击)",
    )),
    _PatternRule("delist_bnb", 0.8, (
        r"\b(delist|delisting|remove)\b.*\bbnb\b",
        r"下架.*bnb", r"bnb.*下架",
    )),
)

_REGULATORY_POSITIVE: Tuple[_PatternRule, ...] = (
    _PatternRule("settlement_resolved", 0.7, (
        r"\b(settlement|resolved|dismissed|cleared|approved)\b.*\b(binance|sec)\b",
        r"币安.*(和解|获批|解除)", r"诉讼.*(和解|撤销|结束)",
    )),
    _PatternRule("license_approval", 0.65, (
        r"\b(license|licensed|approval|authorized|compliance)\b.*\bbinance\b",
        r"币安.*(获牌|合规|批准|牌照)",
    )),
)

_PLATFORM_POSITIVE: Tuple[_PatternRule, ...] = (
    _PatternRule("launchpool", 0.85, (
        r"\blaunch\s*pool\b", r"\blaunchpool\b",
        r"新币挖矿", r"launchpool", r"质押.*bnb.*(挖矿|空投|奖励)",
        r"binance.*launchpool", r"introducing.*launchpool",
    )),
    _PatternRule("megadrop", 0.88, (
        r"\bmegadrop\b", r"mega\s*drop", r"超级空投", r"megadrop",
        r"binance.*megadrop", r"web3.*wallet.*airdrop",
    )),
    _PatternRule("bnb_burn", 0.7, (
        r"\bbnb\b.*\b(burn|burned|destroy|销毁)\b",
        r"bnb.*销毁", r"季度.*销毁.*bnb", r"auto.?burn",
    )),
    _PatternRule("product_growth", 0.55, (
        r"\b(binance)\b.*\b(launch|listing|partnership|integrat)\b",
        r"币安.*(上线|合作|推出|集成)",
    )),
)

_BINANCE_SOURCE_HINTS = (
    "binance", "binancesquare", "币安", "announcement", "support.binance",
)


class BinanceAnnouncementNLP:
    """币安专属公告 / 监管 NLP 分类器。"""

    IMPACT_CRITICAL = "critical"
    IMPACT_HIGH = "high"
    IMPACT_MEDIUM = "medium"
    IMPACT_LOW = "low"

    def __init__(
        self,
        regulatory_weight: float = 1.2,
        launchpool_weight: float = 0.9,
        min_confidence: float = 0.35,
    ):
        self.regulatory_weight = regulatory_weight
        self.launchpool_weight = launchpool_weight
        self.min_confidence = min_confidence

    def analyze_items(self, items: List[Dict]) -> Dict:
        """分析新闻/公告列表，输出 BNB 专属结构化结论。"""
        if not items:
            return self._empty_result()

        scored: List[Dict] = []
        for item in items:
            result = self._score_item(item)
            if result["confidence"] >= self.min_confidence * 0.5:
                scored.append(result)

        if not scored:
            return self._empty_result(total_scanned=len(items))

        # 按影响力 × 置信度排序
        scored.sort(
            key=lambda x: abs(x["score"]) * x["confidence"],
            reverse=True,
        )

        aggregate_score = self._aggregate_score(scored)
        polarity = self._score_to_polarity(aggregate_score)
        top_impact = scored[0]
        impact_level = top_impact.get("impact_level", self.IMPACT_LOW)

        high_impact = [
            s for s in scored[:5]
            if s.get("impact_level") in (self.IMPACT_CRITICAL, self.IMPACT_HIGH)
        ]

        return {
            "polarity": polarity,
            "score": round(aggregate_score, 3),
            "confidence": round(min(1.0, abs(aggregate_score) + 0.25), 3),
            "impact_level": impact_level,
            "dominant_category": top_impact.get("category", "neutral"),
            "trade_bias": self._trade_bias(aggregate_score, impact_level),
            "high_impact_items": high_impact[:3],
            "matched_count": len(scored),
            "total_scanned": len(items),
            "interpretation": self._interpret(scored, aggregate_score, polarity),
            "top_headline": (top_impact.get("title") or "")[:120],
        }

    def analyze_text(self, text: str, source: str = "") -> Dict:
        """单条文本分析（供测试或实时公告流使用）。"""
        return self._score_item({"title": text, "summary": "", "source": source})

    # ---- 内部逻辑 ----

    def _score_item(self, item: Dict) -> Dict:
        title = item.get("title") or ""
        summary = item.get("summary") or ""
        source = (item.get("source") or "").lower()
        text = f"{title} {summary}".strip()
        text_lower = text.lower()

        is_binance_source = any(h in source for h in _BINANCE_SOURCE_HINTS)
        source_boost = 1.25 if is_binance_source else 1.0

        bull_score = 0.0
        bear_score = 0.0
        categories: List[str] = []

        for rule in _REGULATORY_NEGATIVE:
            if self._match_any(text_lower, rule.patterns):
                bear_score += rule.weight * self.regulatory_weight * source_boost
                categories.append(rule.category)

        for rule in _REGULATORY_POSITIVE:
            if self._match_any(text_lower, rule.patterns):
                bull_score += rule.weight * self.regulatory_weight * source_boost
                categories.append(rule.category)

        for rule in _PLATFORM_POSITIVE:
            w = rule.weight
            if rule.category in ("launchpool", "megadrop"):
                w *= self.launchpool_weight
            if self._match_any(text_lower, rule.patterns):
                bull_score += w * source_boost
                categories.append(rule.category)

        net = bull_score - bear_score
        confidence = min(1.0, (bull_score + bear_score) * 0.35 + (0.15 if is_binance_source else 0))
        if not categories:
            confidence = 0.0

        impact_level = self._impact_level(net, categories, is_binance_source)

        return {
            "title": title,
            "source": item.get("source", ""),
            "category": categories[0] if categories else "neutral",
            "categories": categories,
            "score": round(max(-1.0, min(1.0, net / 2.0)), 3),
            "confidence": round(confidence, 3),
            "impact_level": impact_level,
            "is_binance_official": is_binance_source,
        }

    @staticmethod
    def _match_any(text: str, patterns: Tuple[str, ...]) -> bool:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return True
        return False

    def _aggregate_score(self, scored: List[Dict]) -> float:
        """加权聚合，高影响力事件权重更大。"""
        if not scored:
            return 0.0
        total_w = 0.0
        weighted = 0.0
        for i, s in enumerate(scored[:8]):
            decay = 1.0 / (1.0 + i * 0.15)
            impact_mult = {
                self.IMPACT_CRITICAL: 1.5,
                self.IMPACT_HIGH: 1.2,
                self.IMPACT_MEDIUM: 1.0,
                self.IMPACT_LOW: 0.7,
            }.get(s.get("impact_level"), 1.0)
            w = s["confidence"] * decay * impact_mult
            weighted += s["score"] * w
            total_w += w
        if total_w <= 0:
            return 0.0
        return max(-1.0, min(1.0, weighted / total_w))

    @staticmethod
    def _score_to_polarity(score: float) -> str:
        if score > 0.2:
            return "bullish"
        if score < -0.2:
            return "bearish"
        return "neutral"

    @staticmethod
    def _trade_bias(score: float, impact_level: str) -> str:
        if impact_level == BinanceAnnouncementNLP.IMPACT_CRITICAL:
            return "SHORT" if score < -0.15 else ("LONG" if score > 0.15 else "WAIT")
        if score > 0.25:
            return "LONG"
        if score < -0.25:
            return "SHORT"
        return "WAIT"

    def _impact_level(self, net: float, categories: List[str], is_official: bool) -> str:
        abs_net = abs(net)
        critical_cats = {"sec_lawsuit", "withdrawal_halt", "hack_exploit", "regulatory_ban"}
        if any(c in critical_cats for c in categories) and abs_net >= 0.5:
            return self.IMPACT_CRITICAL
        if abs_net >= 0.8 or (is_official and abs_net >= 0.6):
            return self.IMPACT_HIGH
        if abs_net >= 0.35:
            return self.IMPACT_MEDIUM
        return self.IMPACT_LOW

    @staticmethod
    def _interpret(scored: List[Dict], score: float, polarity: str) -> str:
        top = scored[0]
        cat = top.get("category", "neutral")
        cat_labels = {
            "sec_lawsuit": "SEC/监管诉讼",
            "regulatory_ban": "监管禁令",
            "withdrawal_halt": "提币/交易暂停",
            "hack_exploit": "安全事件",
            "launchpool": "Launchpool 挖矿",
            "megadrop": "Megadrop 超级空投",
            "bnb_burn": "BNB 销毁",
            "settlement_resolved": "监管和解",
            "license_approval": "合规获批",
        }
        label = cat_labels.get(cat, cat)
        pol_cn = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(polarity, polarity)
        official = "（币安官方源）" if top.get("is_binance_official") else ""
        return f"币安专属NLP: {pol_cn} score={score:+.2f}，主导事件={label}{official}"

    @staticmethod
    def _empty_result(total_scanned: int = 0) -> Dict:
        return {
            "polarity": "neutral",
            "score": 0.0,
            "confidence": 0.0,
            "impact_level": BinanceAnnouncementNLP.IMPACT_LOW,
            "dominant_category": "neutral",
            "trade_bias": "WAIT",
            "high_impact_items": [],
            "matched_count": 0,
            "total_scanned": total_scanned,
            "interpretation": "未发现高影响币安专属公告/监管事件",
            "top_headline": "",
        }

    @classmethod
    def format_for_prompt(cls, nlp_result: Dict) -> str:
        """格式化为 AI Prompt 片段。"""
        if not nlp_result or not nlp_result.get("matched_count"):
            return ""
        lines = [
            "\n【BNB 专属 — 币安公告/监管 NLP】",
            f"- 综合: {nlp_result.get('interpretation', '')}",
            f"- 交易偏向: {nlp_result.get('trade_bias', 'WAIT')} "
            f"(影响级别: {nlp_result.get('impact_level', 'low')})",
        ]
        for item in (nlp_result.get("high_impact_items") or [])[:2]:
            lines.append(
                f"  · [{item.get('category')}] {item.get('title', '')[:80]} "
                f"(score={item.get('score', 0):+.2f})"
            )
        lines.append(
            "注意: 监管/平台类新闻对 BNB 的影响通常大于通用加密新闻，"
            "SEC 诉讼或提币暂停等事件应显著降低做多 confidence。\n"
        )
        return "\n".join(lines)
