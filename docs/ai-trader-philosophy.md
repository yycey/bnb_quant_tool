# 自进化 AI 交易员 — 核心原则

目标：感知 → 决策 → 执行 → 反思 → 记忆 闭环，把每笔交易变成成长养分。
进化成「经验丰富的交易专家」靠半年以上持续迭代；优势是无贪婪/恐惧/侥幸、100% 执行规则、永不停止复盘。

## 北星（用户校准）：多开多平 · 认错学习 · 提胜率

1. **多开多平**：没有成交就没有标签；验证剖面下小仓高频开平，把想法变成可学样本  
2. **方向错了就认**：逆向达到阈值 / MTF 反向 / 数小时未止盈 → 尽快平仓（`ADMIT_WRONG` / `TIMEOUT_NO_TP`），禁止死扛抬风险  
3. **AI 持续成长**：每笔平仓必走学习管道 → 知识卡 / validation_log / 本地成长课；胜率是学习结果，不是开仓前的借口  
4. **本地很强**：纸面库 + 知识卡 + 本地 DQN 影子 + K 线归档驱动进化  

硬超时 ≤48h；软未止盈默认 6h；认错默认 ≥30min 且逆向 ≥0.35R。

### 持续开平验证（默认 `trading_profile: validation`）

**没有开平仓就没有收益，也无法验证想法对错。**

- 软门控拦下时 → **validation_probe 小仓**（硬拦仍生效：熔断/反记忆/叠仓等）  
- 开仓记假设 → `data/validation_log.jsonl`；平仓标 **正确/错误**  
- 看板：`data/scoreboard.json` → `validation.accuracy_pct`  
- 目标开仓密度：`target_opens_per_day`（验证剖面默认 ≥2）

配置：`validation_trading.*` / `trading_profiles.validation` / `paper_trading.admit_wrong` / `soft_exit`

## 运行模式

| 模式 | 配置 | 用途 |
|------|------|------|
| **验证学习（当前默认）** | `trading_profile: validation` | 多开多平、认错短持、攒样本提胜率 |
| 盈利优先 | `trading_profile: production` | 严门控、少开单，盯自动单 **E[R]** |
| 探索 | `trading_profile: explore` | 学习期探针全开 |

主指标（验证阶段）：**开平密度、validation 正确率、自动单 E[R]**。  
胜率通过「认错+复盘」逐步抬升，不以「怕错」停手。

### 7 天验收口径

自动单累计 ≥15 笔后：

1. **E[R] > 0**（或验证正确率稳步上行）
2. `|avg_loss_r|` 不再约等于 4× `avg_win_r`
3. `gave_back`（MFE≥0.5R 最终亏）占比下降
4. **错单平均持仓明显短于对单**（认错机制生效）

未达标 → 下一刀：regime 禁多 / 提高三家综合一致率门槛；**不加第 4 家 AI**。

## 最大化用 AI（决策质量，不是家数）

| 场景 | AI 用法 |
|------|---------|
| 主决策（可能开仓） | 由各家 `enabled` 开关控制；**当前仅豆包（volcengine）**；DeepSeek/千问默认关 |
| 交易员议会 | 跟启用的分析家（当前豆包） |
| 平静观望 WAIT | 允许知识复用跳过 LLM；超过 `reuse_stale_refresh_hours` 或连续复用≥N → 强制刷新 |
| 开仓方向 LONG/SHORT | **禁止**跳过 LLM；必须新鲜分析；可复用历史 SL/TP 参数 |
| LONG 加严 | `long_min_confidence` / `long_min_net_rr`（历史 LONG 弱于 SHORT） |
| 跟单冷却 | 同 symbol+side ≥ `open_follow_cooldown_minutes`（验证剖面默认 5） |
| 平仓反思 | `learning.use_ai_extract_on_close=true` → 大模型提炼教训入记忆 |
| 盯盘扫盘 | 规则轻量感知，大波动再触发全自动分析 |

开关：`deepseek.enabled` / `qianwen.enabled` / `volcengine.enabled`。

## 出场：认错 + MFE 锁盈 + 超时

1. **ADMIT_WRONG**：持仓≥30min，MAE/浮亏 ≥0.35R，或 MTF 明确反向且浮盈不高 → 市价认错  
2. **TIMEOUT_NO_TP**：≥6h 未触 TP1 → 软平  
3. **TIMEOUT**：≥48h 硬平  
4. **mfe_lock**：MFE≥0.6R 保本；≥1.0R 锁 0.3R；≥1.5R 锁 0.7R  
5. TP1/TP2/TP3 分批仍为第二层  

## 决策归属（避免叠乘冲突）

| 关注点 | 所有者 |
|--------|--------|
| 最终方向 | `follow_ai_direction=true` → Advisor/AI；议会/风控只能否决为 WAIT，禁止改向 |
| 最终仓位 | Advisor 基础 → 熔断 → 置信度 → Kelly → 议会 size → 硬顶；`max_open_positions=1` |
| 盈亏比硬拦 | 净 RR（`min_net_rr`）；LONG 另加 `long_min_net_rr` |
| 叠仓 | 同品种已有 OPEN → 拒开 |
| 学习奖励 | 超额收益 vs 持有 BNB；`beta`/小噪音不抬 Agent/议会权重 |

## 分层记忆

1. 热层：现价/新闻/情绪 TTL 缓存 + 近 7 天高置信知识卡内存热缓存  
2. 交易层：`paper_trading.db` 开平仓与信号追踪  
3. 学习层：`ai_learning.db` 分析记录、反馈、策略权重  
4. 知识层：能力卡片 + 向量检索（局面复用 / 反记忆 / 时间衰减）  
5. 议会层：`trader_memory.db` 交易员胜负与教训  

## 多智能体分工

- 感知：行情 / 多周期 / 新闻 / 链上 / 宏观 / 扫盘  
- 决策：LLM（路由）+ 机构策略 + 议会（仓位系数）+ 门控  
- 执行：模拟盘跟单（纪律优先）+ 认错短持 + MFE 锁盈  
- 反思：平仓学习 + 软反馈 + 批量 AI 反思 + 反事实超额  
- 记忆：卡片强化 / 议会权重 / 经验注入下一轮提示词  

## 不做什么

- 不因「省钱」在异动/高机会时跳过 AI  
- 不因「不够强」再加第 4 家 LLM  
- 不把学习期和盈利模式同时打开  
- 不让复用串票污染议会胜负  
- 不让议会在 follow_ai 模式下改写开仓方向  
- 不用情绪化改规则；规则只通过复盘与配置进化  
- 不以含 MANUAL 的总 PnL / 胜率作为主验收指标  
- **不因怕错而停手**；错了就平、就学，再用下一笔验证  

配置入口：`config.yaml` → `llm` / `ai_trading` / `paper_trading.admit_wrong` / `soft_exit` / `position_reeval` / `kelly` / `capability_memory` / `learning` / `intelligence_loop` / `autopilot`
