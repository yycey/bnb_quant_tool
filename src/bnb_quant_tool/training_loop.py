"""
统一训练节拍 — 复盘后晋升/变异 + 定时 StrategyLab 发现 + 指标遗传进化。

服务器与 GUI 共用，避免双路径漂移。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_REL = "data/training_loop_state.json"


def _cfg_block(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict((config or {}).get("training_loop") or {})


def _project_root_from(path: Optional[str | Path] = None) -> Path:
    if path:
        p = Path(path).resolve()
        if p.is_file():
            return p.parent
        return p
    return Path(__file__).resolve().parents[2]


def state_path(project_root: Path) -> Path:
    return project_root / STATE_REL


def read_state(project_root: Path) -> Dict[str, Any]:
    path = state_path(project_root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(project_root: Path, state: Dict[str, Any]) -> None:
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _closed_trade_count(paper_db_path: Optional[str]) -> int:
    if not paper_db_path:
        return 0
    path = Path(paper_db_path)
    if not path.is_file():
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status='CLOSED'"
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except Exception as e:
        # 兼容旧表名
        try:
            conn = sqlite3.connect(str(path), timeout=10)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE status='CLOSED'"
                ).fetchone()
                return int(row[0] or 0) if row else 0
            finally:
                conn.close()
        except Exception:
            logger.debug("closed trade count: %s", e)
            return 0


def _resolve_learning_db(config: Optional[Dict[str, Any]] = None) -> str:
    try:
        from bnb_quant_tool.data_localization import get_localized_db_path, resolve_db_path

        raw = ((config or {}).get("ai_learning") or {}).get("db_path")
        if raw:
            return str(resolve_db_path(raw, "ai_learning"))
        return str(get_localized_db_path("ai_learning"))
    except Exception:
        return "data/ai_learning.db"


def _resolve_config_path(project_root: Path) -> str:
    return str(project_root / "config.yaml")


def after_successful_review(
    *,
    paper_engine: Any = None,
    learner: Any = None,
    evolution: Any = None,
    review_result: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """复盘成功后：Lab 晋升 + 策略变异入影子队列。"""
    tl = _cfg_block(config)
    if not bool(tl.get("enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    out: Dict[str, Any] = {"ok": True, "promote": None, "mutate": None}
    root = Path(project_root) if project_root else _project_root_from(config_path)
    cfg_path = config_path or _resolve_config_path(root)

    paper_db = None
    if paper_engine is not None:
        paper_db = getattr(paper_engine, "db_path", None)
        if learner is None:
            learner = getattr(paper_engine, "learner", None) or getattr(
                paper_engine, "_learner", None
            )
        if evolution is None:
            evolution = getattr(paper_engine, "_learning_evolution", None)

    # --- promote ---
    if bool(tl.get("auto_promote_on_review", True)):
        try:
            evo = evolution
            if evo is None and learner is not None:
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator

                evo = LearningEvolutionCoordinator(
                    learner,
                    capability_memory=getattr(learner, "capability_memory", None),
                    config=config or {},
                )
            if evo is not None:
                promo = evo.promote_strategy_lab_candidates(paper_db_path=paper_db)
                out["promote"] = promo
                logger.info(
                    "training_loop promote: status=%s promoted=%s",
                    (promo or {}).get("status"),
                    len((promo or {}).get("promoted") or []),
                )
                try:
                    from bnb_quant_tool.strategy_pool import reload_discovered_strategies

                    reload_discovered_strategies(reason="after_promote")
                except Exception as re:
                    logger.debug("reload after promote: %s", re)
            else:
                out["promote"] = {"status": "no_evolution"}
        except Exception as e:
            logger.warning("training_loop promote failed: %s", e)
            out["promote"] = {"status": "error", "error": str(e)}

    # --- mutate ---
    if bool(tl.get("auto_mutate_on_review", True)) and review_result:
        try:
            from bnb_quant_tool.strategy_mutator import StrategyMutator

            learning_db = None
            if learner is not None:
                learning_db = getattr(learner, "db_path", None)
            mutator = StrategyMutator(
                config_path=cfg_path,
                learning_db_path=learning_db or _resolve_learning_db(config),
            )
            candidates = mutator.generate_mutations(review_result)
            added = 0
            for c in candidates or []:
                if mutator.add_to_shadow_queue(c):
                    added += 1
            out["mutate"] = {
                "status": "ok",
                "candidates": len(candidates or []),
                "added": added,
            }
            if added:
                logger.info("training_loop mutate: %s shadow candidates queued", added)
        except Exception as e:
            logger.warning("training_loop mutate failed: %s", e)
            out["mutate"] = {"status": "error", "error": str(e)}

    return out


def _fetch_klines_for_discover(
    symbol: str,
    interval: str,
    bars: int,
    fetcher: Any = None,
):
    import pandas as pd

    if fetcher is None:
        from bnb_quant_tool.data_fetcher import BinanceDataFetcher

        fetcher = BinanceDataFetcher()

    df = fetcher.get_klines(symbol=symbol, interval=interval, limit=min(bars, 1000))
    if df is None or len(df) == 0:
        raise RuntimeError("empty klines")

    if len(df) < bars and "timestamp" in df.columns:
        oldest = df["timestamp"].iloc[0]
        while len(df) < bars:
            try:
                end_ms = int(pd.Timestamp(oldest).timestamp() * 1000)
                more = fetcher.get_klines(
                    symbol=symbol, interval=interval, limit=1000, end_time=end_ms
                )
                if more is None or len(more) == 0:
                    break
                df = pd.concat([more, df], ignore_index=True).drop_duplicates(
                    subset=["timestamp"]
                )
                oldest = df["timestamp"].iloc[0]
            except Exception as e:
                logger.warning("discover kline backfill: %s", e)
                break
    return df.reset_index(drop=True)


def maybe_auto_discover(
    project_root: Path | str,
    config: Optional[Dict[str, Any]] = None,
    *,
    fetcher: Any = None,
    paper_db_path: Optional[str] = None,
    force: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """按平仓数 / 时间间隔触发 StrategyLab 发现。"""
    root = Path(project_root).resolve()
    tl = _cfg_block(config)
    if not bool(tl.get("enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    disc = dict(tl.get("auto_discover") or {})
    if not bool(disc.get("enabled", True)) and not force:
        return {"ok": True, "skipped": True, "reason": "discover_disabled"}

    every_n = int(disc.get("every_n_closed_trades", 40) or 40)
    min_hours = float(disc.get("min_interval_hours", 48) or 48)
    now_dt = now or datetime.now()
    state = read_state(root)

    closed = _closed_trade_count(
        paper_db_path
        or ((config or {}).get("paper_trading") or {}).get("db_path")
    )
    last_at = _parse_iso(state.get("last_discover_at"))
    last_closed = int(state.get("last_discover_closed_trades") or 0)

    due_by_trades = closed > 0 and (closed - last_closed) >= every_n
    due_by_time = last_at is None or (now_dt - last_at) >= timedelta(hours=min_hours)
    due = bool(force) or (due_by_trades and due_by_time)

    if not due:
        hours_since = None
        if last_at:
            hours_since = round((now_dt - last_at).total_seconds() / 3600.0, 2)
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_due",
            "closed_trades": closed,
            "since_last_closed": closed - last_closed,
            "hours_since_last": hours_since,
            "every_n": every_n,
            "min_interval_hours": min_hours,
        }

    symbol = str(disc.get("symbol") or "BNBUSDT")
    interval = str(disc.get("interval") or "1h")
    bars = int(disc.get("bars") or 1500)
    topk = int(disc.get("topk") or 8)
    n_candidates = int(disc.get("candidates") or 120)

    try:
        from bnb_quant_tool.strategy_lab import DiscoveryConfig, StrategyLab
        import logging as _logging

        # 发现阶段会成百上千次算指标，压住刷屏
        _ti_log = _logging.getLogger("bnb_quant_tool.technical_indicators")
        _prev_level = _ti_log.level
        _ti_log.setLevel(_logging.WARNING)
        try:
            df = _fetch_klines_for_discover(symbol, interval, bars, fetcher=fetcher)
            lab_cfg = DiscoveryConfig(n_candidates=n_candidates, top_k=topk)
            # 尊重 learning_evolution OOS 开关
            ev = (config or {}).get("learning_evolution") or {}
            if "strategy_lab_require_oos" in ev:
                lab_cfg.walk_forward_oos = bool(ev.get("strategy_lab_require_oos"))

            lab = StrategyLab(lab_cfg, db_path=_resolve_learning_db(config))
            top = lab.discover_and_save(df, merge_with_existing=True)
        finally:
            _ti_log.setLevel(_prev_level)
        state.update(
            {
                "last_discover_at": now_dt.isoformat(timespec="seconds"),
                "last_discover_closed_trades": closed,
                "last_discover_count": len(top or []),
                "last_discover_symbol": symbol,
            }
        )
        write_state(root, state)
        logger.info(
            "training_loop discover: saved %s strategies (%s %s x%s)",
            len(top or []),
            symbol,
            interval,
            len(df),
        )
        try:
            from bnb_quant_tool.strategy_pool import reload_discovered_strategies

            reload_discovered_strategies(reason="after_discover")
        except Exception as re:
            logger.debug("reload after discover: %s", re)
        return {
            "ok": True,
            "skipped": False,
            "discovered": len(top or []),
            "closed_trades": closed,
            "state": state,
        }
    except Exception as e:
        logger.warning("training_loop discover failed: %s", e)
        return {"ok": False, "error": str(e), "skipped": False}


def maybe_auto_evolve_indicators(
    project_root: Path | str,
    config: Optional[Dict[str, Any]] = None,
    *,
    fetcher: Any = None,
    force: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """长周期触发 IndicatorExplorer 遗传进化。"""
    root = Path(project_root).resolve()
    tl = _cfg_block(config)
    if not bool(tl.get("enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    evo_cfg = dict(tl.get("auto_evolve_indicators") or {})
    if not bool(evo_cfg.get("enabled", True)) and not force:
        return {"ok": True, "skipped": True, "reason": "evolve_disabled"}

    interval_days = float(evo_cfg.get("interval_days", 7) or 7)
    generations = int(evo_cfg.get("generations", 3) or 3)
    now_dt = now or datetime.now()
    state = read_state(root)
    last_at = _parse_iso(state.get("last_evolve_at"))

    if not force and last_at is not None:
        if (now_dt - last_at) < timedelta(days=interval_days):
            hours_since = (now_dt - last_at).total_seconds() / 3600.0
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_due",
                "hours_since_last": round(hours_since, 2),
                "interval_days": interval_days,
            }

    disc = dict(tl.get("auto_discover") or {})
    symbol = str(disc.get("symbol") or evo_cfg.get("symbol") or "BNBUSDT")
    interval = str(disc.get("interval") or evo_cfg.get("interval") or "1h")
    bars = int(evo_cfg.get("bars") or disc.get("bars") or 800)

    try:
        from bnb_quant_tool.indicator_explorer import IndicatorExplorer

        df = _fetch_klines_for_discover(symbol, interval, bars, fetcher=fetcher)
        explorer = IndicatorExplorer(
            config_path=_resolve_config_path(root),
            learning_db_path=_resolve_learning_db(config),
        )
        best = explorer.run_evolution(generations=generations, price_data=df)
        fitness = getattr(best, "fitness", None) if best is not None else None
        state.update(
            {
                "last_evolve_at": now_dt.isoformat(timespec="seconds"),
                "last_evolve_fitness": fitness,
                "last_evolve_generations": generations,
            }
        )
        write_state(root, state)
        logger.info(
            "training_loop evolve: generations=%s best_fitness=%s",
            generations,
            fitness,
        )
        return {
            "ok": True,
            "skipped": False,
            "fitness": fitness,
            "generations": generations,
            "state": state,
        }
    except Exception as e:
        logger.warning("training_loop evolve failed: %s", e)
        return {"ok": False, "error": str(e), "skipped": False}


def run_cadence_hooks(
    project_root: Path | str,
    config: Optional[Dict[str, Any]] = None,
    *,
    fetcher: Any = None,
    paper_db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Autopilot 每轮调用：发现 + 指标进化（各自门控）。"""
    return {
        "discover": maybe_auto_discover(
            project_root,
            config,
            fetcher=fetcher,
            paper_db_path=paper_db_path,
        ),
        "evolve": maybe_auto_evolve_indicators(
            project_root,
            config,
            fetcher=fetcher,
        ),
    }
