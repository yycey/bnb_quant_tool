"""
Scoreboard — 统一绩效看板（E[R] / 胜率 / 开仓密度 / 决策漏斗 / 晋升漏斗）。

借鉴：AICoin 指标胜率看板、Freqtrade 策略报告、AI-Trader 多智能体结果汇总。
单一出口，供 GUI / headless / CLI 消费；写入 data/scoreboard.json。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _workspace_root(config: Optional[Dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager
        return Path(get_localization_manager().workspace)
    except Exception:
        return Path(__file__).resolve().parents[2]


def scoreboard_path(config: Optional[Dict] = None) -> Path:
    cfg = (config or {}).get("scoreboard") or {}
    custom = cfg.get("path")
    if custom:
        return Path(custom)
    return _workspace_root(config) / "data" / "scoreboard.json"


def build_scoreboard(
    *,
    paper_engine=None,
    config: Optional[Dict[str, Any]] = None,
    orderflow: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """汇总纸面统计 + 决策漏斗 + 晋升漏斗 + 可选订单流。"""
    cfg = config or {}
    paper_stats: Dict[str, Any] = {}
    open_density = 0.0
    lookback = int(((cfg.get("learning_evolution") or {}).get("open_density_lookback_days") or 7))

    if paper_engine is not None:
        try:
            paper_stats = paper_engine.get_stats(auto_only=True) or {}
        except Exception as e:
            logger.debug("scoreboard paper stats: %s", e)
        try:
            closed = paper_engine.get_closed_positions(limit=500) or []
            auto = [
                c for c in closed
                if not str(c.get("close_reason") or "").upper().startswith("MANUAL")
            ]
            # 近 lookback 天开仓密度（用 closed_at / opened_at）
            from datetime import timedelta

            now = datetime.now()
            cutoff = now - timedelta(days=lookback)
            recent_opens = 0
            for c in auto:
                ts = c.get("opened_at") or c.get("closed_at") or ""
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
                    if dt >= cutoff:
                        recent_opens += 1
                except Exception:
                    continue
            open_density = recent_opens / max(lookback, 1)
        except Exception as e:
            logger.debug("scoreboard density: %s", e)

    funnel: Dict[str, Any] = {}
    try:
        from bnb_quant_tool.trading_profile import get_decision_funnel
        funnel = get_decision_funnel(cfg) or {}
    except Exception as e:
        logger.debug("scoreboard funnel: %s", e)

    promotion: Dict[str, Any] = {}
    try:
        from bnb_quant_tool.promotion_funnel import PromotionFunnel
        promotion = PromotionFunnel(cfg).summary()
    except Exception as e:
        logger.debug("scoreboard promotion: %s", e)

    er = float(paper_stats.get("expectancy_r") or 0.0)
    wr = float(paper_stats.get("win_rate") or 0.0)
    n = int(paper_stats.get("total_trades") or 0)
    pf = float(paper_stats.get("profit_factor") or 0.0)
    gave_back = int(paper_stats.get("gave_back_count") or 0)

    # 健康灯：对齐 docs/ai-trader-philosophy 7 天验收口径
    health = "insufficient"
    if n >= 15:
        if er > 0 and pf >= 1.0:
            health = "healthy"
        elif er > 0:
            health = "watch"
        else:
            health = "unhealthy"

    board: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "health": health,
        "paper": {
            "total_trades": n,
            "open_count": int(paper_stats.get("open_count") or 0),
            "win_rate": round(wr, 4),
            "expectancy_r": round(er, 4),
            "profit_factor": round(pf, 4),
            "avg_win_r": round(float(paper_stats.get("avg_win_r") or 0), 4),
            "avg_loss_r": round(float(paper_stats.get("avg_loss_r") or 0), 4),
            "total_realized_pnl": round(float(paper_stats.get("total_realized_pnl") or 0), 4),
            "gave_back_count": gave_back,
            "max_drawdown_usdt": round(float(paper_stats.get("max_drawdown_usdt") or 0), 4),
            "auto_only": bool(paper_stats.get("auto_only", True)),
        },
        "density": {
            "opens_per_day": round(open_density, 4),
            "lookback_days": lookback,
            "min_required": float(
                ((cfg.get("learning_evolution") or {}).get("min_open_density_per_day") or 0.3)
            ),
        },
        "decision_funnel": funnel,
        "promotion_funnel": promotion,
        "orderflow": {
            "available": bool((orderflow or {}).get("available")),
            "score": (orderflow or {}).get("orderflow_score"),
            "direction": (orderflow or {}).get("direction"),
            "interpretation": (orderflow or {}).get("interpretation"),
        }
        if orderflow
        else {},
        "verdict": _verdict(health, er, open_density, funnel),
        "local_growth": {},
        "validation": {},
    }
    try:
        from bnb_quant_tool.local_growth_coach import LocalGrowthCoach
        board["local_growth"] = LocalGrowthCoach(cfg).load() or {}
    except Exception:
        pass
    try:
        from bnb_quant_tool.validation_trading import get_validation_stats
        board["validation"] = get_validation_stats(cfg)
    except Exception:
        pass
    if extra:
        if isinstance(extra.get("local_growth"), dict) and extra["local_growth"]:
            board["local_growth"] = extra["local_growth"]
        rest = {k: v for k, v in extra.items() if k != "local_growth"}
        if rest:
            board["extra"] = rest
    return board


def publish_scoreboard(
    *,
    paper_engine=None,
    config: Optional[Dict[str, Any]] = None,
    orderflow: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建并写入 data/scoreboard.json。"""
    board = build_scoreboard(
        paper_engine=paper_engine,
        config=config,
        orderflow=orderflow,
        extra=extra,
    )
    path = scoreboard_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(board, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)
    return board


def load_scoreboard(config: Optional[Dict] = None) -> Dict[str, Any]:
    path = scoreboard_path(config)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _verdict(
    health: str,
    er: float,
    density: float,
    funnel: Dict[str, Any],
) -> str:
    triggered = int(funnel.get("analysis_triggered") or 0)
    opened = int(funnel.get("opened") or 0)
    convert = (opened / triggered) if triggered else 0.0
    if health == "insufficient":
        return f"样本不足；开仓密度 {density:.2f}/日；漏斗转化 {convert:.1%}"
    if health == "healthy":
        return f"E[R]={er:+.3f} 健康；密度 {density:.2f}/日；漏斗转化 {convert:.1%}"
    if health == "watch":
        return f"E[R]={er:+.3f} 观察；密度 {density:.2f}/日；漏斗转化 {convert:.1%}"
    return f"E[R]={er:+.3f} 不健康 — 收紧或暂停晋升；密度 {density:.2f}/日"
