"""sqlite_util：locked 检测与重试。"""
from __future__ import annotations

import sqlite3

from bnb_quant_tool.sqlite_util import is_db_locked, run_db


def test_is_db_locked():
    assert is_db_locked(sqlite3.OperationalError("database is locked"))
    assert is_db_locked(sqlite3.OperationalError("database is busy"))
    assert not is_db_locked(sqlite3.OperationalError("no such table"))
    assert not is_db_locked(ValueError("locked"))


def test_run_db_retries():
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return 42

    resets = {"n": 0}
    assert run_db(flaky, label="t", on_locked=lambda: resets.__setitem__("n", resets["n"] + 1)) == 42
    assert n["i"] == 3
    assert resets["n"] == 2
