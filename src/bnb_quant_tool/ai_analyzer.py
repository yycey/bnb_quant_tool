"""
BNB量化交易工具 - DeepSeek AI分析模块 v1.1
支持接入AI学习系统的历史洞察，让AI越分析越准
"""

import os
import json
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DeepSeekAnalyzer:
    """DeepSeek AI分析器 - 支持学习上下文注入"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com",
                 request_timeout: float = 15.0,
                 thinking_type: str = "enabled",
                 reasoning_effort: str = "high"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # 主分析硬超时（秒）；避免网络抖动/限流把 intelligence_loop hung 住
        self.request_timeout = float(request_timeout) if request_timeout else 15.0
        # 可由 config.<provider>.thinking / reasoning_effort 覆盖
        self.thinking_type = thinking_type or "enabled"
        self.reasoning_effort = reasoning_effort or "high"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

    def _call_api(self, messages: List[Dict],
                  thinking_type: str = "enabled",
                  reasoning_effort: str = "high",
                  temperature: float = 0.7) -> Dict:
        """调用DeepSeek API

        兼容说明：
        - 标准 deepseek-chat / deepseek-coder 不支持 thinking / reasoning_effort 字段
        - 仅在 deepseek-reasoner 或以 "reasoner" 结尾的推理模型上附加这些字段
        - 避免因多余字段导致 400 错误
        """
        endpoint = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False
        }
        # 仅对 DeepSeek 推理模型附加思考参数（千问 / 火山等兼容接口勿传 DS 专属字段）
        from bnb_quant_tool.llm_provider import (
            is_qwen_compatible,
            is_volcengine_compatible,
            uses_deepseek_thinking,
        )
        if uses_deepseek_thinking(self.model, self.base_url):
            if thinking_type:
                payload["thinking"] = {"type": thinking_type}
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
        if is_qwen_compatible(self.model, self.base_url):
            # 关闭千问默认 thinking，避免 content 为空导致主分析解析失败
            payload["enable_thinking"] = False
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 2048)
        if is_volcengine_compatible(self.model, self.base_url):
            # 方舟默认可能开深度思考；关闭以免 content 空、延迟高
            payload["thinking"] = {"type": "disabled"}
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 2048)

        try:
            response = requests.post(
                endpoint,
                headers=self.headers,
                json=payload,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            result = response.json()
            logger.info("LLM API调用成功 model=%s base=%s", self.model, self.base_url)
            return result
        except requests.exceptions.HTTPError as e:
            # 记录响应体以辅助调试
            try:
                err_body = response.text[:500]
            except Exception:
                err_body = ''
            logger.error(f"LLM API HTTP错误: {e} body={err_body}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM API调用失败: {e}")
            raise

    @staticmethod
    def _message_content(response: Dict) -> str:
        """兼容 content / reasoning_content 空正文。"""
        msg = ((response.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content") or ""
        if not str(content).strip():
            content = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return str(content or "").strip()

    def _build_learning_context(self, learning_context: Dict) -> str:
        """
        将学习洞察格式化为字符串，注入AI提示词
        这是让AI"成长"的核心：让DeepSeek看到自己的历史表现
        """
        if not learning_context:
            return ""

        ctx = learning_context
        lines = ["\n【AI历史学习洞察 - 请基于以下历史表现改进本次分析】"]
        lines.append("=" * 50)

        # 智能闭环经验摘要（感知→决策→执行→反思→记忆）
        brief = ctx.get("experience_brief") or ""
        if brief:
            lines.append(brief)
            lines.append("")
        else:
            loop_meta = ctx.get("intelligence_loop") or {}
            if loop_meta.get("enabled"):
                lines.append(
                    "【智能闭环】感知→决策→执行→反思→记忆 — 本次决策必须使用下方记忆，禁止从零臆测。"
                )
                lines.append("")

        growth = ctx.get("growth") or {}
        if growth:
            lines.append("--- 累计能力成长（每次分析/交易后更新，不可忽略） ---")
            lines.append(
                f"  能力等级 L{growth.get('capability_level', 0)}/100 | "
                f"成熟度 {growth.get('learning_maturity', ctx.get('learning_maturity', 'BEGINNER'))}"
            )
            lines.append(
                f"  累计分析 {growth.get('analysis_count', ctx.get('total_analyses', 0))} 次 | "
                f"验证反馈 {growth.get('feedback_count', ctx.get('total_feedbacks', 0))} 次 | "
                f"知识卡片 {growth.get('knowledge_cards', 0)} 条"
            )
            lines.append(
                f"  模式记忆 {growth.get('pattern_memory_count', 0)} 条 | "
                f"权重优化 {growth.get('weight_optimizations', 0)} 次"
            )
            dims = growth.get("capability_dimensions") or {}
            if dims:
                lines.append(
                    f"  五维能力: 样本{dims.get('sample_maturity', 0)} "
                    f"准确{dims.get('prediction_accuracy', 0)} "
                    f"知识{dims.get('knowledge_quality', 0)} "
                    f"纪律{dims.get('discipline', 0)} "
                    f"进化{dims.get('evolution_activity', 0)}"
                )
            lines.append(
                "  要求: 本次分析必须结合下方历史与知识卡片，"
                "与过去同类局面相比给出更精准的判断，不能重复犯相同错误。"
            )
            lines.append("")

        try:
            from bnb_quant_tool.factor_attribution_learner import format_attribution_for_prompt
            attr_text = format_attribution_for_prompt(ctx)
            if attr_text:
                lines.append(attr_text.strip())
                lines.append("")
        except ImportError:
            pass

        conv = ctx.get("institutional_conviction") or {}
        if conv:
            lines.append("--- 机构级方向信念（多因子 ensemble，必须参考） ---")
            lines.append(conv.get("institutional_thesis") or conv.get("summary", ""))
            for c in (conv.get("conflicts") or [])[:3]:
                lines.append(f"  ⚠ {c}")
            lines.append(
                f"  信念分 {conv.get('conviction', 0):+.3f} → 机构建议 {conv.get('direction', 'WAIT')}"
            )
            lines.append("")

        # 议会交易员经验（外部记忆 → 主决策）
        council = ctx.get("council_memory") or {}
        traders = council.get("traders") or []
        if traders:
            lines.append("--- 议会交易员记忆（胜负回写后的人格权重） ---")
            for t in traders[:6]:
                lines.append(
                    f"  {t.get('trader_id')}: 样本{t.get('total', 0)} "
                    f"准确{float(t.get('accuracy') or 0):.0%} 权重×{float(t.get('weight') or 1):.2f}"
                )
                if t.get("lesson"):
                    lines.append(f"    教训: {t['lesson'][:100]}")
            lines.append("")

        lines.append(f"总分析次数: {ctx.get('total_analyses', 0)}")
        lines.append(f"总反馈次数: {ctx.get('total_feedbacks', 0)}")
        lines.append(f"历史准确率: {ctx.get('overall_accuracy', 0):.1%}")
        lines.append(f"平均PnL: {ctx.get('avg_pnl', 0):+.2f}%")
        lines.append(f"学习成熟度: {ctx.get('learning_maturity', 'BEGINNER')}")
        lines.append("")

        # 最佳策略
        best = ctx.get('best_strategies', [])
        if best:
            lines.append("--- 历史表现最佳策略 (参考) ---")
            for s in best[:3]:
                lines.append(f"  {s['name']}: 胜率 {s['win_rate']:.1%} ({s['correct']}/{s['total']})")
            lines.append("")

        # 最差策略
        worst = ctx.get('worst_strategies', [])
        if worst:
            lines.append("--- 历史表现较差策略 (谨慎参考) ---")
            for s in worst[:3]:
                lines.append(f"  {s['name']}: 胜率 {s['win_rate']:.1%} ({s['correct']}/{s['total']})")
            lines.append("")

        # 最近表现
        recent = ctx.get('recent_trend', [])
        if recent:
            lines.append("--- 最近10次分析结果 ---")
            for t in recent[:10]:
                marker = '+' if t.get('result') == 'WIN' else ('-' if t.get('result') == 'LOSS' else '=')
                pnl = t.get('pnl', 0)
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "N/A"
                lines.append(f"  [{marker}] {str(t.get('time',''))[:16]} {t.get('signal','?'):<5s} -> {t.get('result','?'):<8s} {pnl_str}")
            lines.append("")

        # AI建议
        recs = ctx.get('recommendations', [])
        if recs:
            lines.append("--- 基于历史数据的改进建议 ---")
            for r in recs:
                lines.append(f"  * {r}")
            lines.append("")

        try:
            from bnb_quant_tool.learning_analytics import format_win_rate_for_prompt
            wr_text = format_win_rate_for_prompt(ctx.get("win_rate_context"))
            if wr_text:
                lines.append(wr_text)
        except ImportError:
            pass

        try:
            from bnb_quant_tool.win_rate_strategy import format_strategy_win_rate_for_prompt

            strat_text = format_strategy_win_rate_for_prompt(
                ctx.get("strategy_performance") or {},
                best=ctx.get("best_strategies"),
                worst=ctx.get("worst_strategies"),
                regime=ctx.get("regime_weights_applied") or ctx.get("regime_bucket"),
            )
            if strat_text:
                lines.append(strat_text)
        except ImportError:
            pass

        # 近期 AI 复盘调参（学习后直接改变下次行为）
        param_changes = ctx.get("recent_param_changes") or []
        if param_changes:
            lines.append("--- 近期已学习/待生效的参数调整 ---")
            for ch in param_changes[:3]:
                pname = ch.get("param") or ch.get("param_name") or "?"
                old_v = ch.get("old_value", ch.get("old", "?"))
                new_v = ch.get("new_value", ch.get("new", "?"))
                reason = (ch.get("reason") or ch.get("review_summary") or "")[:60]
                lines.append(f"  {pname}: {old_v} → {new_v} ({reason})")
            lines.append("")

        # 当前策略权重 Top（学习后的投票倾向）
        weights = ctx.get("strategy_weights") or {}
        if weights:
            top_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:5]
            lines.append("--- 当前学习后策略权重 Top5 ---")
            for name, w in top_w:
                lines.append(f"  {name}: {float(w):.3f}")
            lines.append("")

        # 模拟盘真实绩效（比 analysis 反馈更准确）
        paper = ctx.get('paper_trading') or {}
        if paper.get('closed_trades'):
            try:
                from bnb_quant_tool.ai_trading_context import format_paper_stats_for_prompt
                lines.append(format_paper_stats_for_prompt(paper).strip())
            except ImportError:
                lines.append(
                    f"模拟盘: {paper['closed_trades']}笔 胜率{paper.get('win_rate', 0):.1%} "
                    f"累计{paper.get('total_pnl_usdt', 0):+.2f}U"
                )

        # 模式记忆
        pm = ctx.get('pattern_memory') or {}
        if pm.get('matched', 0) > 0:
            lines.append("--- 模式记忆 ---")
            lines.append(f"  {pm.get('text', '')}")
            lines.append("")

        # 反事实学习统计
        cf = ctx.get("counterfactual_stats") or {}
        if cf.get("total_analyzed"):
            lines.append("--- 反事实学习 (实际 vs 不交易/反向/晚进场) ---")
            if cf.get("text"):
                lines.append(f"  {cf['text']}")
            else:
                lines.append(
                    f"  已分析 {cf['total_analyzed']} 笔 | "
                    f"实际最优 {cf.get('actual_was_best_pct', 0):.1%} | "
                    f"应反向 {cf.get('should_have_reversed', 0)} | "
                    f"应观望 {cf.get('should_have_waited', 0)}"
                )
            lines.append("")

        # 知识卡片（语义检索或最近高可信）— 分析必带知识库段落
        cards = ctx.get('capability_cards') or []
        kb_summary = ctx.get('capability_summary') or {}
        kb_retrieval = ctx.get('knowledge_base_retrieval') or {}
        mode = ctx.get('capability_retrieval_mode', kb_retrieval.get('mode', ''))
        if cards:
            try:
                from bnb_quant_tool.capability_memory import CapabilityMemory
                card_text = CapabilityMemory.format_for_prompt(cards, retrieval_mode=mode)
                if card_text:
                    lines.append(card_text.strip())
                    lines.append("")
            except ImportError:
                lines.append("--- 本地知识卡片 ---")
                for c in cards[:8]:
                    lines.append(f"  * [{c.get('category', '?')}] {c.get('title')}: {c.get('lesson')}")
                lines.append("")
        else:
            total_active = int(kb_summary.get("total_active") or kb_retrieval.get("total_active") or 0)
            lines.append("--- 本地知识库 ---")
            if total_active > 0:
                lines.append(
                    f"  库内共 {total_active} 条有效卡片；本次检索模式={mode or 'none'}，"
                    "未匹配到高相关条目，请仍参考下方历史统计与策略权重。"
                )
                for c in (kb_summary.get("top_cards") or [])[:3]:
                    lines.append(
                        f"  * [{c.get('category', '?')}] {c.get('title', '')}: "
                        f"{(c.get('lesson') or '')[:80]}"
                    )
            else:
                lines.append(
                    "  知识库暂无卡片。请主要依据历史分析统计与模拟盘绩效；"
                    "平仓/复盘后会自动沉淀结构化经验。"
                )
            lines.append("")

        lines.append("=" * 50)
        lines.append("请结合以上历史表现与知识库，给出本次分析。如果你过去在某些市场条件下预测错误，请在本次分析中说明。")
        lines.append("禁止忽略学习上下文与知识库；self_reflection 必须引用至少一条历史规律或知识卡片。")
        lines.append("")

        return "\n".join(lines)

    def _build_onchain_context(self, onchain: Optional[Dict]) -> str:
        if not onchain:
            return ""
        try:
            from bnb_quant_tool.onchain_analysis import OnChainAnalyzer
            return OnChainAnalyzer.format_for_prompt(onchain)
        except ImportError:
            score = onchain.get("onchain_score", 0)
            return f"\n【链上筹码】综合分 {score:+.2f}: {onchain.get('interpretation', '')}\n"

    def _build_macro_context(self, macro: Optional[Dict]) -> str:
        if not macro:
            return ""
        try:
            from bnb_quant_tool.macro_data import MacroDataLayer
            return MacroDataLayer.format_for_prompt(macro)
        except ImportError:
            return f"\n【宏观因子】综合分 {macro.get('macro_score', 0):+.2f}: {macro.get('interpretation', '')}\n"

    def _build_bnb_factors_context(self, bnb_factors: Optional[Dict]) -> str:
        if not bnb_factors:
            return ""
        try:
            from bnb_quant_tool.bnb_specific_factors import BNBSpecificFactors
            return BNBSpecificFactors.format_for_prompt(bnb_factors)
        except ImportError:
            return (
                f"\n【BNB专属因子】综合分 {bnb_factors.get('bnb_score', 0):+.2f}: "
                f"{bnb_factors.get('interpretation', '')}\n"
            )

    def analyze_market_data(self, df, indicators: Dict,
                            learning_context: Dict = None,
                            onchain_context: Dict = None,
                            macro_context: Dict = None,
                            bnb_factors_context: Dict = None) -> Dict:
        """
        分析市场数据，生成交易建议
        v1.1新增: 支持 learning_context 参数，将历史洞察注入提示词
        v2.8新增: onchain_context / macro_context 链上与宏观因子
        v2.10新增: bnb_factors_context BNB 平台币专属因子

        Args:
            df: 包含OHLCV和技术指标的DataFrame
            indicators: 技术指标计算结果
            learning_context: AI学习洞察（来自AILearningSystem.get_learning_insights()）
            onchain_context: 链上筹码分析（OnChainAnalyzer.fetch_all）
            macro_context: 宏观数据层（MacroDataLayer.fetch_all）
            bnb_factors_context: BNB 专属因子（BNBSpecificFactors.fetch_all）

        Returns:
            分析结果字典
        """
        latest = df.iloc[-1]
        recent_data = df.tail(20)

        data_summary = {
            "symbol": "BNBUSDT",
            "current_price": float(latest['close']),
            "recent_high": float(recent_data['high'].max()),
            "recent_low": float(recent_data['low'].min()),
            "recent_volume_avg": float(recent_data['volume'].mean()),
            "price_change_24h": float(((latest['close'] - df.iloc[-24]['close']) / df.iloc[-24]['close'] * 100)) if len(df) >= 24 else 0,
            "technical_indicators": indicators
        }

        system_prompt = """你是一个专业的加密货币量化交易分析师，精通技术分析和AI辅助决策。
