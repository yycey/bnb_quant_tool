"""Server runtime helpers: lock + dotenv overlay."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bnb_quant_tool.config_access import _apply_env_secret_overrides, load_app_config
from bnb_quant_tool.process_runtime import ProcessLock, load_project_dotenv, setup_logging


def test_process_lock_exclusive(tmp_path: Path):
    lock_path = tmp_path / "t.lock"
    a = ProcessLock(lock_path)
    b = ProcessLock(lock_path)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_setup_logging_writes_file(tmp_path: Path):
    log_path = setup_logging("unit_test", log_dir=tmp_path)
    import logging

    logging.getLogger("unit_test").info("hello-server")
    assert log_path.is_file()
    assert "hello-server" in log_path.read_text(encoding="utf-8")


def test_env_secret_overrides(monkeypatch):
    cfg: dict = {"deepseek": {"api_key": "from-yaml"}, "web": {}}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    monkeypatch.setenv("BNB_TRADING_PROFILE", "production")
    monkeypatch.setenv("BNB_PYTHON_PATH", "/usr/bin/python3")
    _apply_env_secret_overrides(cfg)
    assert cfg["deepseek"]["api_key"] == "from-env"
    assert cfg["trading_profile"] == "production"
    assert cfg["web"]["python_path"] == "/usr/bin/python3"


def test_load_project_dotenv(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("BNB_TRADING_PROFILE=explore\n", encoding="utf-8")
    monkeypatch.delenv("BNB_TRADING_PROFILE", raising=False)
    assert load_project_dotenv(tmp_path) is True
    assert os.environ.get("BNB_TRADING_PROFILE") == "explore"


def test_load_app_config_applies_profile_env(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "trading_profile: validation\n"
        "trading_profiles:\n"
        "  production:\n"
        "    ai_trading:\n"
        "      learning_phase: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BNB_TRADING_PROFILE", "production")
    cfg = load_app_config(cfg_path)
    assert cfg.get("trading_profile") == "production"
