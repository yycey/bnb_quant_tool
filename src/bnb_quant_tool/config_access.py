"""统一 config.yaml 参数读取 — 消除 trading / analysis / risk_management 路径不一致。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union


def _section(config: dict, name: str) -> dict:
    val = config.get(name)
    return val if isinstance(val, dict) else {}


def load_app_config(
    path: Optional[Union[str, Path]] = None,
    *,
    apply_profile: bool = True,
) -> Dict[str, Any]:
    """加载 config.yaml 并（默认）合并 trading_profile 剖面。

    GUI / headless / watcher 应统一走此入口，避免 validation 等剖面只在部分进程生效。
    若项目根存在 `.env`，会先 load_dotenv（不覆盖已有环境变量）。
    """
    import yaml

    if path is None:
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
        except Exception:
            root = Path(__file__).resolve().parents[2]
        path = root / "config.yaml"
    cfg_path = Path(path)
    try:
        from bnb_quant_tool.process_runtime import load_project_dotenv

        load_project_dotenv(cfg_path.parent)
    except Exception:
        pass
    cfg: Dict[str, Any] = {}
    if cfg_path.is_file():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["_config_path"] = str(cfg_path.resolve())
    _apply_env_secret_overrides(cfg)
    if apply_profile:
        from bnb_quant_tool.trading_profile import apply_trading_profile
        cfg = apply_trading_profile(cfg)
    return cfg


def _apply_env_secret_overrides(cfg: Dict[str, Any]) -> None:
    """用环境变量覆盖/补齐密钥与服务器路径（yaml 可留空）。"""
    import os

    def _set_key(section: str, key: str, env_names: tuple) -> None:
        for env_name in env_names:
            val = str(os.environ.get(env_name) or "").strip()
            if val:
                sec = cfg.setdefault(section, {})
                if not isinstance(sec, dict):
                    return
                sec[key] = val
                return

    _set_key("deepseek", "api_key", ("DEEPSEEK_API_KEY",))
    _set_key("qianwen", "api_key", ("QIANWEN_API_KEY", "DASHSCOPE_API_KEY"))
    _set_key("volcengine", "api_key", ("ARK_API_KEY", "VOLCENGINE_API_KEY", "HUOSHAN_API_KEY"))
    _set_key("binance", "api_key", ("BINANCE_API_KEY",))
    _set_key("binance", "api_secret", ("BINANCE_API_SECRET",))
    _set_key("web", "api_token", ("WEB_API_TOKEN", "BNB_WEB_API_TOKEN"))
    py = str(os.environ.get("BNB_PYTHON_PATH") or os.environ.get("WEB_PYTHON_PATH") or "").strip()
    if py:
        web = cfg.setdefault("web", {})
        if isinstance(web, dict):
            web["python_path"] = py
    profile = str(os.environ.get("BNB_TRADING_PROFILE") or "").strip()
    if profile:
        cfg["trading_profile"] = profile


def get_confidence_threshold(config: dict, default: float = 0.55) -> float:
    trading = _section(config, "trading")
    analysis = _section(config, "analysis")
    for src in (trading, analysis):
        if "confidence_threshold" in src:
            return float(src["confidence_threshold"])
    return default


def get_max_open_positions(config: dict, default: int = 0) -> int:
    """已废弃：开仓由保证金占用控制，不再按笔数限制。保留读取以兼容旧配置。"""
    risk = _section(config, "risk_management")
    if "max_open_positions" in risk:
        return int(risk["max_open_positions"])
    return default


def is_position_limit_reached(open_count: int, max_open_positions: int) -> bool:
    """已废弃：请改用 is_margin_insufficient。"""
    if max_open_positions <= 0:
        return False
    return open_count >= max_open_positions


def calc_position_margin(position: dict) -> float:
    """单笔占用保证金 = 名义价值 / 杠杆。"""
    entry = float(position.get("entry_price") or 0)
    qty = position.get("qty_remaining")
    if qty is None:
        qty = position.get("qty_total")
    qty = float(qty or 0)
    leverage = max(1, int(position.get("leverage") or position.get("leverage_suggest") or 1))
    if entry <= 0 or qty <= 0:
        return 0.0
    return entry * qty / leverage


def get_margin_state(
    principal_usdt: float,
    open_positions: list,
    *,
    total_realized_pnl: float = 0.0,
) -> dict:
    """计算保证金占用与可用额度（权益 = 本金 + 累计已实现盈亏）。"""
    equity = float(principal_usdt) + float(total_realized_pnl)
    used = sum(calc_position_margin(p) for p in (open_positions or []))
    available = max(0.0, equity - used)
    return {
        "principal": float(principal_usdt),
        "equity": round(equity, 4),
        "used_margin": round(used, 4),
        "available_margin": round(available, 4),
        "open_count": len(open_positions or []),
    }


def is_margin_insufficient(
    principal_usdt: float,
    open_positions: list,
    required_margin: float = 0.0,
    *,
    total_realized_pnl: float = 0.0,
    min_available: float = 0.01,
) -> bool:
    """保证金不足时返回 True（required_margin=0 表示无任何可用保证金）。"""
    state = get_margin_state(
        principal_usdt, open_positions, total_realized_pnl=total_realized_pnl
    )
    if required_margin > 0:
        return state["available_margin"] + 1e-6 < required_margin
    return state["available_margin"] <= min_available


def get_margin_state_from_db(db_path: str, principal_usdt: float) -> dict:
    """从 paper_trading.db 读取并计算保证金状态（供 Scanner 等轻量模块使用）。"""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        total_realized = float(
            conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usdt), 0) FROM paper_positions"
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT entry_price, qty_remaining, qty_total, leverage "
            "FROM paper_positions WHERE status='OPEN'"
        ).fetchall()
        opens = [dict(r) for r in rows]
    finally:
        conn.close()
    return get_margin_state(principal_usdt, opens, total_realized_pnl=total_realized)


def get_max_position_pct(config: dict, default: float = 0.20) -> float:
    risk = _section(config, "risk_management")
    trading = _section(config, "trading")
    if "max_position_pct" in risk:
        return float(risk["max_position_pct"])
    if "max_position_pct" in trading:
        return float(trading["max_position_pct"])
    return default


def get_atr_sl_mult(config: dict, default: float = 1.8) -> float:
    ta = _section(config, "trade_advisor")
    bt = _section(config, "backtest")
    if "atr_sl_mult" in ta:
        return float(ta["atr_sl_mult"])
    if "atr_sl_mult" in bt:
        return float(bt["atr_sl_mult"])
    return default


def get_trading_config(config: dict) -> dict:
    """合并 trading + analysis.confidence_threshold，供 TradingSignals / RiskManager。"""
    merged = dict(_section(config, "trading"))
    if "confidence_threshold" not in merged:
        merged["confidence_threshold"] = get_confidence_threshold(config)
    return merged


def build_trade_advisor_config(config: dict) -> dict:
    """构建 TradeAdvisor 初始化参数字典。"""
    trading = _section(config, "trading")
    risk = _section(config, "risk_management")
    ta = _section(config, "trade_advisor")

    result = {
        "account_balance": float(trading.get("account_balance", 10000.0)),
        "risk_per_trade": float(trading.get("risk_per_trade", 0.02)),
        "min_confidence": get_confidence_threshold(config),
        "min_risk_reward_ratio": float(risk.get("min_risk_reward_ratio", 1.5)),
        "max_position_pct": get_max_position_pct(config),
        "atr_sl_mult": get_atr_sl_mult(config),
        "atr_tp1_mult": float(ta.get("atr_tp1_mult", 1.5)),
        "atr_tp2_mult": float(ta.get("atr_tp2_mult", 3.0)),
        "atr_tp3_mult": float(ta.get("atr_tp3_mult", 5.0)),
        "news_filter_threshold": float(ta.get("news_filter_threshold", 0.65)),
        "atr_sl_mult_low_vol": float(ta.get("atr_sl_mult_low_vol", 1.3)),
        "atr_sl_mult_high_vol": float(ta.get("atr_sl_mult_high_vol", 2.2)),
        "vol_threshold_low": float(ta.get("vol_threshold_low", 0.015)),
        "vol_threshold_high": float(ta.get("vol_threshold_high", 0.030)),
        "tp_split": ta.get("tp_split", {"tp1": "40%", "tp2": "35%", "tp3": "25%"}),
        "max_open_positions": get_max_open_positions(config, default=0),
        "dl_weight": float(ta.get("dl_weight", 0.25)),
        "dl_min_confidence": float(ta.get("dl_min_confidence", 0.65)),
        "direction_vote_threshold": float(ta.get("direction_vote_threshold", 0.10)),
        "ai_tiebreak_min_confidence": float(ta.get("ai_tiebreak_min_confidence", 0.58)),
        "inst_tiebreak_min_confidence": float(ta.get("inst_tiebreak_min_confidence", 0.55)),
        "inst_vote_skew_min": float(ta.get("inst_vote_skew_min", 0.42)),
        "min_sl_atr_mult": float(ta.get("min_sl_atr_mult", 1.8)),
        "min_sl_pct": float(ta.get("min_sl_pct", 0.018)),
        "use_ai_stop_loss": bool(ta.get("use_ai_stop_loss", True)),
        "allow_weak_signal": bool(ta.get("allow_weak_signal", True)),
    }
    if "ai_guardrail" in config:
        result["ai_guardrail"] = config["ai_guardrail"]
    ai_trading = _section(config, "ai_trading")
    result["follow_ai_direction"] = bool(ai_trading.get("follow_ai_direction", False))
    result["advisor_skip_duplicate_post_gates"] = bool(
        ai_trading.get("advisor_skip_duplicate_post_gates", True)
    )
    result["defer_gross_rr_to_net_gate"] = bool(
        risk.get(
            "defer_gross_rr_to_net_gate",
            ai_trading.get("net_rr_gate_enabled", True),
        )
    )
    if "gate_consec_loss_block" in ai_trading:
        result["gate_consec_loss_block"] = int(ai_trading["gate_consec_loss_block"])
    analysis = _section(config, "analysis")
    if "ta_playbook" in analysis:
        result["ta_playbook"] = analysis["ta_playbook"]
    return result


def resolve_param(config: dict, param: str, default: Any = None) -> Any:
    """按 param_manager 映射规则解析单个参数。"""
    mapping = {
        "confidence_threshold": lambda c: get_confidence_threshold(c),
        "min_confidence": lambda c: get_confidence_threshold(c),
        "max_position_pct": lambda c: get_max_position_pct(c),
        "atr_sl_mult": lambda c: get_atr_sl_mult(c),
        "account_balance": lambda c: float(_section(c, "trading").get("account_balance", 10000)),
        "risk_per_trade": lambda c: float(_section(c, "trading").get("risk_per_trade", 0.02)),
        "min_risk_reward_ratio": lambda c: float(
            _section(c, "risk_management").get("min_risk_reward_ratio", 1.5)
        ),
    }
    resolver = mapping.get(param)
    if resolver is not None:
        try:
            return resolver(config)
        except (TypeError, ValueError):
            return default
    return default


def build_position_sizer_config(config: Optional[dict] = None) -> dict:
    """DynamicPositionSizer 初始化参数（GUI / headless 共用，防漂移）。"""
    cfg = config or {}
    dyn = dict(cfg.get("dynamic_position") or {})
    kelly = cfg.get("kelly") or {}
    return {
        **dyn,
        "kelly": kelly,
        "ai_trading": cfg.get("ai_trading") or {},
        "skip_advisor_dupes": bool(
            kelly.get(
                "skip_advisor_dupes",
                dyn.get("skip_advisor_dupes", True),
            )
        ),
    }


def build_data_fetcher(config: Optional[dict] = None):
    """从 config 构建 BinanceDataFetcher（含 Bitget 备用）。"""
    import os

    from .data_fetcher import BinanceDataFetcher

    cfg = config or {}
    binance = _section(cfg, "binance")
    api_key = binance.get("api_key") or os.environ.get("BINANCE_API_KEY")
    api_secret = binance.get("api_secret") or os.environ.get("BINANCE_API_SECRET")
    if api_key and str(api_key).startswith("YOUR_"):
        api_key = None
    if api_secret and str(api_secret).startswith("YOUR_"):
        api_secret = None
    return BinanceDataFetcher(
        api_key=api_key,
        api_secret=api_secret,
        bitget_config=_section(cfg, "bitget") or None,
        mexc_config=_section(cfg, "mexc") or None,
        kline_archive_config=_section(cfg, "kline_archive") or None,
        default_symbol=str(_section(cfg, "trading").get("symbol", "BNBUSDT")),
    )
