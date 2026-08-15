"""
Launchpool / Megadrop 挖矿事件因子
====================================
基于币安活动周期模型，输出可修正主周期方向的「挖矿事件因子」：

  公告拉升 → 质押锁仓 → 结束前抛售

典型规则：
- 挖矿结束前 pre_unlock_hours（默认 6–12h）自动降低 LONG 置信度
- 解锁砸盘期可触发对冲 SHORT 建议
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LaunchpoolMiningFactor:
    """Launchpool / Megadrop 周期效应因子计算器。"""

    def __init__(
        self,
        pre_unlock_hours: float = 12.0,
        pre_unlock_soft_hours: float = 24.0,
        announcement_boost: float = 0.35,
        staking_mid_factor: float = 0.05,
        pre_end_penalty: float = -0.30,
        unlock_dump_penalty: float = -0.75,
        hedge_short_threshold: float = -0.45,
    ):
        self.pre_unlock_hours = pre_unlock_hours
        self.pre_unlock_soft_hours = pre_unlock_soft_hours
        self.announcement_boost = announcement_boost
        self.staking_mid_factor = staking_mid_factor
        self.pre_end_penalty = pre_end_penalty
        self.unlock_dump_penalty = unlock_dump_penalty
        self.hedge_short_threshold = hedge_short_threshold

    def compute(
        self,
        event_cycle: Optional[Dict] = None,
        launchpool: Optional[Dict] = None,
        nlp_result: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """计算挖矿事件因子 [-1, +1] 及交易修正建议。"""
        event_cycle = event_cycle or {}
        launchpool = launchpool or {}
        nlp_result = nlp_result or {}

        phase = (event_cycle.get("phase") or "normal").lower()
        hours_to_end = self._hours_to_event_end(event_cycle.get("active_event") or {})
        hours_since_announce = self._hours_since_announce(event_cycle.get("active_event") or {})

        factor = 0.0
        confidence_delta = 0.0
        suggest_hedge_short = False
        block_long = False
        notes: list[str] = []

        # NLP 检测到 Launchpool / Megadrop 公告但尚未进入日历阶段
        dom_cat = (nlp_result.get("dominant_category") or "").lower()
        if phase == "normal" and dom_cat in ("launchpool", "megadrop"):
            factor += self.announcement_boost * 0.6
            confidence_delta += 0.05
            notes.append(f"NLP 检测到 {dom_cat} 公告，预期抢筹效应")

        if phase == "anticipation":
            factor += self.announcement_boost
            confidence_delta += float(event_cycle.get("confidence_boost") or 0.08)
            notes.append("预期发酵期：公告拉升阶段")

        elif phase == "staking_lock":
            if hours_to_end is not None and hours_to_end <= self.pre_unlock_hours:
                factor += self.pre_end_penalty
                confidence_delta -= 0.12
                block_long = True
                notes.append(f"挖矿结束前 {hours_to_end:.1f}h：锁仓解锁抛压预期")
            elif hours_to_end is not None and hours_to_end <= self.pre_unlock_soft_hours:
                factor += self.pre_end_penalty * 0.5
                confidence_delta -= 0.06
                notes.append(f"距结束 {hours_to_end:.1f}h：逐步降低做多置信度")
            else:
                factor += self.staking_mid_factor
                notes.append("质押锁仓期：流通盘减少，中性偏多")

        elif phase == "unlock_dump":
            factor += self.unlock_dump_penalty
            confidence_delta += float(event_cycle.get("confidence_boost") or -0.10)
            block_long = bool(event_cycle.get("block_long", True))
            suggest_hedge_short = bool(event_cycle.get("suggest_short", True))
            notes.append("解锁砸盘期：结束前抛售周期")

        elif phase == "value_recovery":
            factor += 0.05
            notes.append("价值回归期：活动影响消退")

        # Launchpool APY 强化
        if launchpool.get("extreme_apy_event"):
            if phase in ("anticipation", "staking_lock"):
                factor = min(1.0, factor + 0.15)
                notes.append(f"极高 APY {launchpool.get('max_apy_pct', 0):.0f}% 强化质押需求")
            elif phase == "unlock_dump":
                factor = max(-1.0, factor - 0.10)
                notes.append("高 APY 挖矿结束，抛压更大")

        factor = max(-1.0, min(1.0, factor))
        if factor <= self.hedge_short_threshold and phase in ("unlock_dump", "staking_lock"):
            suggest_hedge_short = True

        action_hint = self._action_hint(factor, block_long, suggest_hedge_short)

        return {
            "mining_event_factor": round(factor, 3),
            "confidence_delta": round(confidence_delta, 3),
            "block_long": block_long,
            "suggest_hedge_short": suggest_hedge_short,
            "action_hint": action_hint,
            "phase": phase,
            "hours_to_end": round(hours_to_end, 2) if hours_to_end is not None else None,
            "hours_since_announce": round(hours_since_announce, 2) if hours_since_announce is not None else None,
            "interpretation": "；".join(notes) if notes else "无活跃挖矿周期信号",
        }

    @staticmethod
    def _parse_iso(val: Any) -> Optional[datetime]:
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _hours_to_event_end(self, event: Dict) -> Optional[float]:
        end_at = self._parse_iso(event.get("end_at"))
        if not end_at:
            return None
        now = datetime.now(timezone.utc)
        return max(0.0, (end_at - now).total_seconds() / 3600.0)

    def _hours_since_announce(self, event: Dict) -> Optional[float]:
        announce_at = self._parse_iso(event.get("announce_at"))
        if not announce_at:
            return None
        now = datetime.now(timezone.utc)
        return max(0.0, (now - announce_at).total_seconds() / 3600.0)

    @staticmethod
    def _action_hint(factor: float, block_long: bool, suggest_short: bool) -> str:
        if block_long and suggest_short:
            return "SHORT"
        if block_long:
            return "WAIT"
        if factor >= 0.25:
            return "LONG"
        if factor <= -0.25:
            return "SHORT" if suggest_short else "WAIT"
        return "WAIT"

    @classmethod
    def format_for_prompt(cls, result: Dict) -> str:
        if not result or abs(result.get("mining_event_factor", 0)) < 0.05:
            if not result.get("block_long") and not result.get("suggest_hedge_short"):
                return ""
        lines = [
            "\n【Launchpool/Megadrop 挖矿事件因子】",
            f"- 因子: {result.get('mining_event_factor', 0):+.2f} | 建议: {result.get('action_hint', 'WAIT')}",
            f"- {result.get('interpretation', '')}",
        ]
        if result.get("hours_to_end") is not None:
            lines.append(f"- 距挖矿结束: {result['hours_to_end']:.1f} 小时")
        if result.get("block_long"):
            lines.append("- ⚠ 挖矿周期：禁止追多")
        if result.get("suggest_hedge_short"):
            lines.append("- ⚠ 建议考虑对冲 SHORT")
        lines.append("")
        return "\n".join(lines)
