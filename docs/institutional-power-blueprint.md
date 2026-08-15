# 机构级加密研判 — 功能蓝图（2026）

> **最大实现目的**：不是「猜涨跌」，而是像 Two Sigma / Citadel / Renaissance 一样，  
> 用 **多层证据 + AI 综合推理 + 可执行风控 + 持续学习**，输出「此刻是否该做、怎么做、错了学什么」。

---

## 一、大机构真正在做什么（2025–2026 共识）

### 1. 两类机构，两种 edge

| 类型 | 代表 | 核心 edge | 对 BNB 工具的启示 |
|------|------|-----------|-------------------|
| **做市 / 微观结构** | Citadel Securities, Jump, Jane Street | 流动性、点差、库存管理、短周期 alpha | 高波动时启用做市/统计套利权重，不追方向 |
| **系统化多因子** | Two Sigma, Renaissance, AQR, D.E. Shaw | 海量数据 + ML + 因子组合 + 组合级风控 | AI 不是装饰，是因子融合与 regime 切换的大脑 |
| **结构套利** | 各类 crypto fund | Funding、Basis、Delta-neutral、波动率溢价 | 衍生品数据（资金费率/OI）必须进入门控，不只进 Prompt |

机构 **不赌单一指标**。他们赌的是：**在正确 market regime 下，用对的策略族，以可控风险暴露获取 edge**。

### 2. 2026 加密量化「真正有效」的策略族

来源：Quantt、Quant Matter、SparkCore、Regime 研究等。

1. **趋势 / 动量**（Citadel、AQR、Turtle）— 仅在 TRENDING / EUPHORIA 加权
2. **均值回归 / 统计套利**（Renaissance、Bollinger/RSI）— 仅在 RANGING / LOW_VOL 加权
3. **做市 / 波动率**（Jump）— 高波动时提供流动性逻辑，降方向暴露
4. **风险平价 / 波动率目标**（Bridgewater）— 仓位随 vol 缩放，不是固定杠杆
5. **Funding / Basis 结构**（机构 delta-neutral 核心）— 极端 funding → 降多/反向机会
6. **Regime 自适应**（HMM/GMM 研究共识）— **同一策略在不同 regime 的 Sharpe 可差 3 倍以上**

### 3. 机构级 pipeline（Two Sigma 公开描述抽象）

```text
数据 ingest → 特征工程 → 多策略信号 → Regime 路由 → 组合优化/风控 → 执行 → PnL 归因 → 模型监控
     ↑                                                              ↓
     └──────────── 知识卡片 / 反事实 / 参数 shadow / 策略 Lab ←──────┘
```

**Kill switch 是标配**：漂移检测、连亏熔断、极端 funding、模型失效 — 必须硬编码，不能只写进 Prompt。

---

## 二、本工具现状 vs 机构标准

### 已具备（接近机构框架）

| 模块 | 机构对应 | 状态 |
|------|----------|------|
| `institutional_strategies.py` | 13+ 策略投票池 | ✅ 有学习权重 + regime 乘数 |
| `market_regime.py` | Regime 路由 | ✅ 7 种状态 → 策略族加权 |
| `trade_advisor.py` | 组合风控 + 执行 | ✅ 门控/止损/分批止盈/仓位 |
| `ai_analyzer.py` + 多智能体 | Two Sigma 研究层 | ✅ LLM + 4 Agent 辩论 |
| `deep_learning_engine.py` | ML 预测层 | ✅ 可选接入 |
| `bnb_risk_sentry` + 情绪/OI | 衍生品结构 edge | ✅ funding 极值门控 |
| `learning_*` 闭环 | 模型监控 + 归因 | ✅ 分桶权重/反事实/知识门控 |
| `counterfactual_analyzer` | 决策质量审计 | ✅ 可强制 WAIT |
| `pattern_memory` | 相似局面检索 | ✅ 低胜率拦截 |

### 缺口（要做强必须补）

| 缺口 | 机构做法 | 优先级 |
|------|----------|--------|
| **统一信念分数** | 多因子 → 单一 directional conviction + 分解 | P0 ✅ 本次新增 |
| Regime 多信号融合 | 趋势+波动+Funding+情绪 超多数投票 | P0 |
| Funding/Basis 策略层 | 不只门控，参与方向（crowded trade 反向） | P1 |
| CircuitBreaker 接入 | 硬熔断 | P1 |
| 跨所/跨品种相对价值 | Renaissance stat arb | P2 |
| HMM/GMM regime | 隐状态 + 转移概率 | P2 |
| 决策驾驶舱 GUI | 因子条 + Agent 票 + 门控原因 | P1 |
| Walk-forward 策略晋升 | 机构 promotion gate | P2 |

---

## 三、目标架构 —「强大」的定义

