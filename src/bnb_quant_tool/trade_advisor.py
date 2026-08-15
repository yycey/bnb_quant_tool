"""
BNB量化交易工具 - 开单建议模块 (Trade Advisor)
================================================
核心职责：把"技术指标 + AI 分析 + 机构策略 + AI学习洞察"汇总为
一份"可立即下单"的开单建议，包括：

- 方向：LONG / SHORT / WAIT
- 入场区间：[entry_low, entry_high]
- 止损价：基于 ATR 与关键支撑/阻力
- 分批止盈：TP1 / TP2 / TP3 + 各档建议平仓比例
- 推荐仓位：USDT 金额 + BNB 数量
- 风险回报比、信号强度、有效期、取消条件
- 一段可直接复制到交易所的"下单文本"

设计原则：
- AI 学习成熟度低 / 历史胜率差 时自动转保守（缩小仓位、收紧止损、提高门槛）
- 信号强度不足时返回 WAIT，不给具体价格，避免误下单
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
import logging
import math

logger = logging.getLogger(__name__)


# 常量：信号强度
STRENGTH_STRONG = "强"
STRENGTH_MEDIUM = "中"
STRENGTH_WEAK = "弱"
ACTION_LONG = "LONG"
ACTION_SHORT = "SHORT"
ACTION_WAIT = "WAIT"


class TradeAdvisor:
    """开单建议生成器 v3.0：输出立即可下单的参数。
    
    基于模拟交易数据优化:
    - 自适应止损/止盈（波动率感知）
    - 时段过滤器（避开历史亏损时段）
    - TP 结构优化
    - 深度学习信号融合 + 市场状态驱动策略权重
    - 验证探针方向翻转时纠正 SL/TP
    """

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.config = cfg
        # 账户/风控参数
        self.account_balance: float = float(cfg.get("account_balance", 10000.0))
        self.risk_per_trade: float = float(cfg.get("risk_per_trade", 0.02))  # 单笔风险 2%
        self.max_position_pct: float = float(cfg.get("max_position_pct", 0.3))  # 单笔最多用 30% 资金
        self.min_confidence: float = float(cfg.get("min_confidence", 0.55))  # 低于此置信度建议观望
        self.min_rr: float = float(cfg.get("min_risk_reward_ratio", 1.5))  # 最低风险回报比
        # True：粗 RR 不硬拦，交给 post 净 RR 门控（避免 1.5 vs 2.0 双杀）
        self.defer_gross_rr_to_net_gate: bool = bool(
            cfg.get("defer_gross_rr_to_net_gate", True)
        )
        # ATR 倍数（用于回退止损/止盈）
        self.atr_sl_mult: float = float(cfg.get("atr_sl_mult", 1.5))
        self.atr_tp1_mult: float = float(cfg.get("atr_tp1_mult", 1.5))
        self.atr_tp2_mult: float = float(cfg.get("atr_tp2_mult", 3.0))
        self.atr_tp3_mult: float = float(cfg.get("atr_tp3_mult", 5.0))
        # 止损最宽下限：避免 AI/低波动 ATR 给出过紧止损
        self.min_sl_atr_mult: float = float(cfg.get("min_sl_atr_mult", 1.8))
        self.min_sl_pct: float = float(cfg.get("min_sl_pct", 0.018))  # 距入场至少 1.8%
        self.use_ai_stop_loss: bool = bool(cfg.get("use_ai_stop_loss", True))
        # 新闻过滤阈值: 新闻置信度 >= 此值时才拦截交易 (降低则更严格)
        self.news_filter_threshold: float = float(cfg.get("news_filter_threshold", 0.65))
        # 入场缓冲：限价单挂在当前价附近的百分比
        self.entry_buffer_pct: float = float(cfg.get("entry_buffer_pct", 0.003))  # 0.3%
        # 信号有效期（小时），与时间框架挂钩
        self.validity_hours_map: Dict[str, int] = {
            "1m": 1, "5m": 2, "15m": 4, "30m": 6,
            "1h": 12, "4h": 48, "1d": 120,
        }
        
        # ===== v2.0 自适应参数 =====
        # 波动率自适应止损范围
        self.atr_sl_mult_low_vol: float = float(cfg.get("atr_sl_mult_low_vol", 1.3))
        self.atr_sl_mult_high_vol: float = float(cfg.get("atr_sl_mult_high_vol", 2.2))
        self.vol_threshold_low: float = float(cfg.get("vol_threshold_low", 0.015))
        self.vol_threshold_high: float = float(cfg.get("vol_threshold_high", 0.030))
        
        # TP 分批比例优化（数据：仅14.9%到达TP3，应提高前两档）
        self.tp_split: Dict[str, str] = cfg.get("tp_split", {"tp1": "40%", "tp2": "35%", "tp3": "25%"})
        
        # 最大持仓数
        self.max_open_positions: int = int(cfg.get("max_open_positions", 0))
        self._paper_engine = None  # 外部设置，用于查询当前持仓数
        self._circuit_breaker = None  # 熔断器（连亏/回撤/波动率）
        self._structural_config: Dict = dict(cfg.get("structural_strategies") or {})

        # v2.2: 深度学习引擎引用（外部设置）
        self._dl_engine = None
        # v2.2: 深度学习信号融合权重 (0-1, 0=不使用, 1=完全信任DL)
        self.dl_weight: float = float(cfg.get("dl_weight", 0.25))
        # v2.2: 深度学习最低置信度门槛（低于此值忽略DL信号）
        self.dl_min_confidence: float = float(cfg.get("dl_min_confidence", 0.65))

        # v2.5: 指标探索器引用（外部设置）
        self._indicator_explorer = None
        self.explorer_weight: float = float(cfg.get("explorer_weight", 0.15))
        self.explorer_min_confidence: float = float(cfg.get("explorer_min_confidence", 0.5))

        # 方向投票：多空得分差超过此阈值才开单（原 0.15 过严，易长期 WAIT）
        self.direction_vote_threshold: float = float(cfg.get("direction_vote_threshold", 0.10))
        self.ai_tiebreak_min_confidence: float = float(cfg.get("ai_tiebreak_min_confidence", 0.58))
        self.inst_tiebreak_min_confidence: float = float(cfg.get("inst_tiebreak_min_confidence", 0.55))
        self.inst_vote_skew_min: float = float(cfg.get("inst_vote_skew_min", 0.42))
        # 置信度达标时允许「弱」强度开单（避免投票略平就被挡）
        self.allow_weak_signal: bool = bool(cfg.get("allow_weak_signal", True))
        # true：方向以 AI 买入/卖出为准，投票仅作参考；false：四方投票决定方向
        self.follow_ai_direction: bool = bool(cfg.get("follow_ai_direction", False))
        self._gate_consec_loss_block: int = int(cfg.get("gate_consec_loss_block", 5))
        self._ta_playbook_cfg: Dict = dict(cfg.get("ta_playbook") or {})

        # v2.2: 市场状态仓位缩放因子缓存
        self._regime_position_factor: float = 1.0

        guard_cfg = cfg.get("ai_guardrail") or {}
        try:
            from bnb_quant_tool.ai_guardrail import AIGuardrail
            self._guardrail = AIGuardrail(guard_cfg) if guard_cfg.get("enabled", True) else None
        except ImportError:
            self._guardrail = None

    def set_structural_config(self, config: Optional[Dict]) -> None:
        self._structural_config = dict(config or {})

    # ============================================================
    # 主入口
    # ============================================================
    def build_advice(
        self,
        symbol: str,
        timeframe: str,
        current_price: float,
        indicators: Dict,
        ai_analysis: Dict,
        institutional: Optional[Dict] = None,
        learning_insights: Optional[Dict] = None,
        multi_timeframe: Optional[Dict] = None,
        sentiment: Optional[Dict] = None,
        news_summary: Optional[Dict] = None,
        market_regime: Optional[Dict] = None,
        onchain: Optional[Dict] = None,
        macro: Optional[Dict] = None,
        bnb_factors: Optional[Dict] = None,
        leverage: int = 1,
        factor_reliability: Optional[Dict[str, float]] = None,
        analysis_mode: str = "all",
        technical_combined: Optional[Dict] = None,
    ) -> Dict:
        """根据所有上游信号生成最终开单建议。
        新增参数:
        - multi_timeframe: 多周期共振输出 (MultiTimeframeAnalyzer.analyze)
        - sentiment: 市场情绪 (MarketSentiment.fetch_all)
        - news_summary: AI 新闻汇总 (DeepSeekAnalyzer.summarize_news)
        - onchain: 链上筹码分析 (OnChainAnalyzer.fetch_all)
        - macro: 宏观数据层 (MacroDataLayer.fetch_all)
        - bnb_factors: BNB 专属因子 (BNBSpecificFactors.fetch_all)
        """
        institutional = institutional or {}
        learning_insights = learning_insights or {}
        multi_timeframe = multi_timeframe or {}
        sentiment = sentiment or {}
        news_summary = news_summary or {}
        market_regime = market_regime or {}
        onchain = onchain or {}
        macro = macro or {}
        bnb_factors = bnb_factors or {}

        # 1. 方向：follow_ai_direction 时以 AI 买入/卖出为准，投票仅记录参考
        ai_action = self._parse_ai_action(ai_analysis)
        dl_signal = self._get_dl_signal(indicators, current_price)
        explorer_signal = self._get_explorer_signal(indicators, current_price)
        regime_multipliers = market_regime.get("strategy_multipliers", {})
        self._regime_position_factor = market_regime.get("position_factor", 1.0)
        vote_action, vote_strength, votes = self._decide_action(
            ai_analysis, institutional,
            dl_signal=dl_signal,
            explorer_signal=explorer_signal,
            regime_multipliers=regime_multipliers,
            bnb_factors=bnb_factors,
            factor_reliability=factor_reliability,
            sentiment=sentiment,
            learning_insights=learning_insights,
        )
        votes["ai_action"] = ai_action
        votes["vote_action"] = vote_action

        mode = (analysis_mode or "all").lower()
        if mode == "all":
            if self.follow_ai_direction:
                if ai_action == ACTION_WAIT:
                    action = ACTION_WAIT
                    raw_strength = vote_strength
                    votes["decision_reason"] = "ai_hold"
                    votes["block_reason"] = "ai_hold"
                else:
                    action = ai_action
                    ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)
                    raw_strength = self._strength_from_confidence(ai_conf)
                    votes["decision_reason"] = "follow_ai"
            else:
                action = vote_action
                raw_strength = vote_strength
        else:
            action, raw_strength = self._apply_analysis_mode(
                mode, vote_action, vote_strength, ai_analysis, institutional,
                technical_combined, votes,
            )
        votes["decided_action"] = action
        votes["analysis_mode"] = mode
        # 门控前方向快照：供学习期试探 / 价格计算（过滤器改 WAIT 后仍保留可执行计划）
        # AI HOLD 时仍保留投票方向，避免验证探针拿不到 LONG/SHORT、有行情却零开仓
        if action in (ACTION_LONG, ACTION_SHORT):
            intended_direction = action
        elif ai_action in (ACTION_LONG, ACTION_SHORT):
            intended_direction = ai_action
        elif vote_action in (ACTION_LONG, ACTION_SHORT):
            intended_direction = vote_action
            votes["intended_from_vote_on_ai_hold"] = True
        else:
            intended_direction = ACTION_WAIT

        # 1.4 AI Guardrail：投票模式下拦截与硬性指标冲突的方向
        guardrail_result = {}
        guardrail_block_reason = ""
        if self._guardrail and action != ACTION_WAIT and not self.follow_ai_direction:
            guardrail_result = self._guardrail.validate(
                ai_analysis=ai_analysis,
                proposed_action=action,
                indicators=indicators,
                institutional=institutional,
                multi_timeframe=multi_timeframe,
                market_regime=market_regime,
                news_summary=news_summary,
                current_price=current_price,
            )
            if guardrail_result.get("blocked"):
                action = guardrail_result.get("final_action", ACTION_WAIT)
                guardrail_block_reason = guardrail_result.get("interpretation", "AI Guardrail 拦截")
        
        # 1.5 多周期 / 新闻：follow_ai 时不改方向（仅投票模式生效）
        mtf_action = (multi_timeframe.get("recommended_action") or "").upper()
        sentiment_score = float(sentiment.get("sentiment_score", 0.0) or 0.0)
        onchain_score = float(onchain.get("onchain_score", 0.0) or 0.0)
        macro_score = float(macro.get("macro_score", 0.0) or 0.0)
        mtf_block_reason = ""
        news_block_reason = ""
        if not self.follow_ai_direction:
            action, mtf_block_reason = self._apply_mtf_filter(action, mtf_action)
            action, news_block_reason = self._apply_news_filter(
                action, news_summary, bnb_factors,
            )

        # 1.64 挖矿事件因子：结束前降多 / 解锁期 SHORT _bias
        action, mining_block_reason = self._apply_mining_event_filter(action, bnb_factors)

        # 1.65 BNB 专属：币安监管/公告 NLP 硬过滤
        action, bnb_block_reason = self._apply_bnb_regulatory_filter(action, bnb_factors)

        # 1.66 BNB 事件周期：解锁砸盘期强制拦截做多
        action, event_block_reason, event_cycle = self._apply_event_cycle_filter(
            action, bnb_factors
        )

        # 1.67 BNB 风控哨兵：资金费率极值 → 拦截做多
        action, sentry_block_reason = self._apply_risk_sentry_filter(action, bnb_factors)

        # 1.68 知识库确定性门控（高置信历史规则）
        knowledge_block_reasons: List[str] = []
        knowledge_tightening = 0.0
        try:
            from bnb_quant_tool.knowledge_gate import apply_knowledge_gates
            action, knowledge_block_reasons, knowledge_tightening = apply_knowledge_gates(
                action,
                learning_insights,
                ai_confidence=self._safe_float(ai_analysis.get("confidence"), 0.5),
                market_regime=market_regime,
                indicators=indicators,
            )
        except ImportError:
            pass

        # 2. 应用学习系统的“信心修正”
        adj_strength, conservativeness = self._apply_learning_adjustment(
            raw_strength, learning_insights
        )
        conservativeness = self._apply_sentiment_adjustment(
            action, conservativeness, sentiment_score
        )
        conservativeness = self._apply_onchain_adjustment(
            action, conservativeness, onchain_score
        )
        conservativeness = self._apply_macro_adjustment(
            action, conservativeness, macro_score, macro
        )
        conservativeness = self._apply_news_adjustment(
            action, conservativeness, news_summary
        )
        conservativeness = self._apply_bnb_factors_adjustment(
            action, conservativeness, bnb_factors
        )

        # 3. 计算价格参数（入场/止损/止盈）— v2.0 自适应止损
        # 即使用门控把 action 改成 WAIT，也按 intended_direction 算好 SL/TP，避免 RR=0 / 试探无价
        atr = self._safe_float(indicators.get("ATR"), default=current_price * 0.01)
        adaptive_sl_mult = self._get_adaptive_sl_mult(current_price, atr)
        price_basis = (
            action if action in (ACTION_LONG, ACTION_SHORT)
            else intended_direction
        )
        prices = self._calc_prices(price_basis, current_price, atr, ai_analysis, adaptive_sl_mult)

        # 4. 风险回报比、仓位 — v2.0 连亏仓位调整 + v2.2 市场状态仓位缩放
        rr = self._calc_risk_reward(price_basis, prices)
        regime_adj_conservativeness = conservativeness * self._regime_position_factor
        pos_basis = (
            action if action in (ACTION_LONG, ACTION_SHORT)
            else (
                intended_direction
                if intended_direction in (ACTION_LONG, ACTION_SHORT)
                else ACTION_WAIT
            )
        )
        position = self._calc_position(pos_basis, prices, regime_adj_conservativeness, leverage)

        # 4.1 保证金占用检查（本金用尽则不再开单）
        action, margin_block_reason = self._check_margin_available(action, position)

        # 5. 信号是否达到下单门槛（BNB 专属因子 + 事件周期可调整 confidence 门槛）
        gate_relaxation = float(bnb_factors.get("gate_relaxation") or 0.0)
        gate_relaxation += float(getattr(self, "_event_gate_relaxation", 0) or 0)
        gate_tightening = float(getattr(self, "_event_gate_tightening", 0) or 0)
        gate_tightening += knowledge_tightening
        try:
            from bnb_quant_tool.learning_analytics import get_session_gate_boost
            gate_tightening += get_session_gate_boost()
        except ImportError:
            pass
        try:
            from bnb_quant_tool.factor_attribution_learner import (
                gate_tightening_from_attribution,
            )
            gate_tightening += gate_tightening_from_attribution(learning_insights)
        except ImportError:
            pass
        if factor_reliability:
            unreliable = sum(1 for v in factor_reliability.values() if v < 0.7)
            gate_tightening += min(0.08, unreliable * 0.02)
        gate_tightening += self._funding_gate_tightening(sentiment, bnb_factors)

        # 1.69 技术分析 Playbook 门控（经典TA / ADX / Regime 对齐）
        # 若配置 advisor_skip_duplicate_post_gates，则仅构建 bundle，硬拦留给后置栈
        ta_bundle: Dict = {}
        ta_block_reasons: List[str] = []
        ta_tightening = 0.0
        ta_relaxation = 0.0
        skip_dup = bool(
            (getattr(self, "config", None) or {}).get(
                "advisor_skip_duplicate_post_gates", True
            )
        )
        try:
            from bnb_quant_tool.crypto_ta_playbook import build_ta_analysis_bundle
            from bnb_quant_tool.ta_playbook_gate import apply_ta_playbook_gates

            ta_cfg = learning_insights.get("ta_playbook_config") or self._ta_playbook_cfg
            if ta_cfg.get("enabled", True) is not False:
                ta_bundle = learning_insights.get("ta_playbook") or build_ta_analysis_bundle(
                    regime=market_regime.get("regime"),
                    indicators=indicators,
                    inst_results=institutional,
                    config={"analysis": {"ta_playbook": ta_cfg}},
                    account_balance=self.account_balance,
                    symbol=symbol,
                )
                if not skip_dup:
                    action, ta_block_reasons, ta_tightening, ta_relaxation = apply_ta_playbook_gates(
                        action,
                        ta_bundle,
                        indicators=indicators,
                        market_regime=market_regime,
                        config=ta_cfg,
                    )
        except ImportError:
            pass
        except Exception as ta_e:
            logger.debug("ta_playbook_gate: %s", ta_e)

        gate_tightening += ta_tightening
        gate_relaxation += ta_relaxation

        # 1.685 胜率学习门控（历史亏损模式 / 模拟盘绩效）— 后置栈权威时跳过硬拦
        wr_block_reason = ""
        try:
            from bnb_quant_tool.learning_analytics import (
                apply_direction_blocks,
                apply_vote_adjustments,
                gate_adjustments_from_context,
            )

            wrc = learning_insights.get("win_rate_context") or {}
            if not skip_dup:
                action, wr_block_reason = apply_direction_blocks(action, wrc)
            wr_gt, wr_gr = gate_adjustments_from_context(wrc)
            gate_tightening += wr_gt
            gate_relaxation += wr_gr
            if votes and isinstance(votes, dict):
                ls, ss = apply_vote_adjustments(
                    float(votes.get("long_score") or 0),
                    float(votes.get("short_score") or 0),
                    wrc,
                )
                votes["long_score"] = ls
                votes["short_score"] = ss
        except Exception as wr_e:
            logger.debug("win_rate advisor: %s", wr_e)

        passed, fail_reasons = self._gate_check(
            action, adj_strength, rr, ai_analysis, learning_insights,
            gate_relaxation=gate_relaxation,
            gate_tightening=gate_tightening,
            votes=votes,
        )
        if mtf_block_reason and not self.follow_ai_direction:
            fail_reasons.append(mtf_block_reason)
            passed = False
        if news_block_reason and not self.follow_ai_direction:
            fail_reasons.append(news_block_reason)
            passed = False
        if guardrail_block_reason:
            fail_reasons.append(guardrail_block_reason)
            passed = False
        if mining_block_reason:
            fail_reasons.append(mining_block_reason)
            passed = False
        if bnb_block_reason:
            fail_reasons.append(bnb_block_reason)
            passed = False
        if event_block_reason:
            fail_reasons.append(event_block_reason)
            passed = False
        if sentry_block_reason:
            fail_reasons.append(sentry_block_reason)
            passed = False
        if knowledge_block_reasons:
            fail_reasons.extend(knowledge_block_reasons)
            if action == ACTION_WAIT:
                passed = False
        if ta_block_reasons:
            fail_reasons.extend(ta_block_reasons)
            if action == ACTION_WAIT:
                passed = False
        if wr_block_reason:
            fail_reasons.append(wr_block_reason)
            passed = False
        if margin_block_reason:
            fail_reasons.append(margin_block_reason)
            passed = False

        # 5.1 熔断器（连亏 / 24h 回撤 / 冷却期 / ATR突变 / MA20偏离）
        circuit_breaker_result: Dict = {}
        circuit_breaker_blocked = False
        if self._circuit_breaker and action != ACTION_WAIT:
            try:
                rs = (bnb_factors or {}).get("risk_sentry") or {}
                current_atr = self._safe_float(indicators.get("ATR"), default=0)
                # 优先用 regime 预计算的均值 ATR；勿用 current=avg 假比值=1
                avg_atr = self._safe_float(
                    market_regime.get("avg_atr")
                    or indicators.get("ATR_avg")
                    or indicators.get("avg_ATR"),
                    default=0,
                )
                if avg_atr <= 0 and current_atr > 0:
                    atr_ratio = self._safe_float(market_regime.get("atr_ratio"), default=0)
                    if atr_ratio > 0:
                        avg_atr = current_atr / atr_ratio
                ma20 = self._safe_float(
                    indicators.get("MA_20") or indicators.get("SMA_20"),
                    default=0,
                )
                circuit_breaker_result = self._circuit_breaker.check(
                    current_atr=current_atr if current_atr > 0 else None,
                    avg_atr=avg_atr if avg_atr > 0 else None,
                    bnb_risk=rs,
                    current_price=current_price if current_price else None,
                    ma20=ma20 if ma20 > 0 else None,
                )
                cb_pf = float(circuit_breaker_result.get("position_factor") or 1.0)
                if not circuit_breaker_result.get("allowed", True):
                    action = ACTION_WAIT
                    passed = False
                    # 硬旗标：学习期试探不可绕过
                    #（gate_reasons 文案「连续亏损」不含关键词「连亏」）
                    circuit_breaker_blocked = True
                    fail_reasons.extend(circuit_breaker_result.get("reasons") or [])
                elif cb_pf < 1.0 and position:
                    position = dict(position)
                    for k in ("quantity", "usdt_amount", "margin_required"):
                        if position.get(k) is not None:
                            position[k] = round(float(position[k]) * cb_pf, 6)
                    fail_reasons.extend(circuit_breaker_result.get("reasons") or [])
            except Exception as cb_e:
                logger.debug("circuit_breaker: %s", cb_e)

        # 6. 有效期
        validity_hours = self.validity_hours_map.get(timeframe, 12)
        valid_until = (datetime.now() + timedelta(hours=validity_hours)).isoformat(timespec="seconds")

        # 7. 取消条件（什么时候放弃这单）
        invalidate = self._build_invalidation(action, prices, atr)

        # 8. 推理依据
        reasons = self._build_reasons(action, ai_analysis, institutional, indicators)
        if multi_timeframe:
            reasons.append(
                f"多周期: {multi_timeframe.get('confluence', '')} 推荐 {mtf_action} "
                f"(加权得分 {multi_timeframe.get('weighted_score', 0)})"
            )
        if sentiment:
            reasons.append(
                f"市场情绪: {sentiment.get('interpretation', '')}"
            )
        if onchain:
            reasons.append(
                f"链上筹码: {onchain.get('interpretation', '')}"
            )
        if macro:
            reasons.append(
                f"宏观因子: {macro.get('interpretation', '')}"
            )
        if news_summary:
            ns_pol = news_summary.get('polarity', 'neutral')
            ns_sug = news_summary.get('trade_suggestion', 'WAIT')
            ns_sum = news_summary.get('summary', '')
            reasons.append(
                f"新闻情报: {ns_pol} (建议 {ns_sug}) - {ns_sum}"
            )
        if market_regime.get("regime"):
            reasons.append(
                f"市场状态: {market_regime.get('regime')} — {market_regime.get('description', '')} "
                f"(仓位系数 {self._regime_position_factor:.0%})"
            )
        if dl_signal:
            reasons.append(
                f"深度学习: {dl_signal.get('signal')} (置信 {dl_signal.get('confidence', 0):.0%})"
            )
        if explorer_signal:
            reasons.append(
                f"进化策略: {explorer_signal.get('signal')} (置信 {explorer_signal.get('confidence', 0):.0%}, {explorer_signal.get('active_strategies', 0)}个策略)"
            )
        if bnb_factors.get("bnb_score") is not None and bnb_factors.get("enabled") is not False:
            reasons.append(f"BNB专属因子: {bnb_factors.get('interpretation', '')}")
            if gate_relaxation > 0:
                reasons.append(f"Launchpool/Alpha/事件周期 门控放宽 -{gate_relaxation:.0%}")
        if event_cycle and event_cycle.get("phase") not in (None, "normal"):
            reasons.append(f"事件周期: {event_cycle.get('interpretation', '')}")
        rs = (bnb_factors or {}).get("risk_sentry") or {}
        if rs.get("block_long") or rs.get("bnb_btc_weakness", {}).get("weak"):
            reasons.append(f"风控哨兵: {rs.get('interpretation', '')}")
        if ta_bundle.get("enabled"):
            bias = ta_bundle.get("classic_ta_bias", "HOLD")
            align = "✓" if ta_bundle.get("classic_ta_aligned_with_consensus") else "≠"
            hints = ta_bundle.get("indicator_hints") or []
            reasons.append(
                f"TA Playbook: 经典TA偏向 {bias} | 与机构共识{align}"
            )
            if hints:
                reasons.append(f"TA纪律: {hints[0]}")

        advice: Dict = {
            "symbol": symbol,
            "timeframe": timeframe,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "current_price": round(current_price, 4),
            "ai_action": ai_action,
            "follow_ai_direction": self.follow_ai_direction,
            "action": action if passed else ACTION_WAIT,
            # raw = 门控前意图方向（学习期试探跟单用）；勿被 circuit breaker 改成 WAIT 覆盖
            "raw_action": (
                intended_direction
                if intended_direction in (ACTION_LONG, ACTION_SHORT)
                else action
            ),
            "intended_direction": intended_direction,
            "strength": adj_strength,
            "confidence": round(self._extract_confidence(ai_analysis, institutional), 4),
            "votes": votes,
            "passed_gate": passed,
            "gate_reasons": fail_reasons,
            "block_reason": (
                (votes or {}).get("block_reason")
                or ((votes or {}).get("decision_reason") if not passed and (votes or {}).get("decision_reason") == "ai_hold" else "")
                or (fail_reasons[0] if fail_reasons and not passed else "")
            ),
            "conservativeness": round(conservativeness, 3),
            "adaptive_sl_mult": round(adaptive_sl_mult, 2),
            "dl_signal": dl_signal,  # v2.2
            "explorer_signal": explorer_signal,  # v2.5
            "regime_position_factor": round(self._regime_position_factor, 3),  # v2.2
            "prices": prices,
            "risk_reward_ratio": round(rr, 3) if rr else None,
            "position": position,
            "validity_hours": validity_hours,
            "valid_until": valid_until,
            "invalidation": invalidate,
            "reasons": reasons,
            "learning_maturity": learning_insights.get("learning_maturity", "BEGINNER"),
            "historical_accuracy": learning_insights.get("overall_accuracy", 0.0),
            "multi_timeframe": multi_timeframe,
            "sentiment": sentiment,
            "onchain": onchain,
            "macro": macro,
            "bnb_factors": bnb_factors,
            "event_cycle": event_cycle,
            "risk_sentry": rs,
            "gate_relaxation": round(gate_relaxation, 3),
            "gate_tightening": round(gate_tightening, 3),
            "ta_gate_tightening": round(ta_tightening, 3),
            "ta_playbook": ta_bundle,
            "win_rate_context": learning_insights.get("win_rate_context") or {},
            "strategy_mode": (
                event_cycle.get("strategy_mode")
                if event_cycle else getattr(self, "_event_strategy_mode", "normal")
            ),
            "news_summary": news_summary,
            "market_regime": market_regime,
            "guardrail": guardrail_result,
            "mining_event": (bnb_factors or {}).get("mining_event"),
            "news_credibility": (bnb_factors or {}).get("news_credibility"),
            "circuit_breaker": circuit_breaker_result,
            "circuit_breaker_blocked": circuit_breaker_blocked,
            "analysis_mode": mode,
            "structural_vote": votes.get("structural_vote"),
        }
        advice["decision_summary"] = self._build_decision_summary(advice)
        advice["execution_summary"] = self._build_execution_summary(advice)
        try:
            from bnb_quant_tool.decision_state import attach_decision_state
            advice = attach_decision_state(advice)
        except Exception as ds_e:
            logger.debug("decision_state: %s", ds_e)
        # 9. 生成"复制即可下单"的文本
        advice["order_text"] = self.format_order_text(advice)
        advice["report_text"] = self.format_report(advice)

        try:
            from bnb_quant_tool.learning_analytics import load_gate_state, tick_session_gate
            from bnb_quant_tool.data_localization import get_localization_manager

            if load_gate_state(get_localization_manager().workspace):
                tick_session_gate(get_localization_manager().workspace)
        except Exception:
            pass

        logger.info(
            f"开单建议: {symbol} {advice['action']} entry={prices.get('entry_mid')} "
            f"SL={prices.get('stop_loss')} TP1={prices.get('tp1')} RR={advice['risk_reward_ratio']} "
            f"adaptiveSL={adaptive_sl_mult:.2f} "
            f"regime={self._regime_position_factor:.2f} dl={'ON' if dl_signal else 'OFF'} exp={'ON' if explorer_signal else 'OFF'}"
        )
        return advice

    # ============================================================
    # v2.0 自适应止损倍数
    # ============================================================
    def _get_adaptive_sl_mult(self, current_price: float, atr: float) -> float:
        """根据波动率自适应调整 ATR 止损倍数。
        
        数据分析结论：
        - 37.8% 的交易 MAE<-1R 后才止损，说明固定倍数不适应波动率变化
        - 高波动时需要更宽的止损避免假突破
        - 低波动时可以收紧止损减小风险
        """
        if current_price <= 0 or atr <= 0:
            return self.atr_sl_mult
        
        vol_pct = atr / current_price  # ATR 占价格的百分比
        
        if vol_pct <= self.vol_threshold_low:
            # 低波动：不再收紧，至少用基础倍数，避免被正常噪音扫损
            mult = max(self.atr_sl_mult, self.atr_sl_mult_low_vol)
        elif vol_pct >= self.vol_threshold_high:
            # 高波动：放宽止损
            mult = self.atr_sl_mult_high_vol
        else:
            # 中等波动：线性插值（下限不低于基础倍数）
            t = (vol_pct - self.vol_threshold_low) / (self.vol_threshold_high - self.vol_threshold_low)
            base = max(self.atr_sl_mult, self.atr_sl_mult_low_vol)
            mult = base + t * (self.atr_sl_mult_high_vol - base)

        return max(mult, self.min_sl_atr_mult)

    def set_paper_engine(self, engine):
        """v2.1: 设置 paper_engine 引用，用于查询当前持仓数。"""
        self._paper_engine = engine
        if self._circuit_breaker is not None:
            self._circuit_breaker._engine = engine

    def set_circuit_breaker(self, breaker) -> None:
        """接入交易熔断器（连亏停手 / 回撤熔断）。"""
        self._circuit_breaker = breaker
        if breaker is not None and self._paper_engine is not None:
            breaker._engine = self._paper_engine

    def set_account_balance(self, balance: float) -> None:
        """同步账户余额到熔断器。"""
        self.account_balance = float(balance)
        if self._circuit_breaker is not None:
            self._circuit_breaker.account_balance = float(balance)

    @staticmethod
    def _extract_funding_rate(
        sentiment: Optional[Dict],
        bnb_factors: Optional[Dict],
    ) -> Optional[float]:
        rs = (bnb_factors or {}).get("risk_sentry") or {}
        fr = rs.get("funding_extreme") or {}
        if fr.get("rate") is not None:
            return float(fr["rate"])
        if sentiment:
            fr2 = sentiment.get("funding_rate") or {}
            if isinstance(fr2, dict) and fr2.get("rate") is not None:
                return float(fr2["rate"])
        return None

    def _funding_gate_tightening(
        self,
        sentiment: Optional[Dict],
        bnb_factors: Optional[Dict],
    ) -> float:
        """Funding 偏 crowded 时提高置信度门槛。"""
        rate = self._extract_funding_rate(sentiment, bnb_factors)
        if rate is None:
            return 0.0
        if rate >= 0.001:
            return 0.06
        if rate >= 0.0005:
            return 0.04
        if rate <= -0.001:
            return 0.03
        return 0.0

    def set_dl_engine(self, engine):
        """v2.2: 设置深度学习引擎引用，用于融合DL预测信号。"""
        self._dl_engine = engine
    
    def set_indicator_explorer(self, explorer):
        """v2.5: 设置指标探索器引用，用于融合进化策略信号。"""
        self._indicator_explorer = explorer
    
    def _get_explorer_signal(self, indicators: Dict, current_price: float) -> Optional[Dict]:
        """v2.5: 从指标探索器获取进化策略信号。"""
        if self._indicator_explorer is None:
            return None
        try:
            return self._indicator_explorer.get_signal_from_evolved_strategies(indicators)
        except Exception as e:
            logger.debug(f"探索器信号获取失败: {e}")
            return None

    def _get_dl_signal(self, indicators: Dict, current_price: float) -> Optional[Dict]:
        """v2.2: 获取深度学习预测信号。
        
        Returns:
            {"signal": "LONG"/"SHORT"/"HOLD", "confidence": float} or None
        """
        if self._dl_engine is None or not getattr(self._dl_engine, 'is_trained', False):
            return None
        try:
            market_data = {
                'price_data': None,  # DL引擎内部会用indicators
                'indicators': indicators,
                'sentiment': None,
            }
            result = self._dl_engine.predict(market_data)
            if result and result.get('confidence', 0) >= self.dl_min_confidence:
                return {
                    'signal': result.get('signal', 'HOLD'),
                    'confidence': result.get('confidence', 0),
                }
        except Exception as e:
            logger.debug(f"DL预测异常: {e}")
        return None

    def _check_margin_available(self, action: str, position: Dict) -> Tuple[str, str]:
        """保证金模式：可用保证金不足时禁止新开仓。"""
        if action == ACTION_WAIT or self._paper_engine is None:
            return action, ""
        margin_required = float(position.get("margin_required") or 0)
        if margin_required <= 0:
            return action, ""
        ok, reason = self._paper_engine.can_allocate_margin(
            margin_required, self.account_balance
        )
        if not ok:
            return ACTION_WAIT, reason
        return action, ""

    # ============================================================
    # 决策：方向 / 强度 / 投票
    # ============================================================
    @staticmethod
    def _parse_ai_action(ai_analysis: Dict) -> str:
        """解析 DeepSeek 输出的买卖方向。"""
        ai_signal = (ai_analysis.get("signal") or "").upper()
        ai_signal_cn = ai_analysis.get("signal") or ""
        if ai_signal_cn in ("买入",) or ai_signal in ("BUY", "LONG"):
            return ACTION_LONG
        if ai_signal_cn in ("卖出",) or ai_signal in ("SELL", "SHORT"):
            return ACTION_SHORT
        return ACTION_WAIT

    @staticmethod
    def _strength_from_confidence(confidence: float) -> str:
        if confidence >= 0.75:
            return STRENGTH_STRONG
        if confidence >= 0.58:
            return STRENGTH_MEDIUM
        return STRENGTH_WEAK

    def _apply_analysis_mode(
        self,
        mode: str,
        vote_action: str,
        vote_strength: str,
        ai_analysis: Dict,
        institutional: Dict,
        technical_combined: Optional[Dict],
        votes: Dict,
    ) -> Tuple[str, str]:
        """GUI 策略模式 combobox → 真正影响 trade_advice 方向。"""
        mode = (mode or "all").lower()
        if mode == "ai_only":
            ai_action = self._parse_ai_action(ai_analysis)
            if ai_action == ACTION_WAIT:
                votes["decision_reason"] = "analysis_mode_ai_wait"
                return ACTION_WAIT, vote_strength
            ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)
            votes["decision_reason"] = "analysis_mode_ai_only"
            return ai_action, self._strength_from_confidence(ai_conf)

        if mode == "institutional_only":
            inst_signal = (institutional.get("consensus_signal") or "HOLD").upper()
            inst_conf = self._safe_float(institutional.get("consensus_confidence"), 0.5)
            votes["decision_reason"] = "analysis_mode_institutional_only"
            if inst_signal == "BUY":
                return ACTION_LONG, self._strength_from_confidence(inst_conf)
            if inst_signal == "SELL":
                return ACTION_SHORT, self._strength_from_confidence(inst_conf)
            return ACTION_WAIT, vote_strength

        if mode == "technical_only":
            combined = technical_combined or {}
            sig = (combined.get("final_signal") or "HOLD").upper()
            conf = self._safe_float(combined.get("confidence"), 0.55)
            votes["decision_reason"] = "analysis_mode_technical_only"
            if sig == "BUY":
                return ACTION_LONG, self._strength_from_confidence(conf)
            if sig == "SELL":
                return ACTION_SHORT, self._strength_from_confidence(conf)
            return ACTION_WAIT, vote_strength

        votes["decision_reason"] = "analysis_mode_fallback_vote"
        return vote_action, vote_strength

    def _decide_action(
        self, ai_analysis: Dict, institutional: Dict,
        dl_signal: Optional[Dict] = None,
        explorer_signal: Optional[Dict] = None,
        regime_multipliers: Optional[Dict] = None,
        bnb_factors: Optional[Dict] = None,
        factor_reliability: Optional[Dict[str, float]] = None,
        sentiment: Optional[Dict] = None,
        learning_insights: Optional[Dict] = None,
    ) -> Tuple[str, str, Dict]:
        """根据 AI + 机构策略 + 深度学习 + 进化策略综合投票决定方向。
        
        v2.5 更新：
        - 四方投票：AI 45% + 机构 25% + DL 15% + 探索器 15%
        - DL/探索器未启用时自动回退到 AI 60% + 机构 40%
        - 市场状态驱动机构策略权重乘数
        """
        ai_signal = (ai_analysis.get("signal") or "").upper()
        ai_signal_cn = ai_analysis.get("signal") or ""
        ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.5)
        inst_conf = self._safe_float(institutional.get("consensus_confidence"), 0.5)

        rel = factor_reliability or {}
        ai_rel = float(rel.get("ai_confidence", 1.0) or 1.0)
        inst_rel = float(
            rel.get("institutional_consensus")
            or rel.get("institutional_vote_ratio")
            or 1.0
        )
        ai_conf *= max(0.5, min(1.5, ai_rel))
        inst_conf *= max(0.5, min(1.5, inst_rel))

        # AI 中英文兼容
        if ai_signal_cn in ("买入",) or ai_signal in ("BUY", "LONG"):
            ai_dir = ACTION_LONG
        elif ai_signal_cn in ("卖出",) or ai_signal in ("SELL", "SHORT"):
            ai_dir = ACTION_SHORT
        else:
            ai_dir = ACTION_WAIT

        inst_signal = (institutional.get("consensus_signal") or "HOLD").upper()
        if inst_signal == "BUY":
            inst_dir = ACTION_LONG
        elif inst_signal == "SELL":
            inst_dir = ACTION_SHORT
        else:
            inst_dir = ACTION_WAIT

        # v2.2: 深度学习信号
        dl_dir = ACTION_WAIT
        dl_conf = 0.0
        if dl_signal:
            dl_raw = (dl_signal.get("signal") or "HOLD").upper()
            dl_conf = self._safe_float(dl_signal.get("confidence"), 0.5)
            if dl_raw in ("BUY", "LONG"):
                dl_dir = ACTION_LONG
            elif dl_raw in ("SELL", "SHORT"):
                dl_dir = ACTION_SHORT
        
        # v2.5: 进化策略信号
        exp_dir = ACTION_WAIT
        exp_conf = 0.0
        if explorer_signal:
            exp_raw = (explorer_signal.get("signal") or "HOLD").upper()
            exp_conf = self._safe_float(explorer_signal.get("confidence"), 0.5)
            if exp_raw in ("BUY", "LONG"):
                exp_dir = ACTION_LONG
            elif exp_raw in ("SELL", "SHORT"):
                exp_dir = ACTION_SHORT

        # 机构策略票数
        buy_signals = int(institutional.get("buy_signals", 0) or 0)
        sell_signals = int(institutional.get("sell_signals", 0) or 0)
        hold_signals = int(institutional.get("hold_signals", 0) or 0)
        total = max(1, buy_signals + sell_signals + hold_signals)

        # v2.2: 市场状态权重乘数（应用于机构置信度）
        regime_factor = 1.0
        if regime_multipliers and not regime_multipliers.get("_global"):
            # 有具体策略乘数时，用平均乘数作为整体因子
            mults = [v for v in regime_multipliers.values() if isinstance(v, (int, float))]
            if mults:
                regime_factor = sum(mults) / len(mults)
        elif regime_multipliers and regime_multipliers.get("_global"):
            regime_factor = regime_multipliers["_global"]

        # v2.5: 加权得分 — 四方投票
        # 全部启用: AI 45% + 机构(×regime) 25% + DL 15% + 探索器 15%
        # 仅DL: AI 50% + 机构 30% + DL 20%
        # 无DL无探索器: AI 60% + 机构 40%
        long_score = 0.0
        short_score = 0.0

        has_dl = dl_dir != ACTION_WAIT and dl_conf >= self.dl_min_confidence
        has_exp = exp_dir != ACTION_WAIT and exp_conf >= self.explorer_min_confidence

        if has_dl and has_exp:
            # 四方投票
            if ai_dir == ACTION_LONG:
                long_score += 0.45 * ai_conf
            elif ai_dir == ACTION_SHORT:
                short_score += 0.45 * ai_conf
            if inst_dir == ACTION_LONG:
                long_score += 0.25 * inst_conf * regime_factor
            elif inst_dir == ACTION_SHORT:
                short_score += 0.25 * inst_conf * regime_factor
            if dl_dir == ACTION_LONG:
                long_score += 0.15 * dl_conf * self.dl_weight
            elif dl_dir == ACTION_SHORT:
                short_score += 0.15 * dl_conf * self.dl_weight
            if exp_dir == ACTION_LONG:
                long_score += 0.15 * exp_conf * self.explorer_weight
            elif exp_dir == ACTION_SHORT:
                short_score += 0.15 * exp_conf * self.explorer_weight
        elif has_dl:
            # AI + 机构 + DL 三方
            if ai_dir == ACTION_LONG:
                long_score += 0.50 * ai_conf
            elif ai_dir == ACTION_SHORT:
                short_score += 0.50 * ai_conf
            if inst_dir == ACTION_LONG:
                long_score += 0.30 * inst_conf * regime_factor
            elif inst_dir == ACTION_SHORT:
                short_score += 0.30 * inst_conf * regime_factor
            if dl_dir == ACTION_LONG:
                long_score += 0.20 * dl_conf * self.dl_weight
            elif dl_dir == ACTION_SHORT:
                short_score += 0.20 * dl_conf * self.dl_weight
        elif has_exp:
            # AI + 机构 + 探索器 三方
            if ai_dir == ACTION_LONG:
                long_score += 0.50 * ai_conf
            elif ai_dir == ACTION_SHORT:
                short_score += 0.50 * ai_conf
            if inst_dir == ACTION_LONG:
                long_score += 0.30 * inst_conf * regime_factor
            elif inst_dir == ACTION_SHORT:
                short_score += 0.30 * inst_conf * regime_factor
            if exp_dir == ACTION_LONG:
                long_score += 0.20 * exp_conf * self.explorer_weight
            elif exp_dir == ACTION_SHORT:
                short_score += 0.20 * exp_conf * self.explorer_weight
        else:
            # 无 DL/探索器，回退到原始加权
            if ai_dir == ACTION_LONG:
                long_score += 0.6 * ai_conf
            elif ai_dir == ACTION_SHORT:
                short_score += 0.6 * ai_conf
            if inst_dir == ACTION_LONG:
                long_score += 0.4 * inst_conf * regime_factor
            elif inst_dir == ACTION_SHORT:
                short_score += 0.4 * inst_conf * regime_factor

        # 机构共识 HOLD 但买卖票偏斜 — 用 BUY+SELL 计票（不被 HOLD 稀释）
        dir_total = max(1, buy_signals + sell_signals)
        if inst_dir == ACTION_WAIT and dir_total >= 3:
            if buy_signals >= sell_signals + 2 and ai_dir != ACTION_SHORT:
                buy_r = buy_signals / dir_total
                long_score += 0.28 * buy_r * max(inst_conf, 0.5)
            elif sell_signals >= buy_signals + 2 and ai_dir != ACTION_LONG:
                sell_r = sell_signals / dir_total
                short_score += 0.28 * sell_r * max(inst_conf, 0.5)

        # AI 持有但文案/趋势偏多或偏空 → 弱方向票
        if ai_dir == ACTION_WAIT:
            trend = str(ai_analysis.get("trend") or "").lower()
            analysis_text = str(ai_analysis.get("analysis") or "").lower()
            bullish = ("看涨", "bullish", "上升", "偏多", "震荡偏多", "略偏强", "偏强")
            bearish = ("看跌", "bearish", "下降", "偏空", "震荡偏空", "略偏弱", "偏弱")
            if any(h in trend or h in analysis_text for h in bullish) and ai_conf >= 0.30:
                long_score += 0.18 * max(ai_conf, 0.35)
            elif any(h in trend or h in analysis_text for h in bearish) and ai_conf >= 0.30:
                short_score += 0.18 * max(ai_conf, 0.35)

        # 机构信念因子 + 模式记忆 → 纳入投票（不只展示）
        learning_insights = learning_insights or {}
        conv = learning_insights.get("institutional_conviction") or {}
        conv_val = float(conv.get("conviction") or conv.get("score") or 0)
        if abs(conv_val) >= 0.04:
            if conv_val > 0:
                long_score += 0.14 * conv_val
            else:
                short_score += 0.14 * abs(conv_val)
        for f in conv.get("factors") or []:
            fname = str(f.get("name") or "")
            fs = float(f.get("score") or 0)
            w = 0.10
            if fname in ("技术指标偏向", "BTC领先指标", "情绪/链上/宏观") and abs(fs) >= 0.15:
                if fs > 0:
                    long_score += w * fs
                else:
                    short_score += w * abs(fs)

        pm = learning_insights.get("pattern_memory") or {}
        matched = int(pm.get("matched") or 0)
        if matched >= 5:
            wr = float(pm.get("win_rate") or pm.get("historical_win_rate") or 0)
            avg_pnl = float(pm.get("avg_pnl_usdt") or pm.get("avg_pnl") or 0)
            if wr >= 0.65:
                boost = 0.16 * wr
                if avg_pnl >= 0:
                    long_score += boost
                else:
                    short_score += boost
            elif wr < 0.35:
                penalty = 0.16 * (0.35 - wr)
                if avg_pnl > 0:
                    long_score = max(0.0, long_score - penalty)
                elif avg_pnl < 0:
                    short_score = max(0.0, short_score - penalty)
                else:
                    long_score = max(0.0, long_score - penalty * 0.5)
                    short_score = max(0.0, short_score - penalty * 0.5)

        try:
            from bnb_quant_tool.learning_analytics import apply_vote_adjustments

            wrc = learning_insights.get("win_rate_context") or {}
            long_score, short_score = apply_vote_adjustments(long_score, short_score, wrc)
        except ImportError:
            pass

        try:
            from bnb_quant_tool.win_rate_strategy import (
                analyze_institutional_consensus,
                merge_consensus_into_win_rate_context,
                resolve_strategy_win_rate_config,
            )

            perf = learning_insights.get("strategy_performance") or {}
            weights = learning_insights.get("strategy_weights") or {}
            sw_cfg = resolve_strategy_win_rate_config(
                (learning_insights.get("app_config") or {})
            )
            consensus = analyze_institutional_consensus(
                institutional, perf, weights, sw_cfg
            )
            if consensus.get("long_penalty") or consensus.get("short_penalty"):
                long_score = max(0.0, long_score + float(consensus.get("long_boost") or 0)
                                 - float(consensus.get("long_penalty") or 0))
                short_score = max(0.0, short_score + float(consensus.get("short_boost") or 0)
                                  - float(consensus.get("short_penalty") or 0))
            wrc = merge_consensus_into_win_rate_context(
                learning_insights.get("win_rate_context") or {}, consensus
            )
            learning_insights["win_rate_context"] = wrc
            learning_insights["strategy_consensus"] = consensus
        except ImportError:
            pass

        # BNB 专属因子：trade_bias / bnb_score 纳入投票（原先仅展示未参与决策）
        bnb_factors = bnb_factors or {}
        trade_bias = (bnb_factors.get("trade_bias") or "WAIT").upper()
        bnb_score = float(bnb_factors.get("bnb_score") or 0)
        bnb_bias_weight = 0.12
        if trade_bias == ACTION_LONG:
            long_score += bnb_bias_weight * min(1.0, max(0.35, abs(bnb_score) + 0.35))
        elif trade_bias == ACTION_SHORT:
            short_score += bnb_bias_weight * min(1.0, max(0.35, abs(bnb_score) + 0.35))
        elif abs(bnb_score) >= 0.20:
            if bnb_score > 0:
                long_score += 0.08 * min(1.0, bnb_score / 0.35)
            else:
                short_score += 0.08 * min(1.0, abs(bnb_score) / 0.35)

        # 结构性策略层 — Funding carry 软投票
        structural_vote = {}
        try:
            from bnb_quant_tool.structural_strategies import compute_structural_vote
            structural_vote = compute_structural_vote(
                sentiment=sentiment,
                bnb_factors=bnb_factors,
                config=getattr(self, "_structural_config", None) or {},
            )
            ls = float(structural_vote.get("long_score") or 0)
            ss = float(structural_vote.get("short_score") or 0)
            long_score += ls
            short_score += ss
        except Exception as e:
            logger.debug("structural_vote: %s", e)

        threshold = self.direction_vote_threshold
        net = long_score - short_score
        decision_reason = "vote_clear"
        explicit_tiebreak = min(
            self.ai_tiebreak_min_confidence,
            max(self.min_confidence, 0.50),
        )

        if net > threshold:
            action = ACTION_LONG
        elif net < -threshold:
            action = ACTION_SHORT
        elif ai_dir != ACTION_WAIT and ai_conf >= explicit_tiebreak:
            action = ai_dir
            decision_reason = (
                "ai_tiebreak"
                if ai_conf >= self.ai_tiebreak_min_confidence
                else "ai_explicit_tiebreak"
            )
        elif inst_dir != ACTION_WAIT and inst_conf >= self.inst_tiebreak_min_confidence:
            action = inst_dir
            decision_reason = "inst_tiebreak"
        elif trade_bias == ACTION_SHORT and short_score >= long_score:
            ec = bnb_factors.get("event_cycle") or {}
            me = bnb_factors.get("mining_event") or {}
            if ec.get("suggest_short") or me.get("suggest_hedge_short") or bnb_score <= -0.25:
                action = ACTION_SHORT
                decision_reason = "bnb_trade_bias"
        elif trade_bias == ACTION_LONG and long_score >= short_score and bnb_score >= 0.25:
            action = ACTION_LONG
            decision_reason = "bnb_trade_bias"
        else:
            action = ACTION_WAIT
            decision_reason = "vote_tie"

        # 强度：根据综合得分
        net = abs(long_score - short_score)
        if net >= 0.5:
            strength = STRENGTH_STRONG
        elif net >= 0.3:
            strength = STRENGTH_MEDIUM
        else:
            strength = STRENGTH_WEAK

        votes = {
            "ai_direction": ai_dir,
            "ai_confidence": round(ai_conf, 3),
            "institutional_direction": inst_dir,
            "institutional_confidence": round(inst_conf, 3),
            "institutional_distribution": {
                "buy": buy_signals, "sell": sell_signals, "hold": hold_signals, "total": total
            },
            "dl_direction": dl_dir,
            "dl_confidence": round(dl_conf, 3),
            "regime_factor": round(regime_factor, 3),
            "long_score": round(long_score, 3),
            "short_score": round(short_score, 3),
            "bnb_trade_bias": trade_bias,
            "bnb_score": round(bnb_score, 3),
            "vote_threshold": round(threshold, 3),
            "decision_reason": decision_reason,
            "structural_vote": structural_vote,
            "win_rate_context": learning_insights.get("win_rate_context") or {},
        }
        return action, strength, votes

    # ============================================================
    # 学习系统调整
    # ============================================================
    def _apply_mtf_filter(self, action: str, mtf_action: str) -> Tuple[str, str]:
        """多周期共振过滤器：
        - mtf 有明确方向且与当前 action 相反 → 强制转 WAIT
        - mtf 是 WAIT/空 → 不介入
        - mtf 与 action 一致 → 不介入
        """
        if not mtf_action or mtf_action == "WAIT":
            return action, ""
        if action == ACTION_WAIT:
            return action, ""
        if (action == ACTION_LONG and mtf_action == "SHORT") or \
           (action == ACTION_SHORT and mtf_action == "LONG"):
            return ACTION_WAIT, f"多周期方向 ({mtf_action}) 与当前信号冲突，取消开单"
        return action, ""

    def _apply_news_filter(
        self, action: str, news_summary: Dict, bnb_factors: Optional[Dict] = None,
    ) -> Tuple[str, str]:
        """新闻过滤器（含多源可信度交叉验证）。"""
        bnb_factors = bnb_factors or {}
        cred = bnb_factors.get("news_credibility") or {}
        if cred.get("block_extreme_news_filter") and cred.get("regime_impact") == "NOISE":
            return action, ""

        if not news_summary:
            return action, ""
        polarity = str(news_summary.get("polarity", "neutral")).lower()
        try:
            confidence = float(news_summary.get("confidence", 0.0) or 0.0)
        except (ValueError, TypeError):
            confidence = 0.0

        # 仅多源验证的 PANIC 才触发极端拦截
        if cred.get("regime_impact") == "PANIC" and cred.get("verified_panic_count", 0) >= 1:
            if action == ACTION_LONG:
                return ACTION_WAIT, "多源验证 PANIC 事件，取消做多"
        elif cred.get("regime_impact") == "NOISE":
            return action, ""

        if polarity == "neutral" or confidence < self.news_filter_threshold:
            return action, ""
        if action == ACTION_LONG and polarity == "bearish":
            return ACTION_WAIT, f"新闻利空 (置信 {confidence:.0%}) 与买冲突，取消开单"
        if action == ACTION_SHORT and polarity == "bullish":
            return ACTION_WAIT, f"新闻利好 (置信 {confidence:.0%}) 与卖冲突，取消开单"
        return action, ""

    def _apply_news_adjustment(
        self, action: str, conservativeness: float, news_summary: Dict
    ) -> float:
        """新闻修正仓位保守度：
        - 顺势且高置信 → 仓位 *1.1
        - 轻微逆势 → 仓位 *0.85
        - 仓位保守度上限 1.2，下限 0.2
        """
        if not news_summary:
            return conservativeness
        polarity = str(news_summary.get("polarity", "neutral")).lower()
        try:
            confidence = float(news_summary.get("confidence", 0.0) or 0.0)
        except (ValueError, TypeError):
            confidence = 0.0
        if polarity == "neutral":
            return conservativeness
        if action == ACTION_LONG and polarity == "bullish" and confidence >= 0.6:
            return min(1.2, conservativeness * 1.1)
        if action == ACTION_SHORT and polarity == "bearish" and confidence >= 0.6:
            return min(1.2, conservativeness * 1.1)
        if action == ACTION_LONG and polarity == "bearish":
            return max(0.2, conservativeness * 0.85)
        if action == ACTION_SHORT and polarity == "bullish":
            return max(0.2, conservativeness * 0.85)
        return conservativeness

    def _apply_sentiment_adjustment(
        self, action: str, conservativeness: float, sentiment_score: float
    ) -> float:
        """情绪修正仓位保守度：
        - 买遇到极度贪婪 (sentiment<-0.4) → 降50%
        - 卖遇到极度恐惧 (sentiment>+0.4) → 降50%
        - 顺势 (同方向) → 不调整
        """
        if not sentiment_score:
            return conservativeness
        if action == ACTION_LONG and sentiment_score <= -0.4:
            return max(0.2, conservativeness * 0.5)
        if action == ACTION_SHORT and sentiment_score >= 0.4:
            return max(0.2, conservativeness * 0.5)
        # 轻度逆向
        if action == ACTION_LONG and sentiment_score <= -0.15:
            return max(0.3, conservativeness * 0.85)
        if action == ACTION_SHORT and sentiment_score >= 0.15:
            return max(0.3, conservativeness * 0.85)
        return conservativeness

    def _apply_onchain_adjustment(
        self, action: str, conservativeness: float, onchain_score: float
    ) -> float:
        """链上长周期修正：MVRV 高估 + 交易所净流入 → 做多更保守。"""
        if not onchain_score:
            return conservativeness
        if action == ACTION_LONG and onchain_score <= -0.35:
            return max(0.25, conservativeness * 0.55)
        if action == ACTION_SHORT and onchain_score >= 0.35:
            return max(0.25, conservativeness * 0.55)
        if action == ACTION_LONG and onchain_score <= -0.12:
            return max(0.35, conservativeness * 0.85)
        if action == ACTION_SHORT and onchain_score >= 0.12:
            return max(0.35, conservativeness * 0.85)
        return conservativeness

    def _apply_macro_adjustment(
        self,
        action: str,
        conservativeness: float,
        macro_score: float,
        macro: Dict,
    ) -> float:
        """宏观 risk-off 环境压缩仓位；高波动率额外降杠杆。"""
        if macro_score:
            if action == ACTION_LONG and macro_score <= -0.35:
                conservativeness = max(0.2, conservativeness * 0.5)
            elif action == ACTION_SHORT and macro_score >= 0.35:
                conservativeness = max(0.2, conservativeness * 0.5)
            elif action == ACTION_LONG and macro_score <= -0.12:
                conservativeness = max(0.35, conservativeness * 0.8)
            elif action == ACTION_SHORT and macro_score >= 0.12:
                conservativeness = max(0.35, conservativeness * 0.8)

        vol = (macro or {}).get("crypto_volatility") or {}
        if vol.get("level") == "极高" and action in (ACTION_LONG, ACTION_SHORT):
            conservativeness = max(0.25, conservativeness * 0.65)
        elif vol.get("level") == "偏高" and action in (ACTION_LONG, ACTION_SHORT):
            conservativeness = max(0.35, conservativeness * 0.85)
        return conservativeness

    def _apply_mining_event_filter(
        self, action: str, bnb_factors: Dict,
    ) -> Tuple[str, str]:
        """Launchpool/Megadrop 挖矿周期因子过滤。"""
        me = (bnb_factors or {}).get("mining_event") or {}
        if not me or action == ACTION_WAIT:
            return action, ""
        if me.get("block_long") and action == ACTION_LONG:
            hrs = me.get("hours_to_end")
            detail = f"（距结束 {hrs:.0f}h）" if hrs is not None else ""
            return ACTION_WAIT, f"挖矿事件因子：禁止追多{detail}"
        if me.get("suggest_hedge_short") and action == ACTION_LONG:
            if self.follow_ai_direction:
                return ACTION_WAIT, "挖矿解锁砸盘期：AI建议做多，风控暂停"
            return ACTION_SHORT, "挖矿解锁砸盘期：禁止追多，转做空对冲"
        return action, ""

    def _apply_bnb_regulatory_filter(
        self, action: str, bnb_factors: Dict
    ) -> Tuple[str, str]:
        """BNB 专属监管/平台风险硬过滤 — 比通用新闻更严格。"""
        if not bnb_factors or bnb_factors.get("enabled") is False:
            return action, ""
        nlp = bnb_factors.get("announcement_nlp") or {}
        impact = nlp.get("impact_level", "low")
        score = float(nlp.get("score") or 0)
        conf = float(nlp.get("confidence") or 0)
        if action == ACTION_WAIT:
            return action, ""
        if impact == "critical" and score < -0.25 and conf >= 0.5:
            if action == ACTION_LONG:
                if self.follow_ai_direction:
                    return ACTION_WAIT, (
                        f"BNB监管/平台重大利空 ({nlp.get('dominant_category', '?')}) "
                        f"score={score:+.2f}，AI建议做多，风控暂停"
                    )
                return ACTION_SHORT, (
                    f"BNB监管/平台重大利空 ({nlp.get('dominant_category', '?')}) "
                    f"score={score:+.2f}，转做空"
                )
        if impact == "high" and score < -0.4 and conf >= 0.55 and action == ACTION_LONG:
            if self.follow_ai_direction:
                return ACTION_WAIT, f"币安专属 NLP 高影响利空 (score={score:+.2f})，AI建议做多，风控暂停"
            return ACTION_SHORT, f"币安专属 NLP 高影响利空 (score={score:+.2f})，转做空"
        return action, ""

    def _apply_event_cycle_filter(
        self, action: str, bnb_factors: Dict,
    ) -> Tuple[str, str, Dict]:
        """BNB 事件四阶段生命周期门控。"""
        event_cycle = (bnb_factors or {}).get("event_cycle") or getattr(self, "_event_cycle", None) or {}
        if not event_cycle or event_cycle.get("enabled") is False:
            return action, "", event_cycle

        phase = event_cycle.get("phase", "normal")
        block_long = bool(
            event_cycle.get("block_long") or getattr(self, "_event_block_long", False)
        )

        if action == ACTION_LONG and block_long:
            msg = (
                f"BNB事件周期[{event_cycle.get('phase_label', phase)}] "
                f"解锁砸盘窗口：禁止追多"
            )
            if event_cycle.get("suggest_short") and not self.follow_ai_direction:
                msg += "，转做空"
                return ACTION_SHORT, msg, event_cycle
            if self.follow_ai_direction:
                msg += "，AI建议做多，风控暂停"
            return ACTION_WAIT, msg, event_cycle

        if (
            action == ACTION_WAIT
            and block_long
            and event_cycle.get("suggest_short")
            and not self.follow_ai_direction
        ):
            msg = (
                f"BNB事件周期[{event_cycle.get('phase_label', phase)}] "
                f"解锁砸盘窗口：做空偏向"
            )
            return ACTION_SHORT, msg, event_cycle

        if phase == "staking_lock" and action in (ACTION_LONG, ACTION_SHORT):
            # 不硬拦截，由 strategy_mode=grid_or_hold 在 advice 中提示
            pass

        return action, "", event_cycle

    def _apply_risk_sentry_filter(self, action: str, bnb_factors: Dict) -> Tuple[str, str]:
        """资金费率极值 → 强制拦截做多。"""
        rs = (bnb_factors or {}).get("risk_sentry") or {}
        if not rs or rs.get("enabled") is False:
            return action, ""
        if action != ACTION_LONG:
            return action, ""

        if rs.get("block_long"):
            fr = rs.get("funding_extreme") or {}
            if fr.get("block_long"):
                return ACTION_WAIT, (
                    f"资金费率极值 {fr.get('rate_pct', '?')}% > "
                    f"{fr.get('threshold_pct', 0.1)}%：散户狂热做多，插针爆仓前兆，拦截做多"
                )
            liq = rs.get("liquidity_guard") or {}
            if liq.get("block_long"):
                return ACTION_WAIT, liq.get("interpretation", "流动性空洞/插针保护，暂停做多")
            if rs.get("chain_health_block"):
                return ACTION_WAIT, rs.get(
                    "interpretation", "BNB Chain 安全哨兵：黑客/停链风险，拦截做多"
                )
            return ACTION_WAIT, rs.get("interpretation", "BNB风控哨兵拦截做多")
        return action, ""

    def _apply_bnb_factors_adjustment(
        self, action: str, conservativeness: float, bnb_factors: Dict
    ) -> float:
        """Launchpool 高 APY / 正 Alpha → 提高仓位；监管利空 → 压缩。"""
        if not bnb_factors or bnb_factors.get("enabled") is False:
            return conservativeness
        boost = float(bnb_factors.get("position_boost") or 1.0)
        event_factor = float(getattr(self, "_event_position_factor", 1.0) or 1.0)
        boost *= event_factor
        rs = (bnb_factors or {}).get("risk_sentry") or {}
        if rs.get("position_scale"):
            boost *= float(rs["position_scale"])
        bnb_score = float(bnb_factors.get("bnb_score") or 0)
        if action == ACTION_LONG and bnb_score > 0.2:
            conservativeness = min(1.25, conservativeness * boost)
        elif action == ACTION_SHORT and bnb_score < -0.2:
            conservativeness = min(1.2, conservativeness * min(boost, 1.1))
        elif action == ACTION_LONG and bnb_score < -0.35:
            conservativeness = max(0.2, conservativeness * 0.55)
        elif action == ACTION_SHORT and bnb_score > 0.35:
            conservativeness = max(0.2, conservativeness * 0.55)
        return conservativeness

    def _apply_learning_adjustment(
        self, raw_strength: str, learning_insights: Dict
    ) -> Tuple[str, float]:
        """根据历史胜率/PnL 调整信号强度，返回(调整后强度, 保守度系数 0-1)."""
        accuracy = self._safe_float(learning_insights.get("overall_accuracy"), 0.0)
        avg_pnl = self._safe_float(learning_insights.get("avg_pnl"), 0.0)
        feedbacks = int(learning_insights.get("total_feedbacks", 0) or 0)
        maturity = (learning_insights.get("learning_maturity") or "BEGINNER").upper()

        # 默认完全信任（系数=1.0），数据越差越保守（系数越小）
        c = 1.0
        if maturity == "BEGINNER":
            c *= 0.6  # 数据不足，仓位减半
        elif maturity == "INTERMEDIATE":
            c *= 0.85
        elif maturity == "ADVANCED":
            c *= 0.95

        # 历史胜率影响
        if feedbacks >= 10:
            if accuracy < 0.4:
                c *= 0.5
            elif accuracy < 0.5:
                c *= 0.75
            elif accuracy >= 0.65:
                c *= 1.1

        # 近期 PnL 影响
        if avg_pnl < -1.0:
            c *= 0.7
        elif avg_pnl > 1.0:
            c *= 1.05

        # 上下限
        c = max(0.2, min(c, 1.2))

        # 强度衰减：保守度低则强度降级
        adj = raw_strength
        if c < 0.5 and raw_strength == STRENGTH_STRONG:
            adj = STRENGTH_MEDIUM
        if c < 0.4 and raw_strength == STRENGTH_MEDIUM:
            adj = STRENGTH_WEAK

        return adj, c

    # ============================================================
    # 价格计算
    # ============================================================
    def _calc_prices(
        self, action: str, current_price: float, atr: float, ai_analysis: Dict,
        adaptive_sl_mult: float = None,
    ) -> Dict:
        """计算入场区间、止损、分批止盈。优先用 AI 给的价格，缺失则用 ATR 倍数。
        
        v2.0 优化：
        - 使用 adaptive_sl_mult（波动率自适应）
        - TP1 更近确保落袋（数据：仅14.9%到达TP3）
        """
        sl_mult = adaptive_sl_mult if adaptive_sl_mult is not None else self.atr_sl_mult
        
        if action == ACTION_WAIT:
            return {
                "entry_low": None, "entry_high": None, "entry_mid": None,
                "stop_loss": None,
                "tp1": None, "tp2": None, "tp3": None,
            }

        ai_entry = self._safe_float(ai_analysis.get("entry_price"), None)
        ai_sl = self._safe_float(ai_analysis.get("stop_loss"), None)
        ai_tp = self._safe_float(ai_analysis.get("take_profit"), None)

        # 入场：AI 价偏离现价过大则改用现价，避免幻想成交
        entry_mid = current_price
        if ai_entry and ai_entry > 0 and current_price > 0:
            drift = abs(ai_entry - current_price) / current_price
            if drift <= 0.01:
                entry_mid = ai_entry
            else:
                logger.info(
                    "AI entry_price=%.4f 偏离现价 %.2f%%，改用现价 %.4f",
                    ai_entry, drift * 100, current_price,
                )
        buf = entry_mid * self.entry_buffer_pct
        if action == ACTION_LONG:
            entry_low = round(entry_mid - buf, 4)
            entry_high = round(entry_mid + buf * 0.5, 4)
        else:  # SHORT
            entry_low = round(entry_mid - buf * 0.5, 4)
            entry_high = round(entry_mid + buf, 4)

        # 止损：ATR 自适应 + 最宽下限；AI 止损仅在其更宽时采用
        stop_loss = self._resolve_stop_loss(
            action, entry_mid, atr, sl_mult, ai_sl if self.use_ai_stop_loss else None,
        )

        # 分批止盈：TP1 / TP2 / TP3
        # v2.0: TP1 更近确保落袋，TP3 改为移动止盈概念
        if action == ACTION_LONG:
            tp1 = entry_mid + atr * self.atr_tp1_mult
            tp2 = entry_mid + atr * self.atr_tp2_mult
            tp3 = entry_mid + atr * self.atr_tp3_mult
            # 如果 AI 给了 take_profit 且优于 ATR 计算，用 AI 的作为 TP2 锚点
            if ai_tp and ai_tp > entry_mid:
                tp2 = max(tp2, ai_tp)
                tp3 = max(tp3, ai_tp * 1.02)
        else:
            tp1 = entry_mid - atr * self.atr_tp1_mult
            tp2 = entry_mid - atr * self.atr_tp2_mult
            tp3 = entry_mid - atr * self.atr_tp3_mult
            if ai_tp and ai_tp < entry_mid:
                tp2 = min(tp2, ai_tp)
                tp3 = min(tp3, ai_tp * 0.98)

        return {
            "entry_low": round(entry_low, 4),
            "entry_high": round(entry_high, 4),
            "entry_mid": round(entry_mid, 4),
            "stop_loss": round(stop_loss, 4),
            "tp1": round(tp1, 4),
            "tp2": round(tp2, 4),
            "tp3": round(tp3, 4),
            "atr": round(atr, 4),
            "atr_pct": round(atr / current_price * 100, 2) if current_price > 0 else 0,
            "adaptive_sl_mult": round(sl_mult, 2),
            "tp_split": self.tp_split,
        }

    def _resolve_stop_loss(
        self,
        action: str,
        entry: float,
        atr: float,
        sl_mult: float,
        ai_sl: Optional[float] = None,
    ) -> float:
        """计算止损价：取 ATR/百分比下限与 AI 建议中更宽（更安全）的一个。"""
        effective_mult = max(sl_mult, self.min_sl_atr_mult)
        if action == ACTION_LONG:
            atr_sl = entry - atr * effective_mult
            pct_sl = entry * (1 - self.min_sl_pct)
            base_sl = min(atr_sl, pct_sl)
            if ai_sl and self._sl_is_reasonable(action, entry, ai_sl):
                stop_loss = min(ai_sl, base_sl)
            else:
                stop_loss = base_sl
        elif action == ACTION_SHORT:
            atr_sl = entry + atr * effective_mult
            pct_sl = entry * (1 + self.min_sl_pct)
            base_sl = max(atr_sl, pct_sl)
            if ai_sl and self._sl_is_reasonable(action, entry, ai_sl):
                stop_loss = max(ai_sl, base_sl)
            else:
                stop_loss = base_sl
        else:
            return entry
        return round(stop_loss, 4)

    def _sl_is_reasonable(self, action: str, entry: float, sl: float) -> bool:
        """AI 给的止损方向是否合理"""
        if action == ACTION_LONG:
            return 0 < sl < entry
        if action == ACTION_SHORT:
            return sl > entry > 0
        return False

    # ============================================================
    # 风险回报比 / 仓位
    # ============================================================
    def _calc_risk_reward(self, action: str, prices: Dict) -> Optional[float]:
        if action == ACTION_WAIT:
            return None
        entry = prices.get("entry_mid")
        sl = prices.get("stop_loss")
        tp2 = prices.get("tp2")  # 用 TP2 估算综合风险回报比
        if not (entry and sl and tp2):
            return None
        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        if risk <= 0:
            return None
        return reward / risk

    def _calc_position(self, action: str, prices: Dict, conservativeness: float, leverage: int = 1) -> Dict:
        if action == ACTION_WAIT:
            return {
                "usdt_amount": 0.0, "quantity": 0.0,
                "risk_amount": 0.0, "leverage_suggest": 1,
                "margin_required": 0.0,
                "note": "信号不足，建议观望"
            }
        entry = prices.get("entry_mid") or 0
        sl = prices.get("stop_loss") or 0
        if entry <= 0 or sl <= 0:
            return {
                "usdt_amount": 0.0, "quantity": 0.0,
                "risk_amount": 0.0, "leverage_suggest": 1,
                "margin_required": 0.0,
                "note": "价格无效"
            }
        per_unit_risk = abs(entry - sl)
        if per_unit_risk <= 0:
            return {
                "usdt_amount": 0.0, "quantity": 0.0,
                "risk_amount": 0.0, "leverage_suggest": 1,
                "margin_required": 0.0,
                "note": "止损与入场价相同"
            }

        # 单笔风险金额（按学习保守度调整）
        risk_amount = self.account_balance * self.risk_per_trade * conservativeness
        quantity = risk_amount / per_unit_risk
        usdt_amount = quantity * entry

        # 限制不超过 max_position_pct
        cap_amount = self.account_balance * self.max_position_pct
        if usdt_amount > cap_amount:
            usdt_amount = cap_amount
            quantity = usdt_amount / entry

        # 实际需要的保证金（若用合约杠杆）
        margin_required = usdt_amount / max(1, leverage)

        return {
            "usdt_amount": round(usdt_amount, 2),
            "quantity": round(quantity, 4),
            "risk_amount": round(risk_amount, 2),
            "leverage_suggest": leverage,
            "margin_required": round(margin_required, 2),
            "note": f"按账户余额 {self.account_balance:.0f} USDT、单笔风险 "
                    f"{self.risk_per_trade*100:.1f}% × 保守度 {conservativeness:.2f}"
        }

    # ============================================================
    # 下单门槛
    # ============================================================
    def _gate_check(
        self, action: str, strength: str, rr: Optional[float],
        ai_analysis: Dict, learning_insights: Dict,
        gate_relaxation: float = 0.0,
        gate_tightening: float = 0.0,
        votes: Optional[Dict] = None,
    ) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        if action == ACTION_WAIT:
            voted_action = (votes or {}).get("decided_action", ACTION_WAIT)
            reason = (votes or {}).get("decision_reason") or ""
            if voted_action == ACTION_WAIT:
                ls = float((votes or {}).get("long_score") or 0)
                ss = float((votes or {}).get("short_score") or 0)
                th = float((votes or {}).get("vote_threshold") or self.direction_vote_threshold)
                ai_dir = (votes or {}).get("ai_direction")
                ai_conf = float((votes or {}).get("ai_confidence") or 0)
                if reason == "ai_hold":
                    reasons.append(
                        f"AI 建议持有 (block_reason=ai_hold)：跟单模式下以 AI HOLD 为准，"
                        f"非综合投票否决（参考票 多 {ls:.2f} / 空 {ss:.2f}）"
                    )
                elif ai_dir in (ACTION_LONG, ACTION_SHORT) and ai_conf >= max(self.min_confidence, 0.50):
                    ai_cn = "做多" if ai_dir == ACTION_LONG else "做空"
                    reasons.append(
                        f"综合投票未分出方向（多 {ls:.2f} / 空 {ss:.2f}，"
                        f"差 {abs(ls - ss):.2f} < {th:.2f}），但 AI 明确建议{ai_cn} "
                        f"({ai_conf:.0%}) — 请检查投票/门控参数"
                    )
                else:
                    reasons.append(
                        f"综合投票未分出方向（多 {ls:.2f} / 空 {ss:.2f}，"
                        f"差 {abs(ls - ss):.2f} < 阈值 {th:.2f}）"
                    )
                return False, reasons
            # 投票有方向但被下游过滤器置为 WAIT，具体原因由 caller 追加
            return False, reasons

        ai_conf = self._safe_float(ai_analysis.get("confidence"), 0.0)
        conf_boost = float(getattr(self, "_event_confidence_boost", 0) or 0)
        ai_conf_adj = ai_conf + conf_boost
        effective_min_conf = max(
            0.45,
            self.min_confidence - max(0.0, gate_relaxation) + max(0.0, gate_tightening),
        )
        if votes:
            ls = float(votes.get("long_score") or 0)
            ss = float(votes.get("short_score") or 0)
            if abs(ls - ss) >= 0.12:
                effective_min_conf = max(0.38, effective_min_conf - 0.12)
        if ai_conf_adj < effective_min_conf:
            suffix = ""
            if gate_relaxation > 0 or gate_tightening > 0:
                suffix = f"，有效门槛 {effective_min_conf:.2f}"
            reasons.append(
                f"AI 置信度过低（{ai_conf:.2f} < {self.min_confidence:.2f}{suffix}）"
            )

        if strength == STRENGTH_WEAK:
            if not self.allow_weak_signal or ai_conf_adj < effective_min_conf:
                reasons.append("综合信号强度过弱")

        if rr is not None and rr < self.min_rr:
            if not getattr(self, "defer_gross_rr_to_net_gate", True):
                reasons.append(f"风险回报比过低（{rr:.2f} < {self.min_rr:.2f}）")
            # else: 交给 apply_net_rr_gate，此处不硬失败

        # 历史近期严重亏损时禁止
        avg_pnl = self._safe_float(learning_insights.get("avg_pnl"), 0.0)
        feedbacks = int(learning_insights.get("total_feedbacks", 0) or 0)
        if feedbacks >= 10 and avg_pnl < -3.0:
            reasons.append(f"近期 AI 平均 PnL={avg_pnl:.2f}% 过差，建议先暂停下单")

        paper = learning_insights.get("paper_trading") or {}
        consec = int(paper.get("consecutive_losses") or 0)
        cb_on = (
            self._circuit_breaker is not None
            and bool(getattr(self._circuit_breaker, "enabled", True))
        )
        gate_consec = int(getattr(self, "_gate_consec_loss_block", 5))
        if cb_on and gate_consec > 0 and consec >= gate_consec:
            reasons.append(f"模拟盘连亏 {consec} 笔，暂停新开仓")
        closed = int(paper.get("closed_trades") or 0)
        if closed >= 30:
            wr = float(paper.get("win_rate") or 0)
            if wr < 0.40:
                reasons.append(f"模拟盘胜率 {wr:.1%} 过低（{closed}笔），提高门槛")

        return (len(reasons) == 0), reasons

    # ============================================================
    # 取消条件
    # ============================================================
    def _build_invalidation(self, action: str, prices: Dict, atr: float) -> List[str]:
        if action == ACTION_WAIT:
            return ["信号不成立，无需挂单"]
        sl = prices.get("stop_loss")
        entry_mid = prices.get("entry_mid") or 0
        rules = []
        if sl is not None:
            rules.append(f"价格触及止损 {sl} 立即离场")
        # 反向跳空 1.5 ATR 也作废
        if action == ACTION_LONG:
            rules.append(f"若收盘价跌破 {round(entry_mid - atr*1.0, 4)} 取消挂单")
        else:
            rules.append(f"若收盘价升破 {round(entry_mid + atr*1.0, 4)} 取消挂单")
        rules.append("超过有效期未成交则取消，重新评估")
        return rules

    # ============================================================
    # 推理依据
    # ============================================================
    def _build_reasons(
        self, action: str, ai_analysis: Dict, institutional: Dict, indicators: Dict
    ) -> List[str]:
        reasons: List[str] = []
        if ai_analysis.get("analysis"):
            reasons.append("AI: " + str(ai_analysis.get("analysis"))[:160])
        consensus = institutional.get("consensus_signal")
        if consensus:
            reasons.append(
                f"机构策略共识: {consensus} (置信度 "
                f"{self._safe_float(institutional.get('consensus_confidence'), 0):.2f}, "
                f"BUY={institutional.get('buy_signals',0)} "
                f"SELL={institutional.get('sell_signals',0)} "
                f"HOLD={institutional.get('hold_signals',0)})"
            )
        rsi = indicators.get("RSI")
        macd = indicators.get("MACD")
        if rsi is not None:
            reasons.append(f"RSI={self._safe_float(rsi,0):.1f}")
        if macd is not None:
            reasons.append(f"MACD={self._safe_float(macd,0):.4f}")
        return reasons

    # ============================================================
    # 文本格式化
    # ============================================================
    def _build_decision_summary(self, advice: Dict) -> str:
        """用一行话概括 AI 判断（区分分析方向与风控结论）。"""
        action = advice.get("action")
        raw = advice.get("raw_action", action)
        ctx = advice.get("execution_context") or {}
        action_text = {
            ACTION_LONG: "做多",
            ACTION_SHORT: "做空",
            ACTION_WAIT: "观望",
        }.get(action, str(action))
        raw_text = {
            ACTION_LONG: "做多",
            ACTION_SHORT: "做空",
            ACTION_WAIT: "观望",
        }.get(raw, str(raw))
        confidence = self._safe_float(advice.get("confidence"), 0.0)
        strength = advice.get("strength") or "未知"
        if ctx.get("follow_reason"):
            gate_part = ctx.get("gate_label") or ("通过" if advice.get("passed_gate") else "拦截")
            return (
                f"分析方向 {raw_text}，风控建议 {action_text}（门控{gate_part}）。"
                f"置信度 {confidence:.0%}，强度 {strength}。{ctx['follow_reason']}"
            )
        if action == ACTION_WAIT and raw in (ACTION_LONG, ACTION_SHORT):
            reasons = advice.get("gate_reasons") or ["信号未达门槛"]
            return (
                f"分析方向 {raw_text}，风控建议观望。置信度 {confidence:.0%}，"
                f"强度 {strength}。拦截: {'；'.join(reasons[:2])}"
            )
        if action == ACTION_WAIT:
            reasons = advice.get("gate_reasons") or ["信号不足"]
            return (
                f"AI 当前结论: {action_text}。综合置信度 {confidence:.0%}，"
                f"信号强度 {strength}。原因: {'；'.join(reasons[:2])}"
            )
        return (
            f"AI 当前结论: {action_text}。综合置信度 {confidence:.0%}，"
            f"信号强度 {strength}，满足下单条件。"
        )

    def _build_execution_summary(self, advice: Dict) -> str:
        """用一行话概括执行层信息。"""
        ctx = advice.get("execution_context") or {}
        effective = ctx.get("effective_direction") or advice.get("action")
        if ctx.get("will_follow") and effective in (ACTION_LONG, ACTION_SHORT):
            prices = advice.get("prices") or {}
            position = advice.get("position") or {}
            tag = " (风控观望，按分析方向执行)" if advice.get("action") == ACTION_WAIT else ""
            return (
                f"跟单方向 {effective}{tag}: 入场 {prices.get('entry_mid')}，"
                f"止损 {prices.get('stop_loss')}，TP2 {prices.get('tp2')}，"
                f"建议仓位 {position.get('usdt_amount')} USDT。"
            )
        if advice.get("action") == ACTION_WAIT:
            reasons = advice.get("gate_reasons") or ["信号不足"]
            return "暂不生成执行参数；原因: " + "；".join(reasons[:3])
        prices = advice.get("prices") or {}
        position = advice.get("position") or {}
        return (
            f"执行计划: 入场 {prices.get('entry_mid')}，止损 {prices.get('stop_loss')}，"
            f"TP2 {prices.get('tp2')}，建议仓位 {position.get('usdt_amount')} USDT。"
        )

    def format_order_text(self, advice: Dict) -> str:
        """生成可直接复制到交易所的下单文本（精简版，一眼看清）"""
        ctx = advice.get("execution_context") or {}
        effective = ctx.get("effective_direction") or advice.get("action")
        if advice.get("action") == ACTION_WAIT and effective in (ACTION_LONG, ACTION_SHORT):
            lines = [
                f"[{advice['symbol']} {advice['timeframe']}] 分析方向: {effective} | 风控: WAIT",
                advice.get("decision_summary") or f"分析方向 {effective}，风控建议观望。",
                "跟单说明: " + (ctx.get("follow_reason") or "按分析方向执行"),
            ]
            p = advice.get("prices") or {}
            if p.get("entry_mid"):
                lines.append(
                    f"参考入场 {p.get('entry_mid')} SL {p.get('stop_loss')} TP2 {p.get('tp2')}"
                )
            return "\n".join(lines)
        if advice.get("action") == ACTION_WAIT:
            lines = [
                f"[{advice['symbol']} {advice['timeframe']}] AI 结论: WAIT",
                advice.get("decision_summary") or "AI 当前结论: 观望。",
                "阻断原因: " + "；".join(advice.get("gate_reasons") or ["信号不足"]),
            ]
            return "\n".join(lines)
        p = advice.get("prices", {})
        pos = advice.get("position", {})
        action_cn = "买 LONG" if advice["action"] == ACTION_LONG else "卖 SHORT"
        cp = self._safe_float(advice.get('current_price'), 0)
        sl = self._safe_float(p.get('stop_loss'), 0)
        tp1 = self._safe_float(p.get('tp1'), 0)
        tp2 = self._safe_float(p.get('tp2'), 0)
        tp3 = self._safe_float(p.get('tp3'), 0)
        # 计算百分比变动
        sl_pct = (sl - cp) / cp * 100 if cp else 0
        tp1_pct = (tp1 - cp) / cp * 100 if cp else 0
        tp2_pct = (tp2 - cp) / cp * 100 if cp else 0
        tp3_pct = (tp3 - cp) / cp * 100 if cp else 0
        lines = [
            f"{'='*48}",
            f"  {advice['symbol']}  {advice['timeframe']}  {action_cn}",
            f"  AI结论: 置信度 {advice['confidence']:.0%} | 强度 {advice['strength']}",
            f"{'='*48}",
            f"  入场价:  {p.get('entry_mid')} ({p.get('entry_low')} ~ {p.get('entry_high')})",
            f"  止损:    {sl}  ({sl_pct:+.1f}%)",
            f"  止盈1:   {tp1}  ({tp1_pct:+.1f}%)  仓位 {self.tp_split.get('tp1', '40%')}",
            f"  止盈2:   {tp2}  ({tp2_pct:+.1f}%)  仓位 {self.tp_split.get('tp2', '35%')}",
            f"  止盈3:   {tp3}  ({tp3_pct:+.1f}%)  仓位 {self.tp_split.get('tp3', '25%')}",
            f"{'-'*48}",
            f"  建议仓位: {pos.get('quantity')} BNB ≈ {pos.get('usdt_amount')} USDT",
            f"  需保证金: {pos.get('margin_required')} USDT (杠杆 {pos.get('leverage_suggest')}x)",
            f"  最大亏损: {pos.get('risk_amount')} USDT",
            f"  盈亏比:   {advice.get('risk_reward_ratio')}",
            f"  有效期:   {advice.get('validity_hours')}h (至 {advice.get('valid_until')})",
            f"{'='*48}",
        ]
        return "\n".join(lines)

    def format_report(self, advice: Dict) -> str:
        """生成完整的报告文本（GUI 显示用，优化可读性）"""
        action = advice.get("action")
        action_cn = {"LONG": "LONG", "SHORT": "SHORT", "WAIT": "WAIT"}.get(action, action)
        raw_action = advice.get("raw_action", action)
        votes = advice.get("votes") or {}
        p = advice.get("prices") or {}
        pos = advice.get("position") or {}
        cp = self._safe_float(advice.get("current_price"), 0)
        sl = self._safe_float(p.get("stop_loss"), 0)
        tp1 = self._safe_float(p.get("tp1"), 0)
        tp2 = self._safe_float(p.get("tp2"), 0)
        tp3 = self._safe_float(p.get("tp3"), 0)
        sl_pct = (sl - cp) / cp * 100 if cp and sl else 0
        tp1_pct = (tp1 - cp) / cp * 100 if cp and tp1 else 0
        tp2_pct = (tp2 - cp) / cp * 100 if cp and tp2 else 0
        tp3_pct = (tp3 - cp) / cp * 100 if cp and tp3 else 0

        lines: List[str] = []
        lines.append("=" * 64)
        lines.append(f"{advice['symbol']} | {advice['timeframe']} | current={advice['current_price']}")
        lines.append("=" * 64)
        lines.append("")

        lines.append("[AI Core Conclusion]")
        ctx = advice.get("execution_context") or {}
        lines.append(f"- analysis_direction: {ctx.get('analysis_direction', raw_action)}")
        lines.append(f"- risk_action: {action_cn}")
        lines.append(f"- raw_direction: {raw_action}")
        if ctx.get("effective_direction"):
            lines.append(f"- follow_direction: {ctx['effective_direction']}")
        lines.append(f"- confidence: {advice.get('confidence', 0):.0%}")
        lines.append(f"- strength: {advice.get('strength')}")
        lines.append(f"- summary: {advice.get('decision_summary')}")
        if ctx:
            lines.append(f"- follow: {ctx.get('follow_label', '—')}")
        lines.append("")

        if not advice.get("passed_gate"):
            lines.append("[Risk Gate]")
            lines.append(f"- status: {ctx.get('gate_label', 'BLOCKED')}")
            for reason in advice.get("gate_reasons") or []:
                lines.append(f"- reason: {reason}")
            if ctx.get("will_follow"):
                lines.append(f"- note: {ctx.get('follow_reason', '')}")
            lines.append("")

        if action != ACTION_WAIT:
            lines.append("[Execution Plan]")
            lines.append(
                f"- entry: {p.get('entry_mid')} ({p.get('entry_low')} ~ {p.get('entry_high')})"
            )
            lines.append(
                f"- stop_loss: {sl} ({sl_pct:+.1f}%) | adaptive_sl={p.get('adaptive_sl_mult', self.atr_sl_mult):.1f} ATR"
            )
            lines.append(f"- take_profit_1: {tp1} ({tp1_pct:+.1f}%) | size {self.tp_split.get('tp1', '40%')}")
            lines.append(f"- take_profit_2: {tp2} ({tp2_pct:+.1f}%) | size {self.tp_split.get('tp2', '35%')}")
            lines.append(f"- take_profit_3: {tp3} ({tp3_pct:+.1f}%) | size {self.tp_split.get('tp3', '25%')}")
            lines.append(f"- rr: {advice.get('risk_reward_ratio')}")
            lines.append(
                f"- position: {pos.get('quantity')} BNB ~= {pos.get('usdt_amount')} USDT | margin {pos.get('margin_required')} USDT"
            )
            lines.append(f"- max_loss: {pos.get('risk_amount')} USDT")
            lines.append(f"- validity: {advice.get('validity_hours')}h -> {advice.get('valid_until')}")
            lines.append("")
        elif ctx.get("will_follow") and (advice.get("prices") or {}).get("entry_mid"):
            lines.append("[Execution Plan]")
            lines.append(
                f"- note: risk_action=WAIT but follow_direction={ctx.get('effective_direction')}"
            )
            lines.append(
                f"- entry: {p.get('entry_mid')} | stop_loss: {sl} | tp2: {tp2}"
            )
            lines.append("")
        else:
            lines.append("[Execution Plan]")
            lines.append("- skipped: WAIT state, no order parameters should be executed.")
            lines.append("")

        lines.append("[Decision Engine]")
        lines.append(
            f"- AI: {votes.get('ai_direction', '?')} @ {self._safe_float(votes.get('ai_confidence'), 0.0):.0%}"
        )
        lines.append(
            f"- institutional: {votes.get('institutional_direction', '?')} @ {self._safe_float(votes.get('institutional_confidence'), 0.0):.0%}"
        )
        lines.append(
            f"- deep_learning: {votes.get('dl_direction', '?')} @ {self._safe_float(votes.get('dl_confidence'), 0.0):.0%}"
        )
        if advice.get("explorer_signal"):
            exp = advice["explorer_signal"]
            lines.append(
                f"- evolved_strategy: {exp.get('signal', '?')} @ {self._safe_float(exp.get('confidence'), 0.0):.0%}"
            )
        lines.append(
            f"- scoreboard: long {self._safe_float(votes.get('long_score'), 0.0):.2f} | short {self._safe_float(votes.get('short_score'), 0.0):.2f}"
        )
        lines.append(f"- decision_reason: {votes.get('decision_reason', 'n/a')}")
        lines.append(
            f"- learning_state: {advice.get('learning_maturity')} | historical_accuracy {self._safe_float(advice.get('historical_accuracy'), 0.0):.1%} | conservativeness {self._safe_float(advice.get('conservativeness'), 0.0):.2f}"
        )
        lines.append("")

        lines.append("[Key Evidence]")
        for reason in advice.get("reasons") or []:
            lines.append(f"- {reason}")
        lines.append("")

        cancel_conditions = advice.get("invalidation") or []
        if cancel_conditions:
            lines.append("[Invalidation Rules]")
            for reason in cancel_conditions:
                lines.append(f"- {reason}")
            lines.append("")

        lines.append("Risk note: crypto is highly volatile; treat AI output as decision support, not certainty.")
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
    def _extract_confidence(ai_analysis: Dict, institutional: Dict) -> float:
        ai_c = TradeAdvisor._safe_float(ai_analysis.get("confidence"), 0.5)
        inst_c = TradeAdvisor._safe_float(institutional.get("consensus_confidence"), 0.5)
        return ai_c * 0.6 + inst_c * 0.4


if __name__ == "__main__":
    # 自测
    advisor = TradeAdvisor({"account_balance": 5000})
    advice = advisor.build_advice(
        symbol="BNBUSDT", timeframe="1h", current_price=632.94,
        indicators={"RSI": 38.0, "MACD": 0.6, "ATR": 8.5},
        ai_analysis={
            "signal": "买入", "confidence": 0.75,
            "entry_price": 632.0, "stop_loss": 618.0, "take_profit": 660.0,
            "analysis": "RSI 接近超卖叠加多头排列，反弹概率高",
        },
        institutional={
            "consensus_signal": "BUY", "consensus_confidence": 0.7,
            "buy_signals": 8, "sell_signals": 2, "hold_signals": 3,
        },
        learning_insights={
            "overall_accuracy": 0.62, "avg_pnl": 1.4,
            "learning_maturity": "INTERMEDIATE", "total_feedbacks": 18,
        },
    )
    print(advice["report_text"])
    print("\n--- 复制版 ---")
    print(advice["order_text"])
