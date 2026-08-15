# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - 数据本地化管理器 v1.0

核心功能:
1. 将所有AI学习数据保存到工作空间（可Git版本控制）
2. 导出/导入功能支持换电脑迁移
3. 自动备份机制
4. 数据完整性校验

作者: Python全栈工程师
日期: 2026-06-03
"""

import os
import json
import sqlite3
import shutil
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DataLocalizationManager:
    """
    数据本地化管理器
    
    确保所有AI学习数据保存在工作空间内，可随项目迁移
    """
    
    def __init__(self, workspace_path: str = "."):
        """
        初始化管理器
        
        Args:
            workspace_path: 工作空间根目录（默认当前目录）
        """
        self.workspace = Path(workspace_path).resolve()
        self.data_dir = self.workspace / "data"
        self.models_dir = self.data_dir / "models"
        self.exports_dir = self.data_dir / "exports"
        
        # 确保目录存在
        self._ensure_directories()
        
        # 数据库路径映射
        self.db_paths = {
            'ai_learning': self.data_dir / 'ai_learning.db',
            'paper_trading': self.data_dir / 'paper_trading.db',
            'counterfactual': self.data_dir / 'counterfactual.db',
            'pattern_memory': self.data_dir / 'pattern_memory.db'
        }
        
        # 模型文件路径
        self.model_paths = {
            'deep_learning': self.models_dir / 'deep_learning_model.pkl',
            'pattern_recognizer': self.models_dir / 'pattern_recognizer.pkl',
            'strategy_library': self.models_dir / 'strategy_library.json'
        }
        
        logger.info(f"DataLocalizationManager initialized at {self.workspace}")
    
    def _ensure_directories(self):
        """确保必要目录存在"""
        dirs = [
            self.data_dir,
            self.models_dir,
            self.exports_dir,
            self.data_dir / 'backups',
            self.data_dir / 'snapshots'
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
    
    def get_db_path(self, db_name: str) -> Path:
        """获取数据库路径（本地化）"""
        return self.db_paths.get(db_name, self.data_dir / f'{db_name}.db')
    
    def get_model_path(self, model_name: str) -> Path:
        """获取模型文件路径（本地化）"""
        return self.model_paths.get(model_name, self.models_dir / f'{model_name}.pkl')
    
    def migrate_from_old_locations(self) -> Dict[str, bool]:
        """
        从旧位置迁移数据到工作空间
        
        Returns:
            迁移结果字典
        """
        results = {}
        
        # 迁移数据库文件
        db_migrations = [
            ('ai_learning.db', self.db_paths['ai_learning']),
            ('paper_trading.db', self.db_paths['paper_trading']),
            ('counterfactual.db', self.db_paths['counterfactual']),
            ('pattern_memory.db', self.db_paths['pattern_memory'])
        ]
        
        for old_name, new_path in db_migrations:
            old_path = self.workspace / old_name
            if old_path.exists() and not new_path.exists():
                try:
                    shutil.copy2(old_path, new_path)
                    results[f'migrate_{old_name}'] = True
                    logger.info(f"Migrated {old_name} to {new_path}")
                except Exception as e:
                    results[f'migrate_{old_name}'] = False
                    logger.error(f"Failed to migrate {old_name}: {e}")
            else:
                results[f'migrate_{old_name}'] = 'skipped'
        
        # 迁移模型文件
        model_migrations = [
            ('deep_learning_model.pkl', self.model_paths['deep_learning']),
            ('pattern_recognizer.pkl', self.model_paths['pattern_recognizer'])
        ]
        
        for old_name, new_path in model_migrations:
            old_path = self.workspace / old_name
            if old_path.exists() and not new_path.exists():
                try:
                    shutil.copy2(old_path, new_path)
                    results[f'migrate_{old_name}'] = True
                    logger.info(f"Migrated {old_name} to {new_path}")
                except Exception as e:
                    results[f'migrate_{old_name}'] = False
                    logger.error(f"Failed to migrate {old_name}: {e}")
            else:
                results[f'migrate_{old_name}'] = 'skipped'
        
        # 迁移JSON配置文件
        json_migrations = [
            ('discovered_strategies.json', self.models_dir / 'strategy_library.json')
        ]
        
        for old_name, new_path in json_migrations:
            old_path = self.workspace / old_name
            if old_path.exists():
                try:
                    if not new_path.exists():
                        shutil.copy2(old_path, new_path)
                        results[f'migrate_{old_name}'] = True
                        logger.info(f"Migrated {old_name} to {new_path}")
                    else:
                        # 合并策略库
                        self._merge_strategy_library(old_path, new_path)
                        results[f'merge_{old_name}'] = True
                except Exception as e:
                    results[f'migrate_{old_name}'] = False
                    logger.error(f"Failed to migrate {old_name}: {e}")

        # 将模型/策略库文件迁入 ai_learning.db
        try:
            from bnb_quant_tool.sqlite_recovery import check_sqlite_health
            from bnb_quant_tool.db_artifact_store import DbArtifactStore

            ai_db = str(self.db_paths['ai_learning'])
            healthy, reason = check_sqlite_health(ai_db)
            if not healthy and reason != "file_missing":
                results['db_artifact_migrate'] = 'skipped_corrupt'
                logger.warning("跳过 artifact 迁移: ai_learning.db 不健康 (%s)", reason)
            else:
                store = DbArtifactStore(ai_db)
                db_migrate = store.migrate_legacy_files(str(self.workspace))
                results.update({f"db_{k}": v for k, v in db_migrate.items()})
        except Exception as e:
            results['db_artifact_migrate'] = False
            logger.error(f"Failed to migrate artifacts to database: {e}")
        
        return results
    
    def _merge_strategy_library(self, old_path: Path, new_path: Path):
        """合并策略库"""
        try:
            with open(old_path, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
            
            with open(new_path, 'r', encoding='utf-8') as f:
                new_data = json.load(f)
            
            # 合并策略列表（去重）
            old_strategies = old_data.get('strategies', [])
            new_strategies = new_data.get('strategies', [])
            
            existing_ids = {s.get('id') for s in new_strategies}
            for s in old_strategies:
                if s.get('id') not in existing_ids:
                    new_strategies.append(s)
            
            new_data['strategies'] = new_strategies
            new_data['merged_at'] = datetime.now().isoformat()
            
            with open(new_path, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Merged strategy library: {len(new_strategies)} total strategies")
        except Exception as e:
            logger.error(f"Failed to merge strategy library: {e}")
    
    def export_all(self, export_name: Optional[str] = None) -> Path:
        """
        导出所有AI学习数据
        
        Args:
            export_name: 导出包名称（可选）
        
        Returns:
            导出文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_name = export_name or f"ai_data_export_{timestamp}"
        export_dir = self.exports_dir / export_name
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # 复制数据库
        db_backup_dir = export_dir / 'databases'
        db_backup_dir.mkdir(exist_ok=True)
        for name, path in self.db_paths.items():
            if path.exists():
                shutil.copy2(path, db_backup_dir / path.name)
        
        # 复制模型
        model_backup_dir = export_dir / 'models'
        model_backup_dir.mkdir(exist_ok=True)
        for name, path in self.model_paths.items():
            if path.exists():
                shutil.copy2(path, model_backup_dir / path.name)
        
        # 导出配置
        config_path = self.workspace / 'config.yaml'
        if config_path.exists():
            shutil.copy2(config_path, export_dir / 'config.yaml')
        
        # 生成元数据
        metadata = {
            'export_name': export_name,
            'export_time': datetime.now().isoformat(),
            'workspace': str(self.workspace),
            'databases': {name: str(path) for name, path in self.db_paths.items()},
            'models': {name: str(path) for name, path in self.model_paths.items()},
            'checksums': self._calculate_checksums(export_dir)
        }
        
        with open(export_dir / 'metadata.json', 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        # 打包为zip
        zip_path = self.exports_dir / f"{export_name}.zip"
        shutil.make_archive(str(zip_path.with_suffix('')), 'zip', export_dir)
        
        logger.info(f"Exported all data to {zip_path}")
        return zip_path
    
    def _calculate_checksums(self, directory: Path) -> Dict[str, str]:
        """计算文件校验和"""
        checksums = {}
        for file_path in directory.rglob('*'):
            if file_path.is_file():
                rel_path = file_path.relative_to(directory)
                with open(file_path, 'rb') as f:
                    checksum = hashlib.md5(f.read()).hexdigest()
                checksums[str(rel_path)] = checksum
        return checksums
    
    def import_data(self, import_path: str, merge: bool = False) -> Dict[str, bool]:
        """
        从导出包导入数据
        
        Args:
            import_path: 导出文件路径（zip或目录）
            merge: 是否合并（True）还是覆盖（False）
        
        Returns:
            导入结果字典
        """
        import_path = Path(import_path)
        results = {}
        
        # 解压（如果是zip）
        if import_path.suffix == '.zip':
            extract_dir = self.exports_dir / f"temp_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.unpack_archive(import_path, extract_dir)
            import_dir = extract_dir
        else:
            import_dir = import_path
        
        # 验证元数据
        metadata_path = import_dir / 'metadata.json'
        if not metadata_path.exists():
            logger.error("Invalid import: missing metadata.json")
            return {'error': 'missing_metadata'}
        
        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        
        # 验证校验和
        current_checksums = self._calculate_checksums(import_dir)
        original_checksums = metadata.get('checksums', {})
        
        checksum_valid = True
        for rel_path, checksum in original_checksums.items():
            if rel_path != 'metadata.json' and current_checksums.get(rel_path) != checksum:
                checksum_valid = False
                logger.warning(f"Checksum mismatch: {rel_path}")
        
        results['checksum_valid'] = checksum_valid
        
        # 导入数据库
        db_import_dir = import_dir / 'databases'
        if db_import_dir.exists():
            for db_file in db_import_dir.glob('*.db'):
                target_path = self.data_dir / db_file.name
                try:
                    if merge and target_path.exists():
                        self._merge_database(db_file, target_path)
                        results[f'import_{db_file.stem}'] = 'merged'
                    else:
                        shutil.copy2(db_file, target_path)
                        results[f'import_{db_file.stem}'] = 'copied'
                    logger.info(f"Imported database: {db_file.name}")
                except Exception as e:
                    results[f'import_{db_file.stem}'] = False
                    logger.error(f"Failed to import {db_file.name}: {e}")
        
        # 导入模型
        model_import_dir = import_dir / 'models'
        if model_import_dir.exists():
            for model_file in model_import_dir.glob('*'):
                if model_file.is_file():
                    target_path = self.models_dir / model_file.name
                    try:
                        shutil.copy2(model_file, target_path)
                        results[f'import_{model_file.stem}'] = True
                        logger.info(f"Imported model: {model_file.name}")
                    except Exception as e:
                        results[f'import_{model_file.stem}'] = False
                        logger.error(f"Failed to import {model_file.name}: {e}")
        
        # 清理临时目录
        if import_path.suffix == '.zip' and extract_dir.exists():
            shutil.rmtree(extract_dir)
        
        results['import_time'] = datetime.now().isoformat()
        return results
    
    def _merge_database(self, source_db: Path, target_db: Path):
        """合并SQLite数据库"""
        # 连接两个数据库
        src_conn = sqlite3.connect(str(source_db))
        tgt_conn = sqlite3.connect(str(target_db))
        
        # 获取所有表名
        src_cur = src_conn.cursor()
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in src_cur.fetchall()]
        
        for table in tables:
            if table.startswith('sqlite_'):
                continue
            
            # 读取源数据
            src_cur.execute(f"SELECT * FROM {table}")
            rows = src_cur.fetchall()
            
            if not rows:
                continue
            
            # 获取列名
            cols = [desc[0] for desc in src_cur.description]
            
            # 插入到目标数据库（忽略重复）
            tgt_cur = tgt_conn.cursor()
            placeholders = ','.join(['?' for _ in cols])
            col_names = ','.join(cols)
            
            for row in rows:
                try:
                    tgt_cur.execute(
                        f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",
                        row
                    )
                except sqlite3.IntegrityError:
                    pass
        
        tgt_conn.commit()
        src_conn.close()
        tgt_conn.close()
    
    def create_backup(self, backup_name: Optional[str] = None) -> Path:
        """创建备份"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = backup_name or f"backup_{timestamp}"
        backup_dir = self.data_dir / 'backups' / backup_name
        
        shutil.copytree(self.data_dir, backup_dir / 'data', 
                       ignore=shutil.ignore_patterns('backups', 'exports'))
        
        # 记录备份信息
        backup_info = {
            'backup_name': backup_name,
            'backup_time': datetime.now().isoformat(),
            'workspace': str(self.workspace)
        }
        
        with open(backup_dir / 'backup_info.json', 'w', encoding='utf-8') as f:
            json.dump(backup_info, f, indent=2)
        
        logger.info(f"Backup created: {backup_dir}")
        return backup_dir
    
    def list_backups(self) -> List[Dict]:
        """列出所有备份"""
        backups = []
        backup_dir = self.data_dir / 'backups'
        
        if not backup_dir.exists():
            return backups
        
        for backup_path in backup_dir.iterdir():
            if backup_path.is_dir():
                info_path = backup_path / 'backup_info.json'
                if info_path.exists():
                    with open(info_path, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                    backups.append(info)
                else:
                    backups.append({
                        'backup_name': backup_path.name,
                        'backup_time': 'unknown'
                    })
        
        return sorted(backups, key=lambda x: x.get('backup_time', ''), reverse=True)
    
    def restore_backup(self, backup_name: str) -> bool:
        """恢复备份"""
        backup_dir = self.data_dir / 'backups' / backup_name / 'data'
        
        if not backup_dir.exists():
            logger.error(f"Backup not found: {backup_name}")
            return False
        
        # 先备份当前数据
        self.create_backup(f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        
        # 恢复数据
        for item in backup_dir.iterdir():
            if item.is_dir() and item.name not in ['backups', 'exports']:
                target = self.data_dir / item.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
        
        logger.info(f"Restored from backup: {backup_name}")
        return True
    
    def get_data_summary(self) -> Dict:
        """获取数据摘要"""
        summary = {
            'databases': {},
            'models': {},
            'total_size': 0
        }
        
        # 数据库信息
        for name, path in self.db_paths.items():
            if path.exists():
                size = path.stat().st_size
                summary['databases'][name] = {
                    'path': str(path),
                    'size': size,
                    'size_human': self._human_readable_size(size)
                }
                summary['total_size'] += size
                
                # 获取记录数
                try:
                    conn = sqlite3.connect(str(path))
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = cur.fetchall()
                    summary['databases'][name]['tables'] = {}
                    for table in tables:
                        cur.execute(f"SELECT COUNT(*) FROM {table[0]}")
                        count = cur.fetchone()[0]
                        summary['databases'][name]['tables'][table[0]] = count
                    conn.close()
                except Exception as e:
                    logger.error(f"Failed to get table info for {name}: {e}")
        
        # 模型信息（旧版文件；新版已存入 ai_learning.db）
        for name, path in self.model_paths.items():
            if path.exists():
                size = path.stat().st_size
                summary['models'][name] = {
                    'path': str(path),
                    'size': size,
                    'size_human': self._human_readable_size(size),
                    'modified': datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                    'legacy_file': True,
                }
                summary['total_size'] += size

        # 数据库内工件（模型 blob、策略库）
        ai_path = self.db_paths.get('ai_learning')
        if ai_path and ai_path.exists():
            try:
                from bnb_quant_tool.db_artifact_store import DbArtifactStore
                store = DbArtifactStore(str(ai_path))
                summary['artifacts'] = {
                    'blobs': store.blob_summary(),
                    'discovered_strategies': store.strategy_count(),
                }
            except Exception as e:
                logger.debug(f"artifact summary skipped: {e}")
        
        summary['total_size_human'] = self._human_readable_size(summary['total_size'])
        return summary
    
    def _human_readable_size(self, size: int) -> str:
        """转换文件大小为可读格式"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"


