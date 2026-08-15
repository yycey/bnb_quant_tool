#!/usr/bin/env python3
"""Web 维护桥接 — 健康检查 / 修复 / 优化 / 备份 / Git 更新 / 上传 zip"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# 避免 init_workspace 的 INFO 日志干扰 PHP 解析 stdout JSON
logging.basicConfig(level=logging.WARNING, force=True)

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

import yaml

from bnb_quant_tool.data_localization import init_workspace
from bnb_quant_tool.system_maintenance import SystemMaintenance

init_workspace(str(PROJECT_DIR))


def load_config() -> dict:
    cfg_path = PROJECT_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def respond(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def main() -> int:
    if len(sys.argv) < 2:
        return respond({"ok": False, "error": "usage: maintenance.py <action> [args...]"}, 1)

    action = sys.argv[1].strip().lower()
    cfg = load_config()
    web_cfg = cfg.get("web") or {}
    upd_cfg = web_cfg.get("update") or {}

    if not upd_cfg.get("enabled", True) and action not in ("status", "health"):
        return respond({"ok": False, "error": "维护功能已在 config.yaml 中禁用"}, 1)

    maint = SystemMaintenance(PROJECT_DIR, cfg)

    try:
        if action == "status":
            return respond(maint.status())

        if action == "health":
            return respond(maint.health_check())

        if action == "fix":
            return respond(maint.auto_fix())

        if action == "repair_db":
            from bnb_quant_tool.data_localization import DataLocalizationManager
            from bnb_quant_tool.sqlite_recovery import repair_workspace_databases

            mgr = DataLocalizationManager(str(PROJECT_DIR))
            results = repair_workspace_databases(mgr.data_dir)
            ok = all(r.get("ok", False) for r in results.values()) if results else True
            return respond({"ok": ok, "databases": results, "actions": ["repair_workspace_databases"]})

        if action == "optimize":
            return respond(maint.optimize())

        if action == "backup":
            label = sys.argv[2] if len(sys.argv) > 2 else "web"
            return respond(maint.backup(label=label))

        if action == "circuit_breaker_reset":
            from bnb_quant_tool.risk_state import clear_circuit_breaker_stop_time
            clear_circuit_breaker_stop_time()
            return respond({"ok": True, "message": "熔断冷却已清除"})

        if action == "circuit_breaker_status":
            from bnb_quant_tool.circuit_breaker import CircuitBreaker
            from bnb_quant_tool.paper_trading import PaperTradingEngine
            pe = PaperTradingEngine(config=cfg)
            cb = CircuitBreaker(paper_engine=pe, config=cfg.get("circuit_breaker") or {})
            cb.account_balance = float((cfg.get("trading") or {}).get("account_balance", 5000))
            st = cb.check()
            return respond({"ok": True, **st, "enabled": cb.enabled})

        if action == "backups":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
            return respond(maint.list_backups(limit=limit))

        if action == "git_pull":
            return respond(maint.git_pull())

        if action == "apply_zip":
            if len(sys.argv) < 3:
                return respond({"ok": False, "error": "缺少 zip 路径"}, 1)
            zip_path = Path(sys.argv[2])
            return respond(maint.apply_update_zip(zip_path))

        return respond({"ok": False, "error": f"未知 action: {action}"}, 1)
    except Exception as e:
        return respond({"ok": False, "error": str(e)}, 1)


if __name__ == "__main__":
    sys.exit(main())
