"""
机构策略池热更新 — 发现/晋升后无需重启即可进投票。

支持：
1. 同进程：register 后 reload_discovered_strategies
2. 跨进程：bump 信号文件，分析进程周期 maybe_reload_from_signal
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_pool: Any = None
_last_applied_version: int = 0

SIGNAL_REL = "data/strategy_pool_signal.json"


def _workspace_root(project_root: Optional[Path] = None) -> Path:
    if project_root is not None:
        return Path(project_root).resolve()
    try:
        from bnb_quant_tool.data_localization import get_localization_manager

        return Path(get_localization_manager().workspace).resolve()
    except Exception:
        return Path(__file__).resolve().parents[2]


def signal_path(project_root: Optional[Path] = None) -> Path:
    return _workspace_root(project_root) / SIGNAL_REL


def register_strategy_pool(inst: Any) -> None:
    """注册当前进程使用的 InstitutionalStrategies 实例。"""
    global _pool
    with _lock:
        _pool = inst
        logger.debug("strategy pool registered: %s", type(inst).__name__)


def get_strategy_pool() -> Any:
    with _lock:
        return _pool


def bump_reload_signal(
    *,
    reason: str = "",
    project_root: Optional[Path] = None,
) -> dict:
    """任意进程在发现/晋升后调用：通知持有策略池的分析进程热加载。"""
    path = signal_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = 0
    if path.is_file():
        try:
            prev = int(json.loads(path.read_text(encoding="utf-8")).get("version") or 0)
        except Exception:
            prev = 0
    version = prev + 1
    payload = {
        "version": version,
        "reason": reason or "bump",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "version": version, "path": str(path)}


def read_reload_signal(project_root: Optional[Path] = None) -> dict:
    path = signal_path(project_root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def reload_discovered_strategies(
    *,
    reason: str = "",
    project_root: Optional[Path] = None,
    bump: bool = True,
) -> dict:
    """热加载 discovered/promoted 策略到已注册池；无池时仍 bump 跨进程信号。"""
    if bump:
        try:
            bump_reload_signal(reason=reason or "reload", project_root=project_root)
        except Exception as e:
            logger.debug("bump reload signal: %s", e)

    with _lock:
        pool = _pool
    if pool is None:
        return {"ok": True, "skipped": True, "reason": "no_pool", "signaled": bump}

    try:
        if hasattr(pool, "reload_discovered"):
            n = int(pool.reload_discovered() or 0)
        else:
            n = 0
            if hasattr(pool, "_register_discovered_strategies"):
                before = set((getattr(pool, "strategies", {}) or {}).keys())
                pool._register_discovered_strategies()
                after = set((getattr(pool, "strategies", {}) or {}).keys())
                n = len(after - before)
        sig = read_reload_signal(project_root)
        ver = int(sig.get("version") or 0)
        global _last_applied_version
        with _lock:
            if ver > 0:
                _last_applied_version = ver
        logger.info("策略池热加载: n=%s reason=%s ver=%s", n, reason or "-", ver)
        return {"ok": True, "reloaded": n, "reason": reason, "version": ver}
    except Exception as e:
        logger.warning("策略池热加载失败: %s", e)
        return {"ok": False, "error": str(e)}


def maybe_reload_from_signal(
    *,
    project_root: Optional[Path] = None,
    force: bool = False,
) -> dict:
    """分析进程周期调用：若信号版本更新则 reload。"""
    global _last_applied_version
    sig = read_reload_signal(project_root)
    ver = int(sig.get("version") or 0)
    with _lock:
        pool = _pool
        applied = _last_applied_version
    if pool is None:
        return {"ok": True, "skipped": True, "reason": "no_pool"}
    if not force and (ver <= 0 or ver <= applied):
        return {
            "ok": True,
            "skipped": True,
            "reason": "up_to_date",
            "version": ver,
            "applied": applied,
        }
    out = reload_discovered_strategies(
        reason=str(sig.get("reason") or "signal"),
        project_root=project_root,
        bump=False,
    )
    with _lock:
        _last_applied_version = max(ver, int(out.get("version") or 0) or ver)
    out["from_signal"] = True
    return out