你的任务是为用户输出**可立即下单**的交易参数：入场价、止损、分批止盈，让用户照单下单。
你会从历史分析记录中学习，持续改进分析准确率。请认真参考提供的"AI历史学习洞察"与"本地知识库"部分。

严格遵循以下规则：
1. 入场价、止损、止盈必须基于实际价格区间和ATR波动率，不要给出与现价相差超过 ±10% 的离谱价格。
2. 多头：止损必须 < 入场价 < 止盈；空头：止盈 < 入场价 < 止损。方向错了视为无效。
3. 风险回报比(reward/risk) 至少 1.5；做不到则降低 confidence 或改为"持有"。
4. 历史准确率 < 50% 时务必更保守：缩小仓位、收紧止损、提高 confidence 阈值。
5. 如果信号不明确，直接 signal="持有"、confidence<0.5，不要硬给入场价。
6. 若「模拟盘真实绩效」显示连亏≥3 或胜率<45%，应更保守（降低 confidence、缩小仓位），但若机构策略 BUY/SELL 票数明显占优（≥6票且占比>45%）仍可给出对应方向，confidence 可 0.55-0.65。
7. 你的目标是帮用户长期盈利：方向清晰时可给出 买入/卖出，由下游门控（置信度/RR/多周期）过滤低质量单；只有真正震荡无方向时才 signal="持有"。
8. 若提供链上筹码（MVRV/Netflow/巨鲸）或宏观因子（美股/美债/美元/Fed预期），必须纳入长周期判断；MVRV>2.5 或 宏观risk-off 时应更保守。
9. 注意宏观与crypto的非线性关系：收益率急升+美元走强时，即使技术面看多也应降低 confidence。
10. 若提供 BNB 专属因子（Launchpool APY、Beta剥离Alpha、币安监管NLP），必须纳入判断：高 APY Launchpool 意味着 BNB 质押买盘；大盘跌但 Alpha 为正说明独立资金护盘；SEC/监管诉讼等事件对 BNB 影响远大于普通新闻。
11. 若 BNB 风控哨兵报告资金费率 > 0.1%/8h，必须拦截做多并提示插针反转风险；若 BNB/BTC 汇率走弱（BTC涨BNB不涨），必须降低仓位系数。

