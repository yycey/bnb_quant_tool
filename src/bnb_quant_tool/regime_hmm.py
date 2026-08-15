"""
HMM 隐状态 Regime — 2 状态 Gaussian HMM（纯 numpy，无 sklearn 依赖）。

与规则 + 多信号融合 ensemble：HMM 提供平滑的隐状态概率，
当与融合结果一致时提升置信度，冲突时写入 regime_conflicts。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from bnb_quant_tool.market_regime import (
    HIGH_VOLATILITY,
    PANIC,
    RANGING,
    TRENDING,
)

_EPS = 1e-8


def _log_gaussian_pdf(x: np.ndarray, mean: float, var: float) -> np.ndarray:
    var = max(float(var), _EPS)
    return -0.5 * (np.log(2.0 * np.pi * var) + (x - mean) ** 2 / var)


def _fit_gaussian_hmm_1d(
    obs: np.ndarray,
    n_states: int = 2,
    n_iter: int = 40,
    seed: int = 42,
) -> Dict[str, Any]:
    """Baum-Welch EM for 1D Gaussian HMM."""
    obs = np.asarray(obs, dtype=float).ravel()
    obs = obs[np.isfinite(obs)]
    t_len = len(obs)
    if t_len < 30:
        raise ValueError("HMM 需要至少 30 个观测点")

    rng = np.random.default_rng(seed)
    means = np.array([np.percentile(obs, 25), np.percentile(obs, 75)], dtype=float)
    if n_states != 2:
        means = np.linspace(obs.min(), obs.max(), n_states)
    vars_ = np.full(n_states, max(float(np.var(obs)), _EPS))
    trans = np.full((n_states, n_states), 1.0 / n_states)
    start = np.full(n_states, 1.0 / n_states)

    for _ in range(n_iter):
        # E-step: forward-backward
        log_em = np.column_stack([
            _log_gaussian_pdf(obs, means[k], vars_[k]) for k in range(n_states)
        ])
        log_em = np.clip(log_em, -50, 50)

        alpha = np.zeros((t_len, n_states))
        log_start = np.log(start + _EPS)
        alpha[0] = log_start + log_em[0]
        alpha[0] -= np.max(alpha[0])
        for t in range(1, t_len):
            for j in range(n_states):
                alpha[t, j] = log_em[t, j] + np.logaddexp.reduce(
                    alpha[t - 1] + np.log(trans[:, j] + _EPS)
                )
            alpha[t] -= np.max(alpha[t])

        beta = np.zeros((t_len, n_states))
        for t in range(t_len - 2, -1, -1):
            for i in range(n_states):
                beta[t, i] = np.logaddexp.reduce(
                    log_em[t + 1] + np.log(trans[i] + _EPS) + beta[t + 1]
                )
            beta[t] -= np.max(beta[t])

        gamma = alpha + beta
        gamma = np.exp(gamma - gamma.max(axis=1, keepdims=True))
        gamma /= gamma.sum(axis=1, keepdims=True) + _EPS

        xi = np.zeros((t_len - 1, n_states, n_states))
        for t in range(t_len - 1):
            log_xi = (
                alpha[t][:, None]
                + np.log(trans + _EPS)
                + log_em[t + 1][None, :]
                + beta[t + 1][None, :]
            )
            log_xi -= np.logaddexp.reduce(log_xi.ravel())
            xi[t] = np.exp(log_xi)

        start = gamma[0] + _EPS
        start /= start.sum()
        trans = xi.sum(axis=0) + _EPS
        trans /= trans.sum(axis=1, keepdims=True)

        for k in range(n_states):
            w = gamma[:, k]
            w_sum = w.sum() + _EPS
            means[k] = float((w * obs).sum() / w_sum)
            vars_[k] = float((w * (obs - means[k]) ** 2).sum() / w_sum)
            vars_[k] = max(vars_[k], _EPS)

    last_gamma = gamma[-1]
    return {
        "means": means,
        "vars": vars_,
        "trans": trans,
        "start": start,
        "state_probs": last_gamma,
        "dominant_state": int(np.argmax(last_gamma)),
    }


def _state_to_regime(
    state: int,
    means: np.ndarray,
    vars_: np.ndarray,
    vol_ratio: float,
    cfg: Dict,
) -> Tuple[str, str]:
    """将 HMM 状态映射到 market_regime 标签。"""
    trend_th = float(cfg.get("trend_mean_threshold", 0.0003))
    panic_th = float(cfg.get("panic_mean_threshold", -0.0008))
    vol_hi = float(cfg.get("high_vol_ratio", 1.35))

    mu = float(means[state])
    var = float(vars_[state])

    if vol_ratio >= vol_hi or var >= float(np.max(vars_)) * 1.2:
        return HIGH_VOLATILITY, f"HMM 状态{state} 高波动 (μ={mu:.5f}, vol比={vol_ratio:.2f})"
    if mu <= panic_th:
        return PANIC, f"HMM 状态{state} 负漂移 (μ={mu:.5f})"
    if mu >= trend_th:
        return TRENDING, f"HMM 状态{state} 正漂移 (μ={mu:.5f})"
    return RANGING, f"HMM 状态{state} 均值回归 (μ={mu:.5f})"


def infer_hmm_regime(
    df: pd.DataFrame,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """对收盘价序列拟合 2 状态 HMM，推断当前隐状态 Regime。"""
    cfg = config or {}
    lookback = int(cfg.get("lookback", 120))
    min_bars = int(cfg.get("min_bars", 60))

    if df is None or len(df) < min_bars:
        return {
            "hmm_enabled": False,
            "hmm_regime": None,
            "hmm_confidence": 0.0,
            "reason": "K线不足",
        }

    closes = df["close"].astype(float).values[-lookback:]
    if len(closes) < min_bars:
        return {"hmm_enabled": False, "hmm_regime": None, "reason": "lookback 不足"}

    log_ret = np.diff(np.log(closes + _EPS))
    if len(log_ret) < 30:
        return {"hmm_enabled": False, "hmm_regime": None, "reason": "收益序列不足"}

    try:
        model = _fit_gaussian_hmm_1d(
            log_ret,
            n_states=int(cfg.get("n_states", 2)),
            n_iter=int(cfg.get("em_iterations", 40)),
            seed=int(cfg.get("seed", 42)),
        )
    except Exception as e:
        return {"hmm_enabled": False, "hmm_regime": None, "reason": str(e)}

    # 波动率比（当前 vs 历史）
    vol_win = int(cfg.get("vol_window", 20))
    if len(log_ret) >= vol_win * 2:
        recent_vol = float(np.std(log_ret[-vol_win:]))
        hist_vol = float(np.std(log_ret[:-vol_win])) or _EPS
        vol_ratio = recent_vol / hist_vol
    else:
        vol_ratio = 1.0

    state = model["dominant_state"]
    conf = float(model["state_probs"][state])
    regime, detail = _state_to_regime(
        state, model["means"], model["vars"], vol_ratio, cfg
    )

    # 按均值排序状态标签（便于解释）
    order = np.argsort(model["means"])
    state_labels = ["低漂移", "高漂移"] if len(order) == 2 else [f"S{i}" for i in order]

    return {
        "hmm_enabled": True,
        "hmm_regime": regime,
        "hmm_confidence": round(conf, 3),
        "hmm_state": state,
        "hmm_state_probs": [round(float(p), 4) for p in model["state_probs"]],
        "hmm_state_means": [round(float(m), 6) for m in model["means"]],
        "hmm_vol_ratio": round(vol_ratio, 3),
        "hmm_detail": detail,
        "hmm_state_labels": state_labels,
        "description": f"HMM → {regime} (置信 {conf:.0%})",
    }


def merge_hmm_into_regime(
    base: Dict[str, Any],
    hmm: Dict[str, Any],
    *,
    prefer_hmm_on_conflict: bool = False,
    min_hmm_confidence: float = 0.65,
) -> Dict[str, Any]:
    """将 HMM 结果 ensemble 进 market_regime 字典。"""
    out = dict(base)
    if not hmm or not hmm.get("hmm_enabled") or not hmm.get("hmm_regime"):
        return out

    hmm_regime = hmm["hmm_regime"]
    hmm_conf = float(hmm.get("hmm_confidence") or 0)
    current = str(out.get("regime") or "")

    out["hmm_regime"] = hmm_regime
    out["hmm_confidence"] = hmm_conf
    out["hmm_state_probs"] = hmm.get("hmm_state_probs")
    out["hmm_detail"] = hmm.get("hmm_detail")
    out["hmm_state_means"] = hmm.get("hmm_state_means")

    conflicts = list(out.get("regime_conflicts") or [])
    fusion_conf = float(out.get("fusion_confidence") or 0)

    if current == hmm_regime:
        out["hmm_agreement"] = True
        boosted = min(1.0, fusion_conf + hmm_conf * 0.15)
        out["fusion_confidence"] = round(boosted, 3)
        reasons = list(out.get("reasons") or [])
        reasons.append(f"HMM 确认: {hmm.get('hmm_detail', '')}")
        out["reasons"] = reasons[:10]
    else:
        out["hmm_agreement"] = False
        conflicts.append(
            f"HMM={hmm_regime} ({hmm_conf:.0%}) vs 融合={current} — 隐状态与规则不一致"
        )
        out["regime_conflicts"] = conflicts
        if prefer_hmm_on_conflict and hmm_conf >= min_hmm_confidence:
            out["regime"] = hmm_regime
            out["legacy_regime"] = current
            out["description"] = hmm.get("description") or out.get("description")
            from bnb_quant_tool.market_regime import MarketRegimeDetector
            det = MarketRegimeDetector()
            out["strategy_multipliers"] = det.get_regime_multipliers(hmm_regime)
            out["position_factor"] = det._regime_position_factor(hmm_regime)

    return out
