#!/usr/bin/env python3
"""
Autopilot 守护进程 — 无 GUI 后台定时分析 + 自动跟单。

用法:
  python autopilot_daemon.py --once              # 跑一轮
  python autopilot_daemon.py                     # 按 config autopilot/auto_run 间隔循环
  python autopilot_daemon.py --interval 15       # 每 15 分钟
  python autopilot_daemon.py --no-open           # 只分析不开仓
  python autopilot_daemon.py --no-embed-watcher  # 不内嵌 watcher（配合独立 paper_watcher.py）
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bnb_quant_tool.process_runtime import (  # noqa: E402
    ensure_singleton,
    load_project_dotenv,
    setup_logging,
)

load_project_dotenv(PROJECT_ROOT)
setup_logging("autopilot", log_dir=PROJECT_ROOT / "data" / "logs")
logger = logging.getLogger("autopilot_daemon")


def _interval_minutes(config: dict, cli_interval: int | None) -> int:
    if cli_interval and cli_interval > 0:
        return cli_interval
    ap = config.get("autopilot") or {}
    if ap.get("interval_minutes"):
        return int(ap["interval_minutes"])
    return int((config.get("auto_run") or {}).get("interval_minutes", 60))


def _should_run(config: dict) -> bool:
    ap = config.get("autopilot") or {}
    mode = str(ap.get("mode") or "legacy").lower()
    if mode in ("off", "disabled"):
        return False
    if mode in ("fullauto", "unified", "scheduled", "legacy"):
        return True
    return bool((config.get("auto_run") or {}).get("enabled", False))


def _want_embed_watcher(config: dict, cli_no_embed: bool) -> bool:
    if cli_no_embed:
        return False
    ap = config.get("autopilot") or {}
    if "embed_paper_watcher" in ap:
        return bool(ap.get("embed_paper_watcher"))
    startup = config.get("startup") or {}
    pt = config.get("paper_trading") or {}
    return bool(startup.get("paper_watcher", pt.get("auto_follow", True)))


def _ensure_paper_watcher(runner, *, enabled: bool) -> None:
    if not enabled:
        logger.info("内嵌 paper watcher 已关闭（由独立 paper_watcher 负责）")
        return
    # 与独立 paper_watcher 共用锁，避免双进程同时 tick 同一 paper DB
    try:
        from bnb_quant_tool.process_runtime import ProcessLock

        lock_path = PROJECT_ROOT / "data" / "locks" / "paper_watcher.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        wlock = ProcessLock(lock_path)
        if not wlock.acquire():
            logger.warning(
                "独立 paper_watcher 已在运行（锁 %s），跳过内嵌 watcher",
                lock_path,
            )
            return
        runner._paper_watcher_lock = wlock  # 进程存活期间持有
    except Exception as e:
        logger.warning("获取 paper_watcher 锁失败，仍尝试内嵌: %s", e)

    pt = runner.config.get("paper_trading") or {}
    if runner.paper_engine.is_watching():
        return
    if getattr(runner.paper_engine, "_price_provider", None) is None:
        runner.paper_engine.set_price_provider(
            lambda symbol: runner.fetcher.get_price_with_fallback(symbol)
        )
    poll = float(pt.get("poll_interval", 15))
    runner.paper_engine.start_watcher(interval=poll)
    logger.info("模拟盘监控已启动 (interval=%ss)", poll)


def main() -> int:
    parser = argparse.ArgumentParser(description="BNB Quant Autopilot Daemon")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    parser.add_argument("--interval", type=int, default=0, help="循环间隔（分钟）")
    parser.add_argument("--no-open", action="store_true", help="不自动模拟开仓")
    parser.add_argument("--symbol", default="", help="覆盖交易对")
    parser.add_argument("--timeframe", default="", help="覆盖周期")
    parser.add_argument(
        "--no-embed-watcher",
        action="store_true",
        help="不内嵌 watcher（服务器双进程拓扑）",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="跳过单实例锁（仅调试）",
    )
    args = parser.parse_args()

    if not args.no_lock:
        ensure_singleton(
            PROJECT_ROOT / "data" / "locks" / "autopilot.lock",
            logger=logger,
        )

    from bnb_quant_tool.headless_runner import HeadlessAnalysisRunner

    runner = HeadlessAnalysisRunner(str(PROJECT_ROOT))
    cfg = runner.config
    embed = _want_embed_watcher(cfg, args.no_embed_watcher)
    _ensure_paper_watcher(runner, enabled=embed)

    if not args.once and not _should_run(cfg):
        logger.warning(
            "autopilot 未启用 (autopilot.mode=off 且 auto_run.enabled=false)，使用 --once 强制单轮"
        )
        return 0

    interval = _interval_minutes(cfg, args.interval or None)
    ap = cfg.get("autopilot") or {}
    if args.no_open:
        open_paper = False
    else:
        open_paper = bool(
            ap.get(
                "open_paper_on_fullauto",
                (cfg.get("paper_trading") or {}).get("auto_follow", True),
            )
        )

    running = True

    def _stop(_sig=None, _frame=None):
        nonlocal running
        running = False
        logger.info("收到停止信号，完成本轮后退出…")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "Autopilot 模式=%s | 间隔=%smin | 模拟开仓=%s | 内嵌watcher=%s | profile=%s",
        ap.get("mode", "legacy"),
        interval,
        "是" if open_paper else "否",
        "是" if embed else "否",
        cfg.get("trading_profile"),
    )

    def _maybe_learning_backup() -> None:
        try:
            from bnb_quant_tool.system_maintenance import SystemMaintenance

            # 热读配置，便于改 interval 后无需整进程重配
            maint = SystemMaintenance(PROJECT_ROOT, runner.config)
            try:
                from bnb_quant_tool.config_access import load_app_config

                fresh = load_app_config(PROJECT_ROOT / "config.yaml")
                if isinstance(fresh, dict):
                    maint.config = fresh
                    maint.learning_backups_dir = maint._resolve_learning_backup_dir()
            except Exception:
                pass

            out = maint.maybe_scheduled_learning_backup()
            if out.get("skipped"):
                if out.get("reason") == "not_due":
                    logger.debug(
                        "学习备份未到期 (距上次 %.1fh / 间隔 %sd)",
                        float(out.get("hours_since_last") or 0),
                        out.get("interval_days"),
                    )
                return
            if out.get("ok"):
                logger.info(
                    "学习自动备份完成 → %s (%s bytes)",
                    out.get("backup_path"),
                    out.get("size_bytes"),
                )
                pruned = out.get("pruned") or []
                if pruned:
                    logger.info("已清理旧学习备份: %s", ", ".join(pruned[:5]))
            else:
                logger.warning("学习自动备份失败: %s", out.get("error"))
        except Exception as e:
            logger.warning("学习自动备份异常: %s", e)

    _training_cadence_lock = threading.Lock()
    _training_cadence_running = False

    def _maybe_training_cadence() -> None:
        nonlocal _training_cadence_running
        # 策略发现很重：后台跑，不堵分析开仓
        if not _training_cadence_lock.acquire(blocking=False):
            logger.debug("训练节拍跳过: 上一轮仍在跑")
            return
        if _training_cadence_running:
            _training_cadence_lock.release()
            return
        _training_cadence_running = True
        _training_cadence_lock.release()

        def _worker():
            nonlocal _training_cadence_running
            try:
                from bnb_quant_tool.training_loop import run_cadence_hooks
                from bnb_quant_tool.config_access import load_app_config

                cfg = runner.config
                try:
                    fresh = load_app_config(PROJECT_ROOT / "config.yaml")
                    if isinstance(fresh, dict):
                        cfg = fresh
                except Exception:
                    pass

                paper_db = getattr(runner.paper_engine, "db_path", None)
                try:
                    from bnb_quant_tool.strategy_pool import maybe_reload_from_signal

                    rel = maybe_reload_from_signal(project_root=PROJECT_ROOT)
                    if rel.get("ok") and not rel.get("skipped"):
                        logger.info(
                            "策略池信号热加载: n=%s ver=%s",
                            rel.get("reloaded"),
                            rel.get("version"),
                        )
                except Exception as e:
                    logger.debug("strategy pool signal: %s", e)

                out = run_cadence_hooks(
                    PROJECT_ROOT,
                    cfg,
                    fetcher=getattr(runner, "fetcher", None),
                    paper_db_path=paper_db,
                )
                disc = out.get("discover") or {}
                evo = out.get("evolve") or {}
                if disc.get("ok") and not disc.get("skipped"):
                    logger.info(
                        "训练循环发现策略: %s 条",
                        disc.get("discovered"),
                    )
                elif disc.get("skipped") and disc.get("reason") == "not_due":
                    logger.debug(
                        "策略发现未到期 (新平仓=%s / 需=%s)",
                        disc.get("since_last_closed"),
                        disc.get("every_n"),
                    )
                elif not disc.get("ok"):
                    logger.warning("策略发现失败: %s", disc.get("error"))

                if evo.get("ok") and not evo.get("skipped"):
                    logger.info(
                        "训练循环指标进化: fitness=%s",
                        evo.get("fitness"),
                    )
                elif not evo.get("ok") and not evo.get("skipped"):
                    logger.warning("指标进化失败: %s", evo.get("error"))
            except Exception as e:
                logger.warning("训练循环节拍异常: %s", e)
            finally:
                _training_cadence_running = False

        threading.Thread(target=_worker, daemon=True, name="training-cadence").start()
        logger.info("训练节拍已后台启动（发现/进化不阻塞分析）")

    cycle = 0
    while running:
        cycle += 1
        logger.info("=== Autopilot cycle %s ===", cycle)
        _maybe_learning_backup()
        _maybe_training_cadence()
        try:
            result = runner.run_cycle(
                symbol=args.symbol or None,
                timeframe=args.timeframe or None,
                open_paper=open_paper,
            )
            if result.get("ok"):
                logger.info(
                    "结果: %s | 门控=%s | 置信=%.0f%% | 记录#%s | 开仓#%s",
                    result.get("action"),
                    "通过" if result.get("passed_gate") else "拦截",
                    float(result.get("confidence") or 0) * 100,
                    result.get("record_id"),
                    result.get("position_id") or "-",
                )
                if not result.get("passed_gate"):
                    reasons = result.get("gate_reasons") or []
                    if reasons:
                        logger.info("门控原因: %s", "; ".join(str(r) for r in reasons[:3]))
            else:
                logger.error("分析失败: %s", result.get("error"))
        except Exception as e:
            logger.exception("cycle failed: %s", e)

        if args.once or not running:
            break

        logger.info("下次运行: %s 分钟后", interval)
        # 可中断 sleep，便于 systemd stop
        deadline = time.time() + max(60, interval * 60)
        while running and time.time() < deadline:
            time.sleep(1)

    if embed and runner.paper_engine.is_watching():
        try:
            runner.paper_engine.stop_watcher()
        except Exception:
            pass
    logger.info("Autopilot 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
