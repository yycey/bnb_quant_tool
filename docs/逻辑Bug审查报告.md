# BNB 量化交易工具 — 逻辑 Bug 深度审查报告

> 审查范围：`paper_trading.py`、`trade_advisor.py`、`ai_learning_system.py`、`circuit_breaker.py`、`headless_runner.py`、`autopilot_daemon.py` 等核心业务代码
> 审查重点：交易逻辑正确性、并发安全、学习系统数据准确性、PnL 计算合理性

---

## P0 — 严重 Bug（直接导致模拟盘数据失真）

### Bug 1: 并发平仓竞态条件 — 幽灵 PnL

**文件**: `paper_trading.py` — `_do_partial_close()` (L689) + `_evaluate_position()` (L597)

**问题**:
`_evaluate_position` 在 `tick()` 中读取持仓列表后释放锁，然后对每个持仓调用 `_evaluate_position`。`_do_partial_close` 内部重新获取锁并从 DB 读取最新持仓数据，但 **传入的 `qty` 参数来自外层过期的读取**。

```
时序：
1. Watcher 线程: tick() 读取仓位 qty_remaining=10
2. GUI 线程: close_manual() 平掉全部 10 个，qty_remaining=0
3. Watcher 线程: _do_partial_close(pid, 10, price, "SL")
   → 内部读到 qty_remaining=0，但 pnl = (price-entry)*10 - fee  ← 幽灵 PnL！
   → new_realized = old_pnl + phantom_pnl  ← 统计被污染
```

**影响**: 已平仓位被重复计入 PnL，导致：
- 累计盈亏统计错误
- 胜率/盈亏比失真
- 连亏统计错误 → 熔断器误触发或漏触发
- 下游学习系统（策略权重、Pattern Memory）基于错误数据训练

**修复方案**:
```python
# _do_partial_close 中，读取最新数据后 cap qty
r = dict(r)
qty = min(qty, r["qty_remaining"])  # ← 加这一行
if qty <= 0:
    return
```

---

### Bug 2: 滑点模拟是死代码 — 所有成交按精确价格执行

**文件**: `paper_trading.py` — `_apply_slippage()` (L190)

**问题**:
`_apply_slippage` 方法完整实现了基于 ATR 的动态滑点模型（0.1%~0.5%），但 **从未被任何代码调用**。`open_from_advice` 和 `_do_partial_close` 直接使用原始价格成交。

```python
# 搜索结果：_apply_slippage 只出现在定义处，无任何调用
# grep -rn "_apply_slippage" src/ → 仅 L190 定义
```

**影响**:
- 止损按精确 SL 价格成交 → 实际应更差（滑点导致更大亏损）
- 止盈按精确 TP 价格成交 → 实际应更差（滑点导致更少盈利）
- 模拟盘系统性高估 PnL，在高波动市场偏差可达 0.5%~1%
- 所有基于模拟盘数据的学习（策略权重、AI 复盘、Pattern Memory）都被污染

**修复方案**:
```python
# open_from_advice 中，入场时应用滑点
entry = self._apply_slippage(entry, side, is_open=True)

# _do_partial_close 中，平仓时应用滑点
close_price = self._apply_slippage(price, side, is_open=False)
```

---

### Bug 3: 插针过滤是死代码 — 闪崩插针直接触发止损

**文件**: `paper_trading.py` — `_pin_confirmed()` (L212)

**问题**:
`_pin_confirmed` 方法实现了「首次触及后需持续 N 秒仍触及才触发」的插针保护逻辑，`_pending_triggers` 字典也已初始化，但 **从未被任何代码调用**。`_evaluate_position` 中的 SL/TP 判定直接使用 `_sl_touched` / `_tp_touched` 的瞬时判定。

**影响**:
- 交易所常见的瞬时插针（1-3 秒内打穿止损又快速回弹）在模拟盘中会直接触发止损
- 实盘中这类插针经常导致不必要的止损出局
- 模拟盘的止损触发率被高估，止盈达成率被低估
- 基于「止损触发太频繁」的结论去放宽止损，实际上是在解决一个不存在的问题

**修复方案**:
```python
# _evaluate_position 中，SL/TP 判定改为插针过滤
if self._pin_confirmed(f"sl_{pid}", self._sl_touched(side, price, sl)):
    # 触发止损
```

---

## P1 — 高优先级 Bug（学习数据/统计偏差）

### Bug 4: `_mark_closed` 无状态守卫 — 可覆盖已平仓位

**文件**: `paper_trading.py` — `_mark_closed()` (L746)

**问题**:
```python
cur.execute(
    "UPDATE paper_positions SET status=?, closed_at=?, close_avg_price=?, "
    "close_reason=?, r_multiple=? WHERE id=?",
    (STATUS_CLOSED, now, close_price, reason, r_mult, pid)
)
```

