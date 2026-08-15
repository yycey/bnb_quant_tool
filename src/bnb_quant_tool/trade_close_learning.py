"""
模拟盘平仓 → 完整学习闭环（GUI / paper_watcher / 无头模式统一入口）。

目标：每一笔平仓都必须学习并留下可度量进步，服务最终胜率提升。
- 无 learning_record_id 时自动建 stub 分析记录，不断链
- 幂等：同一 position 只完整学一次
- 回写交易员议会胜负权重
- 策略权重 / 模式记忆 / 反事实 / 知识提炼 / 门控收紧
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from bnb_quant_tool.trade_quality import score_closed_trade


def _parse_ts_utc_naive(raw: Any) -> Optional[datetime]:
    """解析时间戳为 UTC naive，便于跨本地/UTC 源比较。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    if not s:
        return None
    # 带时区
    try:
        if len(s) > 19 and ("+" in s[19:] or s.count("-") >= 3):
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19].replace("T", " "), fmt.replace("T", " "))
            # 无时区：优先按本地墙钟转 UTC（模拟盘 historically 用 local now）
            try:
                local_tz = datetime.now().astimezone().tzinfo
                if local_tz is not None:
                    return dt.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
            return dt
        except ValueError:
            continue
    return None

logger = logging.getLogger(__name__)

_PROCESS_LOCK = threading.Lock()


@dataclass
class TradeCloseLearningResult:
    position_id: int
    record_id: Optional[int] = None
    outcome: str = ""
    pnl_usdt: float = 0.0
    feedback_ok: bool = False
    evolution_ran: bool = False
    knowledge_queued: bool = False
    council_updated: int = 0
    stub_record: bool = False
    skipped_duplicate: bool = False
    capability_before: int = 0
    capability_after: int = 0
    errors: list = field(default_factory=list)

    @property
    def progressed(self) -> bool:
        return self.feedback_ok or self.knowledge_queued or self.council_updated > 0


@dataclass
class TradeCloseLearningDeps:
    learner: Any
    config: Dict[str, Any]
    get_position_row: Callable[[int], Optional[Dict]]
    counterfactual: Any = None
    pattern_memory: Any = None
    evolution: Any = None
    trader_memory: Any = None
    paper_engine: Any = None
    on_status: Optional[Callable[[str], None]] = None


def _notify(deps: TradeCloseLearningDeps, msg: str) -> None:
    if deps.on_status:
        try:
            deps.on_status(msg)
        except Exception:
            pass
    logger.info(msg)


def _outcome_from_pnl(pnl: float, quality: Optional[Dict] = None) -> str:
    if pnl > 0.5:
        result = "WIN"
    elif pnl < -0.5:
        result = "LOSS"
    else:
        result = "BREAK_EVEN"
    if quality:
        suggested = quality.get("suggest_feedback")
        if suggested and suggested != result:
            return str(suggested)
    return result


def _resolve_learning_reward(
    outcome: str,
    pnl: float,
    cf_result: Optional[Dict[str, Any]],
) -> tuple:
    """统一 Agent/议会/DQN 奖励口径。

    Returns:
        (learn_outcome, learn_pnl, skip_weight_update)
        - beta：跳过权重更新（运气/大盘）
        - 有 excess：用超额符号定 WIN/LOSS，pnl 用 excess
    """
    if not cf_result:
        return outcome, float(pnl), False
    attr = str(cf_result.get("attribution") or "")
    excess = cf_result.get("excess_pnl")
    if attr == "beta":
        return outcome, float(pnl), True
    if excess is None:
        return outcome, float(pnl), False
    try:
        ex = float(excess)
    except (TypeError, ValueError):
        return outcome, float(pnl), False
    if attr == "noise" and abs(ex) < 1.0:
        return outcome, float(pnl), True
    if ex > 0.5:
        return "WIN", ex, False
    if ex < -0.5:
        return "LOSS", ex, False
    return "BREAK_EVEN", ex, False


