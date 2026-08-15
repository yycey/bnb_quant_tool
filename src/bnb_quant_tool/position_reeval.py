"""持仓轻量二次评估：开仓后周期性用 MTF + Regime 复评（不调 LLM）。

若 Regime 从中性/趋势切到高波动/恐慌，且浮盈 < 阈值，写入待处理「提前平仓」队列，
不自动砍仓，仅提示门控。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

SAFE_REGIMES = frozenset({
    "RANGING",
    "TRENDING",
    "LOW_VOLATILITY",
    "NEWS_DRIVEN",
})
STRESS_REGIMES = frozenset({
    "HIGH_VOLATILITY",
    "PANIC",
})

_lock = threading.Lock()
_last_snapshot_ts: Dict[str, float] = {}
_snapshot_cache: Dict[str, Dict[str, Any]] = {}


def _cfg(config: Optional[dict]) -> dict:
    return dict((config or {}).get("position_reeval") or {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_opened_at(opened_at_str: str) -> Optional[datetime]:
    if not opened_at_str:
        return None
    try:
        dt = datetime.fromisoformat(str(opened_at_str).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_reeval_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS position_reeval_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            position_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT,
            signal TEXT NOT NULL,
            reason TEXT,
            regime_from TEXT,
            regime_to TEXT,
            unrealized_pnl_pct REAL,
            mtf_action TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            payload TEXT,
            acknowledged_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS position_reeval_state (
            position_id INTEGER PRIMARY KEY,
            last_eval_at TEXT,
            last_regime TEXT,
            entry_regime TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_reeval_q_status "
        "ON position_reeval_queue(status, position_id)"
    )
    conn.commit()


def list_pending_reeval(
    db_path: str,
    *,
    symbol: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    path = Path(db_path) if db_path else None
    if not path or not path.exists():
        return []
    with _lock:
        conn = _connect_paper(str(path))
        try:
            ensure_reeval_tables(conn)
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM position_reeval_queue "
                    "WHERE status='pending' AND symbol=? "
                    "ORDER BY id DESC LIMIT ?",
                    (symbol.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM position_reeval_queue "
                    "WHERE status='pending' ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def acknowledge_reeval(db_path: str, queue_id: int, *, status: str = "acknowledged") -> bool:
    with _lock:
        conn = _connect_paper(str(db_path))
        try:
            ensure_reeval_tables(conn)
            cur = conn.execute(
                "UPDATE position_reeval_queue SET status=?, acknowledged_at=? "
                "WHERE id=? AND status='pending'",
                (status, _now_iso(), int(queue_id)),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def _connect_paper(db_path: str) -> sqlite3.Connection:
    from bnb_quant_tool.sqlite_util import connect_writer
    return connect_writer(str(db_path), timeout=60.0, row_factory=True)


def _has_pending_for_position(conn: sqlite3.Connection, position_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM position_reeval_queue "
        "WHERE position_id=? AND status='pending' LIMIT 1",
        (int(position_id),),
    ).fetchone()
    return row is not None


def _get_state(conn: sqlite3.Connection, position_id: int) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM position_reeval_state WHERE position_id=?",
        (int(position_id),),
    ).fetchone()
    return dict(row) if row else {}


def _upsert_state(
    conn: sqlite3.Connection,
    position_id: int,
    *,
    last_eval_at: str,
    last_regime: str,
    entry_regime: Optional[str] = None,
) -> None:
    prev = _get_state(conn, position_id)
    er = entry_regime or prev.get("entry_regime") or last_regime
    conn.execute(
        """
        INSERT INTO position_reeval_state
            (position_id, last_eval_at, last_regime, entry_regime, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(position_id) DO UPDATE SET
            last_eval_at=excluded.last_eval_at,
            last_regime=excluded.last_regime,
            entry_regime=COALESCE(position_reeval_state.entry_regime, excluded.entry_regime),
            updated_at=excluded.updated_at
        """,
        (int(position_id), last_eval_at, last_regime, er, _now_iso()),
    )


def _enqueue(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    symbol: str,
    side: str,
    reason: str,
    regime_from: str,
    regime_to: str,
    unrealized_pnl_pct: float,
    mtf_action: str,
    payload: Dict[str, Any],
    signal: str = "EARLY_CLOSE",
) -> Optional[int]:
    if _has_pending_for_position(conn, position_id):
        return None
    sig = str(signal or "EARLY_CLOSE").upper()
    cur = conn.execute(
        """
        INSERT INTO position_reeval_queue
            (created_at, position_id, symbol, side, signal, reason,
             regime_from, regime_to, unrealized_pnl_pct, mtf_action, status, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            _now_iso(),
            int(position_id),
            symbol.upper(),
            side,
            sig,
            reason,
            regime_from,
            regime_to,
            round(float(unrealized_pnl_pct), 4),
            mtf_action,
            json.dumps(payload, ensure_ascii=False, default=str),
        ),
    )
    return int(cur.lastrowid)


