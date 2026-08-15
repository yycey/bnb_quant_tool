# 策略云行 · 服务器部署

产品：**策略云行量化平台策略管理系统**

**宝塔 Web 界面即管理系统；`启动服务器.bat` 只跑交易引擎。**

| 组件 | 作用 | 怎么开 |
|------|------|--------|
| Web 控制台 | 持仓 / 历史 / 学习 / 监控 / 配置 | 宝塔域名 |
| 交易进程 | 分析 / 开仓 / 盯仓 | `启动服务器.bat` |
| 桌面 GUI | 本机调试 | 服务器上不必开 |

## 宝塔目录

网站目录 = **项目根**；运行目录 = `/web/public`：

```
D:/bb/
├── config.yaml
├── data/
├── src/
├── 启动服务器.bat
└── web/public/
```

PHP 扩展：`pdo_sqlite` / `curl` / `zip`。

## 用法

1. 上传整包项目  
2. 设好宝塔网站 → 浏览器打开域名  
3. 服务器保持 `启动服务器.bat` 运行  
4. Web 改配置后重启交易进程  

日志：`data\logs\autopilot.log`

## 开机自启（可选）

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install_scheduled_tasks.ps1
```
