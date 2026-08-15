"""扫描检测任务删除 API / 引擎方法。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bnb_quant_tool.signal_scanner import SignalScanner
from bnb_quant_tool.price_alert import PriceAlertEngine, PriceRule


def test_delete_and_clear_scan_signals(tmp_path):
    db = tmp_path / "paper.db"
    sc = SignalScanner(fetcher=None, db_path=str(db), config={"symbols": ["BNBUSDT"]})
    # 直接写两条
    conn = sc._get_conn()
    for i in range(3):
        conn.execute(
            "INSERT INTO scan_signals "
            "(signal_type, direction, strength, symbol, price, detail, indicators_json, triggered_fullauto, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RSI_CROSS", "BULLISH", 0.7, "BNBUSDT", 590.0, f"t{i}", "{}", 0, f"2026-08-01T0{i}:00:00"),
        )
    conn.commit()
    assert len(sc.get_recent_signals()) == 3
    rows = sc.get_recent_signals()
    deleted = sc.delete_signals([rows[0]["id"], rows[1]["id"]])
    assert deleted == 2
    assert len(sc.get_recent_signals()) == 1
    cleared = sc.clear_signals()
    assert cleared == 1
    assert sc.get_recent_signals() == []


def test_remove_price_alert_rule():
    eng = PriceAlertEngine()
    eng.add_rule(PriceRule(rule_id="SL", name="止损", direction="cross_below", target=100.0))
    eng.add_rule(PriceRule(rule_id="TP1", name="止盈1", direction="cross_above", target=120.0))
    assert eng.remove_rule("SL") is True
    assert len(eng.rules) == 1
    assert eng.rules[0].rule_id == "TP1"
    assert eng.remove_rule("missing") is False
