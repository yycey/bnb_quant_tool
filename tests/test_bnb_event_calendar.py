"""BNB 事件周期四阶段识别单元测试。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.bnb_event_calendar import BNBEventCalendar, EventPhase


def _cal(**kwargs) -> BNBEventCalendar:
    cfg = {
        "enabled": True,
        "cache_seconds": 0,
        "unlock_window_hours": 24,
        "post_dump_hours": 48,
        "recovery_hours": 48,
        "anticipation_hours": 48,
    }
    cfg.update(kwargs)
    return BNBEventCalendar(config=cfg)


def test_phase_anticipation():
    cal = _cal()
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    ev = {
        "name": "Test LP",
        "event_type": "launchpool",
        "announce_at": "2026-06-05T00:00:00+00:00",
        "start_at": "2026-06-07T00:00:00+00:00",
        "end_at": "2026-06-14T00:00:00+00:00",
    }
    assert cal._resolve_phase(ev, now) == EventPhase.ANTICIPATION


def test_phase_staking_lock():
    cal = _cal()
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    ev = {
        "name": "Test LP",
        "announce_at": "2026-06-05T00:00:00+00:00",
        "start_at": "2026-06-07T00:00:00+00:00",
        "end_at": "2026-06-14T00:00:00+00:00",
    }
    assert cal._resolve_phase(ev, now) == EventPhase.STAKING_LOCK


def test_phase_unlock_dump():
    cal = _cal()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    ev = {
        "name": "Test LP",
        "announce_at": "2026-06-05T00:00:00+00:00",
        "start_at": "2026-06-07T00:00:00+00:00",
        "end_at": "2026-06-14T00:00:00+00:00",
        "listing_at": "2026-06-14T00:00:00+00:00",
    }
    assert cal._resolve_phase(ev, now) == EventPhase.UNLOCK_DUMP


def test_phase_value_recovery():
    cal = _cal()
    now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    ev = {
        "name": "Test LP",
        "announce_at": "2026-06-05T00:00:00+00:00",
        "start_at": "2026-06-07T00:00:00+00:00",
        "end_at": "2026-06-14T00:00:00+00:00",
    }
    assert cal._resolve_phase(ev, now) == EventPhase.VALUE_RECOVERY


def test_manual_event_analyze():
    cal = _cal(manual_events=[{
        "name": "Megadrop X",
        "type": "megadrop",
        "announce_at": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
        "start_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "end_at": (datetime.now(timezone.utc) + timedelta(days=8)).isoformat(),
    }])
    result = cal.analyze(news_items=[], launchpool_projects=[])
    assert result["phase"] == EventPhase.ANTICIPATION.value
    assert result["policy"]["gate_relaxation"] >= 0.08
