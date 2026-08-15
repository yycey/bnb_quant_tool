"""Server / headless 进程运行时：单实例锁 + 文件日志。"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class ProcessLock:
    """跨平台文件锁，防止双 watcher / 双 autopilot 同时写同一 DB。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                if self._fh.read(1) == "":
                    self._fh.write("0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()}\n")
            self._fh.flush()
            self._acquired = True
            atexit.register(self.release)
            return True
        except (OSError, BlockingIOError):
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            self._acquired = False

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"another instance holds lock: {self.path}")
        return self

    def __exit__(self, *args) -> None:
        self.release()


def ensure_singleton(lock_path: Path, *, logger: Optional[logging.Logger] = None) -> ProcessLock:
    """获取单实例锁；失败则打日志并 sys.exit(1)。"""
    lock = ProcessLock(lock_path)
    if lock.acquire():
        log = logger or logging.getLogger(__name__)
        log.info("单实例锁已获取: %s", lock_path)
        return lock
    log = logger or logging.getLogger(__name__)
    log.error("已有实例在运行（锁文件 %s）。退出以免双写 DB。", lock_path)
    sys.exit(1)


def setup_logging(
    name: str,
    *,
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> Path:
    """配置 stdout + 滚动文件日志。返回日志文件路径（若启用文件）。"""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    log_path = Path()
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"
        fh = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    return log_path


def load_project_dotenv(project_root: Path) -> bool:
    """从项目根加载 .env（若存在）。返回是否成功加载。"""
    env_path = Path(project_root) / ".env"
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return True
    except Exception:
        return False