UPDATE 语句没有 `WHERE status='OPEN'` 条件。如果仓位已被另一个线程关闭，此方法会覆盖 `close_reason`、`close_avg_price`、`r_multiple`。

**影响**: 交易历史被篡改，平仓原因和价格可能与实际不符。

**修复方案**:
```python
"WHERE id=? AND status=?"",  (STATUS_CLOSED, now, close_price, reason, r_mult, pid, STATUS_OPEN)
```

---

### Bug 5: 策略胜率分母包含 BREAK_EVEN — 胜率被系统性低估

**文件**: `ai_learning_system.py`

**问题**:
1. `_record_strategy_predictions()` (L1228): 每次分析记录时，对每个策略 `total_predictions += 1`
2. `_update_strategy_performance()` (L1603): 反馈时，BREAK_EVEN 的 `is_strategy_signal_correct` 返回 `None`，直接 `continue`，不增加 `correct_predictions`
3. `win_rate = correct_predictions / total_predictions` (L1670)

```
实际场景：策略做 10 次预测 → 5 WIN(3对) + 3 LOSS(2错) + 2 BREAK_EVEN(跳过)
正确胜率 = 3/(3+2) = 60%
当前计算 = 3/10 = 30%  ← 严重低估！
```

**影响**:
- 策略胜率被低估 → 权重计算偏向保守 → 好策略被压低权重
- `_trigger_learning_optimization` 中 `effective_wr < 0.30` 会将权重设为 0.05
- 软反馈大量产生 BREAK_EVEN（`drain_soft_analysis_feedback`），加剧这个问题

**修复方案**:
```python
# 方案A: BREAK_EVEN 不计入 total_predictions
# 在 _record_strategy_predictions 中不递增，在 _update_strategy_performance 中根据 verdict 递增

# 方案B: win_rate 分母排除 BREAK_EVEN
"win_rate = CAST(correct_predictions AS REAL) / CAST(MAX(total_predictions - break_even_count, 1) AS REAL)"
```

---

### Bug 6: 保本交易被计为亏损 — 连亏统计虚高

**文件**: `paper_trading.py` — `open_from_advice()` (L544) + `_update_consec_losses()` (L789)

**问题**:
开仓时 `realized_pnl_usdt = -open_fee`（扣除开仓手续费）。如果仓位保本平仓（close_price = entry），则：
```
realized_pnl_usdt = -open_fee + ((entry - entry) * qty - close_fee) = -open_fee - close_fee
```
这是负数，`_update_consec_losses` 中 `if r[0] < 0: consec += 1` 会将其计为亏损。

**影响**:
- 保本平仓的交易被计为亏损 → 连亏次数虚高
- 可能导致熔断器在 `consec_loss_stop=5` 时提前触发，阻止正常开仓
- 在手续费 0.04% 双边下，每笔保本交易贡献 -0.08% 的「亏损」

**修复方案**:
```python
# _update_consec_losses 中，用绝对值阈值区分真亏损和手续费噪音
if r[0] < -0.01:  # 超过 0.01 USDT 才算真亏损
    consec += 1
else:
    break
```

---

### Bug 7: 策略权重归一化不完整 — 权重之和不等于 1

**文件**: `ai_learning_system.py` — `_trigger_learning_optimization()` (L1677)

**问题**:
```python
cur.execute("""SELECT ... FROM strategy_performance
    WHERE total_predictions >= 3 AND is_active = 1 ...""")
# 只对 total_predictions >= 3 的策略重新计算权重并归一化
# total_predictions < 3 的策略保持原权重不变
```

归一化只覆盖部分策略，未被归一化的策略保持旧权重。13 个策略中如果只有 8 个达到 3 次预测，归一化后这 8 个的权重之和为 1，但另外 5 个的权重仍然是初始值 1/13。总权重 > 1。

**影响**:
- 投票得分 `long_score` / `short_score` 被系统性放大
- 方向阈值 `direction_vote_threshold` 的实际效果偏离设计意图
- 可能导致更多 WAIT 信号通过门控（或反之）

**修复方案**:
```python
# 归一化所有活跃策略，不只是 >= 3 的
all_active = cur.execute("SELECT strategy_name FROM strategy_performance WHERE is_active=1").fetchall()
for name, in all_active:
    if name not in new_weights:
        new_weights[name] = 0.05  # 低样本策略给最小权重
total_w = sum(new_weights.values())
# 然后归一化
```

---

## P2 — 中等优先级 Bug（边界条件/线程安全）

### Bug 8: `_consec_losses` 线程不安全

**文件**: `paper_trading.py` — `_update_consec_losses()` (L789)

`_update_consec_losses` 在 `_post_close_hooks` 的 daemon 线程中执行，读写 `self._consec_losses` 时未持有 `self._lock`。多个仓位同时平仓时可能产生竞态。

