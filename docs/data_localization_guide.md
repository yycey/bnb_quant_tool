# 数据本地化管理 - 使用指南

## 📋 概述

本系统确保所有AI学习数据保存在工作空间内，支持跨设备迁移。

---

## 📂 数据存储结构

```
bnb_quant_tool/
├── data/                          # 所有AI数据（本地化）
│   ├── databases/                 # 数据库文件
│   │   ├── ai_learning.db        # AI学习记录（176KB）
│   │   ├── paper_trading.db      # 模拟交易记录（32KB）
│   │   ├── counterfactual.db     # 反事实分析
│   │   └── pattern_memory.db     # 模式记忆库
│   │
│   ├── models/                    # AI模型文件
│   │   ├── deep_learning_model.pkl    # 深度学习模型
│   │   ├── pattern_recognizer.pkl     # 模式识别器
│   │   └── strategy_library.json      # 策略库
│   │
│   ├── exports/                   # 导出数据包
│   │   └── ai_export_xxx.zip     # 可迁移的数据包
│   │
│   ├── backups/                   # 自动备份
│   └── snapshots/                  # 快照
│
├── config.yaml                    # 配置文件
└── .gitignore                     # Git规则（数据可追踪）
```

---

## 🚀 快速开始

### 1. 启动管理工具

```bash
cd bnb_quant_tool
python data_manager_gui.py
```

### 2. 迁移旧数据（首次使用）

如果之前的数据在工作空间根目录：

1. 点击 **"迁移旧数据"**
2. 系统自动将所有 `.db` 和 `.pkl` 文件移至 `data/` 目录

---

## 📤 数据导出（换电脑前）

### 方法1：GUI导出

1. 启动 `data_manager_gui.py`
2. 点击 **"导出全部数据"**
3. 生成 `data/exports/ai_export_YYYYMMDD_HHMMSS.zip`

### 方法2：代码导出

```python
from bnb_quant_tool.data_localization import DataLocalizationManager

manager = DataLocalizationManager()
export_path = manager.export_all()
print(f"导出文件: {export_path}")
```

### 导出包内容

- 所有数据库文件
- 所有模型文件
- config.yaml 配置
- metadata.json 元数据
- MD5校验和

---

## 📥 数据导入（新电脑上）

### 方法1：GUI导入

1. 将导出的 `.zip` 文件复制到新电脑
2. 启动 `data_manager_gui.py`
3. 点击 **"导入数据包"**（覆盖）或 **"导入并合并"**（保留现有数据）
4. 选择 `.zip` 文件
5. 等待导入完成

### 方法2：代码导入

```python
from bnb_quant_tool.data_localization import DataLocalizationManager

manager = DataLocalizationManager()
results = manager.import_data("ai_export_xxx.zip", merge=False)
print(f"导入结果: {results}")
```

### 合并 vs 覆盖

| 操作 | 适用场景 | 说明 |
|------|----------|------|
| **导入（覆盖）** | 全新环境 | 直接替换所有数据 |
| **导入并合并** | 已有数据 | 保留原有数据，添加新记录（去重） |

---

## 💾 备份管理

### 创建备份

```python
manager.create_backup("my_backup_20260603")
```

### 查看备份列表

```python
backups = manager.list_backups()
for b in backups:
    print(f"{b['backup_name']}: {b['backup_time']}")
```

### 恢复备份

```python
manager.restore_backup("my_backup_20260603")
```

---

## 📊 数据摘要查看

```python
summary = manager.get_data_summary()
print(f"总大小: {summary['total_size_human']}")

for name, info in summary['databases'].items():
    print(f"{name}: {info['size_human']}")
    for table, count in info['tables'].items():
        print(f"  - {table}: {count} 条记录")
```

---

## 🔄 Git版本控制（推荐）

### 首次设置

```bash
cd bnb_quant_tool
git init
git add data/ config.yaml
git commit -m "Initial commit with AI learning data"
git remote add origin https://github.com/yourusername/bnb_quant_tool.git
git push -u origin main
```

### 日常同步

```bash
# 拉取最新数据
git pull

# 提交新学习数据
git add data/
git commit -m "Update AI learning data"
git push
```

### 新电脑克隆

```bash
git clone https://github.com/yourusername/bnb_quant_tool.git
cd bnb_quant_tool
# 数据自动在 data/ 目录
```

---

## ⚠️ 注意事项

### ✅ 推荐做法

1. **定期导出**: 每周导出一次数据包
2. **Git同步**: 配合Git进行版本控制
3. **备份习惯**: 重要操作前创建备份
4. **验证校验**: 导入后检查校验和是否匹配

### ❌ 避免

1. 不要直接修改 `data/` 目录下的文件
2. 不要忽略 `.gitignore` 中的规则
3. 不要在新电脑上直接覆盖数据（先备份）

---

## 🛠️ 高级用法

### 自定义数据路径

```python
manager = DataLocalizationManager(
    workspace_path="/custom/workspace/path"
)
```

### 编程访问数据

```python
# 获取本地化路径
db_path = manager.get_db_path('ai_learning')
model_path = manager.get_model_path('deep_learning')

# 或使用便捷函数
from bnb_quant_tool.data_localization import get_localized_db_path

path = get_localized_db_path('paper_trading')
```

---

## 📈 数据大小参考

| 数据类型 | 典型大小 | 说明 |
|----------|----------|------|
| ai_learning.db | 100-500KB | 分析记录越多越大 |
| paper_trading.db | 10-100KB | 交易记录 |
| deep_learning_model.pkl | 50-200KB | 训练后的模型 |
| strategy_library.json | 1-10KB | 策略配置 |
| **导出包** | 100KB-1MB | 所有数据打包 |

---

## 🔗 集成状态

已集成模块：
- ✅ `ai_learning_system.py` - AI学习系统
- ✅ `paper_trading.py` - 模拟交易引擎
- ✅ `deep_learning_engine.py` - 深度学习引擎

所有模块自动使用本地化路径，无需手动指定。

---

**数据本地化完成，换电脑无忧！** 🎉
