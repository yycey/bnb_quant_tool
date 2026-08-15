"""SQLite 损坏库自动恢复测试"""

import os
import sqlite3
import tempfile

import pytest

from bnb_quant_tool.sqlite_recovery import (
    check_sqlite_health,
    ensure_sqlite_db_healthy,
    recover_sqlite_db,
)


def _make_valid_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('ok')")
    conn.commit()
    conn.close()


def test_check_sqlite_health_ok():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _make_valid_db(path)
        ok, msg = check_sqlite_health(path)
        assert ok is True
        assert msg == "ok"
    finally:
        os.unlink(path)


def test_recover_recreates_totally_corrupt_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(b"NOT A SQLITE DATABASE" * 100)
        ok, _ = check_sqlite_health(path)
        assert ok is False

        result = recover_sqlite_db(path, label="test")
        assert result["ok"] is True
        assert result["action"] == "recreated"
        assert result["backup"] is not None

        ok2, msg2 = check_sqlite_health(path)
        assert ok2 is True
        assert msg2 == "file_missing"

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.commit()
        conn.close()
        ok3, _ = check_sqlite_health(path)
        assert ok3 is True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def test_ensure_healthy_missing_file():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    result = ensure_sqlite_db_healthy(path, label="missing")
    assert result["ok"] is True
    assert result["action"] == "none"


def test_ail_learning_system_starts_after_corrupt_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(b"corrupt" * 200)
        from bnb_quant_tool.ai_learning_system import AILearningSystem

        learner = AILearningSystem(db_path=path, config={})
        assert learner.db_recovery_info.get("action") in ("recovered", "recreated")
        conn = learner._get_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_records'"
        ).fetchone()
        assert row is not None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
