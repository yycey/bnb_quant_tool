# 策略云行 — Web 控制台

远程监控与配置面板：**策略云行量化平台策略管理系统**。

Python 量化引擎不变，Web 只读 SQLite + 调 Python 桥接脚本。

## 功能

| 模块 | 说明 |
|------|------|
| 仪表盘 | 现价、胜率、R 倍数、累计盈亏、最新 AI 建议 |
| **智能闭环** | 感知→决策→执行→反思→记忆健康度、三家 LLM、知识复用、议会交易员记忆 |
| 持仓 | 实时浮动盈亏，支持 Web 手动平仓 |
| 历史 / 信号 | 最近交易与信号追踪 |
| AI 学习 | 学习日志 + 策略权重 |
| 监控 | watcher 心跳、Python/数据库状态、服务开关 |
| **维护** | 健康检查、自动修复、DB 优化、备份、上传 zip 热更新、Git 拉取 |
| 配置 | 远程修改门槛/自动跟单/扫描器/闭环开关等（自动备份 config.yaml） |

## 快速启动（本机）

### 1. 环境

- PHP 7.4+，扩展：`pdo_sqlite`、`curl`、`json`、**`zip`**（备份/热更新）
- Python 环境已安装（平仓走完整学习闭环时需要）
- 先在 GUI 或 CLI 跑过一次分析，生成 `data/*.db`

检查 PHP：

```bat
php -m | findstr sqlite
php -m | findstr curl
```

### 2. 启动 Web 控制台（可选）

交易主进程请先双击项目根目录 **`启动服务器.bat`**（已内嵌盯仓）。

本机看面板：

```bat
web\start_dev.bat
```

浏览器打开：http://127.0.0.1:8787

> 不要再开独立 `paper_watcher` / 旧「启动监控」——会与服务器进程双写。

## 生产部署（Nginx + PHP-FPM）

1. 复制 `web/nginx.conf.example`，修改 `root` 为 `web/public` 绝对路径
2. `nginx -t && nginx -s reload`
3. 确保 PHP-FPM 运行，且 `pdo_sqlite` 已启用

## 宝塔部署（Windows / Linux）

### 方式 A：仅 web 目录作为站点（你当前的方式）

网站目录 `D:/wwwroot/a.dso.cc`，运行目录 `/public`。

**必须上传完整 `web/` 目录**，不能只传部分文件：

```
D:/wwwroot/a.dso.cc/
├── config.yaml          ← 从项目根复制过来
├── data/                ← 从项目根复制（含 *.db）
│   ├── ai_learning.db
│   └── paper_trading.db
├── public/              ← 网站入口
│   ├── index.html
│   ├── api/index.php
│   └── assets/
├── includes/            ← 7 个 PHP 文件缺一不可
│   ├── bootstrap.php
│   ├── Response.php
│   ├── Database.php
│   ├── Config.php
│   ├── Market.php
│   ├── Bridge.php
│   ├── Maintenance.php
│   ├── AiGrowth.php
│   ├── IntelligenceLoop.php
│   └── PnlEpoch.php
└── scripts/
    └── close_position.py
```

### 方式 B：完整项目在 D:\bb（推荐）

| 宝塔设置 | 值 |
|---------|-----|
| 网站目录 | `D:/bb` |
| 运行目录 | `/web/public` |

目录结构：

```
D:/bb/                      ← 项目根（config.yaml、data/ 在这里）
├── config.yaml
├── data/
│   ├── ai_learning.db
│   └── paper_trading.db
├── src/
├── web/
│   ├── includes/
│   ├── scripts/
│   └── public/             ← 对外网站入口
└── deploy.local.php        ← 可选，已预设 project_root
```

**不需要**把文件散落到 `D:\wwwroot\a.dso.cc`，直接把站点根目录改到 `D:\bb` 即可。

### 方式 C：网站与项目分离

网站文件在 `D:\wwwroot\a.dso.cc`，项目在 `D:\bb` 时，在网站目录放 `deploy.local.php`：

```php
<?php
return ['project_root' => 'D:/bb'];
```

### 宝塔 PHP 设置

- 启用扩展：`pdo_sqlite`、`curl`、**`zip`**（备份与热更新）
- PHP 7.4 或 8.x
- **`shell_exec` 默认被宝塔禁用** — 正常，不影响查看数据；Web 平仓会自动走 DB 回退模式
- 若需 Python 平仓：在 `config.yaml` 设置 `web.python_path: "C:/Python311/python.exe"`（不依赖 shell_exec）

### 路径找不到时

复制 `deploy.local.php.example` 为 `deploy.local.php`，设置：

```php
return ['project_root' => 'D:/wwwroot/a.dso.cc'];
```