输出格式必须是严格的JSON（不要有```包裹），字段如下：
{
    "trend": "看涨/看跌/震荡",
    "confidence": 0.0-1.0,
    "signal": "买入/卖出/持有",
    "entry_price": 建议入场中心价(数字),
    "entry_zone": [入场区间下限, 入场区间上限],
    "stop_loss": 止损价(数字),
    "take_profit": 主要止盈价(数字),
    "tp1": 第一档止盈价(数字, 占比50%),
    "tp2": 第二档止盈价(数字, 占比30%),
    "tp3": 第三档止盈价(数字, 占比20%),
    "risk_reward_ratio": 风险回报比(数字, reward/risk),
    "hold_hours": 建议最长持仓小时数(数字, 期望2-10; 超过10h未止盈会软平, 绝对上限48h),
    "position_size_pct": 建议仓位占账户比例(0-1),
    "analysis": "详细分析说明(中文，200字以内)",
    "key_levels": [{"type":"支撑","price":数字},{"type":"阻力","price":数字}],
    "risks": [风险提示列表，每条不超过20字],
    "invalidation": "什么情况下应放弃此次开单 (一句话)",
    "self_reflection": "基于历史表现的自我反思（如果你过去预测过类似情况且错了，请说明）"
}
"""

        # 构建学习上下文字符串（每次分析必带）
        learning_str = self._build_learning_context(learning_context)
        if not learning_str:
            logger.warning(
                "analyze_market_data: learning_context 为空，本次 AI 无法使用历史学习能力"
            )
            learning_str = (
                "\n【警告】未加载历史学习数据，本次分析质量会下降。"
                "请确保调用方传入 build_analysis_learning_context() 结果。\n"
            )
        onchain_str = self._build_onchain_context(onchain_context)
        macro_str = self._build_macro_context(macro_context)
        bnb_str = self._build_bnb_factors_context(bnb_factors_context)

        playbook_str = ""
        try:
            ta_enabled = True
            if isinstance(learning_context, dict):
                if learning_context.get("ta_playbook_enabled") is False:
                    ta_enabled = False
            if ta_enabled:
                from bnb_quant_tool.crypto_ta_playbook import build_playbook_prompt_section
                regime = None
                if isinstance(learning_context, dict):
                    regime = (
                        (learning_context.get("market_regime") or {}).get("regime")
                        or learning_context.get("regime")
                    )
                playbook_str = "\n" + build_playbook_prompt_section(
                    regime=regime, indicators=indicators
                ) + "\n"
        except Exception as e:
            logger.debug(f"playbook inject skipped: {e}")

        user_prompt = f"""{learning_str}{onchain_str}{macro_str}{bnb_str}{playbook_str}
请分析以下BNB市场数据：

当前价格: ${data_summary['current_price']}
24小时涨跌: {data_summary['price_change_24h']:.2f}%
近期最高: ${data_summary['recent_high']}
近期最低: ${data_summary['recent_low']}
平均成交量: {data_summary['recent_volume_avg']}

技术指标:
{json.dumps(indicators, indent=2, ensure_ascii=False, default=str)}

请基于以上数据{('以及你的历史表现记录' if learning_str else '')}给出交易建议。
如果历史准确率低于50%，请更加谨慎，给出更保守的建议。
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        response = self._call_api(
            messages,
            thinking_type=self.thinking_type,
            reasoning_effort=self.reasoning_effort,
        )

        # 解析响应
        try:
            content = self._message_content(response)
            # 清理可能的代码块包裹
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            analysis_result = json.loads(content)
            logger.info(f"AI分析完成，信号: {analysis_result.get('signal', '未知')}")
            return analysis_result
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"解析AI响应失败: {e}")
            return {
                "trend": "未知",
                "confidence": 0.0,
                "signal": "持有",
                "entry_price": data_summary['current_price'],
                "stop_loss": data_summary['current_price'] * 0.95,
                "take_profit": data_summary['current_price'] * 1.05,
                "risk_reward_ratio": 0,
                "analysis": "AI分析失败，请检查API响应格式",
                "key_levels": [],
                "risks": ["AI分析失败，建议暂不交易"],
                "self_reflection": "",
                "_error": f"parse_failed:{e}",
                "_degraded": True,
            }

    def analyze_sentiment(self, news_data: List[str]) -> Dict:
        """分析市场情绪（可选功能）"""
        if not news_data:
            return {"sentiment": "中性", "score": 0.5}

        combined_text = "\n".join(news_data[:10])

        system_prompt = """你是一个加密货币市场情绪分析师。请分析提供的文本，判断市场情绪。

输出严格JSON格式（不要```包裹）：
{
    "sentiment": "看涨/看跌/中性",
    "score": 0.0-1.0,
    "key_themes": [关键主题列表],
    "summary": "情绪总结(中文50字以内)"
}
"""
        user_prompt = f"请分析以下市场相关文本的情绪：\n\n{combined_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        response = self._call_api(messages, thinking_type="disabled", reasoning_effort="low")

        try:
            content = self._message_content(response)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            return json.loads(content)
        except (json.JSONDecodeError, KeyError):
            return {"sentiment": "中性", "score": 0.5}

    def summarize_news(self, news_items: List[Dict], symbol: str = "BNB") -> Dict:
        """让 DeepSeek 把新闻列表汇总成可用于开单决策的结构化结论。

        输入:
            news_items: NewsCollector.collect 返回的新闻列表
            symbol: 关注的币种代码

        输出 (失败/无新闻时返回 neutral 兜底):
            {
                "polarity": bullish/bearish/neutral,   # 综合方向
                "confidence": 0.0~1.0,                  # 置信度
                "score": -1.0~1.0,                      # 利好为正、利空为负
                "impact_horizon": short/medium/long,    # 影响时长
                "key_bullish": [str],                   # 关键利好要点
                "key_bearish": [str],                   # 关键利空要点
                "summary": str,                         # 中文一句话总结
                "trade_suggestion": LONG/SHORT/WAIT,    # 开单建议
                "caution": str                          # 风险提示
            }
        """
        if not news_items:
            return {
                "polarity": "neutral", "confidence": 0.3, "score": 0.0,
                "impact_horizon": "short",
                "key_bullish": [], "key_bearish": [],
                "summary": "暂无相关新闻", "trade_suggestion": "WAIT",
                "caution": "信息不足，谨慎开单"
            }

        # 截取最近的新闻（控制 token）
        items = news_items[:25]
        bullets = []
        for i, n in enumerate(items, 1):
            ts = n.get("published", "")
            src = n.get("source", "?")
            title = (n.get("title") or "").strip()
            summary = (n.get("summary") or "").strip()
            bullets.append(f"{i}. [{ts} | {src}] {title} :: {summary[:160]}")
        news_block = "\n".join(bullets)

        system_prompt = f"""你是加密货币新闻情报分析师，专门给短线交易者提供决策依据。