def _parse_entry_regime(pos: Dict[str, Any]) -> Optional[str]:
    raw = pos.get("advice_snapshot")
    if not raw:
        return None
    try:
        snap = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(snap, dict):
            return None
        mr = snap.get("market_regime") or snap.get("regime")
        if isinstance(mr, dict):
            return str(mr.get("regime") or "").upper() or None
        if mr:
            return str(mr).upper()
    except Exception:
        return None
    return None


def _pnl_pct(pos: Dict[str, Any], price: float, fee_rate: float = 0.0004) -> float:
    entry = float(pos.get("entry_price") or 0)
    qty = float(pos.get("qty_remaining") or 0)
    if entry <= 0 or qty <= 0 or price <= 0:
        return 0.0
    side = (pos.get("side") or "LONG").upper()
    fee = price * qty * fee_rate
    if side == "LONG":
        pnl = (price - entry) * qty - fee
    else:
        pnl = (entry - price) * qty - fee
    notional = entry * qty
    return (pnl / notional) * 100.0 if notional > 0 else 0.0


def fetch_light_snapshot(
    symbol: str,
    *,
    config: Optional[dict] = None,
    fetcher=None,
) -> Dict[str, Any]:
    """轻量快照：1h 指标 + Regime + 精简 MTF(1h/4h)。带短缓存避免 watcher 刷爆。"""
    import time

    cfg = _cfg(config)
    cache_sec = float(cfg.get("snapshot_cache_seconds", 60) or 60)
    now = time.time()
    cached = _snapshot_cache.get(symbol)
    if cached and (now - _last_snapshot_ts.get(symbol, 0)) < cache_sec:
        return cached

    from bnb_quant_tool.data_fetcher import BinanceDataFetcher
    from bnb_quant_tool.technical_indicators import TechnicalIndicators
    from bnb_quant_tool.market_regime import MarketRegimeDetector
    from bnb_quant_tool.multi_timeframe import MultiTimeframeAnalyzer

    fetcher = fetcher or BinanceDataFetcher()
    df_1h = fetcher.get_klines(symbol=symbol, interval="1h", limit=120)
    if df_1h is None or len(df_1h) < 40:
        return {"ok": False, "error": "kline_insufficient"}

    indicators = TechnicalIndicators.calculate_all_indicators(df_1h)
    regime_det = MarketRegimeDetector((config or {}).get("market_regime") or {})
    regime = regime_det.detect(df_1h, indicators, news_summary=None)

    mtf = {}
    try:
        mtf_an = MultiTimeframeAnalyzer(fetcher=fetcher)
        # 轻量：仅 1h + 4h
        mtf = mtf_an.analyze(
            symbol=symbol,
            timeframes=["1h", "4h"],
            lookback_days_map={"1h": 10, "4h": 30},
            prefetched={"1h": df_1h},
        )
    except Exception as e:
        logger.debug("position_reeval mtf: %s", e)
        mtf = {"recommended_action": "WAIT", "error": str(e)}

    out = {
        "ok": True,
        "symbol": symbol,
        "indicators": indicators,
        "market_regime": regime,
        "regime": (regime or {}).get("regime"),
        "multi_timeframe": mtf,
        "mtf_action": str((mtf or {}).get("recommended_action") or "WAIT").upper(),
        "fetched_at": _now_iso(),
    }
    _snapshot_cache[symbol] = out
    _last_snapshot_ts[symbol] = now
    return out