**影响**: 连亏计数可能不准确，但影响有限（daemon 线程执行很快）。

---

### Bug 9: 仓位截断后 `risk_amount` 未重算

**文件**: `trade_advisor.py` — `_calc_position()` (L1630)

```python
cap_amount = self.account_balance * self.max_position_pct
if usdt_amount > cap_amount:
    usdt_amount = cap_amount
    quantity = usdt_amount / entry
    # ← risk_amount 没有重算！返回的还是截断前的值
```

**影响**: `risk_amount` 字段显示的风险金额大于实际风险，可能误导用户。但实际开仓用的是 `quantity`（已截断），不影响交易执行。

---

### Bug 10: 超时平仓可能使用过期缓存价格

**文件**: `paper_trading.py` — `_check_position_timeout()` (L1470) + `data_fetcher.py` — `get_price_with_fallback()` (L584)

`get_price_with_fallback` 有 5 秒价格缓存。超时平仓调用 `self._price_provider(sym)` 时，可能返回 5 秒前的价格。如果市场在此 5 秒内大幅波动，平仓价格会偏离实际。

**影响**: 24 小时超时平仓的偏差通常很小（5 秒缓存），但在极端行情下可能有 1-2% 偏差。

---

## 逻辑设计问题（非 Bug，但值得关注）

### Issue A: SL 优先于 TP 的单一价格判定

**文件**: `paper_trading.py` — `_evaluate_position()` (L597)

当前逻辑用一个价格点同时检查 SL 和 TP。如果一根 K 线的高低点同时穿越 SL 和 TP（剧烈波动），无法确定哪个先触发。

**建议**: 如果使用 K 线数据，应分别用 high 和 low 判定 TP 和 SL：
- LONG: TP 用 high 判定，SL 用 low 判定
- 如果同一根 K 线 high >= TP 且 low <= SL，默认 SL 先触发（保守原则）

### Issue B: TP1 后 SL 移到入场价，同一 tick 不重新检查

**文件**: `paper_trading.py` — `_evaluate_position()` (L624-636)

TP1 触发后 SL 移到入场价（保本），但代码继续检查 TP2/TP3 而不重新检查新 SL。如果价格在 TP1 附近波动，新 SL 不会被同一 tick 触发。

**影响**: 极端情况下（TP1 后立即反转），保本止损会延迟到下一个 tick 触发。

### Issue C: 软反馈大量产生 BREAK_EVEN 但仍计入 total_predictions

**文件**: `ai_learning_system.py` — `drain_soft_analysis_feedback()` (L627)

WAIT/HOLD 分析的超时软反馈几乎全部标记为 BREAK_EVEN，这些记录的 `submit_feedback` 会触发 `_update_strategy_performance`，但因为 `is_strategy_signal_correct` 对 BREAK_EVEN 返回 None，策略的 `correct_predictions` 不增加但 `total_predictions` 已在分析时递增。

这与 Bug 5 是同一问题的不同表现。

---

## 修复优先级建议

| 优先级 | Bug | 修复难度 | 影响面 |
|--------|-----|----------|--------|
| **P0** | Bug 1: 并发平仓竞态 | 1 行代码 | 所有统计/学习数据 |
| **P0** | Bug 2: 滑点死代码 | 2 行代码 | 模拟盘 PnL 准确性 |
| **P0** | Bug 3: 插针过滤死代码 | 4 行代码 | SL/TP 触发准确性 |
| **P1** | Bug 4: mark_closed 无守卫 | 1 行代码 | 交易历史完整性 |
| **P1** | Bug 5: 胜率分母错误 | 中等 | 策略权重/学习 |
| **P1** | Bug 6: 保本计为亏损 | 1 行代码 | 连亏/熔断器 |
| **P1** | Bug 7: 权重归一化不全 | 中等 | 投票得分 |
| **P2** | Bug 8-10 | 低 | 边界情况 |

---

## 总结

最关键的 3 个问题：

1. **Bug 1（并发竞态）**— 一行代码修复，但如果不修，所有模拟盘数据都不可信
2. **Bug 2+3（滑点/插针死代码）**— 模拟盘环境过于理想化，导致学习系统在「假数据」上训练，实盘效果会大打折扣
3. **Bug 5+6（学习数据偏差）**— 策略胜率被低估 + 连亏被高估，好策略被压权重，熔断器提前触发

这三个问题形成了一条**错误传播链**：
```
滑点/插针缺失 → 模拟盘 PnL 失真 → 策略胜率偏差 → 权重计算错误 → 投票方向偏差 → 开单决策错误
                                                    ↘ 连亏虚高 → 熔断器误触发 → 错过交易机会
```

建议按 P0 → P1 顺序修复，每个 Bug 修复后重新跑一轮模拟盘验证数据合理性。