def _load_analysis_meta(learner, record_id: int) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "regime": None,
        "decision_explanation": None,
        "multi_agent_delib": None,
        "mini_result": None,
    }
    try:
        conn = learner._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT market_regime, decision_explanation, multi_agent_deliberation, "
            "indicators, ai_confidence, consensus_confidence, current_price, symbol "
            "FROM analysis_records WHERE id=?",
            (int(record_id),),
        )
        rec = cur.fetchone()
        if not rec:
            return meta
        meta["regime"] = rec[0]
        if rec[1]:
            try:
                meta["decision_explanation"] = json.loads(rec[1])
            except Exception:
                pass
        if rec[2]:
            try:
                meta["multi_agent_delib"] = json.loads(rec[2])
            except Exception:
                pass
        ind_data = {}
        try:
            ind_data = json.loads(rec[3] or "{}")
        except Exception:
            pass
        meta["mini_result"] = {
            "indicators": ind_data,
            "ai_analysis": {"confidence": rec[4]},
            "institutional_strategies": {"consensus_confidence": rec[5]},
            "current_price": rec[6],
            "symbol": rec[7] or "BNBUSDT",
            "news_summary": {},
        }
    except Exception as e:
        logger.debug("load analysis meta: %s", e)
    return meta


def _resolve_trader_memory(deps: TradeCloseLearningDeps):
    if deps.trader_memory is not None:
        return deps.trader_memory
    try:
        from bnb_quant_tool.agents.trader_memory import TraderMemoryStore
        from pathlib import Path

        tc = deps.config.get("trader_council") or {}
        db_path = tc.get("memory_db") or "data/trader_memory.db"
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
            if not Path(db_path).is_absolute():
                db_path = str(root / db_path)
        except Exception:
            pass
        return TraderMemoryStore(db_path)
    except Exception as e:
        logger.debug("resolve trader_memory: %s", e)
        return None