def should_admit_wrong_direction(
    *,
    side: str,
    mtf_action: str,
    unrealized_pnl_pct: float,
    config: Optional[dict] = None,
) -> tuple[bool, str]:
    """MTF 已明确反向且浮盈不高 → 认错方向，尽快平仓学习。"""
    cfg = _cfg(config)
    aw = cfg.get("admit_wrong") or {}
    if isinstance(aw, bool):
        aw = {"enabled": aw}
    if aw.get("enabled", True) is False:
        return False, ""
    if not bool(aw.get("mtf_flip", True)):
        return False, ""

    max_pnl = float(aw.get("max_unrealized_pnl_pct", 0.35) or 0.35)
    if unrealized_pnl_pct >= max_pnl:
        return False, f"浮盈 {unrealized_pnl_pct:.2f}% ≥ {max_pnl}%，暂不认错"

    side_u = (side or "LONG").upper()
    mtf_u = (mtf_action or "WAIT").upper()
    mtf_flip = (
        (side_u == "LONG" and mtf_u in ("SHORT", "SELL"))
        or (side_u == "SHORT" and mtf_u in ("LONG", "BUY"))
    )
    if not mtf_flip:
        return False, ""
    return True, f"MTF反向={mtf_u} 且浮盈 {unrealized_pnl_pct:.2f}% < {max_pnl}% — 认错平仓"


def should_signal_early_close(
    *,
    entry_regime: Optional[str],
    last_regime: Optional[str],
    current_regime: str,
    unrealized_pnl_pct: float,
    mtf_action: str,
    side: str,
    config: Optional[dict] = None,
) -> tuple[bool, str]:
    """判定是否发出提前平仓提示。"""
    cfg = _cfg(config)
    max_pnl = float(cfg.get("max_unrealized_pnl_pct", 1.0) or 1.0)
    require_mtf_flip = bool(cfg.get("require_mtf_flip", False))

    cur = (current_regime or "").upper()
    if cur not in STRESS_REGIMES:
        return False, ""

    if unrealized_pnl_pct >= max_pnl:
        return False, f"浮盈 {unrealized_pnl_pct:.2f}% ≥ {max_pnl}% 阈值，暂不提示"

    prev = (last_regime or entry_regime or "").upper()
    transition_ok = False
    if not prev or prev in SAFE_REGIMES:
        transition_ok = True
    elif prev in STRESS_REGIMES and cfg.get("retrigger_in_stress", False):
        transition_ok = True

    if not transition_ok:
        return False, f"Regime {prev}→{cur} 非中性/趋势切入高压"

    side_u = (side or "LONG").upper()
    mtf_u = (mtf_action or "WAIT").upper()
    mtf_flip = (
        (side_u == "LONG" and mtf_u in ("SHORT", "SELL"))
        or (side_u == "SHORT" and mtf_u in ("LONG", "BUY"))
    )
    if require_mtf_flip and not mtf_flip:
        return False, "要求 MTF 反向但未满足"

    bits = [
        f"Regime {prev or '未知'}→{cur}",
        f"浮盈 {unrealized_pnl_pct:.2f}% < {max_pnl}%",
    ]
    if mtf_flip:
        bits.append(f"MTF反向={mtf_u}")
    elif mtf_u and mtf_u != "WAIT":
        bits.append(f"MTF={mtf_u}")
    return True, "；".join(bits)


