"""独立 LLM 交易员 — 每人可配独立 API Key / 模型 / 学习记忆。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from .base import (
    Action,
    AgentOpinion,
    AgentRole,
    MarketContext,
    Stance,
    action_from_stance,
    clamp,
    stance_from_score,
)
from .personas import TraderPersona
from .trader_memory import TraderMemoryStore

logger = logging.getLogger(__name__)


class LLMTrader:
    """一位独立驱动的交易员机器人。"""

    role = AgentRole.QUANT  # 兼容旧枚举；实际身份用 persona.id

    def __init__(
        self,
        persona: TraderPersona,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        enabled: bool = True,
        use_llm: bool = True,
        temperature: float = 0.4,
        memory: Optional[TraderMemoryStore] = None,
        timeout: int = 60,
        provider: str = "",
        trader_key: Optional[str] = None,
        name_suffix: str = "",
    ):
        self.persona = persona
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model or "deepseek-chat"
        self.enabled = enabled
        self.use_llm = use_llm
        self.temperature = temperature
        self.memory = memory
        self.timeout = timeout
        self.provider = (provider or "").strip().lower()
        self._trader_key = (trader_key or "").strip() or None
        self.name_suffix = name_suffix or ""

    @property
    def trader_id(self) -> str:
        return self._trader_key or self.persona.id

    @property
    def display_name(self) -> str:
        return f"{self.persona.name}{self.name_suffix}"

    @property
    def has_llm(self) -> bool:
        key = self.api_key
        return bool(key) and key not in ("", "YOUR_API_KEY", "YOUR_DEEPSEEK_API_KEY")

    def analyze(self, context: MarketContext) -> AgentOpinion:
        """规则先验 +（可选）独立 LLM 观点。"""
        if not self.enabled:
            return AgentOpinion(
                role=AgentRole.QUANT,
                stance=Stance.NEUTRAL,
                action=Action.WAIT,
                confidence=0.0,
                score=0.0,
                summary=f"{self.persona.name}: 已禁用",
                evidence=[],
                concerns=["交易员已禁用"],
                metadata={"trader_id": self.trader_id, "source": "disabled"},
            )

        prior_score, prior_evidence, prior_concerns = self._specialty_prior(context)
        lessons = ""
        if self.memory:
            lessons = self.memory.get_lessons(self.trader_id)

        llm_opinion = None
        if self.use_llm and self.has_llm:
            llm_opinion = self._llm_vote(context, prior_score, lessons)

        if llm_opinion:
            opinion = llm_opinion
            source = "llm"
        else:
            stance = stance_from_score(prior_score)
            action = action_from_stance(stance)
            if abs(prior_score) < 0.12:
                action = Action.WAIT
            opinion = AgentOpinion(
                role=AgentRole.QUANT,
                stance=stance,
                action=action,
                confidence=clamp(0.35 + abs(prior_score) * 0.5, 0.2, 0.85),
                score=clamp(prior_score),
                summary=(
                    f"{self.persona.emoji} {self.display_name}: "
                    f"{action.value}（规则先验 score={prior_score:+.2f}）"
                ),
                evidence=prior_evidence[:5],
                concerns=prior_concerns[:4],
                metadata={
                    "trader_id": self.trader_id,
                    "persona_id": self.persona.id,
                    "trader_name": self.display_name,
                    "style": self.persona.style,
                    "source": "rule_prior",
                    "emoji": self.persona.emoji,
                    "color": self.persona.color,
                    "provider": self.provider,
                },
            )
            source = "rule_prior"

        opinion.metadata = {
            **(opinion.metadata or {}),
            "trader_id": self.trader_id,
            "persona_id": self.persona.id,
            "trader_name": self.display_name,
            "style": self.persona.style,
            "emoji": self.persona.emoji,
            "color": self.persona.color,
            "source": source,
            "has_llm_key": self.has_llm,
            "provider": self.provider,
            "model": self.model,
        }

        if self.memory:
            try:
                self.memory.record_vote(
                    self.trader_id,
                    opinion.action.value,
                    opinion.confidence,
                    opinion.score,
                    opinion.summary,
                    source=source,
                )
            except Exception as e:
                logger.debug("record vote failed: %s", e)

        return opinion

    # ── 规则先验（按 specialty）──────────────────────────────────

    def _specialty_prior(
        self, context: MarketContext
    ) -> tuple[float, List[str], List[str]]:
        specialty = self.persona.specialty
        if specialty == "momentum":
            return self._prior_momentum(context)
        if specialty == "mean_reversion":
            return self._prior_mean_reversion(context)
        if specialty == "macro":
            return self._prior_macro(context)
        if specialty == "structure":
            return self._prior_structure(context)
        if specialty == "flow":
            return self._prior_flow(context)
        if specialty == "contrarian":
            return self._prior_contrarian(context)
        return 0.0, [], ["未知 specialty"]

    def _prior_momentum(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0
        ind = ctx.indicators or {}
        mtf = ctx.multi_timeframe or {}

        macd = float(ind.get("MACD") or 0)
        signal = float(ind.get("MACD_Signal") or 0)
        if macd > signal:
            score += 0.25
            evidence.append("MACD 金叉上方")
        elif macd < signal:
            score -= 0.25
            evidence.append("MACD 死叉下方")

        adx = float(ind.get("ADX") or 0)
        if adx >= 25:
            score *= 1.15
            evidence.append(f"ADX={adx:.0f} 趋势足够")
        else:
            score *= 0.6
            concerns.append(f"ADX={adx:.0f} 偏弱，趋势不清")

        align = str(mtf.get("alignment") or mtf.get("consensus") or "").upper()
        if "BULL" in align or "LONG" in align or "UP" in align:
            score += 0.2
            evidence.append(f"多周期偏向多: {align}")
        elif "BEAR" in align or "SHORT" in align or "DOWN" in align:
            score -= 0.2
            evidence.append(f"多周期偏向空: {align}")

        return clamp(score), evidence, concerns

    def _prior_mean_reversion(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0
        ind = ctx.indicators or {}
        rsi = float(ind.get("RSI") or 50)
        price = float(ctx.current_price or 0)
        bb_u = float(ind.get("BB_upper") or 0)
        bb_l = float(ind.get("BB_lower") or 0)

        if rsi >= 70:
            score -= 0.35
            evidence.append(f"RSI={rsi:.0f} 超买，倾向做空回归")
        elif rsi <= 30:
            score += 0.35
            evidence.append(f"RSI={rsi:.0f} 超卖，倾向做多回归")
        else:
            concerns.append(f"RSI={rsi:.0f} 中性区，回归信号弱")

        if price and bb_u and price >= bb_u:
            score -= 0.2
            evidence.append("价格触及布林上轨")
        elif price and bb_l and price <= bb_l:
            score += 0.2
            evidence.append("价格触及布林下轨")

        adx = float(ind.get("ADX") or 0)
        if adx >= 35:
            score *= 0.4
            concerns.append(f"ADX={adx:.0f} 强趋势，均值回归危险")

        return clamp(score), evidence, concerns

    def _prior_macro(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0

        sent = ctx.sentiment or {}
        s_score = sent.get("score")
        if s_score is None:
            s_score = sent.get("sentiment_score")
        if s_score is not None:
            s = float(s_score)
            # 假设 -1~1 或 0~100
            if abs(s) > 1.5:
                s = (s - 50) / 50.0
            score += clamp(s) * 0.4
            evidence.append(f"情绪分 {s:+.2f}")

        news = ctx.news_summary or {}
        bias_raw = (
            news.get("polarity")
            or news.get("bias")
            or news.get("overall")
            or ""
        )
        bias = str(bias_raw).strip().lower()
        if bias in ("bullish", "bull", "positive") or "利好" in bias or "bull" in bias:
            score += 0.25
            evidence.append("新闻偏多")
        elif bias in ("bearish", "bear", "negative") or "利空" in bias or "bear" in bias:
            score -= 0.25
            evidence.append("新闻偏空")
        else:
            # 兼容旧大写 BULLISH / BEARISH / POSITIVE
            bias_u = str(bias_raw).upper()
            if "BULL" in bias_u or "POSITIVE" in bias_u or "利好" in str(bias_raw):
                score += 0.25
                evidence.append("新闻偏多")
            elif "BEAR" in bias_u or "NEGATIVE" in bias_u or "利空" in str(bias_raw):
                score -= 0.25
                evidence.append("新闻偏空")

        macro = ctx.macro or {}
        risk = str(macro.get("risk_regime") or macro.get("regime") or "").upper()
        if "RISK_OFF" in risk or "TIGHT" in risk:
            score -= 0.2
            concerns.append(f"宏观风险偏好偏弱: {risk}")
        elif "RISK_ON" in risk:
            score += 0.15
            evidence.append(f"宏观风险偏好偏强: {risk}")

        return clamp(score), evidence, concerns

    def _prior_structure(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0
        mtf = ctx.multi_timeframe or {}
        advice = ctx.trade_advice or {}

        votes = mtf.get("timeframe_votes") or mtf.get("votes") or {}
        if isinstance(votes, dict):
            bull = sum(1 for v in votes.values() if str(v).upper() in ("LONG", "BUY", "BULLISH"))
            bear = sum(1 for v in votes.values() if str(v).upper() in ("SHORT", "SELL", "BEARISH"))
            if bull + bear > 0:
                score += (bull - bear) / max(bull + bear, 1) * 0.45
                evidence.append(f"多周期票 多{bull}/空{bear}")

        struct = advice.get("structural_vote") or {}
        if isinstance(struct, dict):
            s = float(struct.get("score") or 0)
            if s:
                score += clamp(s) * 0.3
                evidence.append(struct.get("summary") or f"结构分 {s:+.2f}")

        if abs(score) < 0.1:
            concerns.append("结构不清晰，倾向观望")

        return clamp(score), evidence, concerns

    def _prior_flow(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        evidence: List[str] = []
        concerns: List[str] = []
        score = 0.0

        onchain = ctx.onchain or {}
        oc = float(onchain.get("score") or onchain.get("signal_score") or 0)
        if oc:
            # 可能是 -1~1 或 0~100
            if abs(oc) > 1.5:
                oc = (oc - 50) / 50.0
            score += clamp(oc) * 0.35
            evidence.append(f"链上分 {oc:+.2f}")

        inst = ctx.institutional or {}
        buy = int(inst.get("buy_signals") or 0)
        sell = int(inst.get("sell_signals") or 0)
        if buy + sell > 0:
            skew = (buy - sell) / (buy + sell)
            score += skew * 0.3
            evidence.append(f"机构票 买{buy}/卖{sell}")

        bnb = ctx.bnb_factors or {}
        bias = str(bnb.get("trade_bias") or "").upper()
        if bias == "LONG":
            score += 0.2
            evidence.append("BNB 因子偏多")
        elif bias == "SHORT":
            score -= 0.2
            evidence.append("BNB 因子偏空")

        rs = bnb.get("risk_sentry") or {}
        if rs.get("block_long"):
            score -= 0.25
            concerns.append("风控哨兵拦截做多")

        return clamp(score), evidence, concerns

    def _prior_contrarian(self, ctx: MarketContext) -> tuple[float, List[str], List[str]]:
        """情绪/机构一边倒时反向；温和时中性。"""
        evidence: List[str] = []
        concerns: List[str] = []
        crowd = 0.0

        sent = ctx.sentiment or {}
        s = sent.get("score", sent.get("sentiment_score"))
        if s is not None:
            sv = float(s)
            if abs(sv) > 1.5:
                sv = (sv - 50) / 50.0
            crowd += clamp(sv) * 0.5

        inst = ctx.institutional or {}
        buy = int(inst.get("buy_signals") or 0)
        sell = int(inst.get("sell_signals") or 0)
        if buy + sell >= 6:
            crowd += (buy - sell) / (buy + sell) * 0.5
            evidence.append(f"机构拥挤度 买{buy}/卖{sell}")

        regime = str((ctx.market_regime or {}).get("regime") or "").upper()
        if regime in ("EUPHORIA", "PANIC"):
            crowd = 0.8 if regime == "EUPHORIA" else -0.8
            evidence.append(f"极端状态 {regime}")

        # 反向
        if abs(crowd) < 0.35:
            concerns.append("共识不极端，反共识保持观望")
            return 0.0, evidence, concerns

        score = -crowd * 0.7
        evidence.append(f"拥挤方向 {crowd:+.2f} → 反向 {score:+.2f}")
        return clamp(score), evidence, concerns

    # ── LLM 调用 ───────────────────────────────────────────────

    def _build_brief(self, ctx: MarketContext, prior_score: float) -> str:
        advice = ctx.trade_advice or {}
        ai = ctx.ai_analysis or {}
        lines = [
            f"标的: {ctx.symbol} | 周期: {ctx.timeframe} | 现价: {ctx.current_price}",
            f"你的风格: {self.persona.style}",
            f"规则先验 score: {prior_score:+.2f}（仅供参考，可推翻）",
            f"当前建议草稿: action={advice.get('action')} conf={advice.get('confidence')}",
        ]
        if ai:
            lines.append(
                f"主 AI 信号: {ai.get('signal') or ai.get('trend')} "
                f"置信 {ai.get('confidence')}"
            )
        ind = ctx.indicators or {}
        keys = ("RSI", "MACD", "ADX", "BB_upper", "BB_middle", "BB_lower")
        bits = [f"{k}={ind.get(k)}" for k in keys if ind.get(k) is not None]
        if bits:
            lines.append("指标: " + ", ".join(bits[:8]))
        if ctx.sentiment:
            lines.append(f"情绪: {json.dumps(ctx.sentiment, ensure_ascii=False)[:180]}")
        if ctx.news_summary:
            lines.append(f"新闻: {json.dumps(ctx.news_summary, ensure_ascii=False)[:180]}")
        if ctx.onchain:
            lines.append(f"链上: {json.dumps(ctx.onchain, ensure_ascii=False)[:160]}")
        if ctx.macro:
            lines.append(f"宏观: {json.dumps(ctx.macro, ensure_ascii=False)[:160]}")
        if ctx.bnb_factors:
            lines.append(f"BNB因子: {json.dumps(ctx.bnb_factors, ensure_ascii=False)[:160]}")
        if ctx.market_regime:
            lines.append(f"状态: {ctx.market_regime.get('regime')}")
        return "\n".join(lines)

    def _llm_vote(
        self,
        ctx: MarketContext,
        prior_score: float,
        lessons: str,
    ) -> Optional[AgentOpinion]:
        brief = self._build_brief(ctx, prior_score)
        user = (
            f"{brief}\n\n"
            + (f"【你的历史教训】\n{lessons}\n\n" if lessons else "")
            + "请严格输出 JSON（不要 markdown）：\n"
            '{"action":"LONG|SHORT|WAIT","confidence":0.0到1.0,'
            '"score":-1到1,"reasoning":"一句话","key_points":[".."],'
            '"concerns":[".."]}'
        )
        messages = [
            {"role": "system", "content": self.persona.system_prompt},
            {"role": "user", "content": user},
        ]
        try:
            raw = self._chat(messages)
            data = self._parse_json(raw)
            if not data:
                return None
            action_s = str(data.get("action") or "WAIT").upper()
            if action_s not in ("LONG", "SHORT", "WAIT"):
                action_s = "WAIT"
            action = Action(action_s)
            conf = clamp(float(data.get("confidence") or 0.5), 0.0, 1.0)
            score = clamp(float(data.get("score") or 0.0))
            if action == Action.LONG and score < 0:
                score = abs(score) or 0.2
            if action == Action.SHORT and score > 0:
                score = -abs(score) or -0.2
            if action == Action.WAIT:
                score = score * 0.3
            stance = (
                Stance.BULLISH if action == Action.LONG
                else Stance.BEARISH if action == Action.SHORT
                else Stance.NEUTRAL
            )
            reasoning = str(data.get("reasoning") or "").strip()[:200]
            points = data.get("key_points") or []
            concerns = data.get("concerns") or []
            if not isinstance(points, list):
                points = [str(points)]
            if not isinstance(concerns, list):
                concerns = [str(concerns)]
            return AgentOpinion(
                role=AgentRole.QUANT,
                stance=stance,
                action=action,
                confidence=conf,
                score=score,
                summary=(
                    f"{self.persona.emoji} {self.display_name}: "
                    f"{action.value} — {reasoning or 'LLM 独立研判'}"
                ),
                evidence=[str(p)[:120] for p in points[:5]],
                concerns=[str(c)[:120] for c in concerns[:4]],
                metadata={"source": "llm", "raw_model": self.model},
            )
        except Exception as e:
            logger.warning("%s LLM 调用失败，回退规则先验: %s", self.persona.name, e)
            return None

    def _chat(self, messages: List[Dict[str, str]]) -> str:
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 800,
            "stream": False,
        }
        from bnb_quant_tool.llm_provider import (
            is_qwen_compatible,
            is_volcengine_compatible,
            uses_deepseek_thinking,
        )
        if uses_deepseek_thinking(self.model, self.base_url):
            payload["thinking"] = {"type": "disabled"}
        if is_qwen_compatible(self.model, self.base_url):
            # 关闭千问默认 thinking，避免空 content 导致静默回退规则先验
            payload["enable_thinking"] = False
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 800)
        if is_volcengine_compatible(self.model, self.base_url):
            payload["thinking"] = {"type": "disabled"}
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 800)
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        # 兼容部分网关把正文放在 reasoning / reasoning_content
        if not str(content).strip():
            content = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return str(content or "").strip()

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        content = text.strip()
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", content)
            if not m:
                return None
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
