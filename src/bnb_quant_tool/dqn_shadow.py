"""DQN 影子投票 + 千笔增量训练钩子（不直接接管开仓）。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_AGENT = None
_STATE_PATH: Optional[Path] = None


def _state_file(config: Optional[Dict] = None) -> Path:
    global _STATE_PATH
    if _STATE_PATH is not None:
        return _STATE_PATH
    root = Path(__file__).resolve().parent.parent.parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    name = ((config or {}).get("dqn") or {}).get("state_file") or "dqn_shadow_state.json"
    _STATE_PATH = root / name
    return _STATE_PATH


def _load_meta(config: Optional[Dict] = None) -> Dict[str, Any]:
    path = _state_file(config)
    if not path.exists():
        return {"trades_since_train": 0, "train_count": 0, "shadow_hits": 0, "shadow_correct": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"trades_since_train": 0, "train_count": 0, "shadow_hits": 0, "shadow_correct": 0}


def _save_meta(meta: Dict[str, Any], config: Optional[Dict] = None) -> None:
    path = _state_file(config)
    try:
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("dqn meta save: %s", e)


def get_agent(config: Optional[Dict] = None):
    global _AGENT
    with _LOCK:
        if _AGENT is None:
            from bnb_quant_tool.deep_learning_engine import TradingAgent

            cfg = (config or {}).get("dqn") or {}
            _AGENT = TradingAgent(
                state_dim=int(cfg.get("state_dim", 32) or 32),
                action_dim=3,
            )
        return _AGENT


def indicators_to_state(indicators: Optional[Dict], dim: int = 32) -> np.ndarray:
    ind = indicators or {}
    keys = [
        "RSI", "ADX", "ATR", "MACD", "MACD_signal", "MACD_hist",
        "BB_upper", "BB_middle", "BB_lower", "MA_20", "MA_50", "MA_200",
        "Stoch_K", "Stoch_D", "OBV", "volume",
    ]
    vals: List[float] = []
    for k in keys:
        try:
            vals.append(float(ind.get(k) or 0))
        except (TypeError, ValueError):
            vals.append(0.0)
    while len(vals) < dim:
        vals.append(0.0)
    arr = np.asarray(vals[:dim], dtype=float)
    # 简单标准化
    std = arr.std()
    if std > 1e-9:
        arr = (arr - arr.mean()) / std
    return arr


def shadow_vote(
    indicators: Optional[Dict] = None,
    *,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """返回议会可用的辅助票：action/confidence/q_values。"""
    cfg = (config or {}).get("dqn") or {}
    if cfg.get("enabled", True) is False:
        return {"enabled": False}
    try:
        agent = get_agent(config)
        state = indicators_to_state(indicators, dim=int(cfg.get("state_dim", 32) or 32))
        q = agent._forward(state)
        idx = int(np.argmax(q))
        actions = ["SHORT", "WAIT", "LONG"]
        # softmax confidence
        ex = np.exp(q - np.max(q))
        probs = ex / (ex.sum() + 1e-12)
        conf = float(probs[idx])
        weight = float(cfg.get("shadow_vote_weight", 0.15) or 0.15)
        # 准确率达标前保持小权重
        meta = _load_meta(config)
        hits = int(meta.get("shadow_hits") or 0)
        correct = int(meta.get("shadow_correct") or 0)
        acc = (correct / hits) if hits >= 20 else 0.0
        if acc < float(cfg.get("promote_accuracy", 0.55) or 0.55):
            weight = min(weight, float(cfg.get("shadow_vote_weight", 0.15) or 0.15))
        else:
            weight = min(0.45, weight * 1.5)
        return {
            "enabled": True,
            "action": actions[idx],
            "confidence": round(conf, 4),
            "q_values": [round(float(x), 4) for x in q.tolist()],
            "vote_weight": round(weight, 4),
            "shadow_accuracy": round(acc, 4),
            "mode": "shadow",
        }
    except Exception as e:
        logger.debug("dqn shadow_vote: %s", e)
        return {"enabled": False, "error": str(e)[:120]}


def remember_close(
    *,
    indicators: Optional[Dict],
    side: str,
    pnl: float,
    config: Optional[Dict] = None,
) -> None:
    """平仓样本写入经验池，并可能触发增量训练。"""
    cfg = (config or {}).get("dqn") or {}
    if cfg.get("enabled", True) is False:
        return
    try:
        agent = get_agent(config)
        state = indicators_to_state(indicators, dim=int(cfg.get("state_dim", 32) or 32))
        side_u = (side or "").upper()
        action = 2 if side_u in ("LONG", "BUY") else (0 if side_u in ("SHORT", "SELL") else 1)
        reward = float(pnl)
        # 纪律奖励：小盈小亏略正；大亏重罚
        if reward < -50:
            reward -= 10
        next_state = state
        agent.remember(state, action, reward, next_state, True)

        meta = _load_meta(config)
        meta["trades_since_train"] = int(meta.get("trades_since_train") or 0) + 1
        # 影子准确率：方向与盈亏同号记正确
        pred = shadow_vote(indicators, config=config)
        if pred.get("enabled"):
            meta["shadow_hits"] = int(meta.get("shadow_hits") or 0) + 1
            pa = str(pred.get("action") or "WAIT").upper()
            ok = (pa == side_u and pnl > 0) or (pa == "WAIT" and abs(pnl) < 5) or (
                pa != side_u and pa != "WAIT" and pnl < 0
            )
            if ok:
                meta["shadow_correct"] = int(meta.get("shadow_correct") or 0) + 1

        trigger_n = int(cfg.get("train_every_n_trades", 1000) or 1000)
        if meta["trades_since_train"] >= trigger_n and len(agent.memory) >= 64:
            loss = 0.0
            for _ in range(int(cfg.get("train_steps", 50) or 50)):
                loss = agent.replay(batch_size=32)
            meta["trades_since_train"] = 0
            meta["train_count"] = int(meta.get("train_count") or 0) + 1
            meta["last_loss"] = round(float(loss or 0), 6)
            logger.info(
                "[DQN] incremental train #%s loss=%.6f memory=%s",
                meta["train_count"],
                loss,
                len(agent.memory),
            )
        _save_meta(meta, config)
    except Exception as e:
        logger.debug("dqn remember_close: %s", e)