def maybe_reeval_open_positions(
    paper_engine,
    open_positions: Optional[List[Dict]] = None,
) -> List[Dict[str, Any]]:
    """Watcher 钩子：对到期持仓做轻量复评，命中则入队。"""
    cfg = _cfg(getattr(paper_engine, "config", None))
    if cfg.get("enabled", True) is False:
        return []

    interval_min = float(cfg.get("interval_minutes", 15) or 15)
    min_age_min = float(cfg.get("min_age_minutes", interval_min) or interval_min)
    opens = open_positions
    if opens is None:
        opens = paper_engine.get_open_positions()
    if not opens:
        return []

    price_fn = getattr(paper_engine, "_price_provider", None)
    db_path = getattr(paper_engine, "db_path", None)
    if not db_path:
        return []

    emitted: List[Dict[str, Any]] = []
    pending_closes: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # 网络快照必须在打开写连接之前完成，避免长时间占锁
    symbols: Set[str] = {str(p.get("symbol") or "").upper() for p in opens}
    snapshots: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        if not sym:
            continue
        try:
            snapshots[sym] = fetch_light_snapshot(
                sym, config=getattr(paper_engine, "config", None)
            )
        except Exception as e:
            logger.warning("position_reeval snapshot %s: %s", sym, e)
            snapshots[sym] = {"ok": False, "error": str(e)}

    with _lock:
        conn = _connect_paper(str(db_path))
        try:
            ensure_reeval_tables(conn)
            for pos in opens:
                try:
                    pid = int(pos["id"])
                    sym = str(pos.get("symbol") or "").upper()
                    side = str(pos.get("side") or "LONG").upper()
                    opened_at = _parse_opened_at(pos.get("opened_at") or "")
                    if opened_at is None or not sym:
                        continue
                    age_min = (now - opened_at).total_seconds() / 60.0
                    if age_min < min_age_min:
                        continue

                    state = _get_state(conn, pid)
                    last_eval = state.get("last_eval_at")
                    if last_eval:
                        try:
                            last_dt = _parse_opened_at(str(last_eval)) or datetime.fromisoformat(
                                str(last_eval)
                            )
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            if (now - last_dt.astimezone(timezone.utc)).total_seconds() < interval_min * 60:
                                continue
                        except Exception:
                            pass

                    snap = snapshots.get(sym) or {}
                    if not snap.get("ok"):
                        continue
                    cur_regime = str(snap.get("regime") or "").upper()
                    mtf_action = str(snap.get("mtf_action") or "WAIT").upper()

                    price = 0.0
                    if price_fn:
                        try:
                            price = float(price_fn(sym) or 0)
                        except Exception:
                            price = 0.0
                    if price <= 0:
                        try:
                            price = float(
                                (snap.get("indicators") or {}).get("close")
                                or (snap.get("indicators") or {}).get("price")
                                or 0
                            )
                        except Exception:
                            price = 0.0
                    if price <= 0:
                        continue

                    pnl_pct = _pnl_pct(pos, price)
                    entry_regime = state.get("entry_regime") or _parse_entry_regime(pos)
                    last_regime = state.get("last_regime")

                    fire, reason = should_signal_early_close(
                        entry_regime=entry_regime,
                        last_regime=last_regime,
                        current_regime=cur_regime,
                        unrealized_pnl_pct=pnl_pct,
                        mtf_action=mtf_action,
                        side=side,
                        config=getattr(paper_engine, "config", None),
                    )

                    admit, admit_reason = should_admit_wrong_direction(
                        side=side,
                        mtf_action=mtf_action,
                        unrealized_pnl_pct=pnl_pct,
                        config=getattr(paper_engine, "config", None),
                    )
                    if admit and not fire:
                        fire, reason = True, admit_reason
                    elif admit and fire:
                        reason = f"{reason}；{admit_reason}"

                    _upsert_state(
                        conn,
                        pid,
                        last_eval_at=_now_iso(),
                        last_regime=cur_regime or (last_regime or ""),
                        entry_regime=entry_regime,
                    )

                    if not fire:
                        continue

                    signal = "ADMIT_WRONG" if admit else "EARLY_CLOSE"
                    qid = _enqueue(
                        conn,
                        position_id=pid,
                        symbol=sym,
                        side=side,
                        reason=reason,
                        regime_from=str(last_regime or entry_regime or ""),
                        regime_to=cur_regime,
                        unrealized_pnl_pct=pnl_pct,
                        mtf_action=mtf_action,
                        signal=signal,
                        payload={
                            "reason": reason,
                            "signal": signal,
                            "snapshot_at": snap.get("fetched_at"),
                            "resonance": (snap.get("multi_timeframe") or {}).get(
                                "resonance_score"
                            ),
                        },
                    )
                    # 已有 pending 时 qid 可能为 None，仍应尝试自动平仓（避免卡死）
                    pending_qid = qid
                    if pending_qid is None:
                        row = conn.execute(
                            "SELECT id FROM position_reeval_queue "
                            "WHERE position_id=? AND status='pending' "
                            "ORDER BY id DESC LIMIT 1",
                            (pid,),
                        ).fetchone()
                        if row:
                            pending_qid = int(row["id"])

                    item = {
                        "queue_id": pending_qid,
                        "position_id": pid,
                        "symbol": sym,
                        "side": side,
                        "signal": signal,
                        "reason": reason,
                        "regime_to": cur_regime,
                        "unrealized_pnl_pct": pnl_pct,
                        "mtf_action": mtf_action,
                    }
                    if qid:
                        emitted.append(item)
                        logger.warning(
                            "[持仓复评] %s #%s %s %s | %s",
                            "认错平仓" if admit else "提前平仓提示",
                            pid, side, sym, reason,
                        )
                        on_event = getattr(paper_engine, "_on_event", None)
                        if on_event:
                            try:
                                on_event("REEVAL_EARLY_CLOSE", item)
                            except Exception:
                                pass

                    # 认错默认自动平；高压 EARLY_CLOSE 默认只提示（避免波动误砍）
                    if admit:
                        auto_close = bool(
                            (cfg.get("admit_wrong") or {}).get("auto_close", True)
                        )
                    else:
                        auto_close = bool(cfg.get("auto_close", False))
                    if auto_close and price > 0:
                        pending_closes.append({
                            "pid": pid,
                            "price": float(price),
                            "reason": "ADMIT_WRONG" if admit else "REEVAL_EARLY",
                            "queue_id": pending_qid,
                        })
                except Exception as e:
                    logger.warning("position_reeval pos #%s: %s", pos.get("id"), e)

            conn.commit()
        finally:
            conn.close()

    # 必须在释放 paper_trading.db 写连接后再 close_manual，否则易自锁
    for item in pending_closes:
        pid = int(item["pid"])
        try:
            ok = paper_engine.close_manual(
                pid, float(item["price"]), reason=str(item["reason"])
            )
            if ok:
                logger.info(
                    "[持仓复评] 已自动平仓 #%s reason=%s",
                    pid, item["reason"],
                )
                qid = item.get("queue_id")
                if qid:
                    try:
                        acknowledge_reeval(str(db_path), int(qid), status="done")
                    except Exception as e:
                        logger.debug("reeval ack after close #%s: %s", pid, e)
        except Exception as e:
            logger.warning("[持仓复评] 自动平仓失败 #%s: %s", pid, e)

    return emitted


