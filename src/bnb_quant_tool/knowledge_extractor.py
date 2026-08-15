"""
知识提炼器 — 调用 AI API 将交易/复盘数据提炼为结构化知识卡片（非原始聊天记录）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# 知识卡片分类
CATEGORIES = (
    "trading_logic",    # 交易逻辑
    "stop_loss_rule",   # 止损规则
    "market_review",    # 市场复盘
    "error_lesson",     # 错误教训
)

CATEGORY_LABELS = {
    "trading_logic": "交易逻辑",
    "stop_loss_rule": "止损规则",
    "market_review": "市场复盘",
    "error_lesson": "错误教训",
}

_EXTRACT_SYSTEM = """你是量化交易知识提炼专家。你的任务是把交易/复盘数据提炼成结构化「知识卡片」，供未来决策检索复用。

严格要求：
1. 只输出 JSON 数组，不要原始聊天记录或冗长叙述
2. 每条卡片必须可归因、可执行、可复用
3. category 只能是: trading_logic | stop_loss_rule | market_review | error_lesson
4. 每次最多输出 4 条，优先高质量、可验证的经验
5. 亏损交易重点提炼 error_lesson 和 stop_loss_rule；盈利交易提炼 trading_logic；复盘提炼 market_review

JSON 数组元素格式：
{
  "category": "error_lesson",
  "title": "简短标题",
  "trigger_condition": "什么市场/交易条件下适用",
  "action_rule": "下次遇到时应怎么做（可执行规则）",
  "lesson": "一句话核心教训",
  "confidence": 0.0-1.0,
  "tags": ["tag1", "tag2"]
}"""


class KnowledgeExtractor:
    """使用 DeepSeek 将结构化交易/复盘上下文提炼为知识卡片。"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: int = 90,
    ):
        self.api_key = api_key or ""
        self.model = model
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_key not in ("", "YOUR_API_KEY"))

    def extract_from_trade(self, trade_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从单笔平仓的结构化上下文提炼知识卡片。"""
        if not self.available:
            return []
        user_prompt = f"""请根据以下【结构化交易结果】提炼知识卡片（不要编造未提供的数据）：

```json
{json.dumps(trade_context, ensure_ascii=False, indent=2)}
```

侧重：
- 若亏损：error_lesson、stop_loss_rule
- 若盈利：trading_logic
- 若止损前曾有浮盈：stop_loss_rule
"""
        return self._call_and_parse(user_prompt)

    def extract_from_review(
        self,
        review_result: Dict[str, Any],
        stats: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """从 AI 复盘结果提炼知识卡片。"""
        if not self.available:
            return []
        payload = {
            "review": review_result,
            "statistics": stats or {},
        }
        user_prompt = f"""请根据以下【AI 复盘结论】提炼可长期复用的知识卡片：

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

侧重 market_review、trading_logic，参数建议可归入 stop_loss_rule 或 trading_logic。
"""
        return self._call_and_parse(user_prompt)

    def extract_from_analysis(self, analysis_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从单次市场分析提炼可复用的局面认知（无交易结果也可沉淀）。"""
        if not self.available:
            return []
        user_prompt = f"""请根据以下【单次市场分析快照】提炼 1-2 条可长期复用的知识卡片（不要编造未提供的数据）：

```json
{json.dumps(analysis_context, ensure_ascii=False, indent=2)}
```

侧重 market_review、trading_logic；若信号与指标明显冲突，可输出 error_lesson 提醒谨慎。
"""
        return self._call_and_parse(user_prompt)

    def extract_from_trades_batch(
        self,
        sample_trades: List[Dict[str, Any]],
        aggregate_stats: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """从多笔历史交易统计 + 代表性样本一次性提炼知识卡片（适合 500+ 笔批量）。"""
        if not self.available:
            return []
        payload = {
            "aggregate_statistics": aggregate_stats,
            "representative_trades": sample_trades[:25],
            "note": f"共 {aggregate_stats.get('total_trades', 0)} 笔历史交易，以下为统计与代表性样本",
        }
        user_prompt = f"""请根据以下【批量历史交易统计 + 代表性样本】提炼可长期复用的知识卡片（不要逐笔复述）：

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

要求：
- 从统计规律中提炼 3-6 条高价值卡片
- 涵盖 trading_logic / stop_loss_rule / market_review / error_lesson
- 优先总结：止损过紧、超买追多、时段规律、连亏模式等
"""
        return self._call_and_parse(user_prompt)

    def extract_post_trade_reflection(self, trade_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """平仓后结构化反思：假设错在哪、下次规则是什么。"""
        if not self.available:
            return []
        user_prompt = f"""请对以下【已平仓交易】做结构化反思，提炼 1-3 条可执行知识卡片。

反思框架（必须回答）：
1. 当初假设是什么？哪里错了或对了？
2. 若重来一次，具体执行规则是什么？
3. 此类局面下次应 WAIT / 缩仓 / 改止损？

```json
{json.dumps(trade_context, ensure_ascii=False, indent=2)}
```

输出 JSON 数组，category 优先 error_lesson / stop_loss_rule；亏损重点反思假设错误。
"""
        return self._call_and_parse(user_prompt)

    def consolidate_cards(
        self,
        cards: List[Dict[str, Any]],
        max_rules: int = 5,
    ) -> List[Dict[str, Any]]:
        """元学习：合并冗余卡片为少量元规则。"""
        if not self.available or not cards:
            return []
        slim = [
            {
                "title": c.get("title"),
                "category": c.get("category"),
                "trigger_condition": c.get("trigger_condition"),
                "lesson": c.get("lesson"),
                "confidence": c.get("confidence"),
                "validated": c.get("times_validated"),
                "contradicted": c.get("times_contradicted"),
            }
            for c in cards[:40]
        ]
        user_prompt = f"""以下是从历史交易中积累的知识卡片，存在冗余或矛盾。请合并为最多 {max_rules} 条「元规则」：

```json
{json.dumps(slim, ensure_ascii=False, indent=2)}
```

要求：
- 合并相似条目，删除已被证伪的低价值规则
- category 用 market_review 或 trading_logic
- confidence 反映合并后可信度（0.5-0.9）
- 每条必须可执行
"""
        merged = self._call_and_parse(user_prompt)
        for card in merged:
            card["tags"] = list(set((card.get("tags") or []) + ["meta_learning"]))
        return merged

    def prefilter_cards_applicability(
        self,
        cards: List[Dict[str, Any]],
        market_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """AI 标注知识卡片适用性：applicable / verify / skip。"""
        if not self.available or not cards:
            return []
        slim = [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "trigger_condition": c.get("trigger_condition"),
                "lesson": (c.get("lesson") or "")[:120],
                "confidence": c.get("confidence"),
                "validated": c.get("times_validated"),
                "contradicted": c.get("times_contradicted"),
            }
            for c in cards[:12]
        ]
        user_prompt = f"""当前市场局面：
```json
{json.dumps(market_context, ensure_ascii=False, indent=2)[:1500]}
```

知识卡片列表：
```json
{json.dumps(slim, ensure_ascii=False, indent=2)}
```

请为每条卡片标注 applicability（仅输出 JSON 数组）：
[{{"id": 1, "applicability": "applicable|verify|skip", "reason": "一句话"}}]
规则：局面明显不符标 skip；部分相关标 verify；高度吻合标 applicable。
"""
        try:
            endpoint = f"{self.base_url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "只输出 JSON 数组，不要其他文字。"},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "stream": False,
            }
            resp = requests.post(
                endpoint, headers=headers, json=payload, timeout=60
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            text = (content or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.debug("prefilter_cards_applicability failed: %s", e)
        return []

    def _call_and_parse(self, user_prompt: str) -> List[Dict[str, Any]]:
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            resp = requests.post(
                endpoint, headers=headers, json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_cards_json(content)
        except Exception as e:
            logger.warning(f"KnowledgeExtractor API failed: {e}")
            return []

    def _parse_cards_json(self, content: str) -> List[Dict[str, Any]]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 尝试截取 JSON 数组
            m = re.search(r"\[[\s\S]*\]", text)
            if not m:
                logger.warning("KnowledgeExtractor: cannot parse JSON")
                return []
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return []

        if isinstance(data, dict):
            data = data.get("cards") or data.get("knowledge_cards") or [data]
        if not isinstance(data, list):
            return []

        out = []
        for item in data:
            card = self._normalize_card(item)
            if card:
                out.append(card)
        return out[:4]

    def _normalize_card(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        category = str(item.get("category") or "error_lesson").strip()
        if category not in CATEGORIES:
            category = "error_lesson"
        title = str(item.get("title") or "").strip()
        trigger = str(item.get("trigger_condition") or item.get("condition") or "").strip()
        action = str(item.get("action_rule") or "").strip()
        lesson = str(item.get("lesson") or "").strip()
        if not title and not lesson:
            return None
        if not title:
            title = lesson[:40]
        conf = float(item.get("confidence") or 0.55)
        conf = max(0.1, min(1.0, conf))
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        return {
            "category": category,
            "title": title[:200],
            "trigger_condition": trigger[:500],
            "action_rule": action[:500],
            "lesson": lesson[:1000],
            "confidence": conf,
            "tags": [str(t) for t in tags[:8]],
        }


def build_trade_context(
    trade_row: Dict[str, Any],
    analysis_record: Optional[Dict[str, Any]] = None,
    outcome: str = "",
    quality: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建供 AI 提炼用的结构化交易上下文（非聊天记录）。"""
    indicators = {}
    if analysis_record:
        raw = analysis_record.get("indicators")
        if isinstance(raw, str):
            try:
                indicators = json.loads(raw or "{}")
            except json.JSONDecodeError:
                indicators = {}
        elif isinstance(raw, dict):
            indicators = raw

    key_indicators = {}
    for k in ("RSI", "MACD", "MACD_Histogram", "BB_Position", "ATR",
              "Volume_Ratio", "Stoch_K", "EMA_25"):
        if k in indicators:
            key_indicators[k] = indicators[k]

    return {
        "symbol": trade_row.get("symbol") or analysis_record.get("symbol") if analysis_record else "BNBUSDT",
        "side": trade_row.get("side"),
        "outcome": outcome,
        "pnl_usdt": trade_row.get("realized_pnl_usdt"),
        "close_reason": trade_row.get("close_reason"),
        "r_multiple": trade_row.get("r_multiple"),
        "mfe_r": trade_row.get("mfe_r"),
        "mae_r": trade_row.get("mae_r"),
        "mfe_pct": trade_row.get("mfe_pct"),
        "mae_pct": trade_row.get("mae_pct"),
        "entry_price": trade_row.get("entry_price"),
        "close_price": trade_row.get("close_avg_price"),
        "indicators_at_entry": key_indicators,
        "signal_at_entry": analysis_record.get("final_signal") if analysis_record else None,
        "quality": {
            "score": (quality or {}).get("score"),
            "tier": (quality or {}).get("tier"),
            "label": (quality or {}).get("label"),
        } if quality else None,
    }


def build_analysis_context(result: Dict[str, Any], record_id: Optional[int] = None) -> Dict[str, Any]:
    """构建单次分析的结构化上下文（供知识沉淀 / AI 提炼）。"""
    inst = result.get("institutional_strategies") or {}
    ai = result.get("ai_analysis") or {}
    ta = result.get("trade_advice") or {}
    regime = (result.get("market_regime") or {}).get("regime", "")
    indicators = result.get("indicators") or {}
    key_indicators = {
        k: indicators[k]
        for k in ("RSI", "MACD_Histogram", "BB_Position", "Volume_Ratio", "ATR", "ADX")
        if k in indicators and indicators[k] is not None
    }
    top_strategies = []
    for name, detail in (inst.get("strategy_details") or {}).items():
        sig = (detail or {}).get("signal", "HOLD")
        if sig in ("BUY", "SELL"):
            top_strategies.append(
                f"{(detail or {}).get('strategy', name)}:{sig}"
            )
    return {
        "record_id": record_id,
        "symbol": result.get("symbol", "BNBUSDT"),
        "timeframe": result.get("timeframe", "1h"),
        "current_price": result.get("current_price"),
        "market_regime": regime,
        "final_signal": result.get("final_recommendation"),
        "trade_action": ta.get("action") or ta.get("raw_action"),
        "passed_gate": ta.get("passed_gate"),
        "ai_signal": ai.get("signal"),
        "ai_confidence": ai.get("confidence"),
        "consensus_signal": inst.get("consensus_signal"),
        "consensus_confidence": inst.get("consensus_confidence"),
        "buy_signals": inst.get("buy_signals"),
        "sell_signals": inst.get("sell_signals"),
        "active_strategies": top_strategies[:6],
        "indicators": key_indicators,
        "mtf_action": (result.get("multi_timeframe") or {}).get("recommended_action"),
        "news_polarity": (result.get("news_summary") or {}).get("polarity"),
    }


def build_market_query_text(market_context: Dict[str, Any]) -> str:
    """将当前市场局面转为向量检索查询文本。"""
    symbol = market_context.get("symbol", "BNBUSDT")
    price = market_context.get("current_price") or market_context.get("price")
    regime = market_context.get("regime") or market_context.get("market_regime")
    signal = market_context.get("signal") or market_context.get("final_signal")
    indicators = market_context.get("indicators") or {}

    parts = [
        f"交易对 {symbol}",
        f"当前价格 {price}" if price else "",
        f"市场状态 {regime}" if regime else "",
        f"信号方向 {signal}" if signal else "",
    ]
    for k in ("RSI", "MACD_Histogram", "BB_Position", "Volume_Ratio", "ATR"):
        if k in indicators and indicators[k] is not None:
            parts.append(f"{k}={indicators[k]}")
    trend = market_context.get("trend")
    if trend:
        parts.append(f"趋势 {trend}")
    news_pol = market_context.get("news_polarity")
    if news_pol:
        parts.append(f"新闻情绪 {news_pol}")
    mtf = market_context.get("mtf_action")
    if mtf:
        parts.append(f"多周期 {mtf}")
    return " | ".join(p for p in parts if p)
