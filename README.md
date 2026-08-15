# 策略云行量化平台策略管理系统

**版本 3.0.0** — 以 AI 为核心决策引擎的数字货币分析与交易辅助系统（简称「策略云行」）。

这个项目不是单纯的指标计算器，也不是只会调用大模型给一句看涨看跌结论的聊天脚本。它的核心思想是:

**让 AI 结合行情、技术指标、新闻情绪、链上数据、宏观环境、专属事件因子与历史复盘结果，输出可执行、可解释、可持续进化的交易建议。**

## 核心定位

本工具围绕一条完整的 AI 决策链路设计:

1. **先看市场**：抓取 K 线、成交量、技术指标、多周期结构
2. **再看上下文**：融合新闻、情绪、链上筹码、宏观因子、BNB 专属事件
3. **再交给 AI 判断**：让大模型和深度学习模块一起做方向研判
4. **再做风控门控**：不让低质量信号直接变成交易动作
5. **最后给执行方案**：输出入场、止损、分批止盈、仓位、失效条件
6. **交易后继续学习**：通过模拟盘、复盘、参数演化和知识沉淀，让系统越来越稳

## 这套工具真正解决什么问题

面对数字货币市场，交易者常见的问题不是“缺指标”，而是:

- 信息太多，无法快速形成高质量判断
- 单看技术面容易忽略新闻、情绪、宏观和链上背景
- 有方向判断，但没有一套严谨的执行和风控方案
- 做完交易后，经验没有被系统化沉淀，容易重复犯错

这个项目的目标，就是把这些环节统一到一个 AI 驱动闭环里。

## 核心能力

- **AI 主分析引擎**：使用 DeepSeek 对市场状态做结构化判断，而不是只输出自然语言评论
- **多因子融合**：技术指标 + 多周期 + 新闻 + 情绪 + 链上 + 宏观 + BNB 专属因子
- **深度学习辅助判断**：通过 MarketStateEncoder、时序模式识别和强化学习增强方向判断
- **机构策略投票**：把多种策略结果转成共识分数，不让任何单一信号独断
- **风控门控系统**：对低置信度、低盈亏比、事件冲突、极端风险环境做拦截
- **可执行开单建议**：直接给出 entry / stop loss / take profit / position sizing
- **可解释输出**：告诉用户 AI 为什么看多/看空/观望，不只是给结论
- **自学习闭环**：分析记录、模拟盘结果、复盘反馈、参数演化、知识卡片持续反哺下一次判断

## 核心架构

```text
市场数据 / 新闻 / 情绪 / 链上 / 宏观 / BNB事件
                ->
          AI 主分析 + 机构策略 + 深度学习
                ->
              综合投票与门控
                ->
        开单建议（方向 / 仓位 / 风控 / 失效条件）
                ->
           模拟盘 / 复盘 / 学习 / 参数进化
```

## 项目结构

完整目录说明见 [docs/FILE_TREE.md](docs/FILE_TREE.md)，文档索引见 [docs/README.md](docs/README.md)。

```text
bnb_quant_tool/
├── config.example.yaml / .env.example   # 示例配置（复制为 config.yaml / .env）
├── requirements.txt
├── 启动服务器.bat / python_env.bat      # 生产入口（分析+开仓+盯仓）
├── autopilot_daemon.py                  # 无人值守主进程
├── gui.py / paper_watcher.py            # 本机调试（勿与服务器同开）
├── deploy/windows/                      # 计划任务安装
├── docs/                                # 部署与设计文档（含文件树）
├── web/                                 # Web 控制台（悠悠草 UI）
├── src/bnb_quant_tool/                  # 核心包
├── tests/
└── data/                                # 仅运行时数据，禁止放源码（默认不入库）
```

## GitHub

本仓库可直接推送到 GitHub。请勿提交 `config.yaml`、`.env`、`data/*.db`。
## 安装

1. 安装依赖：`pip install -r requirements.txt`
2. 配置密钥（推荐 `.env`，勿提交仓库）：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# 填写 DEEPSEEK_API_KEY / ARK_API_KEY 等；真实 config.yaml 切勿提交 Git
```

3. 启动：双击 **`启动服务器.bat`**（一个进程：分析 + 开仓 + 盯仓）。详见 [docs/server-deploy.md](docs/server-deploy.md)。
## 使用方法

### Windows 服务器（推荐）

双击 **`启动服务器.bat`**（一个进程：分析 + 开仓 + 盯仓）。API 用 `config.yaml`。详见 [docs/server-deploy.md](docs/server-deploy.md)。

### GUI（本机调试）

```bash
python gui.py
```

## 配置说明

### DeepSeek 配置

```yaml
deepseek:
  api_key: "..."           # DeepSeek API Key
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-pro"  # 或 deepseek-v4-flash
  thinking:
    type: "enabled"
  reasoning_effort: "high"
