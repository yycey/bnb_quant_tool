"""data/ 代码镜像卫生检查与清理。"""

from __future__ import annotations

from pathlib import Path

from bnb_quant_tool.system_maintenance import SystemMaintenance


def test_data_pollution_detect_and_purge(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper_trading.db").write_bytes(b"x")
    (data / "ai_learning.db").write_bytes(b"x")
    poison_src = data / "src" / "bnb_quant_tool"
    poison_src.mkdir(parents=True)
    (poison_src / "dummy.py").write_text("# junk\n", encoding="utf-8")
    (data / "gui.py").write_text("print('nope')\n", encoding="utf-8")
    (data / "scoreboard.json").write_text("{}", encoding="utf-8")

    sm = SystemMaintenance(tmp_path)
    check = sm._check_data_pollution()
    assert check["ok"] is False
    assert check["fixable"] is True

    actions = sm._purge_data_code_mirror()
    assert any("src" in a for a in actions)
    assert not (data / "src").exists()
    assert not (data / "gui.py").exists()
    assert (data / "scoreboard.json").is_file()
    assert (data / "paper_trading.db").is_file()

    clean = sm._check_data_pollution()
    assert clean["ok"] is True
