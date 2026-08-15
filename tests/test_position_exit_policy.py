"""纯逻辑：超时 / 认错出场判定（不碰引擎）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bnb_quant_tool.position_exit_policy import (
    evaluate_admit_wrong,
    evaluate_timeout,
    parse_opened_at,
    resolve_timeout_policy,
)


def test_parse_opened_at_aware_and_naive():
    aware = parse_opened_at("2026-08-13T12:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None
    naive = parse_opened_at("2026-08-13T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert naive.tzinfo == timezone.utc


def test_soft_and_hard_timeout_decisions():
    cfg = {
        "paper_trading": {
            "max_position_age_hours": 48,
            "soft_exit": {"enabled": True, "min_hours": 2, "hours": 6, "require_tp1_not_hit": True},
        }
    }
    policy = resolve_timeout_policy(cfg)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    pos = {
        "opened_at": (now - timedelta(hours=7)).isoformat(),
        "tp1_hit": 0,
    }
    soft = evaluate_timeout(pos, policy, now=now)
    assert soft is not None and soft.reason == "TIMEOUT_NO_TP"

    pos2 = dict(pos)
    pos2["tp1_hit"] = 1
    assert evaluate_timeout(pos2, policy, now=now) is None

    pos3 = {
        "opened_at": (now - timedelta(hours=49)).isoformat(),
        "tp1_hit": 1,
    }
    hard = evaluate_timeout(pos3, policy, now=now)
    assert hard is not None and hard.reason == "TIMEOUT"


def test_admit_wrong_uses_live_r_not_stale_mae_alone():
    cfg = {
        "paper_trading": {
            "admit_wrong": {
                "enabled": True,
                "min_age_minutes": 30,
                "adverse_r": 0.35,
                "skip_if_tp1_hit": True,
            }
        }
    }
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    pos = {
        "opened_at": (now - timedelta(hours=1)).isoformat(),
        "side": "LONG",
        "entry_price": 100.0,
        "sl_initial": 98.0,
        "mae_r": -0.8,
        "tp1_hit": 0,
    }
    # 已回本：有历史 MAE 但 live>=0 → 不认错
    assert evaluate_admit_wrong(pos, 100.5, cfg, now=now) is None
    # 仍深亏
    hit = evaluate_admit_wrong(pos, 99.0, cfg, now=now)
    assert hit is not None and hit.reason == "ADMIT_WRONG"
