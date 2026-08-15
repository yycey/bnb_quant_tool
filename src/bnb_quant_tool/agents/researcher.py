"""研究员 Agent — 新闻、链上、宏观、市场情绪。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import (
    Action,
    AgentOpinion,
    AgentRole,
    BaseAgent,
    MarketContext,
    Stance,
    action_from_stance,
    clamp,
    stance_from_score,
)

logger = logging.getLogger(__name__)


class ResearcherAgent(BaseAgent):
    """宏观研究员：抓取并综合非价格类情报。"""

    role = AgentRole.RESEARCHER

    def __init__(
        self,
        news_collector=None,
        sentiment_engine=None,
        onchain_analyzer=None,
        macro_layer=None,
        config: Optional[Dict] = None,
    ):
        self.news_collector = news_collector
        self.sentiment_engine = sentiment_engine
        self.onchain_analyzer = onchain_analyzer
        self.macro_layer = macro_layer
        cfg = (config or {}).get("multi_agent", {}).get("researcher", {})
        self.news_weight = float(cfg.get("news_weight", 0.30))
        self.sentiment_weight = float(cfg.get("sentiment_weight", 0.25))
        self.onchain_weight = float(cfg.get("onchain_weight", 0.20))
        self.macro_weight = float(cfg.get("macro_weight", 0.15))
        self.bnb_factors_weight = float(cfg.get("bnb_factors_weight", 0.20))
        self.min_confidence = float(cfg.get("min_confidence", 0.45))

    def analyze(self, context: MarketContext) -> AgentOpinion:
        evidence: List[str] = []
        concerns: List[str] = []
        components: List[tuple] = []

        news_score, news_evidence, news_concerns = self._score_news(context)
        if news_evidence:
            components.append((news_score, self.news_weight, "news"))
            evidence.extend(news_evidence)
            concerns.extend(news_concerns)

        sent_score, sent_evidence, sent_concerns = self._score_sentiment(context)
        if sent_evidence:
            components.append((sent_score, self.sentiment_weight, "sentiment"))
            evidence.extend(sent_evidence)
            concerns.extend(sent_concerns)

        onchain_score, oc_evidence, oc_concerns = self._score_onchain(context)
        if oc_evidence:
            components.append((onchain_score, self.onchain_weight, "onchain"))
            evidence.extend(oc_evidence)
            concerns.extend(oc_concerns)

        macro_score, macro_evidence, macro_concerns = self._score_macro(context)
        if macro_evidence:
            components.append((macro_score, self.macro_weight, "macro"))
            evidence.extend(macro_evidence)
            concerns.extend(macro_concerns)

        bnb_score, bnb_evidence, bnb_concerns = self._score_bnb_factors(context)
        if bnb_evidence:
            components.append((bnb_score, self.bnb_factors_weight, "bnb_factors"))
            evidence.extend(bnb_evidence)
            concerns.extend(bnb_concerns)

        if not components:
            return AgentOpinion(
                role=self.role,
                stance=Stance.NEUTRAL,
                action=Action.WAIT,
                confidence=0.3,
                score=0.0,
                summary="情报不足，研究员建议观望",
                evidence=["未获取到有效新闻/链上/宏观数据"],
            )

        total_weight = sum(w for _, w, _ in components)
        score = clamp(sum(s * w for s, w, _ in components) / total_weight)
        stance = stance_from_score(score)
        action = action_from_stance(stance)

        avg_abs = sum(abs(s) for s, _, _ in components) / len(components)
        confidence = clamp(0.35 + avg_abs * 0.55 + len(components) * 0.05, 0.0, 1.0)

        summary = self._build_summary(stance, score, len(evidence))
        return AgentOpinion(
            role=self.role,
            stance=stance,
            action=action,
            confidence=confidence,
            score=score,
            summary=summary,
            evidence=evidence[:8],
            concerns=concerns[:5],
            metadata={
                "component_scores": {
                    name: round(s, 3) for s, _, name in components
                },
            },
        )

    def fetch_fresh_intel(self, symbol: str, hours: int = 24) -> Dict[str, Any]:
        """主动拉取最新情报（供全自动模式使用）。"""
        result: Dict[str, Any] = {}
        base = symbol.replace("USDT", "")

        if self.news_collector:
            try:
                result["news_items"] = self.news_collector.collect(
                    symbol=base, hours=hours, max_items=30
                )
            except Exception as e:
                logger.debug(f"研究员拉取新闻失败: {e}")

        if self.sentiment_engine:
            try:
                result["sentiment"] = self.sentiment_engine.fetch_all(symbol=symbol) or {}
            except Exception as e:
                logger.debug(f"研究员拉取情绪失败: {e}")

        if self.onchain_analyzer:
            try:
                result["onchain"] = self.onchain_analyzer.fetch_all(symbol=symbol) or {}
            except Exception as e:
                logger.debug(f"研究员拉取链上失败: {e}")

        if self.macro_layer:
            try:
                result["macro"] = self.macro_layer.fetch_all() or {}
            except Exception as e:
                logger.debug(f"研究员拉取宏观失败: {e}")

        return result

    # ---- 评分子模块 ----

    def _score_news(self, ctx: MarketContext) -> tuple:
        ns = ctx.news_summary or {}
        if not ns:
            return 0.0, [], []

        polarity = (ns.get("polarity") or "neutral").lower()
        conf = float(ns.get("confidence") or 0.5)
        suggestion = (ns.get("trade_suggestion") or "WAIT").upper()

        score = 0.0
        if polarity in ("bullish", "positive", "利好"):
            score = 0.5 + conf * 0.5
        elif polarity in ("bearish", "negative", "利空"):
            score = -0.5 - conf * 0.5
        elif suggestion == "LONG":
            score = 0.4
        elif suggestion == "SHORT":
            score = -0.4

        evidence = [
            f"新闻情绪: {polarity} (置信 {conf:.0%})",
            f"新闻建议: {suggestion}",
        ]
        headline = ns.get("headline_summary") or ns.get("summary")
        if headline:
            evidence.append(f"要点: {str(headline)[:80]}")

        concerns = []
        if conf < self.min_confidence:
            concerns.append(f"新闻置信度偏低 ({conf:.0%})")

        return score, evidence, concerns

    def _score_sentiment(self, ctx: MarketContext) -> tuple:
        s = ctx.sentiment or {}
        if not s:
            return 0.0, [], []

        score_val = float(s.get("sentiment_score") or 0)
        label = s.get("label") or "中性"
        fg = s.get("fear_greed") or {}
        fg_val = fg.get("value") if isinstance(fg, dict) else None

        score = clamp(score_val)
        evidence = [f"市场情绪: {label} (score={score_val:+.2f})"]
        if fg_val is not None:
            evidence.append(f"恐惧贪婪指数: {fg_val}")

        concerns = []
        funding = s.get("funding_rate")
        if isinstance(funding, dict):
            funding = funding.get("rate")
        if funding is not None:
            try:
                funding = float(funding)
            except (TypeError, ValueError):
                funding = None
        if funding is not None and abs(funding) > 0.0005:
            concerns.append(f"资金费率异常 ({funding:+.4%})")

        return score, evidence, concerns

    def _score_onchain(self, ctx: MarketContext) -> tuple:
        oc = ctx.onchain or {}
        if not oc:
            return 0.0, [], []

        score = clamp(float(oc.get("onchain_score") or 0))
        interp = oc.get("interpretation") or ""
        source = oc.get("data_source") or "?"

        evidence = [f"链上综合分: {score:+.2f} (数据源: {source})"]
        if interp:
            evidence.append(interp[:100])

        concerns = []
        if abs(score) < 0.1:
            concerns.append("链上信号微弱，参考价值有限")

        return score, evidence, concerns

    def _score_macro(self, ctx: MarketContext) -> tuple:
        mc = ctx.macro or {}
        if not mc:
            return 0.0, [], []

        score = clamp(float(mc.get("macro_score") or 0))
        interp = mc.get("interpretation") or ""
        fed = mc.get("fed_policy_proxy") or {}

        evidence = [f"宏观综合分: {score:+.2f}"]
        if interp:
            evidence.append(interp[:100])
        if fed.get("signal"):
            evidence.append(f"Fed预期代理: {fed.get('signal')}")

        concerns = []
        vix = (mc.get("snapshots") or {}).get("vix", {})
        if isinstance(vix, dict):
            chg = vix.get("change_pct")
            if chg is not None and float(chg) > 5:
                concerns.append(f"VIX 飙升 ({float(chg):+.1f}%)，宏观风险升高")

        return score, evidence, concerns

    def _score_bnb_factors(self, ctx: MarketContext) -> tuple:
        bf = ctx.bnb_factors or {}
        if not bf or bf.get("enabled") is False:
            return 0.0, [], []

        score = clamp(float(bf.get("bnb_score") or 0))
        evidence = [f"BNB专属因子: {bf.get('interpretation', '')[:100]}"]
        lp = bf.get("launchpool") or {}
        if lp.get("high_apy_event"):
            evidence.append(f"Launchpool 高 APY {lp.get('max_apy_pct', 0):.1f}%")
        al = bf.get("alpha") or {}
        if al.get("market_down_bnb_resilient"):
            evidence.append("大盘走弱但 BNB Alpha 为正（独立买盘）")

        concerns = []
        nlp = bf.get("announcement_nlp") or {}
        if nlp.get("impact_level") in ("critical", "high") and (nlp.get("score") or 0) < -0.2:
            concerns.append(f"币安监管/平台事件: {nlp.get('dominant_category', '?')}")

        return score, evidence, concerns

    @staticmethod
    def _build_summary(stance: Stance, score: float, evidence_count: int) -> str:
        labels = {
            Stance.BULLISH: "偏多",
            Stance.BEARISH: "偏空",
            Stance.NEUTRAL: "中性",
        }
        return (
            f"研究员综合 {evidence_count} 条情报，宏观立场「{labels[stance]}」"
            f" (score={score:+.2f})"
        )
