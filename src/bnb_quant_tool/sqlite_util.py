"""SQLite 并发辅助：统一 PRAGMA、locked 重试、半开事务复位。

与 paper_trading.PaperTradingEngine._run_db 对齐，供学习/扫描/记忆等
共享 paper_trading.db / ai_learning.db 的模块复用，避免止盈平仓被锁阻塞。
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from functools import wraps
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_CONNECT_TIMEOUT = 60.0
DEFAULT_BUSY_TIMEOUT_MS = 30000


def is_db_locked(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def apply_writer_pragmas(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    autocommit: bool = True,
) -> sqlite3.Connection:
    """WAL + 长 busy_timeout；可选关闭隐式事务（配合 BEGIN IMMEDIATE）。"""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        pass
    if autocommit:
        try:
            conn.isolation_level = None
        except Exception:
            pass
    return conn


def connect_writer(
    db_path: str,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    row_factory: bool = False,
    autocommit: bool = True,
) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=float(timeout))
    if row_factory:
        conn.row_factory = sqlite3.Row
    return apply_writer_pragmas(
        conn, busy_timeout_ms=busy_timeout_ms, autocommit=autocommit
    )


def safe_rollback(conn: Optional[sqlite3.Connection]) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass


def run_db(
    op: Callable[[], T],
    *,
    label: str = "db",
    max_retries: int = 8,
    on_locked: Optional[Callable[[], None]] = None,
) -> T:
    """遇 database is locked 退避重试；每次失败调用 on_locked（通常 rollback+重置连接）。"""
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            return op()
        except sqlite3.OperationalError as e:
            last_exc = e
            if on_locked is not None:
                try:
                    on_locked()
                except Exception:
                    pass
            if not is_db_locked(e):
                raise
            delay = min(2.5, 0.08 * (2 ** attempt)) + random.uniform(0.0, 0.06)
            logger.warning(
                "[SQLite] %s database locked (尝试 %d/%d)，%.2fs 后重试",
                label,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def retry_db(
    max_retries: int = 6,
    base_delay: float = 0.15,
    *,
    reset_attr: str = "_local",
    conn_attr: str = "conn",
):
    """方法装饰器：locked 时 rollback + 清空线程本地连接后重试。"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            self = args[0] if args else None

            def _reset():
                if self is None:
                    return
                local = getattr(self, reset_attr, None)
                if local is None:
                    return
                conn = getattr(local, conn_attr, None)
                safe_rollback(conn)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                try:
                    setattr(local, conn_attr, None)
                except Exception:
                    pass
                for extra in ("paper_conn", "learning_conn"):
                    extra_conn = getattr(local, extra, None)
                    if extra_conn is None:
                        continue
                    safe_rollback(extra_conn)
                    try:
                        extra_conn.close()
                    except Exception:
                        pass
                    try:
                        setattr(local, extra, None)
                    except Exception:
                        pass

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    _reset()
                    if not is_db_locked(e):
                        raise
                    if attempt >= max_retries - 1:
                        logger.error(
                            "[SQLite] %s locked 重试%d次后仍失败",
                            getattr(func, "__name__", "op"),
                            max_retries,
                        )
                        raise
                    delay = min(2.5, base_delay * (2 ** attempt)) + random.uniform(0, 0.05)
                    logger.warning(
                        "[SQLite] %s locked (尝试 %d/%d)，%.2fs 后重试",
                        getattr(func, "__name__", "op"),
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
            return None

        return wrapper

    return decorator


def begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