你需要阅读 {symbol} 相关的真实新闻列表，判断对未来 1~7 天 {symbol} 价格的方向影响。

严格要求：
1. 只基于事实判断，不要编造没出现的事件。
2. 如果新闻互相矛盾或权重接近，老老实实给 neutral，不要硬给方向。
3. ETF 通过、监管放松、机构买入 -> 利好；交易所被黑、SEC 起诉、大额砸盘、监管禁令 -> 利空。
4. 评估时考虑新闻发布时间，越近越重要。
5. score 必须与 polarity 一致：bullish>0、bearish<0、neutral≈0。

输出严格 JSON（不要 ``` 包裹，不要任何注释）：
{{
  "polarity": "bullish" | "bearish" | "neutral",
  "confidence": 0.0~1.0,
  "score": -1.0~1.0,
  "impact_horizon": "short" | "medium" | "long",
  "key_bullish": ["中文要点1", "中文要点2"],
  "key_bearish": ["中文要点1", "中文要点2"],
  "summary": "中文 50 字以内总结",
  "trade_suggestion": "LONG" | "SHORT" | "WAIT",
  "caution": "中文 30 字以内的风险提示"
}}
"""
        user_prompt = f"""币种: {symbol}
时间窗口: 最近 24~48 小时
新闻条数: {len(items)}

新闻列表:
{news_block}

请汇总并判断对 {symbol} 价格的影响。
"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = self._call_api(messages, thinking_type="disabled",
                                       reasoning_effort="medium", temperature=0.3)
            content = self._message_content(response)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            data = json.loads(content)
        except Exception as e:
            logger.warning(f"summarize_news 解析失败: {e}")
            return {
                "polarity": "neutral", "confidence": 0.3, "score": 0.0,
                "impact_horizon": "short",
                "key_bullish": [], "key_bearish": [],
                "summary": "AI解析失败，请人工查看新闻",
                "trade_suggestion": "WAIT",
                "caution": "模型异常，避免依赖此结论"
            }

        # 字段兜底
        polarity = str(data.get("polarity", "neutral")).lower()
        if polarity not in ("bullish", "bearish", "neutral"):
            polarity = "neutral"
        try:
            score = float(data.get("score", 0.0))
            score = max(-1.0, min(1.0, score))
        except (ValueError, TypeError):
            score = 0.0
        try:
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5
        suggestion = str(data.get("trade_suggestion", "WAIT")).upper()
        if suggestion not in ("LONG", "SHORT", "WAIT"):
            suggestion = "WAIT"

        return {
            "polarity": polarity,
            "confidence": round(confidence, 3),
            "score": round(score, 3),
            "impact_horizon": data.get("impact_horizon", "short"),
            "key_bullish": data.get("key_bullish", []) or [],
            "key_bearish": data.get("key_bearish", []) or [],
            "summary": data.get("summary", ""),
            "trade_suggestion": suggestion,
            "caution": data.get("caution", ""),
            "news_count": len(items),
        }

    def review_paper_trades(self, payload: Dict, symbol: str = "BNB") -> Dict:
        """
        复盘模拟交易历史, 输出 AI 改进建议.
        输入: payload = {"stats": {...}, "trades": [...]}  (来自 PaperTradingEngine.build_review_payload)
        输出: {
            grade, key_findings, mistakes, what_works,
            param_suggestions, next_focus, summary
        }
        """
        try:
            stats = payload.get("stats") or {}
            trades = payload.get("trades") or []
            if stats.get("total_trades", 0) == 0:
                return {
                    "grade": "N/A",
                    "key_findings": ["尚无已完成交易, 请先跟单几天"],
                    "mistakes": [],
                    "what_works": [],
                    "param_suggestions": [],
                    "strategy_adjustments": [],
                    "regime_rules": [],
                    "sl_tp_diagnosis": {},
                    "revert_suggestion": {},
                    "next_focus": "累积至少 10 笔交易后再复盘",
                    "summary": "数据不足, 无法复盘",
                }

            system_prompt = """你是一名专业量化策略复盘顾问, 具备以下能力:
  ① 常规参数调优 (confidence/atr/news_filter/max_position)
  ② 机构策略权重调整 (发现糟糕策略要减权/禁用)
  ③ 坏行情识别 (某 regime/时段/side 胜率过低 → 输出停手规则)
  ④ 止盈止损诊断 (MFE/MAE/R 倍数 → 发现赢单只吃到1R 或者损单滢于1R)
  ⑤ 上次参数调整的事后评估 (胜率下降 → 给 revert_suggestion)
根据用户提供的多维度数据 (含 stats/trades/breakdown/strategy_perf/反事实统计等) 输出可执行改进.
严格输出JSON (不要用```包裹), 字段:
{
  "grade": "A/B/C/D/F",
  "key_findings": ["发现1", "发现2", ...],
  "mistakes": ["存在的问题1", "问题2"],
  "what_works": ["表现好的点1", "点2"],
  "param_suggestions": [
    {"param": "confidence_threshold", "current": 0.6, "suggest": 0.7, "reason": "..."}
  ],
  "strategy_adjustments": [
    {"name": "BridgewaterAllWeather", "current_weight": 0.08, "suggest_weight": 0.04,
     "action": "DOWNWEIGHT", "reason": "近 N 笔 WR 仅 25%"}
  ],
  "regime_rules": [
    {"condition": "ATR > 2*avg", "action": "DISABLE_LONG",
     "reason": "高波动期 LONG 胜率仅18%"}
  ],
  "sl_tp_diagnosis": {
    "tp1_too_close": false,
    "sl_too_loose": false,
    "hint": "根据MFE/MAE/R给出的一句诊断"
  },
  "revert_suggestion": {
    "should_revert": false,
    "target_param": "confidence_threshold",
    "reason": "上次 0.6→0.7 后胜率从45%降到35%, 建议回滚"
  },
  "next_focus": "下一阶段重点关注什么",
  "summary": "一句话总结 (<=80字)"
}
评分标准:
  A: 胜率50%+ 且 PF>=1.8 且 最大连亏<=2
  B: 胜率45%+ 且 PF>=1.4
  C: 胜率40%+ 且 PF>=1.1
  D: 仅打平手续费
  F: 总盈亏为负