```text
                    ┌─────────────────────────────────────┐
                    │     Layer 0: 全量数据 ingest         │
                    │ K线 MTF 新闻 情绪 链上 宏观 BNB 衍生品 │
                    └─────────────────┬───────────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  Layer 1: Regime Engine（状态路由）    │
                    │  TREND / RANGE / VOLATILE / PANIC …   │
                    │  → 策略族权重 + 仓位系数 + AI 上下文    │
                    └─────────────────┬───────────────────┘
                                      ▼
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
┌───────────────┐           ┌─────────────────┐           ┌─────────────────┐
│ L2a 机构策略池 │           │ L2b ML/DL 预测   │           │ L2c 结构因子     │
│ 13+ 投票      │           │ Two Sigma 风格   │           │ Funding OI Basis │
└───────┬───────┘           └────────┬────────┘           └────────┬────────┘
        └─────────────────────────────┼─────────────────────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  Layer 3: Institutional Conviction   │
                    │  方向信念 + 因子分解 + 冲突检测       │
                    └─────────────────┬───────────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  Layer 4: AI 主脑（DeepSeek + 知识）  │
                    │  必须被 L3 约束，不能单独拍板          │
                    └─────────────────┬───────────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  Layer 5: 硬门控（机构 kill switch）  │
                    │  置信度 RR 连亏 反事实 知识卡 funding  │
                    └─────────────────┬───────────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  Layer 6: 可执行输出 + 学习闭环       │
                    └─────────────────────────────────────┘
```

**方向研判公式（机构化）**：

```text
最终方向 = f(
  Regime 适配后的策略共识,
  衍生品结构（crowding）,
  AI 推理（带知识注入）,
  多周期结构,
  学习权重（分桶 + 全局）
) → 再经硬门控 → LONG | SHORT | WAIT
```

**WAIT 不是失败** — 机构大量时间 delta-neutral 或 flat；能识别「不该赌方向」本身就是 alpha。

---

## 四、各机构策略在本工具中的映射

| 机构 | 真实逻辑（简化） | 工具内策略 key | Regime 偏好 |
|------|------------------|----------------|-------------|
| **Renaissance** | 短周期 stat arb、均值回归、非线性模式 | `renissance_stat_arb`, BB, RSI | RANGING, LOW_VOL |
| **Citadel** | 多因子动量、跨资产、执行质量 | `citadel_momentum`, EMA/SMA | TRENDING |
| **Bridgewater** | 风险平价、vol targeting | `bridgewater_risk_parity` | HIGH_VOL（缩仓） |
| **AQR** | Value + Momentum 因子组合 | `aqr_value_momentum` | TRENDING, LOW_VOL |
| **Two Sigma** | ML ensemble + alt data | `two_sigma_ml`, DL engine | ALL（需 regime 过滤） |
| **Jump** | 做市、库存、spread | `jump_market_making` | HIGH_VOL, RANGING |
| **Turtle** | 突破趋势跟随 | `turtle_trading` | TRENDING |

StrategyLab 自动发现策略 = 机构「research pod」落盘 — 需 walk-forward + promotion gate 才进主池。

---

## 五、实施路线图（不怕大，但要分层）

### Phase A — 方向研判核心（当前 ~1 个月）

- [x] Regime 分桶学习权重
- [x] 反事实 / 知识 / 模式 硬门控
- [x] **Institutional Conviction Engine**（多因子信念分）
- [ ] Regime 检测 + Funding + MTF 多信号融合
- [ ] GUI 决策驾驶舱（信念分 + 门控 + Agent 票）

### Phase B — 结构 edge（1–2 个月）

- [ ] Funding 拥挤度 → 方向偏置（不只 block long）
- [ ] BTC/BNB 相对强弱因子
- [ ] CircuitBreaker 全链路接入
- [ ] Basis / 期限结构（若接 perp + spot）

### Phase C — 机构 research 工业线（2–3 个月）

- [ ] HMM regime（可选，与规则 regime ensemble）
- [ ] Walk-forward + Monte Carlo promotion
- [ ] 因子 IC 监控 + 自动降权失效因子
- [ ] Web/GUI 统一 Autopilot（一条 pipeline）

---

## 六、成功标准（是否「真能分析走向」）

1. **可解释**：每次 WAIT/LONG/SHORT 能列出 ≥3 条独立证据及冲突项
2. **Regime 一致**：震荡市不会主要由 Turtle/Citadel 动量主导投票
3. **学习有效**：同 regime 下 20+ 样本后，胜率曲线前半 vs 后半可量化
4. **结构感知**：极端 funding 时系统自动降多或 WAIT，且原因可见
5. **可执行**：100% 输出含 entry/SL/TP/仓位/失效条件
6. **可复盘**：每笔可追溯到策略票、Agent 票、门控、信念分

---

## 参考

- [Quant Matter — Institutional Crypto Trading](https://quantmatter.com/institutional-crypto-trading/)
- [Quantt — Crypto Quant Strategies 2026](https://www.quantt.co.uk/resources/crypto-quant-strategies-2026)
- [Regime Detection in Crypto](https://getregime.com/blog/how-crypto-market-regime-detection-works)
- [QuantInsti — Regime Adaptive HMM + RF](https://blog.quantinsti.com/regime-adaptive-trading-python/)
- [Two Sigma — Research-to-Trade Pipeline](https://insights.wisdomchain.com/two-sigma-investments-trading-systems-and-strategies/)
