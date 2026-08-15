# Bug 修复报告

**日期**: 2026-08-03  
**修复人**: 资深开发工程师  
**文件**: paper_trading.py, circuit_breaker.py, ai_learning_system.py

---

## 修复清单

### Bug 1 (P0): 并发平仓竞态 — 幽灵 PnL
**文件**: `src/bnb_quant_tool/paper_trading.py` — `_do_partial_close()`

**问题**: Watcher 线程和手动平仓同时操作同一仓位时，`_do_partial_close` 用传入的 `qty` 算 PnL，但此时仓位可能已被另一线程平掉（qty_remaining=0），导致幽灵盈亏被加到 `realized_pnl_usdt` 上。

**修复**:
```python
# 在读取仓位后，用实际剩余 qty 计算
qty = min(qty, r["qty_remaining"])
if qty <= 0:
    return
```

---

### Bug 2 (P0): 滑点模拟是死代码
**文件**: `src/bnb_quant_tool/paper_trading.py` — `open_from_advice()`, `_do_partial_close()`

**问题**: `_apply_slippage()` 方法已实现完整的 ATR 动态滑点模型（0.1%~0.5%），但从未被调用。所有成交按精确价格执行，模拟盘系统性高估 PnL。

**修复**:
- 开仓时: `entry = self._apply_slippage(entry, side, is_open=True)`
- 平仓时: `price = self._apply_slippage(price, side, is_open=False)`

---

### Bug 3 (P0): 插针过滤是死代码
**文件**: `src/bnb_quant_tool/paper_trading.py` — `_evaluate_position()`

**问题**: `_pin_confirmed()` 实现了「首次触及后需持续 N 秒才触发」的插针保护，但从未被调用。交易所常见的 1-3 秒瞬时插针直接触发止损。

**修复**: 将所有 `_sl_touched()` / `_tp_touched()` 调用包装为 `_pin_confirmed()`:
```python
if self._pin_confirmed(f"SL_{pid}", self._sl_touched(side, price, sl)):
if self._pin_confirmed(f"TP1_{pid}", self._tp_touched(side, price, tp1)):
if self._pin_confirmed(f"TP2_{pid}", self._tp_touched(side, price, tp2)):
if self._pin_confirmed(f"TP3_{pid}", self._tp_touched(side, price, tp3)):
```

---

### Bug 4 (P1): `_mark_closed` 无状态守卫
**文件**: `src/bnb_quant_tool/paper_trading.py` — `_mark_closed()`

**问题**: `_mark_closed` 不检查仓位是否已 CLOSED，并发调用会重复标记、重复触发 post-close hooks（学习管道、信号回填、AI 复盘）。

**修复**:
1. 进入时检查状态: `if existing[0] != STATUS_OPEN: return`
2. UPDATE 加条件: `WHERE id=? AND status=?` (STATUS_OPEN)

---

### Bug 5 (P1): 胜率分母错误（含 BREAK_EVEN）
**文件**: `src/bnb_quant_tool/paper_trading.py` — `get_stats()`, `_compute_breakdown._agg()`  
**文件**: `src/bnb_quant_tool/ai_learning_system.py` — `_update_strategy_performance()`

**问题**: 胜率 = wins / total_closed，但 total_closed 包含 BREAK_EVEN 交易，导致胜率被系统性低估。

**修复**:
- paper_trading: `win_rate = len(wins) / (len(wins) + len(losses))`
- ai_learning: BREAK_EVEN 时撤销 `total_predictions` 预增:
  ```python
  if verdict is None:
      cur.execute("UPDATE strategy_performance SET total_predictions = MAX(0, total_predictions - 1) ...")
      continue
  ```

---

### Bug 6 (P1): 保本计为亏损
**文件**: `src/bnb_quant_tool/circuit_breaker.py` — `_get_current_consec_losses()`  
**文件**: `src/bnb_quant_tool/paper_trading.py` — `_update_consec_losses()`, `get_stats()`

**问题**: BREAK_EVEN 仓位的 `realized_pnl_usdt` 因手续费而为微小负值（如 -0.02 USDT），`pnl < 0` 判定将其计为亏损，导致连亏虚高、熔断器误触发。

**修复**: 用手续费阈值区分真实亏损和保本:
```python
notional = entry * qty
fee_threshold = max(notional * 0.0015, 0.01)  # 0.15% 或至少 0.01 USDT
if pnl < -fee_threshold:
    consec += 1      # 真实亏损
elif pnl > fee_threshold:
    break            # 真实盈利
# else: 保本 — 不增加连亏也不终止
```

---

### Bug 7 (P1): 权重归一化不全
**文件**: `src/bnb_quant_tool/ai_learning_system.py` — `_trigger_learning_optimization()`

**问题**: 只对 `total_predictions >= 3` 的策略做归一化，样本不足的策略保留旧权重（默认 1.0），导致所有策略权重之和 > 1.0。

**修复**: 查询所有活跃策略，样本不足的给默认权重，全部一起归一化:
```python
# 查询所有活跃策略（不再只查 >= 3 样本的）
WHERE is_active = 1 AND ...

# 样本不足的策略给默认权重
if total >= 3:
    new_weights[name] = win_factor * sample_factor
else:
    new_weights[name] = max(0.1, float(old_w or 0)) if old_w else 0.5

# 所有策略一起归一化
total_w = sum(new_weights.values())
for name, w in new_weights.items():
    normalized = w / total_w
```

---

## 影响范围

| 修改文件 | 修改方法 |
|---------|---------|
| paper_trading.py | `_do_partial_close`, `open_from_advice`, `_evaluate_position`, `_mark_closed`, `_update_consec_losses`, `get_stats`, `_compute_breakdown` |
| circuit_breaker.py | `_get_current_consec_losses` |
| ai_learning_system.py | `_update_strategy_performance`, `_trigger_learning_optimization` |

## 注意事项

1. **滑点和插针过滤现在生效了** — 新的模拟盘数据会比之前更真实（PnL 可能降低 0.2%-1%），之前的策略权重和学习结论可能需要重新校准
2. **BREAK_EVEN 不再污染统计** — 胜率会上升，连亏会下降，这是正确的
3. **`data/src/` 目录有同样的代码副本但未被运行时使用** — 如需同步可后续处理
4. **建议**: 修复后跑一轮新模拟盘，对比修复前后的统计数据差异