重要约束:
  · param_suggestions 必须是可调节的数值 (confidence_threshold / atr_sl_mult / atr_tp1_mult /
    atr_tp2_mult / atr_tp3_mult / max_position_pct / news_filter_threshold / conservativeness_min)
  · strategy_adjustments 仅当 strategy_perf 中某策略样本 >= 8 笔且胜率明显偏差时才输出
  · regime_rules 仅当 by_regime/by_hour/by_side 某桶 n>=8 且胜率<35% 才输出
  · sl_tp_diagnosis 要参考 breakdown.diagnostics
  · revert_suggestion 仅当 recent_param_changes 存在且调整后胜率明显下降才输出 should_revert=true
用中文输出."""

            # 按需上报 payload 子集 (避免 prompt 过长)
            user_parts = [f"交易对: {symbol}\n"]
            user_parts.append(
                "=== 总统计 ===\n" + json.dumps(stats, ensure_ascii=False, indent=2)
            )
            breakdown = payload.get("breakdown")
            if breakdown:
                user_parts.append(
                    "\n=== 分桶表现 (by_side / regime / hour / confidence / close_reason / diagnostics) ===\n"
                    + json.dumps(breakdown, ensure_ascii=False, indent=2)
                )
            sp = payload.get("strategy_perf")
            if sp:
                user_parts.append(
                    "\n=== 13 机构策略表现 ===\n"
                    + json.dumps(sp, ensure_ascii=False, indent=2, default=str)
                )
            rpc = payload.get("recent_param_changes")
            if rpc:
                user_parts.append(
                    "\n=== 近期参数调整及后续胜率变化 ===\n"
                    + json.dumps(rpc, ensure_ascii=False, indent=2, default=str)
                )
            pi = payload.get("pattern_insight")
            if pi:
                user_parts.append(
                    "\n=== Pattern Memory 洞察 ===\n"
                    + json.dumps(pi, ensure_ascii=False, indent=2, default=str)
                )
            cf = payload.get("counterfactual_stats")
            if cf:
                user_parts.append(
                    "\n=== 反事实统计 (不交易/反向/晚进场) ===\n"
                    + json.dumps(cf, ensure_ascii=False, indent=2, default=str)
                )
            user_parts.append(
                f"\n=== 最近 {len(trades)} 笔交易 (含 mfe_r/mae_r/r_multiple/regime) ===\n"
                + json.dumps(trades, ensure_ascii=False, indent=2, default=str)
            )
            user_parts.append("\n请复盘并输出改进建议.")
            user_prompt = "\n".join(user_parts)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            content = self._message_content(self._call_api(messages, temperature=0.3))
            # 去掉可能的 ``` 包裹
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:].strip()
            data = json.loads(content)
            return {
                "grade": str(data.get("grade", "C")),
                "key_findings": list(data.get("key_findings") or []),
                "mistakes": list(data.get("mistakes") or []),
                "what_works": list(data.get("what_works") or []),
                "param_suggestions": list(data.get("param_suggestions") or []),
                "strategy_adjustments": list(data.get("strategy_adjustments") or []),
                "regime_rules": list(data.get("regime_rules") or []),
                "sl_tp_diagnosis": data.get("sl_tp_diagnosis") or {},
                "revert_suggestion": data.get("revert_suggestion") or {},
                "next_focus": str(data.get("next_focus", "")),
                "summary": str(data.get("summary", "")),
            }
        except Exception as e:
            logger.warning(f"review_paper_trades 失败, fallback: {e}")
            stats = payload.get("stats") or {}
            wr = stats.get("win_rate", 0.0)
            pf = stats.get("profit_factor", 0.0)
            grade = "C"
            if stats.get("total_realized_pnl", 0) < 0:
                grade = "F"
            elif wr >= 0.5 and pf >= 1.8:
                grade = "A"
            elif wr >= 0.45 and pf >= 1.4:
                grade = "B"
            elif wr >= 0.4 and pf >= 1.1:
                grade = "C"
            else:
                grade = "D"
            return {
                "grade": grade,
                "key_findings": [f"本地评分 (AI 复盘不可用): 胜率 {wr:.1%}, PF {pf:.2f}"],
                "mistakes": [],
                "what_works": [],
                "param_suggestions": [],
                "strategy_adjustments": [],
                "regime_rules": [],
                "sl_tp_diagnosis": {},
                "revert_suggestion": {},
                "next_focus": "继续累积交易样本",
                "summary": f"AI 复盘调用失败: {e}",
            }

    def backtest_analysis(self, historical_signals: List[Dict],
                         price_data: List[float]) -> Dict:
        system_prompt = """你是一个量化交易策略评估专家。