# ============================================================
# 便捷函数（供其他模块直接调用）
# ============================================================

_localization_manager = None


def _resolve_project_root() -> Path:
    """固定到包所在项目根目录，避免 cwd 变化导致 data/ 路径漂移。"""
    return Path(__file__).resolve().parent.parent.parent


def init_workspace(workspace_path: str) -> DataLocalizationManager:
    """显式绑定工作空间根目录（GUI / watcher 启动时调用）。"""
    global _localization_manager
    root = Path(workspace_path).resolve()
    _localization_manager = DataLocalizationManager(str(root))
    try:
        from bnb_quant_tool.sqlite_recovery import repair_workspace_databases

        repair_workspace_databases(_localization_manager.data_dir)
    except Exception as e:
        logger.warning("Database repair skipped: %s", e)
    try:
        migrated = _localization_manager.migrate_from_old_locations()
        if any(migrated.values()):
            logger.info("Data migration: %s", migrated)
    except Exception as e:
        logger.warning("Data migration skipped: %s", e)
    logger.info("Workspace pinned: %s", root)
    return _localization_manager


def get_localization_manager(workspace_path: Optional[str] = None) -> DataLocalizationManager:
    """获取本地化管理器单例。"""
    global _localization_manager
    if _localization_manager is None:
        ws = Path(workspace_path).resolve() if workspace_path else _resolve_project_root()
        _localization_manager = DataLocalizationManager(str(ws))
    return _localization_manager

