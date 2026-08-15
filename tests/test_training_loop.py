from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from bnb_quant_tool.training_loop import (
    after_successful_review,
    maybe_auto_discover,
    maybe_auto_evolve_indicators,
    read_state,
    write_state,
)


def test_after_successful_review_calls_promote_and_mutate(tmp_path: Path):
    cfg = {
        "training_loop": {
            "enabled": True,
            "auto_promote_on_review": True,
            "auto_mutate_on_review": True,
        }
    }
    paper = MagicMock()
    paper.db_path = str(tmp_path / "paper.db")
    evolution = MagicMock()
    evolution.promote_strategy_lab_candidates.return_value = {
        "status": "ok",
        "promoted": ["s1"],
    }

    candidate = MagicMock()
    mutator = MagicMock()
    mutator.generate_mutations.return_value = [candidate]
    mutator.add_to_shadow_queue.return_value = True

    with patch(
        "bnb_quant_tool.strategy_mutator.StrategyMutator",
        return_value=mutator,
    ):
        out = after_successful_review(
            paper_engine=paper,
            learner=MagicMock(db_path=str(tmp_path / "ai.db")),
            evolution=evolution,
            review_result={"status": "success", "ai_result": {}},
            config=cfg,
            project_root=tmp_path,
            config_path=str(tmp_path / "config.yaml"),
        )

    assert out["ok"] is True
    evolution.promote_strategy_lab_candidates.assert_called_once()
    assert out["promote"]["promoted"] == ["s1"]
    assert out["mutate"]["added"] == 1
    mutator.generate_mutations.assert_called_once()


def test_after_successful_review_respects_disabled(tmp_path: Path):
    out = after_successful_review(
        config={"training_loop": {"enabled": False}},
        review_result={"status": "success"},
        project_root=tmp_path,
    )
    assert out.get("skipped") is True
    assert out.get("reason") == "disabled"


def test_maybe_auto_discover_skips_when_not_due(tmp_path: Path):
    cfg = {
        "training_loop": {
            "enabled": True,
            "auto_discover": {
                "enabled": True,
                "every_n_closed_trades": 40,
                "min_interval_hours": 48,
            },
        }
    }
    t0 = datetime(2026, 8, 14, 0, 0, 0)
    write_state(
        tmp_path,
        {
            "last_discover_at": t0.isoformat(timespec="seconds"),
            "last_discover_closed_trades": 100,
        },
    )

    with patch(
        "bnb_quant_tool.training_loop._closed_trade_count",
        return_value=120,
    ):
        out = maybe_auto_discover(
            tmp_path,
            cfg,
            now=t0 + timedelta(hours=10),
        )

    assert out["skipped"] is True
    assert out["reason"] == "not_due"


def test_maybe_auto_discover_runs_when_due(tmp_path: Path):
    cfg = {
        "training_loop": {
            "enabled": True,
            "auto_discover": {
                "enabled": True,
                "every_n_closed_trades": 40,
                "min_interval_hours": 48,
                "topk": 3,
                "candidates": 10,
            },
        },
        "learning_evolution": {"strategy_lab_require_oos": False},
    }
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    t0 = datetime(2026, 8, 1, 0, 0, 0)
    write_state(
        tmp_path,
        {
            "last_discover_at": t0.isoformat(timespec="seconds"),
            "last_discover_closed_trades": 100,
        },
    )

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=300, freq="1h"),
            "open": range(300),
            "high": range(300),
            "low": range(300),
            "close": range(300),
            "volume": [1000] * 300,
        }
    )
    lab = MagicMock()
    lab.discover_and_save.return_value = [{"id": "a"}, {"id": "b"}]

    with patch(
        "bnb_quant_tool.training_loop._closed_trade_count",
        return_value=150,
    ), patch(
        "bnb_quant_tool.training_loop._fetch_klines_for_discover",
        return_value=df,
    ), patch(
        "bnb_quant_tool.strategy_lab.StrategyLab",
        return_value=lab,
    ), patch(
        "bnb_quant_tool.strategy_lab.DiscoveryConfig",
    ):
        out = maybe_auto_discover(
            tmp_path,
            cfg,
            now=t0 + timedelta(hours=50),
        )

    assert out["ok"] is True
    assert out.get("skipped") is False
    assert out["discovered"] == 2
    lab.discover_and_save.assert_called_once()
    state = read_state(tmp_path)
    assert state["last_discover_count"] == 2
    assert state["last_discover_closed_trades"] == 150


def test_maybe_auto_evolve_skips_when_not_due(tmp_path: Path):
    cfg = {
        "training_loop": {
            "enabled": True,
            "auto_evolve_indicators": {
                "enabled": True,
                "interval_days": 7,
            },
        }
    }
    t0 = datetime(2026, 8, 10, 0, 0, 0)
    write_state(tmp_path, {"last_evolve_at": t0.isoformat(timespec="seconds")})

    out = maybe_auto_evolve_indicators(
        tmp_path,
        cfg,
        now=t0 + timedelta(days=2),
    )
    assert out["skipped"] is True
    assert out["reason"] == "not_due"
