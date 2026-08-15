#!/usr/bin/env python3
"""Web 桥接 — 启动前修复损坏的 SQLite 数据库（与 GUI init_workspace 共用逻辑）"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.WARNING, force=True)

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

from bnb_quant_tool.data_localization import DataLocalizationManager
from bnb_quant_tool.sqlite_recovery import repair_workspace_databases


def main() -> int:
    mgr = DataLocalizationManager(str(PROJECT_DIR))
    results = repair_workspace_databases(mgr.data_dir)
    payload = {
        "ok": all(r.get("ok", False) for r in results.values()) if results else True,
        "databases": results,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