def apply_position_reeval_gate(
    advice: Dict[str, Any],
    *,
    learning_context: Optional[Dict] = None,
    config: Optional[Dict] = None,
    paper_engine=None,
) -> Dict[str, Any]:
    """门控提示：存在待处理提前平仓时写入 gate_reasons（默认不强制 WAIT）。"""
    cfg = _cfg(config)
    if cfg.get("hint_gate", True) is False:
        return advice

    eng = paper_engine
    lc = learning_context or {}
    if eng is None:
        eng = lc.get("_paper_engine") or lc.get("paper_engine")
    if eng is None:
        return advice

    db = getattr(eng, "db_path", None)
    if not db:
        return advice

    pending = list_pending_reeval(str(db), limit=20)
    if not pending:
        return advice

    out = dict(advice)
    out["position_reeval_alerts"] = pending
    reasons = list(out.get("gate_reasons") or [])
    for p in pending[:5]:
        msg = (
            f"持仓复评提示提前平仓 #{p.get('position_id')} "
            f"{p.get('side')} {p.get('symbol')}："
            f"{p.get('reason') or p.get('regime_to')}"
        )
        if msg not in reasons:
            reasons.append(msg)
    out["gate_reasons"] = reasons

    if cfg.get("block_new_on_pending_close", False):
        act = str(out.get("action") or "").upper()
        if act in ("LONG", "SHORT"):
            out["action"] = "WAIT"
            out["passed_gate"] = False
            block_msg = "持仓复评待处理提前平仓：暂停新开仓"
            if block_msg not in reasons:
                reasons.append(block_msg)
            out["gate_reasons"] = reasons
    return out
