"""
BNB量化交易工具 - 决策可解释性 (Decision Explainer)
================================================
核心职责：把每一笔开单建议的**决策过程**分解为可读的加减分明细。

输出格式:
    本次 LONG 原因：
      + EMA 趋势        +18
      + 多周期共振       +22
      + 新闻情绪正面     +10
      - RSI 超买         -7
      - 波动率偏高       -5
      ────────────────────
      总分: +38  置信度: 0.81

设计原则：
- 纯只读模块，**不修改**任何上游数据
- 在 TradeAdvisor.build_advice 返回后调用，附加 explanation 字段
- 所有评分规则与 TradeAdvisor / TradingSignals 逻辑保持一致
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 评分因子定义 (name, max_contribution)
# 正分 = 支持交易方向，负分 = 反对
FACTOR_WEIGHTS = {
    "ai_direction": 25,         # AI 方向一致
    "ai_confidence": 15,        # AI 置信度
    "institutional_consensus": 20,  # 机构策略共识
    "institutional_vote_ratio": 10,  # 买卖票数比
    "rsi_signal": 10,           # RSI 超买/超卖
    "macd_signal": 10,          # MACD 金叉/死叉
    "multi_timeframe": 22,      # 多周期共振
    "news_sentiment": 12,       # 新闻情绪
    "market_sentiment": 8,      # 市场情绪指数
    "bnb_launchpool": 14,       # Launchpool 质押需求
    "bnb_alpha": 16,            # BTC/ETH Beta 剥离 Alpha
    "bnb_regulatory_nlp": 18,   # 币安监管/公告 NLP
    "bnb_event_cycle": 20,      # 事件周期四阶段
    "bnb_funding_extreme": 18,  # 资金费率极值
    "bnb_btc_weakness": 14,     # BNB/BTC 汇率弱势
    "learning_maturity": 8,     # AI 学习成熟度
    "historical_accuracy": 10,  # 历史胜率
    "risk_reward": 10,          # 风险回报比
    "volatility": -8,           # 波动率惩罚（高波动扣分）
}


class DecisionExplainer:
    """决策可解释性引擎 — 把 AI 的"黑盒决策"变成人可读的加减分"""

    def explain(
        self,
        action: str,
        indicators: Dict,
        ai_analysis: Dict,
        institutional: Optional[Dict] = None,
        learning_insights: Optional[Dict] = None,
        multi_timeframe: Optional[Dict] = None,
        sentiment: Optional[Dict] = None,
        news_summary: Optional[Dict] = None,
        bnb_factors: Optional[Dict] = None,
        prices: Optional[Dict] = None,
        risk_reward_ratio: Optional[float] = None,
        factor_reliability: Optional[Dict[str, float]] = None,
        votes: Optional[Dict] = None,
        dl_signal: Optional[Dict] = None,
        explorer_signal: Optional[Dict] = None,
        gate_reasons: Optional[List[str]] = None,
    ) -> Dict:
        """生成决策分解。

        Returns:
            {
                "action": str,
                "factors": [{"name": str, "score": int, "detail": str, "icon": str}],
                "total_score": int,
                "max_possible": int,
                "confidence_pct": float,  # total/max 归一化
                "text": str,  # 格式化文本（GUI 直接显示）
            }
        """
        if action == "WAIT":
            return self._explain_wait(
                votes=votes,
                ai_analysis=ai_analysis,
                institutional=institutional,
                dl_signal=dl_signal,
                explorer_signal=explorer_signal,
                gate_reasons=gate_reasons,
            )

        institutional = institutional or {}
        learning_insights = learning_insights or {}
        multi_timeframe = multi_timeframe or {}
        sentiment = sentiment or {}
        news_summary = news_summary or {}
        bnb_factors = bnb_factors or {}
        prices = prices or {}

        factors: List[Dict] = []

        # ----- 1. AI 方向 -----
        ai_signal = (ai_analysis.get("signal") or "").upper()
        ai_signal_cn = ai_analysis.get("signal") or ""
        ai_dir = self._parse_direction(ai_signal, ai_signal_cn)
        ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)

        if ai_dir == action:
            score = int(FACTOR_WEIGHTS["ai_direction"] * ai_conf)
            factors.append({"name": "AI 方向一致", "score": score,
                           "detail": f"AI={ai_dir} conf={ai_conf:.0%}", "icon": "🤖"})
        elif ai_dir == "WAIT":
            factors.append({"name": "AI 方向中立", "score": 0,
                           "detail": "AI 无明确方向", "icon": "🤖"})
        else:
            score = -int(FACTOR_WEIGHTS["ai_direction"] * ai_conf)
            factors.append({"name": "AI 方向冲突", "score": score,
                           "detail": f"AI={ai_dir} 与 {action} 相反", "icon": "🤖"})

        # ----- 2. AI 置信度 -----
        if ai_conf >= 0.75:
            s = FACTOR_WEIGHTS["ai_confidence"]
            factors.append({"name": "AI 高置信", "score": s,
                           "detail": f"置信度 {ai_conf:.0%} ≥ 75%", "icon": "📊"})
        elif ai_conf >= 0.6:
            s = int(FACTOR_WEIGHTS["ai_confidence"] * 0.5)
            factors.append({"name": "AI 中置信", "score": s,
                           "detail": f"置信度 {ai_conf:.0%}", "icon": "📊"})
        elif ai_conf < 0.45:
            s = -int(FACTOR_WEIGHTS["ai_confidence"] * 0.5)
            factors.append({"name": "AI 低置信", "score": s,
                           "detail": f"置信度 {ai_conf:.0%} < 45%", "icon": "📊"})

        # ----- 3. 机构策略共识 -----
        inst_signal = (institutional.get("consensus_signal") or "HOLD").upper()
        inst_conf = self._safe_float(institutional.get("consensus_confidence"), 0.5)
        buy_signals = int(institutional.get("buy_signals", 0) or 0)
        sell_signals = int(institutional.get("sell_signals", 0) or 0)
        total_signals = max(1, buy_signals + sell_signals + int(institutional.get("hold_signals", 0) or 0))

        inst_dir = "LONG" if inst_signal == "BUY" else ("SHORT" if inst_signal == "SELL" else "WAIT")
        if inst_dir == action:
            s = int(FACTOR_WEIGHTS["institutional_consensus"] * inst_conf)
            factors.append({"name": "机构共识一致", "score": s,
                           "detail": f"共识={inst_signal} conf={inst_conf:.0%}", "icon": "🏛️"})
        elif inst_dir == "WAIT":
            factors.append({"name": "机构无共识", "score": 0,
                           "detail": f"HOLD 信号", "icon": "🏛️"})
        else:
            s = -int(FACTOR_WEIGHTS["institutional_consensus"] * inst_conf * 0.7)
            factors.append({"name": "机构共识相反", "score": s,
                           "detail": f"共识={inst_signal}", "icon": "🏛️"})

        # 票数比
        if action == "LONG":
            ratio = buy_signals / total_signals
        elif action == "SHORT":
            ratio = sell_signals / total_signals
        else:
            ratio = 0
        if ratio >= 0.6:
            s = FACTOR_WEIGHTS["institutional_vote_ratio"]
            factors.append({"name": "策略投票倾斜", "score": s,
                           "detail": f"支持票 {ratio:.0%} (BUY={buy_signals} SELL={sell_signals})", "icon": "🗳️"})
        elif ratio <= 0.3:
            s = -int(FACTOR_WEIGHTS["institutional_vote_ratio"] * 0.7)
            factors.append({"name": "策略投票反对", "score": s,
                           "detail": f"支持票仅 {ratio:.0%}", "icon": "🗳️"})

        # ----- 4. RSI -----
        rsi = self._safe_float(indicators.get("RSI"), 50)
        if action == "LONG" and rsi < 30:
            factors.append({"name": "RSI 超卖", "score": FACTOR_WEIGHTS["rsi_signal"],
                           "detail": f"RSI={rsi:.1f} < 30 支持买", "icon": "📈"})
        elif action == "SHORT" and rsi > 70:
            factors.append({"name": "RSI 超买", "score": FACTOR_WEIGHTS["rsi_signal"],
                           "detail": f"RSI={rsi:.1f} > 70 支持卖", "icon": "📉"})
        elif action == "LONG" and rsi > 70:
            factors.append({"name": "RSI 超买", "score": -FACTOR_WEIGHTS["rsi_signal"],
                           "detail": f"RSI={rsi:.1f} > 70 不利买", "icon": "⚠️"})
        elif action == "SHORT" and rsi < 30:
            factors.append({"name": "RSI 超卖", "score": -FACTOR_WEIGHTS["rsi_signal"],
                           "detail": f"RSI={rsi:.1f} < 30 不利卖", "icon": "⚠️"})

        # ----- 5. MACD -----
        macd = self._safe_float(indicators.get("MACD"), 0)
        macd_signal_val = self._safe_float(indicators.get("MACD_Signal"), 0)
        macd_hist = self._safe_float(indicators.get("MACD_Histogram"), 0)
        if action == "LONG" and macd > macd_signal_val and macd_hist > 0:
            factors.append({"name": "MACD 金叉", "score": FACTOR_WEIGHTS["macd_signal"],
                           "detail": "MACD > Signal 且柱状图为正", "icon": "📈"})
        elif action == "SHORT" and macd < macd_signal_val and macd_hist < 0:
            factors.append({"name": "MACD 死叉", "score": FACTOR_WEIGHTS["macd_signal"],
                           "detail": "MACD < Signal 且柱状图为负", "icon": "📉"})
        elif action == "LONG" and macd < macd_signal_val:
            factors.append({"name": "MACD 死叉", "score": -int(FACTOR_WEIGHTS["macd_signal"] * 0.6),
                           "detail": "MACD < Signal 不利买", "icon": "⚠️"})
        elif action == "SHORT" and macd > macd_signal_val:
            factors.append({"name": "MACD 金叉", "score": -int(FACTOR_WEIGHTS["macd_signal"] * 0.6),
                           "detail": "MACD > Signal 不利卖", "icon": "⚠️"})

        # ----- 6. 多周期共振 -----
        mtf_action = (multi_timeframe.get("recommended_action") or "").upper()
        mtf_score = self._safe_float(multi_timeframe.get("weighted_score"), 0)
        confluence = multi_timeframe.get("confluence", "")
        if mtf_action == action:
            s = int(FACTOR_WEIGHTS["multi_timeframe"] * min(abs(mtf_score) / 50, 1.0))
            factors.append({"name": "多周期共振", "score": max(s, 5),
                           "detail": f"{confluence} 推荐 {mtf_action} (得分 {mtf_score})", "icon": "🔗"})
        elif mtf_action and mtf_action != "WAIT" and mtf_action != action:
            s = -FACTOR_WEIGHTS["multi_timeframe"]
            factors.append({"name": "多周期冲突", "score": s,
                           "detail": f"多周期推荐 {mtf_action} 与 {action} 冲突", "icon": "🚫"})

        # ----- 7. 新闻情绪 -----
        news_polarity = str(news_summary.get("polarity", "neutral")).lower()
        news_conf = self._safe_float(news_summary.get("confidence"), 0)
        if news_polarity == "bullish" and action == "LONG":
            s = int(FACTOR_WEIGHTS["news_sentiment"] * min(news_conf, 1.0))
            factors.append({"name": "新闻利好", "score": s,
                           "detail": f"新闻偏多 conf={news_conf:.0%}", "icon": "📰"})
        elif news_polarity == "bearish" and action == "SHORT":
            s = int(FACTOR_WEIGHTS["news_sentiment"] * min(news_conf, 1.0))
            factors.append({"name": "新闻利空", "score": s,
                           "detail": f"新闻偏空 conf={news_conf:.0%}", "icon": "📰"})
        elif news_polarity == "bearish" and action == "LONG":
            s = -int(FACTOR_WEIGHTS["news_sentiment"] * min(news_conf, 1.0))
            factors.append({"name": "新闻利空", "score": s,
                           "detail": f"新闻偏空与买冲突", "icon": "📰"})
        elif news_polarity == "bullish" and action == "SHORT":
            s = -int(FACTOR_WEIGHTS["news_sentiment"] * min(news_conf, 1.0))
            factors.append({"name": "新闻利好", "score": s,
                           "detail": f"新闻偏多与卖冲突", "icon": "📰"})

        # ----- 8. 市场情绪 -----
        sent_score = self._safe_float(sentiment.get("sentiment_score"), 0)
        if action == "LONG" and sent_score > 0.2:
            s = int(FACTOR_WEIGHTS["market_sentiment"] * min(sent_score, 1.0))
            factors.append({"name": "情绪偏多", "score": s,
                           "detail": f"情绪得分 {sent_score:.2f}", "icon": "💚"})
        elif action == "SHORT" and sent_score < -0.2:
            s = int(FACTOR_WEIGHTS["market_sentiment"] * min(abs(sent_score), 1.0))
            factors.append({"name": "情绪偏空", "score": s,
                           "detail": f"情绪得分 {sent_score:.2f}", "icon": "❤️"})
        elif action == "LONG" and sent_score < -0.3:
            s = -int(FACTOR_WEIGHTS["market_sentiment"] * min(abs(sent_score), 1.0))
            factors.append({"name": "情绪偏空", "score": s,
                           "detail": f"情绪 {sent_score:.2f} 不利买", "icon": "⚠️"})
        elif action == "SHORT" and sent_score > 0.3:
            s = -int(FACTOR_WEIGHTS["market_sentiment"] * min(sent_score, 1.0))
            factors.append({"name": "情绪偏多", "score": s,
                           "detail": f"情绪 {sent_score:.2f} 不利卖", "icon": "⚠️"})

        # ----- 8.5 BNB 专属因子 -----
        if bnb_factors and bnb_factors.get("enabled") is not False:
            lp = bnb_factors.get("launchpool") or {}
            al = bnb_factors.get("alpha") or {}
            nlp = bnb_factors.get("announcement_nlp") or {}

            if lp.get("high_apy_event") and action == "LONG":
                s = int(FACTOR_WEIGHTS["bnb_launchpool"] * min(lp.get("signal_strength", 0.5), 1.0))
                factors.append({"name": "Launchpool 高APY", "score": s,
                               "detail": f"最高 APY {lp.get('max_apy_pct', 0):.1f}% 质押买盘", "icon": "⛏️"})
            elif lp.get("high_apy_event") and action == "SHORT":
                s = -int(FACTOR_WEIGHTS["bnb_launchpool"] * 0.4)
                factors.append({"name": "Launchpool 质押需求", "score": s,
                               "detail": "高 APY 质押需求与做空冲突", "icon": "⛏️"})

            if al.get("market_down_bnb_resilient") and action == "LONG":
                s = FACTOR_WEIGHTS["bnb_alpha"]
                factors.append({"name": "BNB 抗跌 Alpha", "score": s,
                               "detail": f"大盘跌 Alpha={al.get('alpha_recent', 0):+.3%}", "icon": "🛡️"})
            elif al.get("positive_alpha") and action == "LONG":
                s = int(FACTOR_WEIGHTS["bnb_alpha"] * 0.6)
                factors.append({"name": "正 Alpha", "score": s,
                               "detail": al.get("interpretation", "")[:50], "icon": "📈"})
            elif al.get("alpha_score", 0) < -0.3 and action == "LONG":
                s = -int(FACTOR_WEIGHTS["bnb_alpha"] * 0.7)
                factors.append({"name": "跑输大盘", "score": s,
                               "detail": "BNB 相对 BTC/ETH 超额收益为负", "icon": "⚠️"})

            nl_score = float(nlp.get("score") or 0)
            nl_conf = float(nlp.get("confidence") or 0)
            if nl_conf >= 0.4:
                if nl_score > 0.2 and action == "LONG":
                    s = int(FACTOR_WEIGHTS["bnb_regulatory_nlp"] * min(nl_conf, 1.0))
                    factors.append({"name": "币安公告利好", "score": s,
                                   "detail": nlp.get("dominant_category", ""), "icon": "📢"})
                elif nl_score < -0.2 and action == "SHORT":
                    s = int(FACTOR_WEIGHTS["bnb_regulatory_nlp"] * min(nl_conf, 1.0))
                    factors.append({"name": "币安监管利空", "score": s,
                                   "detail": nlp.get("top_headline", "")[:50], "icon": "⚖️"})
                elif nl_score < -0.25 and action == "LONG":
                    s = -int(FACTOR_WEIGHTS["bnb_regulatory_nlp"] * min(nl_conf, 1.0))
                    factors.append({"name": "币安监管利空", "score": s,
                                   "detail": f"影响级别 {nlp.get('impact_level', '?')}", "icon": "🚨"})

            ec = bnb_factors.get("event_cycle") or {}
            phase = ec.get("phase", "normal")
            if phase == "anticipation" and action == "LONG":
                factors.append({"name": "事件发酵期", "score": FACTOR_WEIGHTS["bnb_event_cycle"],
                               "detail": "Launchpool 抢筹窗口", "icon": "📅"})
            elif phase == "unlock_dump" and action == "LONG":
                factors.append({"name": "解锁砸盘期", "score": -FACTOR_WEIGHTS["bnb_event_cycle"],
                               "detail": "强制拦截做多", "icon": "🚨"})
            elif phase == "unlock_dump" and action == "SHORT":
                factors.append({"name": "解锁砸盘期", "score": int(FACTOR_WEIGHTS["bnb_event_cycle"] * 0.8),
                               "detail": "BNB 解锁抛售窗口", "icon": "📉"})
            elif phase == "staking_lock":
                factors.append({"name": "质押锁仓期", "score": 0,
                               "detail": "建议网格/观望", "icon": "🔒"})

            rs = bnb_factors.get("risk_sentry") or {}
            fr = rs.get("funding_extreme") or {}
            if fr.get("block_long") and action == "LONG":
                factors.append({"name": "资金费率极值", "score": -FACTOR_WEIGHTS["bnb_funding_extreme"],
                               "detail": fr.get("interpretation", "")[:50], "icon": "🔥"})
            elif fr.get("reversal_risk") and action == "LONG":
                factors.append({"name": "资金费率偏高", "score": -int(FACTOR_WEIGHTS["bnb_funding_extreme"] * 0.5),
                               "detail": f"{fr.get('rate_pct')}%/8h", "icon": "⚠️"})

            ratio = rs.get("bnb_btc_weakness") or {}
            if ratio.get("weak") and action == "LONG":
                factors.append({"name": "BNB/BTC 弱势", "score": -FACTOR_WEIGHTS["bnb_btc_weakness"],
                               "detail": ratio.get("interpretation", "")[:50], "icon": "📉"})

        # ----- 9. 学习成熟度 -----
        maturity = (learning_insights.get("learning_maturity") or "BEGINNER").upper()
        accuracy = self._safe_float(learning_insights.get("overall_accuracy"), 0)
        maturity_map = {"BEGINNER": -4, "INTERMEDIATE": 2, "ADVANCED": 5, "EXPERT": 8}
        m_score = maturity_map.get(maturity, 0)
        if m_score != 0:
            factors.append({"name": f"学习成熟度({maturity})", "score": m_score,
                           "detail": f"成熟度影响置信 ({'+' if m_score > 0 else ''}{m_score})", "icon": "🧠"})

        # ----- 10. 历史胜率 -----
        if accuracy > 0:
            if accuracy >= 0.6:
                s = int(FACTOR_WEIGHTS["historical_accuracy"] * (accuracy - 0.5) * 2)
                factors.append({"name": "历史胜率好", "score": s,
                               "detail": f"胜率 {accuracy:.0%}", "icon": "🏆"})
            elif accuracy < 0.4:
                s = -int(FACTOR_WEIGHTS["historical_accuracy"] * (0.5 - accuracy) * 2)
                factors.append({"name": "历史胜率差", "score": s,
                               "detail": f"胜率仅 {accuracy:.0%}", "icon": "💀"})

        # ----- 11. 风险回报比 -----
        if risk_reward_ratio:
            if risk_reward_ratio >= 2.5:
                s = FACTOR_WEIGHTS["risk_reward"]
                factors.append({"name": "RR 优秀", "score": s,
                               "detail": f"RR={risk_reward_ratio:.1f} ≥ 2.5", "icon": "💎"})
            elif risk_reward_ratio >= 1.8:
                s = int(FACTOR_WEIGHTS["risk_reward"] * 0.5)
                factors.append({"name": "RR 合格", "score": s,
                               "detail": f"RR={risk_reward_ratio:.1f}", "icon": "✅"})
            elif risk_reward_ratio < 1.5:
                s = -FACTOR_WEIGHTS["risk_reward"]
                factors.append({"name": "RR 过低", "score": s,
                               "detail": f"RR={risk_reward_ratio:.1f} < 1.5", "icon": "⚠️"})

        # ----- 12. 波动率 -----
        atr = self._safe_float(prices.get("atr"), 0)
        entry = self._safe_float(prices.get("entry_mid"), 0)
        if atr and entry:
            atr_pct = atr / entry
            if atr_pct > 0.025:  # ATR > 2.5% of price = 高波动
                s = FACTOR_WEIGHTS["volatility"]  # 负数
                factors.append({"name": "波动率偏高", "score": s,
                               "detail": f"ATR/Price={atr_pct:.1%} > 2.5%", "icon": "🌊"})

        # ----- 汇总（应用因子归因可靠度）-----
        if factor_reliability:
            try:
                from bnb_quant_tool.factor_attribution_learner import (
                    apply_reliability_to_factors,
                )
                factors = apply_reliability_to_factors(factors, factor_reliability)
            except ImportError:
                pass

        total_score = sum(f["score"] for f in factors)
        max_possible = sum(abs(v) for v in FACTOR_WEIGHTS.values())

        # 排序：正分在前（支持理由），负分在后（反对理由）
        factors.sort(key=lambda x: x["score"], reverse=True)

        text = self._format_text(action, factors, total_score, max_possible)

        return {
            "action": action,
            "factors": factors,
            "total_score": total_score,
            "max_possible": max_possible,
            "confidence_pct": round(max(0, total_score) / max_possible, 3) if max_possible else 0,
            "text": text,
        }

    # ============================================================
    # WAIT 状态分解
    # ============================================================
    def _explain_wait(
        self,
        votes: Optional[Dict],
        ai_analysis: Dict,
        institutional: Optional[Dict] = None,
        dl_signal: Optional[Dict] = None,
        explorer_signal: Optional[Dict] = None,
        gate_reasons: Optional[List[str]] = None,
    ) -> Dict:
        """观望时拆解各方投票与拦截原因，避免笼统的「多空接近」误导。"""
        institutional = institutional or {}
        votes = votes or {}
        gate_reasons = gate_reasons or []

        long_score = self._safe_float(votes.get("long_score"), 0.0)
        short_score = self._safe_float(votes.get("short_score"), 0.0)
        threshold = self._safe_float(votes.get("vote_threshold"), 0.10)
        diff = abs(long_score - short_score)
        decided_action = (votes.get("decided_action") or "WAIT").upper()
        decision_reason = votes.get("decision_reason") or "vote_tie"

        factors: List[Dict] = []

        # ----- 综合投票得分 -----
        if long_score <= 0 and short_score <= 0:
            vote_detail = (
                f"多 {long_score:.2f} / 空 {short_score:.2f}，"
                f"各方均未给出方向信号"
            )
            summary = "各方信号中性，综合投票无方向"
        elif diff < threshold:
            vote_detail = (
                f"多 {long_score:.2f} / 空 {short_score:.2f}，"
                f"差 {diff:.2f} < 阈值 {threshold:.2f}"
            )
            summary = "多空得分接近，未达到开仓阈值"
        else:
            favored = "做多" if long_score > short_score else "做空"
            vote_detail = (
                f"多 {long_score:.2f} / 空 {short_score:.2f}，"
                f"倾向 {favored} 但未通过后续门槛"
            )
            summary = f"投票倾向{favored}，最终仍为观望"

        factors.append({
            "name": "综合投票",
            "score": 0,
            "detail": vote_detail,
            "icon": "🗳️",
        })

        # ----- AI -----
        ai_signal_raw = ai_analysis.get("signal") or "—"
        ai_dir = (votes.get("ai_direction") or self._parse_direction(
            (ai_analysis.get("signal") or "").upper(),
            ai_analysis.get("signal") or "",
        )).upper()
        ai_conf = self._safe_float(
            votes.get("ai_confidence", ai_analysis.get("confidence")), 0.5
        )
        factors.append({
            "name": "AI 信号",
            "score": 0,
            "detail": (
                f"{ai_signal_raw} → {self._dir_label(ai_dir)}"
                f"（置信 {ai_conf:.0%}）"
            ),
            "icon": "🤖",
        })

        trend = str(ai_analysis.get("trend") or "")
        if ai_dir == "WAIT" and trend:
            factors.append({
                "name": "AI 趋势",
                "score": 0,
                "detail": f"趋势={trend}，未转化为开仓方向",
                "icon": "📈" if "涨" in trend or "bull" in trend.lower() else "📉",
            })

        # ----- 机构 -----
        inst_dir = (votes.get("institutional_direction") or "WAIT").upper()
        inst_conf = self._safe_float(
            votes.get("institutional_confidence", institutional.get("consensus_confidence")),
            0.5,
        )
        dist = votes.get("institutional_distribution") or {}
        buy_n = int(dist.get("buy", institutional.get("buy_signals", 0)) or 0)
        sell_n = int(dist.get("sell", institutional.get("sell_signals", 0)) or 0)
        hold_n = int(dist.get("hold", institutional.get("hold_signals", 0)) or 0)
        inst_consensus = (institutional.get("consensus_signal") or "HOLD").upper()
        factors.append({
            "name": "机构策略",
            "score": 0,
            "detail": (
                f"共识 {inst_consensus} → {self._dir_label(inst_dir)}"
                f"（置信 {inst_conf:.0%}，"
                f"BUY={buy_n} SELL={sell_n} HOLD={hold_n}）"
            ),
            "icon": "🏛️",
        })

        # ----- 深度学习 -----
        dl_dir = (votes.get("dl_direction") or "WAIT").upper()
        dl_conf = self._safe_float(votes.get("dl_confidence"), 0.0)
        if dl_signal:
            dl_raw = (dl_signal.get("signal") or "HOLD").upper()
            factors.append({
                "name": "深度学习",
                "score": 0,
                "detail": (
                    f"{dl_raw} → {self._dir_label(dl_dir)}"
                    f"（置信 {dl_conf:.0%}）"
                ),
                "icon": "🧠",
            })
        elif dl_dir != "WAIT" and dl_conf > 0:
            factors.append({
                "name": "深度学习",
                "score": 0,
                "detail": f"{self._dir_label(dl_dir)}（置信 {dl_conf:.0%}）",
                "icon": "🧠",
            })
        else:
            factors.append({
                "name": "深度学习",
                "score": 0,
                "detail": "未参与（未启用、未训练或置信度不足）",
                "icon": "🧠",
            })

        # ----- 进化策略 -----
        if explorer_signal:
            exp_raw = (explorer_signal.get("signal") or "HOLD").upper()
            exp_conf = self._safe_float(explorer_signal.get("confidence"), 0.0)
            exp_dir = "LONG" if exp_raw in ("BUY", "LONG") else (
                "SHORT" if exp_raw in ("SELL", "SHORT") else "WAIT"
            )
            active = int(explorer_signal.get("active_strategies", 0) or 0)
            factors.append({
                "name": "进化策略",
                "score": 0,
                "detail": (
                    f"{exp_raw} → {self._dir_label(exp_dir)}"
                    f"（置信 {exp_conf:.0%}，{active} 个策略）"
                ),
                "icon": "🧬",
            })
        else:
            factors.append({
                "name": "进化策略",
                "score": 0,
                "detail": "未参与（未启用或置信度不足）",
                "icon": "🧬",
            })

        # ----- 投票有方向但被下游改掉 -----
        if decided_action in ("LONG", "SHORT") and decision_reason in (
            "vote_clear", "ai_tiebreak", "inst_tiebreak"
        ):
            factors.append({
                "name": "投票结果",
                "score": 0,
                "detail": (
                    f"曾倾向 {self._dir_label(decided_action)}"
                    f"（{self._reason_label(decision_reason)}），"
                    f"后被过滤器/门控改为观望"
                ),
                "icon": "↩️",
            })

        # ----- 门控原因（排除与投票重复的文案）-----
        for reason in gate_reasons[:3]:
            if "综合投票未分出方向" in reason:
                continue
            factors.append({
                "name": "门控拦截",
                "score": 0,
                "detail": reason,
                "icon": "🚫",
            })

        max_possible = sum(abs(v) for v in FACTOR_WEIGHTS.values())
        text = self._format_wait_text(summary, factors)

        return {
            "action": "WAIT",
            "factors": factors,
            "total_score": 0,
            "max_possible": max_possible,
            "confidence_pct": 0.0,
            "text": text,
            "vote_summary": {
                "long_score": long_score,
                "short_score": short_score,
                "diff": diff,
                "threshold": threshold,
                "decided_action": decided_action,
                "decision_reason": decision_reason,
            },
        }

    def _format_wait_text(self, summary: str, factors: List[Dict]) -> str:
        lines = [f"⚪ {summary}，建议观望", "─" * 42]
        for f in factors:
            icon = f.get("icon", "•")
            lines.append(f"  {icon} {f['name']}: {f['detail']}")
        return "\n".join(lines)

    @staticmethod
    def _dir_label(direction: str) -> str:
        return {"LONG": "做多", "SHORT": "做空", "WAIT": "观望"}.get(
            (direction or "WAIT").upper(), "观望"
        )

    @staticmethod
    def _reason_label(reason: str) -> str:
        return {
            "vote_clear": "得分超阈值",
            "ai_tiebreak": "AI 决胜",
            "inst_tiebreak": "机构决胜",
            "vote_tie": "未分胜负",
        }.get(reason, reason)

    # ============================================================
    # 格式化
    # ============================================================
    def _format_text(self, action: str, factors: List[Dict], total: int, max_possible: int) -> str:
        """生成人可读的评分明细文本"""
        action_cn = {"LONG": "🟢 买", "SHORT": "🔴 卖"}.get(action, action)
        lines = [f"决策解释 — {action_cn}"]
        lines.append("─" * 42)

        for f in factors:
            sign = "+" if f["score"] >= 0 else ""
            icon = f.get("icon", "")
            lines.append(f"  {icon} {f['name']:<14} {sign}{f['score']:>+4}  ({f['detail']})")

        lines.append("─" * 42)
        lines.append(f"  总分: {total:+d} / {max_possible}  "
                     f"(占比 {max(0, total)/max_possible:.0%})" if max_possible else "  总分: 0")
        return "\n".join(lines)

    # ============================================================
    # 工具
    # ============================================================
    @staticmethod
    def _safe_float(v, default=0.0):
        try:
            if v is None:
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_direction(signal_upper: str, signal_raw: str) -> str:
        if signal_raw in ("买入",) or signal_upper in ("BUY", "LONG"):
            return "LONG"
        elif signal_raw in ("卖出",) or signal_upper in ("SELL", "SHORT"):
            return "SHORT"
        return "WAIT"
