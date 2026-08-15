"""
DecisionState — 单一决策权威，避免 raw/gated/executable 口径分裂。

字段约定：
- intended: 门控前意图方向（AI/投票结果）
- gated: 门控后动作（可能是 WAIT）
- executable: 是否允许开仓
- blockers: 可审计拦截原因列表（含 code）
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Blocker:
    code: str
    message: str
    hard: bool = True
    gate: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hard": self.hard,
            "gate": self.gate,
        }


@dataclass
class DecisionState:
    intended: str = "WAIT"
    gated: str = "WAIT"
    executable: bool = False
    blockers: List[Blocker] = field(default_factory=list)
    block_reason: str = ""
    follow_ai: bool = False
    confidence: float = 0.0
    passed_gate: bool = False

    def add_blocker(
        self,
        code: str,
        message: str,
        *,
        hard: bool = True,
        gate: str = "",
    ) -> None:
        self.blockers.append(
            Blocker(code=code, message=message, hard=hard, gate=gate or code)
        )
        if not self.block_reason:
            self.block_reason = code
        if hard:
            self.executable = False
            self.passed_gate = False
            if self.gated in ("LONG", "SHORT"):
                self.gated = "WAIT"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intended": self.intended,
            "gated": self.gated,
            "executable": self.executable,
            "block_reason": self.block_reason,
            "follow_ai": self.follow_ai,
            "confidence": self.confidence,
            "passed_gate": self.passed_gate,
            "blockers": [b.to_dict() for b in self.blockers],
        }


def build_decision_state_from_advice(advice: Optional[Dict[str, Any]]) -> DecisionState:
    a = advice or {}
    intended = str(
        a.get("intended_direction")
        or a.get("raw_action")
        or a.get("ai_action")
        or "WAIT"
    ).upper()
    if intended in ("BUY",):
        intended = "LONG"
    elif intended in ("SELL", "HOLD", ""):
        intended = "WAIT" if intended in ("HOLD", "") else "SHORT"

    gated = str(a.get("action") or "WAIT").upper()
    if gated in ("BUY",):
        gated = "LONG"
    elif gated in ("SELL",):
        gated = "SHORT"
    elif gated not in ("LONG", "SHORT"):
        gated = "WAIT"

    passed = bool(a.get("passed_gate"))
    conf = float(a.get("confidence") or 0.0)
    state = DecisionState(
        intended=intended if intended in ("LONG", "SHORT", "WAIT") else "WAIT",
        gated=gated,
        executable=passed and gated in ("LONG", "SHORT"),
        follow_ai=bool(a.get("follow_ai_direction")),
        confidence=conf,
        passed_gate=passed,
        block_reason=str(a.get("block_reason") or ""),
    )

    existing = a.get("blockers")
    if isinstance(existing, list) and existing:
        for b in existing:
            if isinstance(b, dict):
                state.blockers.append(
                    Blocker(
                        code=str(b.get("code") or "gate"),
                        message=str(b.get("message") or ""),
                        hard=bool(b.get("hard", True)),
                        gate=str(b.get("gate") or b.get("code") or ""),
                    )
                )
            elif isinstance(b, str):
                state.blockers.append(Blocker(code="gate", message=b))
    else:
        for msg in a.get("gate_reasons") or []:
            code = _infer_blocker_code(str(msg))
            state.blockers.append(Blocker(code=code, message=str(msg), gate=code))
        if state.blockers and not state.block_reason:
            state.block_reason = state.blockers[0].code

    return state


def _infer_blocker_code(msg: str) -> str:
    m = msg or ""
    rules = (
        ("ai_hold", ("AI 建议持有", "ai_hold", "AI HOLD")),
        ("knowledge", ("知识门控", "知识卡片")),
        ("reuse", ("局面复用", "知识复用", "知识卡片复用")),
        ("confidence", ("置信度", "confidence")),
        ("net_rr", ("净 RR", "风险回报", "net_rr")),
        ("long_strict", ("LONG 加严", "long_strict", "做多加严")),
        ("funding", ("资金费率", "funding")),
        ("cooldown", ("冷却", "cooldown")),
        ("pattern", ("模式记忆", "pattern")),
        ("counterfactual", ("反事实", "counterfactual")),
        ("resonance", ("共振", "resonance")),
        ("circuit_breaker", ("熔断", "连亏", "circuit")),
        ("vote", ("综合投票", "投票未")),
    )
    for code, keys in rules:
        if any(k in m for k in keys):
            return code
    return "gate"


def attach_decision_state(advice: Dict[str, Any]) -> Dict[str, Any]:
    """把 DecisionState 写回 advice，统一审计字段。"""
    state = build_decision_state_from_advice(advice)
    # 若已有显式 block_reason（如 ai_hold），保留
    if advice.get("block_reason") and not state.block_reason:
        state.block_reason = str(advice["block_reason"])
    advice["decision_state"] = state.to_dict()
    advice["blockers"] = [b.to_dict() for b in state.blockers]
    if state.block_reason and not advice.get("block_reason"):
        advice["block_reason"] = state.block_reason
    # 禁止隐式用 raw_action 复活执行：executable 仅看 gated+passed
    advice["executable"] = bool(state.executable)
    return advice


# ── 门控注册表：声明 phase / hard / follow_ai 旁路 / probe 可覆盖 ──

GATE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "knowledge": {
        "phase": "advisor",
        "hard": True,
        "follow_ai_bypass": False,
        "probe_override": False,
    },
    "ta_playbook": {
        "phase": "post",
        "hard": True,
        "follow_ai_bypass": False,
        "probe_override": True,
        "skip_in_advisor_when_duplicate": True,
    },
    "win_rate": {
        "phase": "post",
        "hard": True,
        "follow_ai_bypass": False,
        "probe_override": True,
        "skip_in_advisor_when_duplicate": True,
    },
    "mtf_filter": {
        "phase": "advisor",
        "hard": True,
        "follow_ai_bypass": True,
        "probe_override": False,
    },
    "news_filter": {
        "phase": "advisor",
        "hard": True,
        "follow_ai_bypass": True,
        "probe_override": False,
    },
    "guardrail": {
        "phase": "advisor",
        "hard": True,
        "follow_ai_bypass": True,
        "probe_override": False,
    },
    "pattern_memory": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "counterfactual": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "factor_attribution": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "funding": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "confidence_hard": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "mtf_resonance": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "cross_modal": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "net_rr": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": False},
    "long_strict": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": True},
    "follow_cooldown": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": False},
    "anti_memory": {"phase": "post", "hard": True, "follow_ai_bypass": False, "probe_override": False},
    "circuit_breaker": {"phase": "advisor", "hard": True, "follow_ai_bypass": False, "probe_override": False},
    "ai_hold": {"phase": "advisor", "hard": True, "follow_ai_bypass": False, "probe_override": False},
}


def gate_meta(name: str) -> Dict[str, Any]:
    return dict(GATE_REGISTRY.get(name) or {"phase": "post", "hard": True})