```

### 币安配置

```yaml
binance:
  api_key: "..."
  api_secret: "..."
  testnet: true  # 使用测试网
```

### 交易配置

```yaml
trading:
  symbol: "BNBUSDT"
  timeframe: "1h"      # 1m, 5m, 15m, 1h, 4h, 1d
  lookback_days: 30
  risk_per_trade: 0.02    # 每笔交易风险 2%
  confidence_threshold: 0.7
```

## 输出示例

```text
============================================================
BNB AI 交易分析结果
============================================================
时间: 2024-01-15T10:30:00
交易对: BNBUSDT (1h)
数据点: 720
当前价格: $598.50

[AI Core Conclusion]
- final_action: LONG
- confidence: 75%
- summary: AI 当前结论是做多，且满足开单条件

[Execution Plan]
- entry: 598.50
- stop_loss: 585.00
- take_profit_1: 607.00
- take_profit_2: 620.00
- position: 0.5234 BNB

[Decision Engine]
- AI: LONG
- institutional: BUY
- deep_learning: BUY
- scoreboard: long 0.71 | short 0.18

[Key Evidence]
- 技术指标偏多
- 多周期方向一致
- 新闻与情绪未出现强冲突
- 风险回报比满足要求
============================================================
```

## 核心模块说明

### DataFetcher - 数据获取

```python
from bnb_quant_tool.data_fetcher import BinanceDataFetcher

fetcher = BinanceDataFetcher()
df = fetcher.get_klines(symbol="BNBUSDT", interval="1h", limit=100)
```

### AIAnalyzer - AI 主分析

```python
from bnb_quant_tool.ai_analyzer import DeepSeekAnalyzer

analyzer = DeepSeekAnalyzer(api_key="your_key")
result = analyzer.analyze_market_data(df, indicators)
```

### TechnicalIndicators - 技术指标

```python
from bnb_quant_tool.technical_indicators import TechnicalIndicators

indicators = TechnicalIndicators.calculate_all_indicators(df)
```

### TradeAdvisor - 最终交易执行建议

`TradeAdvisor` 不是简单拼接文本，而是把以下信息真正汇总到一起:

- AI 主分析结果
- 机构策略共识
- 深度学习预测
- 多周期过滤
- 新闻和情绪冲突检查
- 链上与宏观修正
- BNB 专属事件门控
- 学习系统的历史胜率与保守度修正

最终输出的不只是 `BUY/SELL/HOLD`，而是:

- 是否应该开单
- 开哪个方向
- 入场区间、止损、分批止盈
- 应该用多大仓位
- 为什么这样做
- 什么情况下取消这笔计划

## 产品哲学

这个工具坚持以下原则:

1. **AI 是核心，不是装饰**
   AI 不只是解释指标，而是参与最终方向判断与执行建议生成。

2. **结论必须可执行**
   输出必须落到价格、仓位、止损、止盈和失效条件，而不是停留在“偏多”“谨慎乐观”。

3. **风控优先于激进**
   当信号不清晰、盈亏比不够、环境冲突或历史表现恶化时，系统应该主动选择 WAIT。

4. **学习必须进入下一次决策**
   复盘、模拟盘和历史统计的价值，不是存档，而是直接改变下一轮 AI 判断与门控阈值。

5. **解释性很重要**
   用户不仅要知道“做什么”，还要知道“为什么做”以及“为什么现在不该做”。

## 注意事项

1. **API Key 安全**：不要将包含真实 API Key 的配置文件提交到公开仓库
2. **测试优先**：建议先在测试网或小额资金下测试策略
3. **风险控制**：严格遵守风险管理规则，不要投入超过承受能力的资金
4. **AI 分析仅供参考**：AI 分析结果不保证盈利，需要结合自己的判断

## 当前方向与后续优化

- [ ] 将 AI 决策链做成更清晰的 Web / GUI 可视化面板
- [ ] 增强多交易对、多市场状态下的统一调度能力
- [ ] 继续优化深度学习训练样本质量，而不是只堆模型复杂度
- [ ] 提升 AI 输出稳定性、结构化程度与异常兜底能力
- [ ] 强化复盘系统，让“学到什么”更容易被人直接理解

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

---

**免责声明**：本工具用于学习、研究与交易辅助分析，不构成投资建议。数字货币波动剧烈，任何 AI 结论、策略信号或执行计划都不保证盈利，使用者需自行承担风险并做好资金管理。
