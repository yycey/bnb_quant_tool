"""
交易质量评分 (Trade Quality Score)
用 MFE/MAE、持仓效率、RR 达成等评估单笔交易质量，避免仅靠 WIN/LOSS 学习。
"""

from __future__ import annotations

from typing import Dict, Optional


def score_closed_trade(row: Dict) -> Dict:
    """
    根据模拟盘/实盘平仓记录计算质量分 (0-100)。

    Args:
        row: 含 realized_pnl_usdt, mfe_pct, mae_pct, mfe_r, mae_r,
             r_multiple, close_reason, tp1_hit, side 等字段
    """
    pnl = float(row.get("realized_pnl_usdt", 0) or 0)
    mfe_r = float(row.get("mfe_r", 0) or 0)
    mae_r = float(row.get("mae_r", 0) or 0)
    r_mult = row.get("r_multiple")
    close_reason = str(row.get("close_reason") or "")
    tp1_hit = bool(row.get("tp1_hit"))

    # 归一化 PnL 分量 (-1 ~ 1)，假设单笔风险约 50-150 USDT
    pnl_norm = max(-1.0, min(1.0, pnl / 100.0))

    rr_hit = 1.0 if (r_mult is not None and float(r_mult) >= 1.0) else 0.0
    if tp1_hit or "TP" in close_reason.upper():
        rr_hit = max(rr_hit, 0.7)

    mfe_component = max(0.0, min(1.0, mfe_r / 2.0)) if mfe_r else 0.0
    mae_penalty = max(0.0, min(1.0, abs(mae_r) / 2.0)) if mae_r else 0.0

    holding_penalty = 0.0
    reason_u = close_reason.upper()
    if reason_u in ("TIMEOUT", "EXPIRED", "TIMEOUT_NO_TP"):
        holding_penalty = 0.15
    elif reason_u in ("ADMIT_WRONG", "REEVAL_EARLY"):
        # 认错短平：过程差但纪律对，略罚持仓质量、不额外重罚
        holding_penalty = 0.05

    raw = (
        0.30 * pnl_norm
        + 0.20 * rr_hit
        + 0.20 * mfe_component
        - 0.20 * mae_penalty
        - 0.10 * holding_penalty
    )
    score = int(max(0, min(100, (raw + 0.5) * 100)))

    tier = "A" if score >= 75 else ("B" if score >= 55 else ("C" if score >= 35 else "D"))
    label = {
        "A": "优质交易",
        "B": "合格",
        "C": "勉强",
        "D": "劣质",
    }.get(tier, tier)

    suggest_feedback = _suggest_feedback(pnl, score, tier)

    return {
        "score": score,
        "tier": tier,
        "label": label,
        "suggest_feedback": suggest_feedback,
        "components": {
            "pnl_norm": round(pnl_norm, 3),
            "rr_hit": rr_hit,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
        },
        "text": f"交易质量 {score}/100 ({label}) — MFE_R={mfe_r:.2f} MAE_R={mae_r:.2f}",
    }


def _suggest_feedback(pnl: float, score: int, tier: str) -> str:
    """在 WIN/LOSS 基础上给出学习反馈建议（不强制覆盖）。"""
    if pnl > 0.5 and tier == "D":
        return "BREAK_EVEN"  # 侥幸盈利但过程很差
    if pnl < -0.5 and score >= 55:
        return "BREAK_EVEN"  # 方向对但止损过紧
    if pnl > 0.5:
        return "WIN"
    if pnl < -0.5:
        return "LOSS"
    return "BREAK_EVEN"