def get_localized_db_path(db_name: str) -> Path:
    """获取本地化数据库路径"""
    return get_localization_manager().get_db_path(db_name)


def resolve_db_path(db_path: Optional[str] = None, db_name: str = "paper_trading") -> str:
    """将 None / 相对 / 绝对路径统一解析为绝对 DB 路径（与 PaperTradingEngine 一致）。"""
    if db_path is None:
        return str(get_localized_db_path(db_name).resolve())
    p = Path(db_path)
    if p.is_absolute():
        return str(p.resolve())
    return str((_resolve_project_root() / db_path).resolve())

def get_localized_model_path(model_name: str) -> Path:
    """获取本地化模型路径"""
    return get_localization_manager().get_model_path(model_name)


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Data Localization Manager")
    print("=" * 60)
    
    manager = DataLocalizationManager()
    
    # 迁移旧数据
    print("\n=== Migrating old data ===")
    results = manager.migrate_from_old_locations()
    for k, v in results.items():
        print(f"  {k}: {v}")
    
    # 显示数据摘要
    print("\n=== Data Summary ===")
    summary = manager.get_data_summary()
    print(f"Total Size: {summary['total_size_human']}")
    print("\nDatabases:")
    for name, info in summary['databases'].items():
        print(f"  {name}: {info['size_human']}")
        for table, count in info.get('tables', {}).items():
            print(f"    - {table}: {count} records")
    
    print("\nModels:")
    for name, info in summary['models'].items():
        print(f"  {name}: {info['size_human']}")
    
    # 创建导出
    print("\n=== Creating Export ===")
    export_path = manager.export_all()
    print(f"Export created: {export_path}")
