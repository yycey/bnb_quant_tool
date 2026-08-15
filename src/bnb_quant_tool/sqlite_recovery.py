"""SQLite 健康检查与损坏库自动恢复。"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import tempfile
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def close_sqlite_connections_for_path(db_path: str) -> int:
    """关闭本进程内 DbArtifactStore 等对该库的持有连接。"""
    key = str(Path(db_path).resolve())
    closed = 0
    try:
        from bnb_quant_tool.db_artifact_store import DbArtifactStore

        closed += DbArtifactStore.close_all_for_path(key)
    except Exception:
        pass
    return closed


def _related_db_files(db_path: Path) -> List[Path]:
    files = [db_path]
    for suffix in ("-wal", "-shm", "-journal"):
        p = Path(f"{db_path}{suffix}")
        if p.is_file():
            files.append(p)
    return files


def check_sqlite_health(db_path: str) -> Tuple[bool, str]:
    """打开数据库并执行 quick_check，返回 (是否健康, 说明)。"""
    path = Path(db_path)
    if not path.is_file():
        return True, "file_missing"

    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute("PRAGMA quick_check").fetchone()
        status = (row[0] if row else "unknown") if row else "unknown"
        if status == "ok":
            return True, "ok"
        return False, f"quick_check:{status}"
    except sqlite3.DatabaseError as e:
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def backup_db_files(db_path: str, label: str = "sqlite") -> Optional[Path]:
    """将数据库及其 WAL/SHM 备份到 data/backups/corrupt_时间戳/。"""
    src = Path(db_path)
    if not src.is_file() and not Path(f"{src}-wal").is_file():
        return None

    backup_root = src.parent / "backups" / f"corrupt_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    for f in _related_db_files(src):
        if f.is_file():
            shutil.copy2(f, backup_root / f.name)
            copied += 1

    if copied == 0:
        return None
    logger.warning("已备份损坏数据库到 %s (%d 个文件)", backup_root, copied)
    return backup_root


def _try_iterdump_recover(src: Path, dst: Path) -> bool:
    """尝试用 iterdump 从损坏库导出可读数据到新库。"""
    if dst.is_file():
        dst.unlink()

    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(str(src), timeout=10)
        dst_conn = sqlite3.connect(str(dst), timeout=30)
        for line in src_conn.iterdump():
            try:
                dst_conn.executescript(line)
            except sqlite3.DatabaseError:
                continue
        dst_conn.commit()
        row = dst_conn.execute("PRAGMA quick_check").fetchone()
        return bool(row and row[0] == "ok")
    except Exception as e:
        logger.debug("iterdump recover failed: %s", e)
        if dst.is_file():
            try:
                dst.unlink()
            except OSError:
                pass
        return False
    finally:
        for c in (src_conn, dst_conn):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


def _try_cli_recover(src: Path, dst: Path) -> bool:
    """使用 sqlite3 命令行 .recover（若系统已安装）。"""
    sqlite3_bin = shutil.which("sqlite3")
    if not sqlite3_bin or not src.is_file():
        return False
    if dst.is_file():
        dst.unlink()

    try:
        proc = subprocess.run(
            [sqlite3_bin, str(src), ".recover"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False

        dst_conn = sqlite3.connect(str(dst), timeout=30)
        dst_conn.executescript(proc.stdout)
        dst_conn.commit()
        row = dst_conn.execute("PRAGMA quick_check").fetchone()
        dst_conn.close()
        return bool(row and row[0] == "ok")
    except Exception as e:
        logger.debug("cli recover failed: %s", e)
        if dst.is_file():
            try:
                dst.unlink()
            except OSError:
                pass
        return False


def _quarantine_db_files(db_path: Path) -> Tuple[bool, Optional[Path]]:
    """
    将损坏库移入 backups/quarantine_*，释放原路径供重建。
    删除失败时尝试 rename，避免 Windows 文件占用导致恢复假成功。
    """
    close_sqlite_connections_for_path(str(db_path))
    time.sleep(0.15)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine_dir = db_path.parent / "backups" / f"quarantine_{db_path.stem}_{ts}"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    all_clear = True
    for f in _related_db_files(db_path):
        if not f.is_file():
            continue
        moved = False
        for attempt in range(4):
            try:
                shutil.move(str(f), str(quarantine_dir / f.name))
                moved = True
                break
            except OSError:
                close_sqlite_connections_for_path(str(db_path))
                time.sleep(0.25 * (attempt + 1))
        if moved:
            continue
        try:
            alt = f.with_name(f"{f.name}.corrupt.{ts}")
            f.rename(alt)
            shutil.move(str(alt), str(quarantine_dir / f.name))
            moved = True
        except OSError as e:
            logger.warning("无法隔离 %s: %s", f, e)
            all_clear = False

    if db_path.is_file():
        healthy, _ = check_sqlite_health(str(db_path))
        if not healthy:
            all_clear = False

    if not any(quarantine_dir.iterdir()) and quarantine_dir.is_dir():
        try:
            quarantine_dir.rmdir()
        except OSError:
            pass
        return all_clear, None
    return all_clear, quarantine_dir


def _swap_recovered_db(original: Path, recovered: Path) -> bool:
    ok, _ = _quarantine_db_files(original)
    if not ok and original.is_file():
        return False
    shutil.move(str(recovered), str(original))
    healthy, _ = check_sqlite_health(str(original))
    return healthy


def recover_sqlite_db(db_path: str, label: str = "sqlite") -> Dict[str, Any]:
    """
    尝试修复损坏的 SQLite 库。
    返回: {ok, action, backup, message}
    action: none | recovered | recreated | failed
    """
    path = Path(db_path)
    healthy, reason = check_sqlite_health(str(path))
    if healthy:
        return {"ok": True, "action": "none", "backup": None, "message": reason}

    backup_dir = backup_db_files(str(path), label=label)
    recovered_path: Optional[Path] = None
    method: Optional[str] = None

    with tempfile.TemporaryDirectory(prefix="sqlite_recover_") as tmp:
        tmp_recovered = Path(tmp) / f"{path.stem}_recovered.db"

        if path.is_file() and _try_iterdump_recover(path, tmp_recovered):
            recovered_path = tmp_recovered
            method = "iterdump"
        elif path.is_file() and _try_cli_recover(path, tmp_recovered):
            recovered_path = tmp_recovered
            method = "cli_recover"

        if recovered_path is not None and recovered_path.is_file():
            final_recovered = path.parent / f".{path.stem}.recovering.db"
            if final_recovered.is_file():
                try:
                    final_recovered.unlink()
                except OSError:
                    pass
            shutil.copy2(recovered_path, final_recovered)
            if _swap_recovered_db(path, final_recovered):
                message = f"数据库已从备份恢复 ({method})，旧文件在 {backup_dir}"
                logger.warning(message)
                return {
                    "ok": True,
                    "action": "recovered",
                    "backup": str(backup_dir) if backup_dir else None,
                    "message": message,
                }
            if final_recovered.is_file():
                try:
                    final_recovered.unlink()
                except OSError:
                    pass

    quarantined, quarantine_dir = _quarantine_db_files(path)
    healthy_after, health_msg = check_sqlite_health(str(path))

    if quarantined and healthy_after:
        message = (
            f"数据库已损坏，已隔离旧文件到 {quarantine_dir or backup_dir}，将重建空库。"
            "历史学习记录可能丢失，可从 data/backups 或「大脑导入」恢复。"
        )
        logger.error(message)
        return {
            "ok": True,
            "action": "recreated",
            "backup": str(backup_dir) if backup_dir else str(quarantine_dir) if quarantine_dir else None,
            "message": message,
        }

    message = (
        f"数据库损坏且无法自动修复（{health_msg}）。"
        f"已备份到 {backup_dir}。"
        "请关闭所有占用该程序的实例后重试，或手动将 data/ai_learning.db* 移走后重启。"
    )
    logger.error(message)
    return {
        "ok": False,
        "action": "failed",
        "backup": str(backup_dir) if backup_dir else None,
        "message": message,
    }


def ensure_sqlite_db_healthy(db_path: str, label: str = "sqlite") -> Dict[str, Any]:
    """启动前确保数据库可打开；损坏时自动备份并尝试恢复。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return recover_sqlite_db(str(path), label=label)


def repair_workspace_databases(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    """在工作空间迁移/打开数据库之前，先修复所有核心库。"""
    labels = {
        "ai_learning.db": "ai_learning",
        "paper_trading.db": "paper_trading",
        "counterfactual.db": "counterfactual",
        "pattern_memory.db": "pattern_memory",
    }
    results: Dict[str, Dict[str, Any]] = {}
    for filename, label in labels.items():
        db_path = data_dir / filename
        if db_path.is_file() or Path(f"{db_path}-wal").is_file():
            info = ensure_sqlite_db_healthy(str(db_path), label=label)
            results[filename] = info
            if not info.get("ok"):
                logger.error("数据库 %s 修复失败: %s", filename, info.get("message"))
            elif info.get("action") not in (None, "none"):
                logger.warning("数据库 %s: %s", filename, info.get("message"))
    return results
