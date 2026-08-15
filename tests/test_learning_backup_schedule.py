from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from bnb_quant_tool.system_maintenance import SystemMaintenance


def _seed_project(root: Path) -> None:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("learning_backup:\n  enabled: true\n", encoding="utf-8")
    (data / "ai_learning.db").write_bytes(b"ai")
    (data / "paper_trading.db").write_bytes(b"paper")
    (data / "pattern_memory.db").write_bytes(b"pattern")
    (data / "trader_memory.db").write_bytes(b"council")
    (data / "local_growth_plan.json").write_text('{"stage":"EDGE"}', encoding="utf-8")
    (data / "dqn_shadow_state.json").write_text('{"n":1}', encoding="utf-8")


def test_scheduled_learning_backup_every_two_days(tmp_path: Path):
    _seed_project(tmp_path)
    cfg = {
        "learning_backup": {
            "enabled": True,
            "interval_days": 2,
            "dir": "data/learning_backups",
            "keep_count": 3,
        }
    }
    maint = SystemMaintenance(tmp_path, cfg)
    t0 = datetime(2026, 8, 13, 12, 0, 0)

    first = maint.maybe_scheduled_learning_backup(now=t0)
    assert first["ok"] is True
    assert first.get("skipped") is False
    zip_path = Path(first["backup_path"])
    assert zip_path.is_file()
    assert "learning_backups" in str(zip_path.parent).replace("\\", "/")
    assert zip_path.parent == maint.learning_backups_dir

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    assert "data/ai_learning.db" in names
    assert "data/trader_memory.db" in names
    assert "data/local_growth_plan.json" in names
    assert "data/dqn_shadow_state.json" in names

    state = json.loads((maint.learning_backups_dir / "last_backup.json").read_text(encoding="utf-8"))
    assert state["last_backup_name"] == first["backup_name"]

    skip = maint.maybe_scheduled_learning_backup(now=t0 + timedelta(hours=40))
    assert skip.get("skipped") is True
    assert skip.get("reason") == "not_due"

    second = maint.maybe_scheduled_learning_backup(now=t0 + timedelta(hours=49))
    assert second["ok"] is True
    assert second.get("skipped") is False
    assert Path(second["backup_path"]).is_file()
    assert second["backup_name"] != first["backup_name"]


def test_learning_backup_retention(tmp_path: Path):
    _seed_project(tmp_path)
    cfg = {
        "learning_backup": {
            "enabled": True,
            "interval_days": 0.0001,
            "dir": "data/learning_backups",
            "keep_count": 2,
        }
    }
    maint = SystemMaintenance(tmp_path, cfg)
    base = datetime(2026, 8, 1, 0, 0, 0)
    for i in range(4):
        out = maint.maybe_scheduled_learning_backup(
            force=True,
            now=base + timedelta(days=i),
        )
        assert out["ok"] is True

    zips = list(maint.learning_backups_dir.glob("backup_*.zip"))
    assert len(zips) == 2
