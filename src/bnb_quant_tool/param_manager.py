# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - 参数管理器 v3.0
自动应用AI复盘建议 + 版本回滚能力
作者: Python全栈工程师
日期: 2026-06-03
"""

import json
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging
import yaml

logger = logging.getLogger(__name__)


class ParamManager:
    """
    参数管理器 - 负责参数的版本控制、自动应用和回滚
    
    核心功能:
    1. 记录每次参数变更的历史版本
    2. 自动应用AI建议的参数调整
    3. 支持一键回滚到任意历史版本
    4. 性能对比：变更前后的胜率/PnL对比
    """
    
    MAX_VERSIONS = 10  # 最多保留10个历史版本
    
    @staticmethod
    def resolve_config_path(config_path: Optional[str] = None) -> Path:
        """始终解析到项目根 config.yaml，避免 cwd=data/ 写错文件。"""
        if config_path:
            p = Path(config_path)
            if p.is_absolute():
                return p
            # 相对路径：优先相对项目根（src/bnb_quant_tool/../../）
            root = Path(__file__).resolve().parent.parent.parent
            cand = root / p
            if cand.exists() or str(p) in ("config.yaml", "./config.yaml"):
                return cand
            return Path.cwd() / p
        try:
            from bnb_quant_tool.data_localization import get_localization_manager
            root = Path(get_localization_manager().workspace)
            return root / "config.yaml"
        except Exception:
            return Path(__file__).resolve().parent.parent.parent / "config.yaml"

    def __init__(self, config_path: str = "config.yaml", 
                 learning_db_path: str = None):
        self.config_path = self.resolve_config_path(config_path)
        if learning_db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.learning_db_path = Path(str(get_localized_db_path('ai_learning')))
            except ImportError:
                self.learning_db_path = Path("ai_learning.db")
        else:
            self.learning_db_path = Path(learning_db_path)
        self.backup_dir = self.config_path.parent / "config_backups"
        self.backup_dir.mkdir(exist_ok=True)
        
        # 参数路径映射 (AI输出参数名 -> config.yaml路径)
        self.param_paths = {
            # trading 层级
            "risk_per_trade": ("trading", "risk_per_trade"),
            "confidence_threshold": ("trading", "confidence_threshold"),
            "account_balance": ("trading", "account_balance"),
            
            # risk_management 层级
            "max_position_pct": ("risk_management", "max_position_pct"),
            "max_risk_per_trade": ("risk_management", "max_risk_per_trade"),
            "min_risk_reward_ratio": ("risk_management", "min_risk_reward_ratio"),
            
            # trade_advisor 层级
            "atr_sl_mult": ("trade_advisor", "atr_sl_mult"),
            "atr_tp1_mult": ("trade_advisor", "atr_tp1_mult"),
            "atr_tp2_mult": ("trade_advisor", "atr_tp2_mult"),
            "atr_tp3_mult": ("trade_advisor", "atr_tp3_mult"),
            "news_filter_threshold": ("trade_advisor", "news_filter_threshold"),
        }
        
        logger.info(f"ParamManager initialized, backup_dir={self.backup_dir}")
        self._local = threading.local()

    def _get_conn(self):
        """线程本地连接（WAL模式，避免 database is locked）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(str(self.learning_db_path), timeout=60.0)
        return self._local.conn

    def load_config(self) -> Dict:
        """加载当前配置"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {}
    
    def save_config(self, config: Dict) -> bool:
        """保存配置"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            logger.info("配置已保存")
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False
    
    def get_param_value(self, param_name: str) -> Optional[float]:
        """获取参数当前值"""
        if param_name not in self.param_paths:
            return None
        
        config = self.load_config()
        path = self.param_paths[param_name]
        
        # 逐层访问
        value = config
        for key in path:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        
        return float(value) if value is not None else None
    
    def set_param_value(self, param_name: str, new_value: float) -> Tuple[bool, str]:
        """设置参数新值"""
        if param_name not in self.param_paths:
            return False, f"未知参数: {param_name}"
        
        config = self.load_config()
        path = self.param_paths[param_name]
        
        # 逐层导航到目标位置
        target = config
        for key in path[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        
        old_value = target.get(path[-1])
        target[path[-1]] = new_value
        
        # 保存
        if self.save_config(config):
            return True, f"{param_name}: {old_value} → {new_value}"
        else:
            return False, "保存失败"
    
    def create_version_backup(self, reason: str = "auto") -> Optional[str]:
        """
        创建当前配置的版本备份
        
        Returns:
            版本ID (时间戳字符串)
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_id = f"v{timestamp}"
        
        # 复制当前配置
        backup_path = self.backup_dir / f"config_{version_id}.yaml"
        
        try:
            shutil.copy2(self.config_path, backup_path)
            
            # 记录到数据库
            self._record_version(version_id, reason, backup_path)
            
            # 清理旧版本
            self._cleanup_old_versions()
            
            logger.info(f"创建版本备份: {version_id}")
            return version_id
        
        except Exception as e:
            logger.error(f"创建备份失败: {e}")
            return None
    
    def _record_version(self, version_id: str, reason: str, backup_path: Path):
        """记录版本到数据库"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            # 创建版本表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS param_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id TEXT UNIQUE,
                    created_at TEXT,
                    reason TEXT,
                    backup_path TEXT,
                    config_json TEXT,
                    trades_before INTEGER,
                    winrate_before REAL,
                    pnl_pct_before REAL
                )
            """)
            
            # 读取当前配置
            config = self.load_config()
            
            cur.execute("""
                INSERT INTO param_versions (version_id, created_at, reason, backup_path, config_json)
                VALUES (?, ?, ?, ?, ?)
            """, (version_id, datetime.now().isoformat(), reason, 
                  str(backup_path), json.dumps(config, ensure_ascii=False)))
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"记录版本失败: {e}")
    
    def _cleanup_old_versions(self):
        """清理旧版本，只保留最近N个"""
        try:
            versions = sorted(self.backup_dir.glob("config_v*.yaml"), 
                            key=lambda p: p.stat().st_mtime, reverse=True)
            
            for old_file in versions[self.MAX_VERSIONS:]:
                old_file.unlink()
                logger.debug(f"清理旧版本: {old_file.name}")
        
        except Exception as e:
            logger.error(f"清理旧版本失败: {e}")
    
    def list_versions(self, limit: int = 10) -> List[Dict]:
        """列出历史版本"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute("""
                SELECT version_id, created_at, reason, trades_before, winrate_before, pnl_pct_before
                FROM param_versions
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            
            rows = cur.fetchall()
            
            versions = []
            for r in rows:
                versions.append({
                    "version_id": r["version_id"],
                    "created_at": r["created_at"],
                    "reason": r["reason"],
                    "trades_before": r["trades_before"],
                    "winrate_before": r["winrate_before"],
                    "pnl_pct_before": r["pnl_pct_before"]
                })
            
            return versions
        
        except Exception as e:
            logger.error(f"列出版本失败: {e}")
            return []
    
    def rollback_to_version(self, version_id: str) -> Tuple[bool, str]:
        """
        回滚到指定版本
        
        Returns:
            (success, message)
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute("""
                SELECT backup_path, config_json FROM param_versions WHERE version_id=?
            """, (version_id,))
            
            row = cur.fetchone()
            
            if not row:
                return False, f"版本不存在: {version_id}"
            
            # 先备份当前配置
            self.create_version_backup(f"rollback_from_{version_id}")
            
            # 恢复历史配置
            backup_path = Path(row["backup_path"])
            if backup_path.exists():
                shutil.copy2(backup_path, self.config_path)
                return True, f"已回滚到 {version_id}"
            else:
                # 从 JSON 恢复
                config = json.loads(row["config_json"])
                self.save_config(config)
                return True, f"已从JSON恢复到 {version_id}"
        
        except Exception as e:
            logger.error(f"回滚失败: {e}")
            return False, f"回滚失败: {e}"
    
    def apply_suggestions(self, suggestions: List[Dict], 
                         paper_engine=None) -> Tuple[bool, Dict]:
        """
        应用AI参数建议
        
        Args:
            suggestions: AI输出的参数调整列表
            paper_engine: 模拟盘引擎（用于记录变更前快照）
        
        Returns:
            (success, result)
        """
        if not suggestions:
            return False, {"error": "无参数建议"}
        
        # 创建当前版本备份
        version_id = self.create_version_backup("before_ai_adjustment")
        
        # 记录变更前性能快照
        if paper_engine:
            self._update_version_snapshot(version_id, paper_engine)
        
        # 应用每个建议
        applied = []
        failed = []
        
        for sug in suggestions:
            param_name = sug.get("param")
            new_value = sug.get("new")
            reason = sug.get("reason", "")
            
            if param_name not in self.param_paths:
                failed.append({"param": param_name, "error": "未知参数"})
                continue
            
            success, msg = self.set_param_value(param_name, new_value)
            
            if success:
                applied.append({
                    "param": param_name,
                    "old": sug.get("old"),
                    "new": new_value,
                    "reason": reason
                })
            else:
                failed.append({"param": param_name, "error": msg})
        
        # 更新 param_change_log
        self._log_param_changes(applied, "AI_REVIEW")
        
        return True, {
            "version_id": version_id,
            "applied": applied,
            "failed": failed,
            "message": f"已应用 {len(applied)} 个参数，失败 {len(failed)} 个"
        }
    
    def _update_version_snapshot(self, version_id: str, paper_engine):
        """更新版本的性能快照"""
        try:
            stats = paper_engine.get_stats()
            
            conn = self._get_conn()
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE param_versions
                SET trades_before=?, winrate_before=?, pnl_pct_before=?
                WHERE version_id=?
            """, (
                stats.get("total_closed_trades", 0),
                stats.get("win_rate", 0),
                stats.get("total_pnl_pct", 0),
                version_id
            ))
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"更新快照失败: {e}")
    
    def _log_param_changes(self, changes: List[Dict], source: str):
        """记录参数变更到数据库"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            for c in changes:
                cur.execute("""
                    INSERT INTO param_change_log (timestamp, param_name, old_value, new_value, source, review_summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now().isoformat(),
                    c["param"],
                    c["old"],
                    c["new"],
                    source,
                    c.get("reason", "")
                ))
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"记录变更失败: {e}")
    
    def compare_performance(self, version_id: str, paper_engine) -> Dict:
        """
        对比指定版本与当前性能
        
        Returns:
            性能对比数据
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute("""
                SELECT trades_before, winrate_before, pnl_pct_before
                FROM param_versions WHERE version_id=?
            """, (version_id,))
            
            row = cur.fetchone()
            
            if not row:
                return {"error": "版本不存在"}
            
            current_stats = paper_engine.get_stats()
            
            return {
                "version_id": version_id,
                "before": {
                    "trades": row["trades_before"] or 0,
                    "winrate": row["winrate_before"] or 0,
                    "pnl_pct": row["pnl_pct_before"] or 0
                },
                "after": {
                    "trades": current_stats.get("total_closed_trades", 0),
                    "winrate": current_stats.get("win_rate", 0),
                    "pnl_pct": current_stats.get("total_pnl_pct", 0)
                },
                "delta": {
                    "winrate": (current_stats.get("win_rate", 0) - (row["winrate_before"] or 0)),
                    "pnl_pct": (current_stats.get("total_pnl_pct", 0) - (row["pnl_pct_before"] or 0))
                }
            }
        
        except Exception as e:
            logger.error(f"对比性能失败: {e}")
            return {"error": str(e)}
    
    def get_change_history(self, param_name: Optional[str] = None, 
                          limit: int = 20) -> List[Dict]:
        """获取参数变更历史"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            if param_name:
                cur.execute("""
                    SELECT timestamp, param_name, old_value, new_value, source, review_summary
                    FROM param_change_log
                    WHERE param_name=?
                    ORDER BY id DESC
                    LIMIT ?
                """, (param_name, limit))
            else:
                cur.execute("""
                    SELECT timestamp, param_name, old_value, new_value, source, review_summary
                    FROM param_change_log
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,))
            
            rows = cur.fetchall()
            
            changes = []
            for r in rows:
                changes.append({
                    "timestamp": r["timestamp"],
                    "param": r["param_name"],
                    "old": r["old_value"],
                    "new": r["new_value"],
                    "source": r["source"],
                    "reason": r["review_summary"]
                })
            
            return changes
        
        except Exception as e:
            logger.error(f"获取变更历史失败: {e}")
            return []


if __name__ == "__main__":
    print("=" * 60)
    print("ParamManager Test")
    print("=" * 60)
    
    manager = ParamManager(
        config_path="config.yaml",
        learning_db_path="ai_learning.db"
    )
    
    # 测试获取参数
    print("\n当前参数值:")
    for param in ["atr_sl_mult", "risk_per_trade", "max_position_pct"]:
        value = manager.get_param_value(param)
        print(f"  {param}: {value}")
    
    # 测试版本列表
    print("\n历史版本:")
    versions = manager.list_versions()
    for v in versions[:5]:
        print(f"  {v['version_id']}: {v['reason']} @ {v['created_at']}")
    
    # 测试变更历史
    print("\n变更历史:")
    changes = manager.get_change_history(limit=5)
    for c in changes:
        print(f"  {c['param']}: {c['old']} → {c['new']} ({c['source']})")
