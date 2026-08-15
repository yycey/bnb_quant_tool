"""
系统维护 — Web 远程健康检查 / 自动修复 / 优化 / 安全更新
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 热更新允许覆盖的路径（相对项目根）
UPDATE_ALLOW_PREFIXES = (
    "src/",
    "web/public/",
    "web/includes/",
    "web/scripts/",
    "gui.py",
    "main.py",
    "paper_watcher.py",
    "autopilot_daemon.py",
    "requirements.txt",
    "启动服务器.bat",
    "python_env.bat",
)

# 永不覆盖
UPDATE_PROTECTED = (
    "data/",
    "config.yaml",
    "deploy.local.php",
    ".env",
    ".git/",
)

REQUIRED_DBS = ("ai_learning.db", "paper_trading.db")
OPTIONAL_DBS = ("counterfactual.db", "pattern_memory.db")

# 学习/成长换机必需的额外状态（相对 data/）
LEARNING_EXTRA_FILES = (
    "trader_memory.db",
    "local_growth_plan.json",
    "dqn_shadow_state.json",
    "confidence_platt.json",
    "validation_log.jsonl",
    "scoreboard.json",
)

DEFAULT_LEARNING_BACKUP_DIR = "data/learning_backups"
LEARNING_BACKUP_STATE = "last_backup.json"

# data/ 只应存运行时产物；这些名字出现在 data/ 顶层 = 误拷贝的代码镜像
DATA_CODE_MIRROR_DIRS = (
    "src",
    "tests",
    "web",
    "scripts",
    "gui",
    "docs",
    "memory",
    "_db_merge_backup",
    "__pycache__",
    ".pytest_cache",
)
DATA_CODE_MIRROR_FILES = (
    "gui.py",
    "paper_watcher.py",
    "autopilot_daemon.py",
    "data_manager_gui.py",
    "discover_strategies.py",
    "requirements.txt",
    "README.md",
    "ROADMAP.md",
    "launch.sh",
    "python_env.bat",
    "config.yaml",
)

ANALYSIS_REQUIRED_COLUMNS = (
    "decision_explanation",
    "gate_reasons",
    "raw_action",
    "passed_gate",
)


class SystemMaintenance:
    """宝塔 / Web 远程维护入口。"""

    def __init__(self, project_root: Path, config: Optional[Dict] = None):
        self.project_root = Path(project_root).resolve()
        self.config = config or {}
        self.data_dir = self.project_root / "data"
        self.backups_dir = self.data_dir / "backups"
        self.updates_dir = self.data_dir / "updates"
        self.learning_backups_dir = self._resolve_learning_backup_dir()
        self._ensure_dirs()

    def _learning_backup_cfg(self) -> Dict[str, Any]:
        return dict(self.config.get("learning_backup") or {})

    def _resolve_learning_backup_dir(self) -> Path:
        cfg = self._learning_backup_cfg()
        raw = str(cfg.get("dir") or DEFAULT_LEARNING_BACKUP_DIR).strip()
        p = Path(raw)
        if not p.is_absolute():
            p = self.project_root / p
        return p.resolve()

    def _ensure_dirs(self) -> None:
        for d in (self.data_dir, self.backups_dir, self.updates_dir, self.learning_backups_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 状态 / 版本
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        version = self._read_version()
        git = self._git_info()
        return {
            "ok": True,
            "version": version,
            "git": git,
            "project_root": str(self.project_root),
            "data_dir": str(self.data_dir),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "update_config": self._update_config(),
        }

    def health_check(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []

        checks.append(self._check_path("config.yaml", self.project_root / "config.yaml", required=True))
        checks.append(self._check_path("data 目录", self.data_dir, required=True, is_dir=True))

        for name in REQUIRED_DBS:
            checks.append(self._check_path(name, self.data_dir / name, required=True))

        for name in OPTIONAL_DBS:
            checks.append(self._check_path(name, self.data_dir / name, required=False))

        checks.append(self._check_db_columns())
        checks.append(self._check_python_imports())
        checks.append(self._check_web_files())
        checks.append(self._check_orphan_dbs())
        checks.append(self._check_data_pollution())
        checks.append(self._check_watcher_heartbeat())

        failed = [c for c in checks if not c.get("ok")]
        warn = [c for c in checks if c.get("ok") and c.get("level") == "warn"]

        return {
            "ok": len(failed) == 0,
            "checks": checks,
            "summary": {
                "total": len(checks),
                "failed": len(failed),
                "warnings": len(warn),
            },
            "fixable": any(c.get("fixable") for c in failed + warn),
        }

    def auto_fix(self) -> Dict[str, Any]:
        actions: List[str] = []

        mig = self._migrate_root_dbs()
        actions.extend(mig)

        purged = self._purge_data_code_mirror()
        actions.extend(purged)

        try:
            from bnb_quant_tool.sqlite_recovery import ensure_sqlite_db_healthy

            for db_name in REQUIRED_DBS:
                path = self.data_dir / db_name
                label = db_name.replace(".db", "")
                result = ensure_sqlite_db_healthy(str(path), label=label)
                if result.get("action") in ("recovered", "recreated"):
                    actions.append(f"{db_name}: {result.get('message', result.get('action'))}")
        except Exception as e:
            actions.append(f"数据库健康检查跳过: {e}")

        try:
            from bnb_quant_tool.ai_learning_system import AILearningSystem

            AILearningSystem()
            actions.append("AI 学习库 schema 已同步（含 decision_explanation 等列）")
        except Exception as e:
            return {"ok": False, "error": f"AI 学习库修复失败: {e}", "actions": actions}

        try:
            from bnb_quant_tool.data_localization import DataLocalizationManager

            mgr = DataLocalizationManager(str(self.project_root))
            results = mgr.migrate_from_old_locations()
            migrated = [k for k, v in results.items() if v is True]
            if migrated:
                actions.append(f"数据本地化迁移: {', '.join(migrated)}")
        except Exception as e:
            actions.append(f"数据迁移跳过: {e}")

        self._ensure_dirs()
        actions.append("目录结构已校验")

        health = self.health_check()
        return {
            "ok": health["ok"],
            "actions": actions,
            "health": health,
        }

    def optimize(self) -> Dict[str, Any]:
        actions: List[str] = []
        hb = self.data_dir / "watcher.heartbeat"
        if hb.is_file():
            try:
                age = max(0.0, time.time() - hb.stat().st_mtime)
                if age <= 120:
                    return {
                        "ok": False,
                        "error": (
                            f"模拟盘监控运行中（心跳 {age:.0f}s 前），"
                            "请先停止监控再 VACUUM，以免锁库导致止盈/平仓失败"
                        ),
                        "actions": [],
                        "blocked_by_watcher": True,
                    }
            except OSError:
                pass

        for db_name in list(REQUIRED_DBS) + list(OPTIONAL_DBS):
            path = self.data_dir / db_name
            if not path.is_file():
                continue
            try:
                conn = sqlite3.connect(str(path), timeout=30)
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.close()
                size_mb = path.stat().st_size / (1024 * 1024)
                actions.append(f"{db_name} 已优化 ({size_mb:.2f} MB)")
            except Exception as e:
                actions.append(f"{db_name} 优化失败: {e}")

        return {"ok": True, "actions": actions}

    def _collect_backup_files(self, *, include_learning_extras: bool) -> List[Tuple[Path, str]]:
        files_to_pack: List[Tuple[Path, str]] = []
        cfg = self.project_root / "config.yaml"
        if cfg.is_file():
            files_to_pack.append((cfg, "config.yaml"))

        for db_name in list(REQUIRED_DBS) + list(OPTIONAL_DBS):
            p = self.data_dir / db_name
            if p.is_file():
                files_to_pack.append((p, f"data/{db_name}"))

        if include_learning_extras:
            for name in LEARNING_EXTRA_FILES:
                p = self.data_dir / name
                if p.is_file():
                    files_to_pack.append((p, f"data/{name}"))

        deploy = self.project_root / "web" / "deploy.local.php"
        if deploy.is_file():
            files_to_pack.append((deploy, "web/deploy.local.php"))

        return files_to_pack

    def backup(
        self,
        label: str = "web",
        *,
        dest_dir: Optional[Path] = None,
        include_learning_extras: bool = False,
    ) -> Dict[str, Any]:
        out_dir = Path(dest_dir) if dest_dir is not None else self.backups_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{label}_{ts}"
        zip_path = out_dir / f"{name}.zip"

        files_to_pack = self._collect_backup_files(
            include_learning_extras=include_learning_extras
        )
        if not files_to_pack:
            return {"ok": False, "error": "没有可备份的文件"}

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            meta = {
                "name": name,
                "label": label,
                "created_at": datetime.now().isoformat(),
                "project_root": str(self.project_root),
                "include_learning_extras": include_learning_extras,
                "files": [arc for _, arc in files_to_pack],
            }
            zf.writestr("metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
            for src, arc in files_to_pack:
                zf.write(src, arc)

        return {
            "ok": True,
            "backup_path": str(zip_path),
            "backup_name": zip_path.name,
            "backup_dir": str(out_dir),
            "size_bytes": zip_path.stat().st_size,
            "files": [arc for _, arc in files_to_pack],
        }

    def _learning_backup_state_path(self) -> Path:
        return self.learning_backups_dir / LEARNING_BACKUP_STATE

    def _read_learning_backup_state(self) -> Dict[str, Any]:
        path = self._learning_backup_state_path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_learning_backup_state(self, state: Dict[str, Any]) -> None:
        self.learning_backups_dir.mkdir(parents=True, exist_ok=True)
        self._learning_backup_state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _prune_learning_backups(self, keep_count: int) -> List[str]:
        if keep_count <= 0:
            return []
        zips = sorted(
            self.learning_backups_dir.glob("backup_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed: List[str] = []
        for old in zips[keep_count:]:
            try:
                old.unlink()
                removed.append(old.name)
            except Exception as e:
                logger.warning("prune learning backup failed %s: %s", old, e)
        return removed

    def maybe_scheduled_learning_backup(
        self,
        *,
        force: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """每 N 天自动备份学习状态到独立目录（默认 data/learning_backups）。"""
        cfg = self._learning_backup_cfg()
        if not bool(cfg.get("enabled", True)) and not force:
            return {"ok": True, "skipped": True, "reason": "disabled"}

        interval_days = float(cfg.get("interval_days", 2))
        if interval_days <= 0:
            interval_days = 2.0
        keep_count = int(cfg.get("keep_count", 15))

        self.learning_backups_dir = self._resolve_learning_backup_dir()
        self.learning_backups_dir.mkdir(parents=True, exist_ok=True)

        now_dt = now or datetime.now()
        state = self._read_learning_backup_state()
        last_iso = state.get("last_backup_at")
        due = True
        hours_since: Optional[float] = None
        if last_iso and not force:
            try:
                last_dt = datetime.fromisoformat(str(last_iso))
                hours_since = (now_dt - last_dt).total_seconds() / 3600.0
                due = hours_since >= interval_days * 24.0
            except Exception:
                due = True

        if not due:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_due",
                "interval_days": interval_days,
                "hours_since_last": round(hours_since or 0.0, 2),
                "backup_dir": str(self.learning_backups_dir),
            }

        result = self.backup(
            label="learning_auto",
            dest_dir=self.learning_backups_dir,
            include_learning_extras=True,
        )
        if not result.get("ok"):
            return result

        pruned = self._prune_learning_backups(keep_count)
        new_state = {
            "last_backup_at": now_dt.isoformat(timespec="seconds"),
            "last_backup_name": result.get("backup_name"),
            "last_backup_path": result.get("backup_path"),
            "interval_days": interval_days,
            "files": result.get("files") or [],
        }
        self._write_learning_backup_state(new_state)

        result.update(
            {
                "skipped": False,
                "scheduled": True,
                "interval_days": interval_days,
                "pruned": pruned,
                "state": new_state,
            }
        )
        return result

    def git_pull(self) -> Dict[str, Any]:
        upd = self._update_config()
        if not upd.get("allow_git_pull"):
            return {"ok": False, "error": "config.yaml 中 web.update.allow_git_pull 未启用"}

        git_dir = self.project_root / ".git"
        if not git_dir.is_dir():
            return {"ok": False, "error": "项目根目录不是 Git 仓库"}

        branch = upd.get("git_branch") or "main"
        try:
            fetch = subprocess.run(
                ["git", "fetch", "origin", branch],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if fetch.returncode != 0:
                return {"ok": False, "error": fetch.stderr.strip() or fetch.stdout.strip()}

            pull = subprocess.run(
                ["git", "pull", "origin", branch],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if pull.returncode != 0:
                return {"ok": False, "error": pull.stderr.strip() or pull.stdout.strip()}

            return {
                "ok": True,
                "branch": branch,
                "output": (pull.stdout or "").strip(),
                "git": self._git_info(),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Git 操作超时"}
        except FileNotFoundError:
            return {"ok": False, "error": "未找到 git 命令"}

    def apply_update_zip(self, zip_path: Path, *, backup_first: bool = True) -> Dict[str, Any]:
        zip_path = Path(zip_path).resolve()
        if not zip_path.is_file() or not zipfile.is_zipfile(zip_path):
            return {"ok": False, "error": "无效的 zip 文件"}

        backup_info = None
        if backup_first:
            backup_info = self.backup(label="pre_update")
            if not backup_info.get("ok"):
                return {"ok": False, "error": "更新前备份失败", "backup": backup_info}

        code_backup = self._backup_code_snapshot()
        updated: List[str] = []
        skipped: List[str] = []
        blocked: List[str] = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                norm = name.replace("\\", "/").lstrip("./")
                if self._is_protected(norm):
                    blocked.append(norm)
                    continue
                if not self._is_allowed_update(norm):
                    skipped.append(norm)
                    continue

                target = self.project_root / norm
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                updated.append(norm)

        fix_result = self.auto_fix()

        return {
            "ok": True,
            "updated": updated,
            "skipped": skipped[:20],
            "blocked": blocked[:20],
            "backup": backup_info,
            "code_snapshot": code_backup,
            "post_fix": fix_result,
        }

    def list_backups(self, limit: int = 10) -> Dict[str, Any]:
        items: List[Dict[str, Any]] = []
        if self.backups_dir.is_dir():
            for p in sorted(self.backups_dir.glob("*.zip"), reverse=True)[:limit]:
                items.append({
                    "name": p.name,
                    "path": str(p),
                    "size_bytes": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                })
        return {"ok": True, "backups": items}

    # ------------------------------------------------------------------
    # 内部检查
    # ------------------------------------------------------------------
    def _check_path(self, label: str, path: Path, *, required: bool, is_dir: bool = False) -> Dict[str, Any]:
        exists = path.is_dir() if is_dir else path.is_file()
        ok = exists or not required
        level = "error" if required and not exists else ("warn" if not required and not exists else "ok")
        return {
            "id": label,
            "ok": ok,
            "level": level,
            "message": f"{label} {'存在' if exists else '缺失'}: {path}",
            "fixable": required and not exists and label == "data 目录",
        }

    def _check_db_columns(self) -> Dict[str, Any]:
        db_path = self.data_dir / "ai_learning.db"
        if not db_path.is_file():
            return {"id": "db_schema", "ok": False, "level": "error", "message": "ai_learning.db 不存在", "fixable": False}

        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(analysis_records)")
            cols = {row[1] for row in cur.fetchall()}
            conn.close()
            missing = [c for c in ANALYSIS_REQUIRED_COLUMNS if c not in cols]
            if missing:
                return {
                    "id": "db_schema",
                    "ok": False,
                    "level": "warn",
                    "message": f"analysis_records 缺少列: {', '.join(missing)}",
                    "fixable": True,
                }
            return {"id": "db_schema", "ok": True, "level": "ok", "message": "数据库 schema 正常", "fixable": False}
        except Exception as e:
            return {"id": "db_schema", "ok": False, "level": "error", "message": str(e), "fixable": True}

    def _check_python_imports(self) -> Dict[str, Any]:
        modules = [
            "bnb_quant_tool.ai_learning_system",
            "bnb_quant_tool.trade_advisor",
            "bnb_quant_tool.paper_trading",
            "bnb_quant_tool.data_fetcher",
        ]
        failed = []
        for mod in modules:
            try:
                __import__(mod)
            except Exception as e:
                failed.append(f"{mod}: {e}")
        if failed:
            return {
                "id": "python_imports",
                "ok": False,
                "level": "error",
                "message": "; ".join(failed[:3]),
                "fixable": False,
            }
        return {"id": "python_imports", "ok": True, "level": "ok", "message": "核心模块导入正常", "fixable": False}

    def _check_web_files(self) -> Dict[str, Any]:
        required = [
            "web/public/index.html",
            "web/public/api/index.php",
            "web/includes/bootstrap.php",
        ]
        missing = [r for r in required if not (self.project_root / r).is_file()]
        if missing:
            return {
                "id": "web_files",
                "ok": False,
                "level": "error",
                "message": f"Web 文件缺失: {', '.join(missing)}",
                "fixable": False,
            }
        return {"id": "web_files", "ok": True, "level": "ok", "message": "Web 控制台文件完整", "fixable": False}

    def _check_orphan_dbs(self) -> Dict[str, Any]:
        orphans = []
        for name in REQUIRED_DBS + OPTIONAL_DBS:
            root_db = self.project_root / name
            data_db = self.data_dir / name
            if root_db.is_file() and data_db.is_file():
                orphans.append(name)
        if orphans:
            return {
                "id": "orphan_dbs",
                "ok": False,
                "level": "warn",
                "message": f"根目录存在重复数据库（应只在 data/）: {', '.join(orphans)}",
                "fixable": True,
            }
        return {"id": "orphan_dbs", "ok": True, "level": "ok", "message": "数据库路径正常", "fixable": False}

    def _list_data_pollution(self) -> List[str]:
        hits: List[str] = []
        if not self.data_dir.is_dir():
            return hits
        for name in DATA_CODE_MIRROR_DIRS:
            if (self.data_dir / name).exists():
                hits.append(f"{name}/")
        for name in DATA_CODE_MIRROR_FILES:
            if (self.data_dir / name).is_file():
                hits.append(name)
        for p in self.data_dir.glob("config.yaml.bak.*"):
            hits.append(p.name)
        for p in self.data_dir.glob("*.bak_*"):
            hits.append(p.name)
        for p in self.data_dir.glob("*.backup_*"):
            hits.append(p.name)
        for p in self.data_dir.glob("_diag_*"):
            hits.append(p.name)
        for p in self.data_dir.glob("_tmp_*"):
            hits.append(p.name)
        return sorted(set(hits))

    def _check_data_pollution(self) -> Dict[str, Any]:
        hits = self._list_data_pollution()
        if hits:
            preview = ", ".join(hits[:8])
            more = f" 等{len(hits)}项" if len(hits) > 8 else ""
            return {
                "id": "data_pollution",
                "ok": False,
                "level": "warn",
                "message": f"data/ 混入代码或备份镜像: {preview}{more}",
                "fixable": True,
            }
        return {
            "id": "data_pollution",
            "ok": True,
            "level": "ok",
            "message": "data/ 目录干净（无代码镜像）",
            "fixable": False,
        }

    def _purge_data_code_mirror(self) -> List[str]:
        """清除 data/ 下误拷贝的源码/文档/备份，保留 DB/缓存/模型。"""
        actions: List[str] = []
        if not self.data_dir.is_dir():
            return actions
        for name in DATA_CODE_MIRROR_DIRS:
            path = self.data_dir / name
            if path.is_dir():
                try:
                    shutil.rmtree(path)
                    actions.append(f"已清除 data/{name}/")
                except Exception as e:
                    actions.append(f"清除 data/{name}/ 失败: {e}")
        for name in DATA_CODE_MIRROR_FILES:
            path = self.data_dir / name
            if path.is_file():
                try:
                    path.unlink()
                    actions.append(f"已清除 data/{name}")
                except Exception as e:
                    actions.append(f"清除 data/{name} 失败: {e}")
        for pattern in (
            "config.yaml.bak.*",
            "*.bak_*",
            "*.backup_*",
            "_diag_*",
            "_tmp_*",
            "*.log",
        ):
            for path in self.data_dir.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    actions.append(f"已清除 data/{path.name}")
                except Exception as e:
                    actions.append(f"清除 data/{path.name} 失败: {e}")
        return actions

    def _check_watcher_heartbeat(self) -> Dict[str, Any]:
        hb = self.data_dir / "watcher.heartbeat"
        if not hb.is_file():
            return {
                "id": "watcher",
                "ok": False,
                "level": "warn",
                "message": "监控未运行（无 heartbeat 文件）",
                "fixable": False,
            }
        age = datetime.now().timestamp() - hb.stat().st_mtime
        if age > 120:
            return {
                "id": "watcher",
                "ok": False,
                "level": "warn",
                "message": f"监控心跳过期 ({int(age)}s 前)",
                "fixable": False,
            }
        return {"id": "watcher", "ok": True, "level": "ok", "message": "模拟盘监控心跳正常", "fixable": False}

    def _migrate_root_dbs(self) -> List[str]:
        actions: List[str] = []
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_DBS + OPTIONAL_DBS:
            src = self.project_root / name
            dst = self.data_dir / name
            if src.is_file() and not dst.is_file():
                shutil.copy2(src, dst)
                actions.append(f"已迁移 {name} → data/")
        return actions

    def _backup_code_snapshot(self) -> Optional[Dict[str, Any]]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_dir = self.backups_dir / f"code_snapshot_{ts}"
        try:
            snap_dir.mkdir(parents=True, exist_ok=True)
            for prefix in ("src", "web"):
                src = self.project_root / prefix
                if src.is_dir():
                    shutil.copytree(src, snap_dir / prefix, dirs_exist_ok=True)
            for py in self.project_root.glob("*.py"):
                shutil.copy2(py, snap_dir / py.name)
            return {"ok": True, "path": str(snap_dir)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _read_version(self) -> str:
        try:
            from bnb_quant_tool import __version__

            return str(__version__)
        except Exception:
            return "unknown"

    def _git_info(self) -> Dict[str, Any]:
        git_dir = self.project_root / ".git"
        if not git_dir.is_dir():
            return {"available": False}
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return {
                "available": True,
                "branch": (branch.stdout or "").strip(),
                "commit": (commit.stdout or "").strip(),
            }
        except Exception:
            return {"available": False}

    def _update_config(self) -> Dict[str, Any]:
        web = self.config.get("web") or {}
        upd = web.get("update") or {}
        return {
            "enabled": bool(upd.get("enabled", True)),
            "allow_git_pull": bool(upd.get("allow_git_pull", False)),
            "git_branch": str(upd.get("git_branch") or "main"),
        }

    @staticmethod
    def _is_protected(path: str) -> bool:
        norm = path.replace("\\", "/")
        for p in UPDATE_PROTECTED:
            if norm == p.rstrip("/") or norm.startswith(p):
                return True
        if norm.endswith("deploy.local.php"):
            return True
        return False

    @staticmethod
    def _is_allowed_update(path: str) -> bool:
        norm = path.replace("\\", "/")
        for prefix in UPDATE_ALLOW_PREFIXES:
            if norm == prefix.rstrip("/") or norm.startswith(prefix):
                return True
        return False