并确保该目录下有 `config.yaml` 和 `data/*.db`。

### 上传后自检

访问 `https://你的域名/api/index.php?endpoint=monitor`，应返回 JSON 而非 PHP 报错。

编辑 `config.yaml`：

```yaml
web:
  api_token: "your-strong-random-token"
  python_path: ""   # 可选，平仓脚本用的 Python 路径
```

设置 `api_token` 后，**所有 API** 需携带 Token：

```
Authorization: Bearer your-strong-random-token
```

Web 页面点右上角 **🔑 Token** 保存到浏览器。

建议：

- 仅内网或 VPN 暴露；公网请 Nginx 加 HTTPS + IP 白名单
- Token 足够长（32+ 随机字符）
- 不要提交含真实 Token 的 config 到公开仓库

## API 一览

| 方法 | endpoint | 说明 |
|------|----------|------|
| GET | overview | 总览统计 |
| GET | market | 行情快照 |
| GET | positions | 当前持仓 |
| GET | history | 历史交易 |
| GET | signals | 信号追踪 |
| GET | ai_learning | AI 学习概览 |
| GET | strategies | 13 策略权重 |
| GET | performance | 日/周绩效 |
| GET | latest_advice | 最新 AI 建议 + 决策解释 |
| GET | decision_history | 最近 N 条决策解释（`limit` 默认 10） |
| GET | maintenance | 维护：`?action=status\|health\|backups` |
| POST | maintenance | 维护操作 `{ "action": "fix\|optimize\|backup\|git_pull" }` |
| POST | maintenance_upload | 上传 zip 更新包（multipart 字段 `package`） |
| GET | status | watcher 简要状态 |
| GET | monitor | 完整监控面板数据 |
| GET | config | 脱敏配置 + 可编辑 schema |
| POST | config_update | 保存配置 `{ "patch": { "trading": { "confidence_threshold": 0.65 } } }` |
| POST | close_position | 平仓 `{ "id": 1 }` |
| GET | pnl_epoch | 查看累计盈亏统计周期 |
| POST | pnl_epoch | 重置周期 `{ "action": "reset_today\|reset_now\|clear" }`（不删交易） |

示例：

```bat
curl "http://127.0.0.1:8787/api/index.php?endpoint=overview"
curl -H "Authorization: Bearer TOKEN" "http://host/api/index.php?endpoint=monitor"
```

## 目录结构

```
web/
├── public/           # 网站根目录
│   ├── index.html
│   ├── api/index.php
│   └── assets/
├── includes/         # PHP 核心
├── scripts/          # Python 桥接
├── nginx.conf.example
└── start_dev.bat
```

## 常见问题

**页面有数据但持仓为空**  
确认 `data/paper_trading.db` 存在；旧版可能在项目根目录，Web 会自动查找两处。

**平仓失败**  
本机需可用 Python + 依赖；或走 DB 回退模式（不触发完整学习反馈）。

**监控显示未运行**  
运行 `paper_watcher.py`，心跳文件：`data/watcher.heartbeat`。

**改配置后 GUI 未生效**  
GUI 启动时读 config；重启 GUI 或重新加载配置后生效。

## 宝塔在线维护 / 更新

Web 控制台新增 **「维护」** Tab，适合服务器远程运维：

| 操作 | 说明 |
|------|------|
| 健康检查 | 数据库、schema、Web 文件、Python 模块 |
| 自动修复 | DB 列迁移、根目录数据库迁入 data/ |
| 优化数据库 | SQLite VACUUM + WAL checkpoint |
| 备份数据 | 打包 config.yaml + data/*.db → `data/backups/` |
| 上传更新包 | zip 热更新 src/web/ 代码，**不覆盖** data/ 与 config.yaml |
| Git 拉取 | 需在 config.yaml 设置 `web.update.allow_git_pull: true` |

### 宝塔必配

```yaml
web:
  api_token: "你的长Token"
  python_path: "C:/Python311/python.exe"   # Windows 宝塔
  # python_path: "/usr/bin/python3"       # Linux 宝塔
  update:
    enabled: true
    allow_git_pull: false    # 仅 Git 部署时改 true
    git_branch: main
```

### 更新 zip 打包方式（本地开发机）

在项目根目录打包（保留目录结构）：

```
zip -r update.zip src/ web/ gui.py main.py paper_watcher.py requirements.txt
```

上传到 Web → 维护 → 应用更新。更新前会自动备份，更新后自动跑修复。

### 安全提示

- 所有写操作（修复/优化/备份/上传/Git）**必须 Token**
- 生产环境务必 HTTPS + 强 Token
- `allow_git_pull` 默认关闭，防止误拉代码