def _bind_record_to_position(get_position_row, position_id: int, record_id: int, row: Dict) -> None:
    """尽量把 stub/补建的 record_id 写回仓位，方便下次追溯。"""
    try:
        engine = getattr(get_position_row, "__self__", None)
        if engine is None or not hasattr(engine, "_conn"):
            return

        def _op():
            with getattr(engine, "_lock", _NullLock()):
                conn = engine._conn()
                cur = conn.cursor()
                try:
                    from bnb_quant_tool.sqlite_util import begin_immediate
                    begin_immediate(conn)
                    cur.execute(
                        "UPDATE paper_positions SET learning_record_id=? WHERE id=? AND "
                        "(learning_record_id IS NULL OR learning_record_id=0)",
                        (int(record_id), int(position_id)),
                    )
                    conn.commit()
                except Exception:
                    if hasattr(engine, "_safe_rollback"):
                        engine._safe_rollback()
                    else:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    raise

        if hasattr(engine, "_run_db"):
            engine._run_db(_op, label=f"bind_record#{position_id}")
        else:
            from bnb_quant_tool.sqlite_util import run_db
            run_db(
                _op,
                label=f"bind_record#{position_id}",
                on_locked=lambda: getattr(engine, "reset_connection", lambda: None)(),
            )
    except Exception as e:
        logger.debug("bind record to position: %s", e)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _votes_from_trader_memory(
    trader_memory,
    *,
    around_iso: Optional[str] = None,
    window_minutes: int = 45,
    limit: int = 24,
) -> List[Dict[str, Any]]:
    """平仓时若无 multi_agent 快照，从 trader_votes 近邻回填议会投票。"""
    if trader_memory is None:
        return []
    try:
        conn = trader_memory._connect()
        try:
            rows = conn.execute(
                """
                SELECT trader_id, action, confidence, score, summary, source, created_at
                FROM trader_votes
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("votes_from_trader_memory: %s", e)
        return []

    center = _parse_ts_utc_naive(around_iso) if around_iso else None

    votes: List[Dict[str, Any]] = []
    seen = set()
    for r in rows or []:
        tid = str(r["trader_id"] or "").strip()
        if not tid or tid in seen:
            continue
        if center is not None:
            ct = _parse_ts_utc_naive(r["created_at"])
            if ct is not None and abs((ct - center).total_seconds()) > window_minutes * 60:
                continue
        seen.add(tid)
        votes.append(
            {
                "trader_id": tid,
                "action": str(r["action"] or "WAIT"),
                "confidence": float(r["confidence"] or 0),
                "score": float(r["score"] or 0),
                "summary": str(r["summary"] or ""),
                "source": str(r["source"] or ""),
            }
        )
        if len(votes) >= limit:
            break

    # 时间窗内无票：禁止回退「全局最近投票」（易把无关议会票绑到本仓）
    if not votes and center is not None:
        logger.debug(
            "votes_from_trader_memory: 无窗内投票 around=%s window=%smin，跳过回退",
            around_iso, window_minutes,
        )
    return votes


def _votes_for_record(trader_memory, record_id: Optional[int], limit: int = 24) -> List[Dict[str, Any]]:
    if trader_memory is None or not record_id:
        return []
    try:
        conn = trader_memory._connect()
        try:
            rows = conn.execute(
                """
                SELECT trader_id, action, confidence, score, summary, source
                FROM trader_votes
                WHERE record_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (int(record_id), limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("votes_for_record: %s", e)
        return []
    votes = []
    seen = set()
    for r in rows or []:
        tid = str(r["trader_id"] or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        votes.append(
            {
                "trader_id": tid,
                "action": str(r["action"] or "WAIT"),
                "confidence": float(r["confidence"] or 0),
                "score": float(r["score"] or 0),
                "summary": str(r["summary"] or ""),
            }
        )
    return votes


def _record_council_outcomes(
    trader_memory,
    multi_agent_delib: Optional[Dict],
    *,
    trade_side: str,
    outcome: str,
    pnl: float,
    position_id: int,
    opened_at: Optional[str] = None,
    record_id: Optional[int] = None,
    allow_time_backfill: bool = True,
) -> int:
    """根据议会投票 vs 实际盈亏，更新每位交易员权重。"""
    if trader_memory is None:
        return 0
    if outcome not in ("WIN", "LOSS"):
        return 0

    council = multi_agent_delib.get("council") if isinstance(multi_agent_delib, dict) else None
    votes: List[Dict] = []
    if isinstance(council, dict):
        votes = council.get("votes") or []
    if not votes and isinstance(multi_agent_delib, dict):
        votes = (
            multi_agent_delib.get("agent_votes")
            or multi_agent_delib.get("votes")
            or []
        )
    if not isinstance(votes, list) or not votes:
        votes = _votes_for_record(trader_memory, record_id, limit=24)
    if (not isinstance(votes, list) or not votes) and allow_time_backfill:
        votes = _votes_from_trader_memory(
            trader_memory, around_iso=opened_at, window_minutes=60, limit=24
        )
        if votes:
            logger.info(
                "council outcomes: backfilled %s votes from trader_memory for paper#%s",
                len(votes),
                position_id,
            )
    if not isinstance(votes, list) or not votes:
        return 0

    side = (trade_side or "").upper()
    if side in ("BUY",):
        side = "LONG"
    if side in ("SELL",):
        side = "SHORT"
    trade_won = outcome == "WIN"
    updated = 0

    for v in votes:
        if not isinstance(v, dict):
            continue
        tid = str(v.get("trader_id") or "").strip()
        if not tid:
            continue
        action = str(v.get("action") or v.get("vote") or "").upper()
        if action in ("BUY",):
            action = "LONG"
        if action in ("SELL",):
            action = "SHORT"

        if action in ("LONG", "SHORT") and side in ("LONG", "SHORT"):
            aligned = action == side
            correct = aligned if trade_won else (not aligned)
        elif action in ("WAIT", "HOLD", ""):
            # 观望：盈利单视为偏保守（错），亏损单视为避险（对）
            correct = not trade_won
        else:
            continue

        note = (
            f"paper#{position_id} side={side} vote={action} "
            f"outcome={outcome} pnl={pnl:+.2f}"
        )
        try:
            # 只写投票实际 ID；get_accuracy 会桥接 persona / persona__provider
            # （勿双写，否则样本数翻倍、权重失真）
            trader_memory.record_outcome(
                tid,
                correct=bool(correct),
                pnl=float(pnl),
                note=note,
            )
            try:
                lesson = (
                    f"[{outcome}] 投{action} vs 实盘{side} → "
                    f"{'正确' if correct else '错误'} PnL={pnl:+.1f}"
                )
                prev = trader_memory.get_lessons(tid, max_chars=500) or ""
                merged = (lesson + "\n" + prev).strip()[:2000]
                trader_memory.update_lessons(tid, merged)
                if "__" in tid:
                    base = tid.split("__", 1)[0].strip()
                    if base:
                        trader_memory.update_lessons(base, merged)
            except Exception:
                pass
            updated += 1
        except Exception as e:
            logger.debug("council outcome %s: %s", tid, e)
    return updated


def process_trade_close(
    position_id: int,
    deps: TradeCloseLearningDeps,
) -> TradeCloseLearningResult:
    """平仓后完整学习管道 — 所有运行模式共用；缺记录也不中断。"""
    result = TradeCloseLearningResult(position_id=int(position_id))
    learn_cfg = (deps.config or {}).get("learning") or {}
    allow_stub = bool(learn_cfg.get("create_stub_record_if_missing", True))
    update_council = bool(learn_cfg.get("update_council_memory", True))

    with _PROCESS_LOCK:
        try:
            if hasattr(deps.learner, "has_paper_close_learned"):
                if deps.learner.has_paper_close_learned(int(position_id)):
                    result.skipped_duplicate = True
                    result.errors.append("already_learned")
                    return result
        except Exception:
            pass

        try:
            try:
                growth0 = deps.learner.get_growth_snapshot() or {}
                result.capability_before = int(growth0.get("capability_level") or 0)
            except Exception:
                pass

            row = deps.get_position_row(int(position_id))
            if not row:
                result.errors.append("position_not_found")
                return result

            record_id = row.get("learning_record_id")
            stub = False
            if not record_id:
                if allow_stub and hasattr(deps.learner, "ensure_stub_analysis_record_for_position"):
                    record_id = deps.learner.ensure_stub_analysis_record_for_position(row)
                    stub = bool(record_id)
                if not record_id:
                    result.errors.append("no_learning_record")
                    # 即便无记录，仍尽量更新议会记忆（若 snapshot 里有）
                    _notify(deps, f"平仓 #{position_id} 无分析记录且 stub 失败，跳过反馈回填")
                else:
                    _bind_record_to_position(
                        deps.get_position_row, int(position_id), int(record_id), row
                    )
                    _notify(deps, f"平仓 #{position_id} 已补建分析记录 #{record_id}")

            result.record_id = int(record_id) if record_id else None
            result.stub_record = stub

            pnl = float(row.get("realized_pnl_usdt", 0) or 0)
            close_price = float(row.get("close_avg_price", 0) or 0)
            result.pnl_usdt = pnl

            quality = score_closed_trade(row)
            outcome = _outcome_from_pnl(pnl, quality)
            result.outcome = outcome

            meta: Dict[str, Any] = {}
            decision_explanation = None
            multi_agent_delib = None
            regime = None

            if record_id:
                meta = _load_analysis_meta(deps.learner, int(record_id))
                decision_explanation = meta.get("decision_explanation")
                multi_agent_delib = meta.get("multi_agent_delib")
                regime = meta.get("regime")

                # 仓位 snapshot 里也可能有议会结果
                if not multi_agent_delib:
                    try:
                        snap_raw = row.get("advice_snapshot")
                        snap = json.loads(snap_raw) if isinstance(snap_raw, str) else (snap_raw or {})
                        ma = snap.get("multi_agent_deliberation") if isinstance(snap, dict) else None
                        if isinstance(ma, dict):
                            multi_agent_delib = ma
                    except Exception:
                        pass

                ok = deps.learner.submit_feedback(
                    record_id=int(record_id),
                    actual_result=outcome,
                    actual_price=close_price if close_price > 0 else None,
                    notes=(
                        f"PaperTrade #{position_id} PnL=${pnl:.2f} "
                        f"reason={row.get('close_reason', '?')} | {quality.get('text', '')}"
                        + (" | stub_record" if stub else "")
                    ),
                    quality=quality,
                    decision_explanation=decision_explanation,
                )
                result.feedback_ok = bool(ok)

                if ok:
                    growth = deps.learner.get_growth_snapshot() or {}
                    dims = growth.get("capability_dimensions") or {}
                    result.capability_after = int(growth.get("capability_level") or 0)
                    _notify(
                        deps,
                        f"学习反馈 #{record_id} → {outcome} (PnL=${pnl:.2f}) | "
                        f"能力 L{result.capability_before}→L{result.capability_after} "
                        f"准确{dims.get('prediction_accuracy', 0)}",
                    )

            cf_result = None
            if deps.counterfactual:
                try:
                    symbol = deps.config.get("trading", {}).get("symbol", "BNBUSDT")
                    cf_result = deps.counterfactual.analyze(row, symbol=symbol)
                    if cf_result and cf_result.get("best_scenario"):
                        excess = cf_result.get("excess_pnl")
                        attr = cf_result.get("attribution") or ""
                        _notify(
                            deps,
                            f"反事实 #{position_id}: 最优={cf_result['best_scenario']} "
                            f"评分={cf_result.get('decision_score', 0):.0f}/100 "
                            f"超额={excess} [{attr}]",
                        )
                except Exception as ce:
                    logger.warning("counterfactual: %s", ce)

            # 学习奖励口径：超额优先；beta 不抬 Agent/议会权重
            learn_outcome, learn_pnl, skip_weight_update = _resolve_learning_reward(
                outcome, pnl, cf_result
            )

            # 议会交易员胜负回写（在反事实之后，用超额口径）
            if update_council and not skip_weight_update:
                try:
                    allow_bf = True
                    try:
                        snap_raw = row.get("advice_snapshot")
                        snap = (
                            json.loads(snap_raw)
                            if isinstance(snap_raw, str)
                            else (snap_raw or {})
                        )
                        if isinstance(snap, dict) and snap.get("_skipped_council_reuse"):
                            allow_bf = False
                            if not multi_agent_delib:
                                logger.info(
                                    "paper#%s skipped council outcomes (reuse, no deliberation)",
                                    position_id,
                                )
                    except Exception:
                        pass
                    tm = _resolve_trader_memory(deps)
                    n = _record_council_outcomes(
                        tm,
                        multi_agent_delib,
                        trade_side=str(row.get("side") or ""),
                        outcome=learn_outcome,
                        pnl=learn_pnl,
                        position_id=int(position_id),
                        opened_at=str(row.get("opened_at") or "") or None,
                        record_id=int(record_id) if record_id else None,
                        allow_time_backfill=allow_bf,
                    )
                    result.council_updated = int(n)
                    if n:
                        _notify(
                            deps,
                            f"议会记忆已更新：{n} 位交易员回写 "
                            f"({learn_outcome}, pnl={learn_pnl:.2f})",
                        )
                except Exception as ce:
                    logger.warning("council memory update: %s", ce)
                    result.errors.append(f"council:{ce}")
            elif update_council and skip_weight_update:
                _notify(deps, f"议会权重跳过（归因={((cf_result or {}).get('attribution') or 'beta')}）")

            # 模式记忆：有 mini_result 才写；否则用仓位快照凑最小指纹
            if deps.pattern_memory:
                try:
                    mini = meta.get("mini_result") if meta else None
                    if not mini:
                        mini = {
                            "indicators": {},
                            "ai_analysis": {"confidence": 0.5},
                            "institutional_strategies": {},
                            "current_price": close_price or float(row.get("entry_price") or 0),
                            "symbol": row.get("symbol") or "BNBUSDT",
                            "news_summary": {},
                        }
                    deps.pattern_memory.record_pattern(
                        mini,
                        outcome=outcome,
                        pnl=pnl,
                        record_id=int(record_id) if record_id else None,
                        side=row.get("side"),
                    )
                except Exception as pe:
                    logger.warning("pattern_memory: %s", pe)

            # Agent 准确度：与 DQN/议会对齐
            if record_id and multi_agent_delib and not skip_weight_update:
                try:
                    if learn_outcome in ("WIN", "LOSS"):
                        deps.learner.record_agent_accuracy(
                            multi_agent_delib,
                            trade_side=str(row.get("side") or ""),
                            outcome=learn_outcome,
                            record_id=int(record_id),
                        )
                except Exception as ae:
                    logger.debug("record_agent_accuracy: %s", ae)

            # DQN 影子样本 + 增量训练钩子
            try:
                from bnb_quant_tool.dqn_shadow import remember_close

                ind = {}
                if isinstance(meta, dict):
                    ind = meta.get("indicators") or {}
                if not ind:
                    try:
                        snap_raw = row.get("advice_snapshot")
                        snap = json.loads(snap_raw) if isinstance(snap_raw, str) else (snap_raw or {})
                        ind = (snap.get("indicators") if isinstance(snap, dict) else None) or {}
                    except Exception:
                        ind = {}
                remember_close(
                    indicators=ind if isinstance(ind, dict) else {},
                    side=str(row.get("side") or ""),
                    pnl=float(learn_pnl),
                    config=deps.config,
                )
            except Exception as dqn_e:
                logger.debug("dqn remember_close: %s", dqn_e)

            # 亏损 → 反记忆
            if outcome == "LOSS" and pnl < 0:
                try:
                    from bnb_quant_tool.memory_governance import save_anti_memory_lesson
                    from bnb_quant_tool.analysis_reuse import situation_key

                    ind = {}
                    mr = regime
                    if isinstance(meta, dict):
                        ind = meta.get("indicators") or {}
                        mr = meta.get("regime") or mr
                    sit = ""
                    try:
                        sit = situation_key(
                            ind if isinstance(ind, dict) else {},
                            mr,
                            symbol=str(row.get("symbol") or "BNBUSDT"),
                        )
                    except Exception:
                        sit = str((meta or {}).get("situation_key") or "")
                    cm = getattr(deps.learner, "capability_memory", None)
                    if cm and sit:
                        save_anti_memory_lesson(
                            cm,
                            situation_key=sit,
                            side=str(row.get("side") or ""),
                            pnl=pnl,
                            note=f"excess={(cf_result or {}).get('excess_pnl')} "
                                 f"attr={(cf_result or {}).get('attribution')}",
                        )
                except Exception as am_e:
                    logger.debug("anti_memory save: %s", am_e)

            if deps.evolution and record_id:
                trade_row = {**row, "id": position_id, "learning_record_id": record_id}
                try:
                    deps.evolution.on_trade_closed(
                        position_id=int(position_id),
                        trade_row=trade_row,
                        record_id=int(record_id),
                        outcome=outcome,
                        quality=quality,
                        cf_result=cf_result,
                        decision_explanation=decision_explanation,
                        regime=regime,
                    )
                    deps.evolution.on_trade_closed_async(
                        position_id=int(position_id),
                        trade_row=trade_row,
                        record_id=int(record_id),
                        outcome=outcome,
                        quality=quality,
                        decision_explanation=decision_explanation,
                        regime=regime,
                    )
                    result.evolution_ran = True
                except Exception as ev_e:
                    logger.warning("evolution on_trade_closed: %s", ev_e)
                    result.errors.append(f"evolution:{ev_e}")

            # 影子策略同期记账（独立模块，失败不影响主学习）
            try:
                from bnb_quant_tool.shadow_close_hooks import record_paper_close_on_shadows

                record_paper_close_on_shadows(
                    pnl=float(pnl or 0.0),
                    win=(outcome == "WIN"),
                    config=deps.config,
                    learning_db_path=getattr(deps.learner, "db_path", None),
                    market_data={
                        "position_id": int(position_id),
                        "outcome": outcome,
                        "close_reason": str(row.get("close_reason") or ""),
                        "side": str(row.get("side") or ""),
                    },
                )
            except Exception as sh_e:
                logger.debug("shadow close hook: %s", sh_e)

            if record_id:
                try:
                    from bnb_quant_tool.capability_memory import (
                        extract_knowledge_async,
                        load_analysis_record,
                    )

                    analysis_rec = load_analysis_record(deps.learner, int(record_id))
                    extract_knowledge_async(
                        deps.learner.capability_memory,
                        "extract_and_save_from_trade",
                        trade_row={**row, "id": position_id, "learning_record_id": record_id},
                        analysis_record=analysis_rec,
                        outcome=outcome,
                        quality=quality,
                    )
                    result.knowledge_queued = True
                    _notify(deps, "知识提炼已提交（后台 AI 处理）")
                except Exception as ke:
                    logger.debug("knowledge extract: %s", ke)

            if outcome == "LOSS":
                try:
                    from bnb_quant_tool.learning_analytics import maybe_auto_tighten_gate_after_loss
                    from bnb_quant_tool.data_localization import get_localization_manager

                    gate_result = maybe_auto_tighten_gate_after_loss(
                        deps.learner,
                        config=deps.config,
                        project_root=get_localization_manager().workspace,
                        outcome=outcome,
                    )
                    if gate_result and gate_result.get("ok"):
                        _notify(deps, f"自动门控收紧: {gate_result.get('message', '')}")
                except Exception as ge:
                    logger.debug("auto gate tighten: %s", ge)

            try:
                growth1 = deps.learner.get_growth_snapshot() or {}
                result.capability_after = int(
                    growth1.get("capability_level") or result.capability_after or 0
                )
            except Exception:
                pass

            # 本地盈利成长：定阶 + 单点下一课（围绕下单 E[R]）
            try:
                from bnb_quant_tool.local_growth_coach import run_growth_coach_after_close

                paper_eng = getattr(deps, "paper_engine", None)
                gplan = run_growth_coach_after_close(
                    config=deps.config,
                    paper_engine=paper_eng,
                    learner=deps.learner,
                    last_trade={
                        **row,
                        "realized_pnl_usdt": pnl,
                        "side": row.get("side"),
                        "mfe_r": row.get("mfe_r"),
                    },
                )
                if gplan and gplan.get("stage"):
                    _notify(
                        deps,
                        f"本地成长 [{gplan.get('stage')}]: {gplan.get('next_lesson', '')[:100]}",
                    )
            except Exception as gc_e:
                logger.debug("local_growth_coach: %s", gc_e)

            # 验证模式：记录假设对错
            try:
                from bnb_quant_tool.validation_trading import record_validation_close
                vclose = record_validation_close(
                    position_id=int(position_id),
                    trade_row=row,
                    outcome=outcome,
                    pnl=float(pnl),
                    config=deps.config,
                )
                if vclose.get("verdict"):
                    _notify(
                        deps,
                        f"验证结果 #{position_id}: 想法{vclose['verdict']} "
                        f"({outcome} {pnl:+.2f}U)",
                    )
            except Exception as vc_e:
                logger.debug("validation close record: %s", vc_e)

            if hasattr(deps.learner, "mark_paper_close_learned") and result.progressed:
                deps.learner.mark_paper_close_learned(
                    int(position_id),
                    {
                        "outcome": outcome,
                        "pnl": pnl,
                        "record_id": result.record_id,
                        "capability_before": result.capability_before,
                        "capability_after": result.capability_after,
                        "council_updated": result.council_updated,
                    },
                )

            if result.progressed:
                _notify(
                    deps,
                    f"平仓学习完成 #{position_id}: {outcome} | "
                    f"能力 L{result.capability_before}→L{result.capability_after} | "
                    f"议会回写 {result.council_updated}",
                )

        except Exception as e:
            logger.warning("process_trade_close failed: %s", e)
            result.errors.append(str(e))

    return result


def process_manual_feedback(
    record_id: int,
    actual_result: str,
    deps: TradeCloseLearningDeps,
    *,
    actual_price: Optional[float] = None,
    notes: str = "",
) -> TradeCloseLearningResult:
    """手动反馈（待反馈列表）— 补齐模式记忆、知识提炼、自动门控。"""
    result = TradeCloseLearningResult(position_id=0, record_id=int(record_id))
    try:
        meta = _load_analysis_meta(deps.learner, int(record_id))
        pnl = 0.0
        if actual_price and meta.get("mini_result", {}).get("current_price"):
            cp = float(meta["mini_result"]["current_price"])
            if cp > 0:
                pnl = (float(actual_price) - cp) / cp * 100

        ok = deps.learner.submit_feedback(
            record_id=int(record_id),
            actual_result=actual_result,
            actual_price=actual_price,
            notes=notes or f"Manual feedback: {actual_result}",
        )
        result.feedback_ok = bool(ok)
        result.outcome = actual_result

        if ok and deps.pattern_memory and meta.get("mini_result"):
            try:
                deps.pattern_memory.record_pattern(
                    meta["mini_result"],
                    outcome=actual_result,
                    pnl=pnl,
                    record_id=int(record_id),
                )
            except Exception as pe:
                logger.warning("pattern_memory manual: %s", pe)

        if ok:
            try:
                from bnb_quant_tool.capability_memory import (
                    extract_knowledge_async,
                    load_analysis_record,
                )

                analysis_rec = load_analysis_record(deps.learner, int(record_id))
                extract_knowledge_async(
                    deps.learner.capability_memory,
                    "extract_and_save_from_trade",
                    trade_row={"learning_record_id": record_id, "realized_pnl_usdt": pnl},
                    analysis_record=analysis_rec,
                    outcome=actual_result,
                )
                result.knowledge_queued = True
            except Exception as ke:
                logger.debug("manual knowledge extract: %s", ke)

            if actual_result == "LOSS":
                try:
                    from bnb_quant_tool.learning_analytics import maybe_auto_tighten_gate_after_loss
                    from bnb_quant_tool.data_localization import get_localization_manager

                    gate_result = maybe_auto_tighten_gate_after_loss(
                        deps.learner,
                        config=deps.config,
                        project_root=get_localization_manager().workspace,
                        outcome=actual_result,
                    )
                    if gate_result and gate_result.get("ok"):
                        _notify(deps, f"自动门控收紧: {gate_result.get('message', '')}")
                except Exception:
                    pass

    except Exception as e:
        logger.warning("process_manual_feedback failed: %s", e)
        result.errors.append(str(e))

    return result


def build_evolution_coordinator(learner, config: Dict, counterfactual=None):
    """懒创建 LearningEvolutionCoordinator。"""
    try:
        from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator

        return LearningEvolutionCoordinator(
            learner,
            capability_memory=learner.capability_memory,
            counterfactual=counterfactual,
            config=config,
        )
    except Exception as e:
        logger.warning("LearningEvolutionCoordinator init failed: %s", e)
        return None


def backfill_missing_council_outcomes(
    config: Optional[Dict[str, Any]] = None,
    *,
    limit: int = 8,
) -> int:
    """把历史已平仓、但尚未写入 trader_outcomes 的议会胜负补回来。"""
    cfg = config or {}
    learn_cfg = cfg.get("learning") or {}
    if not bool(learn_cfg.get("update_council_memory", True)):
        return 0
    limit = max(1, int(limit or learn_cfg.get("backfill_council_batch", 8) or 8))
    # 多扫一些历史仓，尽量把有议会快照的都补上
    scan_n = max(limit * 5, 40)

    try:
        from bnb_quant_tool.data_localization import get_localized_db_path
        paper_db = str(get_localized_db_path("paper_trading"))
    except Exception:
        paper_db = "data/paper_trading.db"

    tm = _resolve_trader_memory(
        TradeCloseLearningDeps(learner=None, config=cfg, get_position_row=lambda _i: None)
    )
    if tm is None:
        return 0

    try:
        import sqlite3
        conn = sqlite3.connect(paper_db, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, side, realized_pnl_usdt, learning_record_id, opened_at,
                   advice_snapshot, close_reason
            FROM paper_positions
            WHERE status='CLOSED'
            ORDER BY id DESC
            LIMIT ?
            """,
            (scan_n,),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("backfill council load paper: %s", e)
        return 0

    # 已写过的 paper#id
    existing_notes = set()
    try:
        tconn = tm._connect()
        try:
            for r in tconn.execute(
                "SELECT note FROM trader_outcomes ORDER BY id DESC LIMIT 2000"
            ).fetchall():
                note = str(r["note"] or "")
                if "paper#" in note:
                    existing_notes.add(note.split("paper#", 1)[-1].split()[0])
        finally:
            tconn.close()
    except Exception:
        pass

    updated_positions = 0
    for row in rows:
        if updated_positions >= limit:
            break
        pid = str(row["id"])
        if pid in existing_notes:
            continue
        pnl = float(row["realized_pnl_usdt"] or 0)
        outcome = _outcome_from_pnl(pnl)
        if outcome not in ("WIN", "LOSS"):
            continue

        multi_agent_delib = None
        try:
            snap_raw = row["advice_snapshot"]
            snap = json.loads(snap_raw) if isinstance(snap_raw, str) else (snap_raw or {})
            ma = snap.get("multi_agent_deliberation") if isinstance(snap, dict) else None
            if isinstance(ma, dict):
                multi_agent_delib = ma
        except Exception:
            pass

        n = _record_council_outcomes(
            tm,
            multi_agent_delib,
            trade_side=str(row["side"] or ""),
            outcome=outcome,
            pnl=pnl,
            position_id=int(row["id"]),
            opened_at=str(row["opened_at"] or "") or None,
            record_id=int(row["learning_record_id"]) if row["learning_record_id"] else None,
        )
        if n > 0:
            updated_positions += 1
            existing_notes.add(pid)
            logger.info(
                "council backfill: paper#%s → %s traders (%s)",
                pid, n, outcome,
            )

    if updated_positions:
        logger.info("backfill council outcomes: %s positions", updated_positions)
    return updated_positions
