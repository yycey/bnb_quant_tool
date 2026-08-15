"""
策略胜率优化 — 将各机构策略历史胜率接入权重、投票与门控，提升综合胜率。

与 learning_analytics.win_rate_context 互补：
- 本模块聚焦「哪条策略在拖后腿 / 哪条策略可靠」
- learning_analytics 聚焦模拟盘、模式记忆、regime 亏损模式
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STRATEGY_WIN_RATE_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "min_samples_penalize": 5,
    "min_samples_disable": 8,
    "disable_wr": 0.25,
    "heavy_penalty_wr": 0.32,
    "light_penalty_wr": 0.40,
    "boost_wr": 0.58,
    "heavy_penalty_mult": 0.10,
    "light_penalty_mult": 0.40,
    "boost_mult": 1.35,
    "min_vote_weight": 0.02,
    "consensus_bad_penalty": 0.14,
    "consensus_good_boost": 0.10,
}


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(STRATEGY_WIN_RATE_DEFAULT_CFG)
    if config:
        merged.update(config)
    return merged


def load_strategy_performance_map(
    learner,
    regime: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """从学习库加载策略胜率表 {策略名: {win_rate, total, weight, regime_wr}}。"""
    out: Dict[str, Dict[str, Any]] = {}
    if learner is None:
        return out
    try:
        conn = learner._get_conn()
        cur = conn.cursor()
        filt = learner._strategy_filter_sql()
        cur.execute(
            f"""SELECT strategy_name, win_rate, total_predictions, correct_predictions, weight
                FROM strategy_performance
                WHERE is_active = 1 AND {filt}"""
        )
        for name, wr, total, correct, weight in cur.fetchall():
            out[str(name)] = {
                "win_rate": float(wr or 0),
                "total": int(total or 0),
                "correct": int(correct or 0),
                "weight": float(weight or 0),
                "regime_wr": None,
                "regime_total": 0,
            }

        if regime:
            from bnb_quant_tool.ai_learning_system import normalize_regime_bucket_for_learning

            bucket = normalize_regime_bucket_for_learning(regime)
            for reg in (regime, bucket):
                cur.execute(
                    """SELECT strategy_name, win_rate, total_predictions, weight
                       FROM strategy_regime_performance WHERE regime=?""",
                    (reg,),
                )
                for name, wr, total, weight in cur.fetchall():
                    key = str(name)
                    if key not in out:
                        out[key] = {
                            "win_rate": float(wr or 0),
                            "total": 0,
                            "correct": 0,
                            "weight": float(weight or 0),
                            "regime_wr": float(wr or 0),
                            "regime_total": int(total or 0),
                        }
                    elif int(total or 0) >= int(out[key].get("regime_total") or 0):
                        out[key]["regime_wr"] = float(wr or 0)
                        out[key]["regime_total"] = int(total or 0)
    except Exception as e:
        logger.debug("load_strategy_performance_map: %s", e)
    return out


def effective_strategy_wr(
    perf: Dict[str, Any],
    *,
    prefer_regime: bool = True,
) -> Tuple[float, int]:
    """返回 (有效胜率, 有效样本数)。"""
    if not perf:
        return 0.5, 0
    rt = int(perf.get("regime_total") or 0)
    if prefer_regime and rt >= 3 and perf.get("regime_wr") is not None:
        return float(perf["regime_wr"]), rt
    total = int(perf.get("total") or 0)
    return float(perf.get("win_rate") or 0.5), total


def adjust_weight_by_performance(
    base_weight: float,
    perf: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> float:
    """按历史胜率缩放单策略投票权重。"""
    cfg = _cfg(config)
    if not cfg.get("enabled", True) or not perf:
        return base_weight

    wr, total = effective_strategy_wr(perf)
    min_pen = int(cfg["min_samples_penalize"])
    min_dis = int(cfg["min_samples_disable"])
    w = float(base_weight)

    if total >= min_dis and wr < float(cfg["disable_wr"]):
        return 0.0
    if total >= min_pen:
        if wr < float(cfg["heavy_penalty_wr"]):
            w *= float(cfg["heavy_penalty_mult"])
        elif wr < float(cfg["light_penalty_wr"]):
            w *= float(cfg["light_penalty_mult"])
        elif wr >= float(cfg["boost_wr"]):
            w *= float(cfg["boost_mult"])

    return max(float(cfg["min_vote_weight"]), min(2.0, w))


def penalize_weights_by_performance(
    weights: Dict[str, float],
    perf_map: Dict[str, Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """对学习权重整体按胜率再缩放并归一化。"""
    if not weights or not perf_map:
        return weights
    cfg = _cfg(config)
    if not cfg.get("enabled", True):
        return weights

    adjusted = {
        name: adjust_weight_by_performance(w, perf_map.get(name), cfg)
        for name, w in weights.items()
    }
    adjusted = {k: v for k, v in adjusted.items() if v > 0}
    total = sum(adjusted.values())
    if total <= 0:
        return weights
    return {k: round(v / total, 6) for k, v in adjusted.items()}


def analyze_institutional_consensus(
    institutional: Dict[str, Any],
    perf_map: Dict[str, Dict[str, Any]],
    weights: Optional[Dict[str, float]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """分析当前机构投票中好/差策略占比，输出方向加减分。"""
    cfg = _cfg(config)
    result: Dict[str, Any] = {
        "long_boost": 0.0,
        "short_boost": 0.0,
        "long_penalty": 0.0,
        "short_penalty": 0.0,
        "bad_strategies": [],
        "good_strategies": [],
        "reasons": [],
    }
    if not cfg.get("enabled", True) or not perf_map:
        return result

    details = institutional.get("strategy_details") or {}
    if not details:
        return result

    weights = weights or {}
    min_pen = int(cfg["min_samples_penalize"])
    heavy_wr = float(cfg["heavy_penalty_wr"])
    boost_wr = float(cfg["boost_wr"])

    bad_long = bad_short = good_long = good_short = 0.0
    bad_names: List[str] = []
    good_names: List[str] = []

    for key, res in details.items():
        if not isinstance(res, dict):
            continue
        signal = str(res.get("signal") or "HOLD").upper()
        if signal not in ("BUY", "SELL"):
            continue
        display = str(res.get("strategy") or key)
        perf = perf_map.get(display) or perf_map.get(key) or {}
        wr, total = effective_strategy_wr(perf)
        if total < min_pen:
            continue
        vw = float(res.get("vote_weight") or weights.get(display) or weights.get(key) or 0.05)

        if wr < heavy_wr:
            bad_names.append(f"{display}({wr:.0%})")
            if signal == "BUY":
                bad_long += vw
            else:
                bad_short += vw
        elif wr >= boost_wr:
            good_names.append(f"{display}({wr:.0%})")
            if signal == "BUY":
                good_long += vw
            else:
                good_short += vw

    pen_scale = float(cfg["consensus_bad_penalty"])
    boost_scale = float(cfg["consensus_good_boost"])

    if bad_long > 0.12:
        result["long_penalty"] = pen_scale * min(1.0, bad_long)
        result["reasons"].append(f"做多票含差策略: {', '.join(bad_names[:3])}")
    if bad_short > 0.12:
        result["short_penalty"] = pen_scale * min(1.0, bad_short)
        result["reasons"].append(f"做空票含差策略: {', '.join(bad_names[:3])}")
    if good_long > 0.15:
        result["long_boost"] = boost_scale * min(1.0, good_long)
    if good_short > 0.15:
        result["short_boost"] = boost_scale * min(1.0, good_short)

    result["bad_strategies"] = bad_names[:5]
    result["good_strategies"] = good_names[:5]
    return result


def merge_consensus_into_win_rate_context(
    ctx: Dict[str, Any],
    consensus: Dict[str, Any],
) -> Dict[str, Any]:
    """将策略共识分析合并进 win_rate_context。"""
    if not consensus:
        return ctx
    out = dict(ctx)
    out["long_boost"] = float(out.get("long_boost") or 0) + float(consensus.get("long_boost") or 0)
    out["short_boost"] = float(out.get("short_boost") or 0) + float(consensus.get("short_boost") or 0)
    out["long_penalty"] = float(out.get("long_penalty") or 0) + float(consensus.get("long_penalty") or 0)
    out["short_penalty"] = float(out.get("short_penalty") or 0) + float(consensus.get("short_penalty") or 0)
    for r in consensus.get("reasons") or []:
        if r not in (out.get("reasons") or []):
            out.setdefault("reasons", []).append(r)
    if consensus.get("good_strategies"):
        out.setdefault("hints", []).append(
            f"可靠策略支持: {', '.join(consensus['good_strategies'][:3])}"
        )
    return out


def format_strategy_win_rate_for_prompt(
    perf_map: Dict[str, Dict[str, Any]],
    *,
    best: Optional[List[Dict]] = None,
    worst: Optional[List[Dict]] = None,
    regime: Optional[str] = None,
) -> str:
    """注入 DeepSeek：当前 regime 下策略胜率纪律。"""
    if not perf_map and not best and not worst:
        return ""

    lines = ["--- 策略胜率学习（必须参考，差策略降权、好策略加权） ---"]
    if regime:
        lines.append(f"  当前市场: {regime}")
    if worst:
        lines.append("  历史较差策略（谨慎跟随其投票方向）:")
        for s in worst[:4]:
            lines.append(
                f"    · {s.get('name', '?')}: 胜率 {float(s.get('win_rate', 0)):.0%} "
                f"({s.get('correct', 0)}/{s.get('total', 0)})"
            )
    if best:
        lines.append("  历史可靠策略（可加强其方向信号）:")
        for s in best[:4]:
            lines.append(
                f"    · {s.get('name', '?')}: 胜率 {float(s.get('win_rate', 0)):.0%} "
                f"({s.get('correct', 0)}/{s.get('total', 0)})"
            )
    lines.append("  规则: 差策略(胜率<32%)主导的方向 → 降低置信或 WAIT；好策略(>58%)主导 → 可适度加强。")
    lines.append("")
    return "\n".join(lines)


def resolve_strategy_win_rate_config(
    app_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return dict((app_config or {}).get("win_rate_strategy") or {})