请分析历史交易信号的表现，给出策略改进建议。

输出严格JSON格式（不要```包裹）：
{
    "total_signals": 总信号数,
    "win_rate": 胜率(0-1),
    "average_return": 平均收益率(小数),
    "sharpe_ratio": 夏普比率,
    "max_drawdown": 最大回撤(小数),
    "strengths": [策略优势列表],
    "weaknesses": [策略劣势列表],
    "improvements": [改进建议列表]
}
"""
        user_prompt = f"""请分析以下历史信号表现：

信号数量: {len(historical_signals)}
价格数据点: {len(price_data)}

最近10个信号:
{json.dumps(historical_signals[-10:], indent=2, ensure_ascii=False)}
"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        response = self._call_api(
            messages,
            thinking_type=self.thinking_type,
            reasoning_effort=self.reasoning_effort,
        )

        try:
            content = self._message_content(response)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            return json.loads(content)
        except (json.JSONDecodeError, KeyError):
            return {"error": "回测分析失败"}

    def quick_summarize(self, prompt: str, max_tokens: int = 200) -> str:
        """轻量摘要（多智能体辩论等），低温度短回复。"""
        if not self.api_key or self.api_key in ("", "YOUR_API_KEY"):
            return ""
        messages = [
            {"role": "system", "content": "你是量化交易助手，用简洁中文回答。"},
            {"role": "user", "content": prompt[:2000]},
        ]
        try:
            # 走 _call_api：自动关千问/火山 thinking，避免空 content
            result = self._call_api(
                messages,
                thinking_type="disabled",
                reasoning_effort="low",
                temperature=0.2,
            )
            return self._message_content(result)[: max(max_tokens * 4, 200)]
        except Exception as e:
            logger.debug("quick_summarize failed: %s", e)
            return ""


if __name__ == "__main__":
    import os
    api_key = os.getenv("DEEPSEEK_API_KEY", "YOUR_API_KEY_HERE")

    if api_key == "YOUR_API_KEY_HERE":
        print("请设置环境变量 DEEPSEEK_API_KEY")
    else:
        analyzer = DeepSeekAnalyzer(api_key=api_key)

        # 测试带学习上下文的分析
        mock_indicators = {
            "RSI": 65.5, "MACD": 2.3, "MACD_Signal": 1.8,
            "BB_upper": 612.5, "BB_middle": 598.3, "BB_lower": 584.1,
            "MA_20": 598.3, "MA_50": 589.7
        }

        # 模拟学习上下文
        mock_learning = {
            'total_analyses': 15, 'total_feedbacks': 8, 'overall_accuracy': 0.625,
            'avg_pnl': 1.2, 'learning_maturity': 'INTERMEDIATE',
            'best_strategies': [{'name': 'SMA Crossover', 'win_rate': 0.71, 'correct': 5, 'total': 7}],
            'worst_strategies': [{'name': 'RSI Extreme', 'win_rate': 0.33, 'correct': 1, 'total': 3}],
            'recent_trend': [{'time': '2024-01-01', 'signal': 'BUY', 'result': 'WIN', 'pnl': 2.1}],
            'recommendations': ['Strategy SMA Crossover performing well, consider increasing weight']
        }

        print("DeepSeek分析器已初始化（带学习上下文支持）")
        print("需要实际市场数据 + API Key 才能完整测试")
