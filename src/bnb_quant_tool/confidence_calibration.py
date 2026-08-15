"""LLM 置信度后验校准（Platt 缩放，按 Regime / 场景分桶）。

用历史 analysis_records 的 (原始置信度 → WIN/LOSS) 拟合
p = σ(A · conf + B)，校准后再用于硬门控与 Kelly。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_GLOBAL = "GLOBAL"
_CACHE: Dict[str, Any] = {"models": {}, "loaded_at": 0.0}


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _norm_conf(c: Any) -> float:
    try:
        v = float(c or 0)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return _clamp01(v)


def _data_dir(config: Optional[dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager

        root = Path(get_localization_manager().workspace)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def params_path(config: Optional[dict] = None) -> Path:
    cfg = (config or {}).get("confidence_calibration") or {}
    raw = cfg.get("params_path") or "data/confidence_platt.json"
    p = Path(str(raw))
    if p.is_absolute():
        return p
    return _data_dir(config) / p.name


def fit_platt(
    confidences: List[float],
    outcomes: List[int],
    *,
    max_iter: int = 80,
) -> Dict[str, float]:
    """极大似然拟合 p = σ(A·x + B)。outcomes: 1=WIN, 0=LOSS。"""
    xs = [_norm_conf(c) for c in confidences]
    ys = [1 if int(y) else 0 for y in outcomes]
    n = len(xs)
    if n < 5 or sum(ys) == 0 or sum(ys) == n:
        # 退化：恒等映射附近
        return {"A": 1.0, "B": 0.0, "n": float(n), "degenerate": 1.0}

    # 初值：轻微压缩过度自信
    A, B = 2.0, -1.0
    for _ in range(max_iter):
        gA = gB = 0.0
        hAA = hAB = hBB = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(A * x + B)
            # 稳定：避免 0/1
            p = max(1e-6, min(1.0 - 1e-6, p))
            err = p - y
            gA += err * x
            gB += err
            w = p * (1.0 - p)
            hAA += w * x * x
            hAB += w * x
            hBB += w
        # 2x2 Newton
        det = hAA * hBB - hAB * hAB
        if abs(det) < 1e-12:
            break
        dA = (hBB * gA - hAB * gB) / det
        dB = (hAA * gB - hAB * gA) / det
        A -= dA
        B -= dB
        if abs(dA) + abs(dB) < 1e-6:
            break
    return {"A": float(A), "B": float(B), "n": float(n), "degenerate": 0.0}


def apply_platt(conf: float, model: Optional[Dict[str, float]]) -> float:
    if not model or model.get("degenerate"):
        # 无模型时轻度收缩过度自信：0.8*c + 0.1
        c = _norm_conf(conf)
        return _clamp01(0.85 * c + 0.05)
    c = _norm_conf(conf)
    return _clamp01(_sigmoid(float(model["A"]) * c + float(model["B"])))


def _regime_key(regime: Any) -> str:
    if isinstance(regime, dict):
        r = str(regime.get("regime") or regime.get("label") or _GLOBAL)
    else:
        r = str(regime or _GLOBAL)
    r = r.strip().upper() or _GLOBAL
    return r


def load_models(config: Optional[dict] = None, *, force: bool = False) -> Dict[str, Dict]:
    now = time.time()
    ttl = float(((config or {}).get("confidence_calibration") or {}).get("cache_seconds", 300) or 300)
    if not force and _CACHE.get("models") and (now - float(_CACHE.get("loaded_at") or 0)) < ttl:
        return dict(_CACHE["models"])
    path = params_path(config)
    models: Dict[str, Dict] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            models = dict(raw.get("by_regime") or {})
        except Exception as e:
            logger.debug("load platt models: %s", e)
    _CACHE["models"] = models
    _CACHE["loaded_at"] = now
    return models


def save_models(models: Dict[str, Dict], config: Optional[dict] = None) -> Path:
    path = params_path(config)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "by_regime": models,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _CACHE["models"] = dict(models)
    _CACHE["loaded_at"] = time.time()
    return path


def fetch_labeled_samples(
    db_path: str,
    *,
    min_samples: int = 20,
    lookback: int = 500,
) -> Dict[str, Tuple[List[float], List[int]]]:
    """从 analysis_records 拉取按 regime 分桶的 (conf, win) 样本。"""
    path = Path(db_path)
    if not path.exists():
        return {}
    buckets: Dict[str, Tuple[List[float], List[int]]] = {}
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(analysis_records)").fetchall()}
            has_snap = "trade_advice_snapshot" in cols
            sql = """
                SELECT ai_confidence, consensus_confidence, market_regime,
                       actual_result, trading_action, pnl_percent
                {snap}
                FROM analysis_records
                WHERE actual_result IS NOT NULL AND actual_result != ''
                ORDER BY id DESC LIMIT ?
            """.format(snap=", trade_advice_snapshot" if has_snap else "")
            rows = conn.execute(sql, (int(lookback),)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("fetch calibration samples: %s", e)
        return {}

    for row in rows:
        act = str(row["trading_action"] or "").upper()
        # 仅校准开仓方向；观望不参与拟合
        if act in ("WAIT", "HOLD", "持有"):
            continue

        result = str(row["actual_result"] or "").upper()
        if result not in ("WIN", "LOSS", "CORRECT", "WRONG", "TRUE", "FALSE"):
            # 兼容中文
            if "赢" in result or "盈" in result or "对" in result:
                y = 1
            elif "亏" in result or "错" in result or "败" in result:
                y = 0
            else:
                continue
        else:
            y = 1 if result in ("WIN", "CORRECT", "TRUE") else 0

        # 优先 snapshot.raw_confidence（真正 LLM 原始分），再 ai_confidence
        conf = None
        try:
            if "trade_advice_snapshot" in row.keys() and row["trade_advice_snapshot"]:
                snap = row["trade_advice_snapshot"]
                snap = json.loads(snap) if isinstance(snap, str) else snap
                if isinstance(snap, dict):
                    conf = snap.get("raw_confidence") or snap.get("confidence")
        except Exception:
            conf = None
        if conf is None or float(conf or 0) <= 0:
            conf = row["ai_confidence"]
        if conf is None or float(conf or 0) <= 0:
            conf = row["consensus_confidence"]
        x = _norm_conf(conf)
        if x <= 0:
            continue
        regime = _regime_key(row["market_regime"] or _GLOBAL)
        xs, ys = buckets.setdefault(regime, ([], []))
        xs.append(x)
        ys.append(y)
        # 同时积累 GLOBAL
        gxs, gys = buckets.setdefault(_GLOBAL, ([], []))
        gxs.append(x)
        gys.append(y)

    # 过滤过少桶（GLOBAL 除外）
    out = {}
    for k, (xs, ys) in buckets.items():
        if k == _GLOBAL or len(xs) >= min_samples // 2:
            out[k] = (xs, ys)
    return out


def refit_from_learner(learner, config: Optional[dict] = None) -> Dict[str, Any]:
    """用 learner DB 重拟合并落盘。"""
    cfg = (config or {}).get("confidence_calibration") or {}
    if cfg.get("enabled", True) is False:
        return {"ok": False, "reason": "disabled"}
    db = getattr(learner, "db_path", None) if learner is not None else None
    if not db:
        return {"ok": False, "reason": "no_db"}
    min_n = int(cfg.get("min_samples", 20) or 20)
    lookback = int(cfg.get("lookback", 500) or 500)
    buckets = fetch_labeled_samples(str(db), min_samples=min_n, lookback=lookback)
    models: Dict[str, Dict] = {}
    for regime, (xs, ys) in buckets.items():
        if len(xs) < max(5, min_n // 2) and regime != _GLOBAL:
            continue
        if len(xs) < 5:
            continue
        models[regime] = fit_platt(xs, ys)
    if not models:
        return {"ok": False, "reason": "insufficient_samples", "buckets": {k: len(v[0]) for k, v in buckets.items()}}
    save_models(models, config)
    return {"ok": True, "regimes": list(models.keys()), "n": {k: int(m.get("n") or 0) for k, m in models.items()}}


def calibrate_confidence(
    raw_conf: float,
    *,
    regime: Any = None,
    config: Optional[dict] = None,
    learner=None,
    auto_refit: bool = True,
) -> Tuple[float, Dict[str, Any]]:
    """返回 (校准置信度, 元信息)。"""
    cfg = (config or {}).get("confidence_calibration") or {}
    raw = _norm_conf(raw_conf)
    if cfg.get("enabled", True) is False:
        return raw, {"enabled": False, "raw": raw, "calibrated": raw}

    models = load_models(config)
    # 样本足够且过期则尝试重拟合
    if auto_refit and learner is not None:
        refit_every = float(cfg.get("refit_seconds", 3600) or 3600)
        path = params_path(config)
        stale = True
        if path.exists():
            stale = (time.time() - path.stat().st_mtime) > refit_every
        if stale or not models:
            try:
                refit_from_learner(learner, config)
                models = load_models(config, force=True)
            except Exception as e:
                logger.debug("auto refit platt: %s", e)

    key = _regime_key(regime)
    model = models.get(key) or models.get(_GLOBAL)
    cal = apply_platt(raw, model)
    meta = {
        "enabled": True,
        "raw": round(raw, 4),
        "calibrated": round(cal, 4),
        "regime": key,
        "model_regime": key if key in models else (_GLOBAL if _GLOBAL in models else None),
        "A": (model or {}).get("A"),
        "B": (model or {}).get("B"),
        "n": (model or {}).get("n"),
    }
    return cal, meta


def apply_confidence_calibration(
    advice: Dict[str, Any],
    *,
    ai_analysis: Optional[Dict] = None,
    learning_context: Optional[Dict] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """写入 raw/calibrated confidence，并把 advice.confidence 替换为校准值供下游使用。"""
    cfg = (config or {}).get("confidence_calibration") or {}
    if cfg.get("enabled", True) is False:
        return advice

    out = dict(advice)
    lc = learning_context or {}
    ai = ai_analysis if isinstance(ai_analysis, dict) else {}

    conf = float(out.get("confidence") or 0)
    if conf > 1.0:
        conf = conf / 100.0
    ai_conf = float(ai.get("confidence") or 0)
    if ai_conf > 1.0:
        ai_conf = ai_conf / 100.0
    raw = min(conf, ai_conf) if ai_conf > 0 else conf

    regime = (
        out.get("market_regime")
        or lc.get("market_regime")
        or lc.get("regime")
        or (ai.get("_situation_regime"))
    )
    learner = lc.get("_learner") or lc.get("learner")
    if learner is None:
        # capability path sometimes embeds via paper only — skip auto refit
        pass

    cal, meta = calibrate_confidence(
        raw, regime=regime, config=config, learner=learner, auto_refit=bool(cfg.get("auto_refit", True))
    )
    out["raw_confidence"] = round(raw, 4)
    out["calibrated_confidence"] = round(cal, 4)
    out["confidence"] = round(cal, 4)
    out["confidence_calibration"] = meta
    return out
