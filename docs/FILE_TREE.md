# 项目文件树

策略云行（`bnb_quant_tool`）目录职责一览。运行时库与密钥默认不入库。

```text
bnb_quant_tool/
├── README.md                 # 项目总览
├── ROADMAP.md                # 演进路线
├── requirements.txt          # Python 依赖
├── config.example.yaml       # 配置示例（复制为 config.yaml，勿提交真实密钥）
├── .env.example              # 环境变量示例
├── .gitignore
├── autopilot_daemon.py       # 无人值守主进程
├── gui.py                    # 本机 GUI 调试
├── paper_watcher.py          # 模拟盘盯仓（调试用）
├── python_env.bat            # Python 环境辅助
├── 启动服务器.bat            # 生产推荐入口（分析+开仓+盯仓）
├── 启动脚本使用说明.md
├── 已实现功能总结.md
│
├── docs/                     # 设计与部署文档
│   ├── README.md             # 文档索引
│   ├── FILE_TREE.md          # 本文件
│   ├── server-deploy.md
│   ├── data_localization_guide.md
│   ├── core-philosophy.md
│   ├── ai-trader-philosophy.md
│   └── …
│
├── src/bnb_quant_tool/       # 核心 Python 包
│   ├── paper_trading.py      # 模拟盘引擎（SL/TP/超时）
│   ├── trade_advisor.py      # 开单建议
│   ├── ai_learning_system.py # 学习库
│   ├── signal_scanner.py     # 行情扫描
│   ├── sqlite_util.py        # SQLite 锁重试工具
│   ├── agents/               # 交易员议会 / 记忆
│   └── …
│
├── web/                      # Web 控制台（悠悠草 UI）
│   ├── README.md
│   ├── public/               # 静态页 + API 入口
│   │   ├── index.html
│   │   ├── assets/css/       # tokens.css + style.css
│   │   ├── assets/js/app.js
│   │   └── api/index.php
│   ├── includes/             # PHP Bridge / Database / Market
│   └── scripts/              # 分析、平仓、维护脚本
│
├── tests/                    # pytest
├── deploy/windows/           # Windows 计划任务等
├── scripts/                  # 运维/工具脚本
├── gui/                      # GUI 子模块
│
└── data/                     # 仅运行时产物（*.db / 缓存 / 日志，默认 gitignore）
```

## 不入库内容

| 路径 / 模式 | 原因 |
|-------------|------|
| `config.yaml` / `.env` | 含 API Key |
| `data/*.db`、`data/logs/`、`data/klines/` 等 | 运行时数据 |
| `.venv/`、`__pycache__/` | 本地环境 |
| `*.exe` / `*.zip` | 二进制与安装包 |

## 模块速查

| 能力 | 主要位置 |
|------|----------|
| 行情与指标 | `data_fetcher.py`、`technical_indicators.py`、`multi_timeframe.py` |
| AI 分析 | `ai_analyzer.py`、`llm_router.py`、`agents/` |
| 开单与风控 | `trade_advisor.py`、`risk_manager.py`、`circuit_breaker.py` |
| 模拟盘 | `paper_trading.py`、`position_exit_policy.py`、`position_reeval.py` |
| 学习闭环 | `ai_learning_system.py`、`trade_close_learning.py`、`capability_memory.py` |
| Web | `web/public/`、`web/includes/` |
| SQLite 并发 | `sqlite_util.py`、`paper_trading._run_db` |
