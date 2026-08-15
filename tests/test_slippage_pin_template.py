"""滑点 / 插针过滤 / 执行模板改 SL 后仓位重算。"""

from bnb_quant_tool.analysis_reuse import apply_execution_template
from bnb_quant_tool.paper_trading import PaperTradingEngine, SIDE_LONG
from bnb_quant_tool.param_manager import ParamManager


def test_slippage_applied_on_open_and_close(tmp_path):
    cfg = {
        "paper_trading": {
            "slippage_enabled": True,
            "slippage_min_pct": 0.01,
            "slippage_max_pct": 0.01,
            "slippage_atr_link": False,
            "pin_filter_enabled": False,
        }
    }
    eng = PaperTradingEngine(db_path=str(tmp_path / "p.db"), config=cfg)
    mid = 100.0
    slipped_open = eng._apply_slippage(mid, SIDE_LONG, is_open=True)
    assert slipped_open > mid  # 开多不利 = 更高成交价
    slipped_close = eng._apply_slippage(mid, SIDE_LONG, is_open=False)
    assert slipped_close < mid  # 平多不利 = 更低成交价


def test_pin_filter_requires_sustained_touch(tmp_path, monkeypatch):
    cfg = {
        "paper_trading": {
            "slippage_enabled": False,
            "pin_filter_enabled": True,
            "pin_confirm_seconds": 2,
        }
    }
    eng = PaperTradingEngine(db_path=str(tmp_path / "p2.db"), config=cfg)
    t0 = 1_000_000.0
    monkeypatch.setattr("bnb_quant_tool.paper_trading.time.time", lambda: t0)
    assert eng._pin_confirmed("1:SL", True) is False  # 首次仅记时
    monkeypatch.setattr("bnb_quant_tool.paper_trading.time.time", lambda: t0 + 1.0)
    assert eng._pin_confirmed("1:SL", True) is False  # 未满 2s
    monkeypatch.setattr("bnb_quant_tool.paper_trading.time.time", lambda: t0 + 2.5)
    assert eng._pin_confirmed("1:SL", True) is True  # 持续触及确认
    # 价格收回后清状态
    assert eng._pin_confirmed("1:SL", False) is False


def test_execution_template_resizes_on_wider_sl():
    advice = {
        "action": "LONG",
        "prices": {
            "entry_mid": 100.0,
            "stop_loss": 99.0,  # 原风险 1
            "tp1": 102.0,
            "tp2": 103.0,
        },
        "position": {
            "quantity": 10.0,  # risk = 10 * 1 = 10
            "usdt_amount": 1000.0,
            "risk_amount": 10.0,
            "margin_required": 1000.0,
            "leverage_suggest": 1,
        },
    }
    # 新 SL 距 entry = 2 ATR → 若 ATR=1, sl=98
    template = {
        "action": "LONG",
        "sl_atr_mult": 2.0,
        "tp_atr_mult": 3.0,
        "size_scale": 1.0,
        "reason": "test",
    }
    out = apply_execution_template(advice, template, indicators={"ATR": 1.0})
    assert abs(float(out["prices"]["stop_loss"]) - 98.0) < 1e-6
    # 风险金额保持 10，距离变 2 → qty 应变 5
    assert abs(float(out["position"]["quantity"]) - 5.0) < 1e-6
    assert abs(float(out["position"]["risk_amount"]) - 10.0) < 1e-6


def test_param_manager_resolves_project_root_config():
    p = ParamManager.resolve_config_path("config.yaml")
    assert p.name == "config.yaml"
    assert p.is_absolute()
    # 应指向项目根，而非随意 cwd
    assert (p.parent / "src" / "bnb_quant_tool").exists() or p.exists()
