"""
BNB量化交易工具 - 熔断机制 (Circuit Breaker)
================================================
核心职责：**保护训练成果**，在坏行情里自动停手或缩仓。

触发规则：
┌──────────────────────────┬───────────────────────────────┐
│ 触发条件                  │ 动作                           │
├──────────────────────────┼───────────────────────────────┤
│ 连亏 ≥ 3 笔              │ 仓位缩至 50%                   │
│ 连亏 ≥ 5 笔              │ 禁止开单（只允许 WAIT）         │
│ 24h 累计亏损 > 8% 账户    │ 禁止开单                       │
│ 上一次熔断后 < 冷却时间    │ 保持 WAIT（默认 4h）           │
│ 波动率异常（ATR > 2x 均值）│ 仓位缩至 60%                   │
│ ATR 突变 ≥ 3x 均值        │ 只平不开（STOPPED）             │
│ 价格偏离 MA20 ≥ 15%       │ 只平不开（STOPPED）             │
└──────────────────────────┴───────────────────────────────┘

设计原则：
- 在 trade_advisor 门控后调用 `breaker.check()`；STOPPED 时强制 WAIT
- 波动率/MA 偏离依赖传入的 ATR 均值与 MA20（由 market_regime / indicators 提供）
- 连亏/回撤状态从 paper_trading.db 计算；冷却时间见 risk_state
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 认错/超时等「主动平仓学习」默认不计入连亏（全系统统一）
DEFAULT_CONSEC_IGNORE_REASONS = frozenset({
    "ADMIT_WRONG",
    "TIMEOUT",
    "TIMEOUT_NO_TP",
})


def normalize_consec_ignore_reasons(raw=None) -> set:
    """解析 consec_ignore_reasons 配置；None 时用默认集合。"""
    if raw is None:
        return set(DEFAULT_CONSEC_IGNORE_REASONS)
    return {str(x).strip().upper() for x in raw if str(x).strip()}


def is_ignored_consec_close_reason(reason: Optional[str], ignore: Optional[set] = None) -> bool:
    """平仓原因是否应从连亏统计中跳过。"""
    ignore = ignore if ignore is not None else set(DEFAULT_CONSEC_IGNORE_REASONS)
    return str(reason or "").strip().upper() in ignore


class CircuitBreaker:
    """交易熔断器 — 连亏停手 / 回撤熔断 / 波动率缩仓"""

    def __init__(
        self,
        paper_engine=None,
        config: Optional[Dict] = None,
    ):
        """
        Args:
            paper_engine: PaperTradingEngine 实例（用于读取最近平仓记录）
            config: 可选配置覆盖
        """
        cfg = config or {}
        self._engine = paper_engine
        self.enabled: bool = bool(cfg.get("enabled", True))

        # 连亏阈值
        self.consec_loss_half: int = int(cfg.get("consec_loss_half", 3))
        self.consec_loss_stop: int = int(cfg.get("consec_loss_stop", 5))
        # 认错/超时等「主动平仓学习」不计入连亏（避免惩罚正确行为）
        self.consec_ignore_reasons = normalize_consec_ignore_reasons(
            cfg.get("consec_ignore_reasons")
        )

        # 24h 回撤阈值（占账户余额百分比）
        self.max_daily_drawdown_pct: float = float(cfg.get("max_daily_drawdown_pct", 0.08))
        self.account_balance: float = float(cfg.get("account_balance", 5000.0))

        # 冷却期（小时）：触发 STOP 级别后至少等这么久
        self.cooldown_hours: float = float(cfg.get("cooldown_hours", 4.0))

        # 波动率异常阈值：当前 ATR > 均值 * 此倍数 → 缩仓
        self.volatility_mult_threshold: float = float(cfg.get("volatility_mult_threshold", 2.0))
        self.volatility_position_factor: float = float(cfg.get("volatility_position_factor", 0.6))
        # ATR 突变放大 N 倍 → 只平不开
        self.atr_spike_stop_mult: float = float(cfg.get("atr_spike_stop_mult", 3.0))
        # 价格偏离 MA20 超过该比例 → 只平不开
        self.ma20_dev_stop_pct: float = float(cfg.get("ma20_dev_stop_pct", 0.15))

        # 内部状态
        self._last_stop_time: Optional[float] = None  # Unix timestamp of last STOP trigger
        try:
            from bnb_quant_tool.risk_state import get_circuit_breaker_stop_time
            self._last_stop_time = get_circuit_breaker_stop_time()
        except Exception:
            pass

    # ============================================================
    # 主入口
    # ============================================================
    def check(
        self,
        current_atr: Optional[float] = None,
        avg_atr: Optional[float] = None,
        bnb_risk: Optional[Dict] = None,
        current_price: Optional[float] = None,
        ma20: Optional[float] = None,
    ) -> Dict:
        """检查当前是否应该熔断。

        Args:
            current_atr: 当前 ATR（可选，用于波动率检查）
            avg_atr: 近期平均 ATR（可选）
            bnb_risk: BNB 风控哨兵结果（BNBRiskSentry.fetch_all）
            current_price: 现价（可选，用于 MA20 偏离）
            ma20: 20 日均线（可选）
        """
        if not self.enabled:
            return {
                "allowed": True,
                "position_factor": 1.0,
                "reasons": [],
                "level": "NORMAL",
                "consec_losses": 0,
                "daily_loss_pct": 0.0,
                "disabled": True,
            }

        reasons: List[str] = []
        position_factor = 1.0
        allowed = True
        consec_losses = 0
        daily_loss_pct = 0.0

        # ----- 1. 连亏检查 -----
        # 达停手线时只触发一次冷却；冷却结束后轻仓恢复，避免「永远 STOPPED」死锁
        if self._engine:
            consec_losses = self._get_current_consec_losses()
            if consec_losses >= self.consec_loss_stop:
                in_cd = self._in_cooldown()
                if self._last_stop_time is None:
                    self._trigger_stop()
                    in_cd = True
                if in_cd:
                    allowed = False
                    reasons.append(
                        f"🛑 连续亏损 {consec_losses} 笔 ≥ {self.consec_loss_stop}，强制停手"
                    )
                else:
                    position_factor = min(position_factor, 0.5)
                    reasons.append(
                        f"⚠️ 连亏 {consec_losses} 笔冷却已结束，轻仓恢复交易"
                    )
            elif consec_losses >= self.consec_loss_half:
                position_factor = min(position_factor, 0.5)
                reasons.append(
                    f"⚠️ 连续亏损 {consec_losses} 笔 ≥ {self.consec_loss_half}，仓位减半"
                )

        # ----- 2. 24h 回撤检查 -----
        if self._engine:
            daily_loss_pct = self._get_daily_loss_pct()
            if daily_loss_pct >= self.max_daily_drawdown_pct:
                allowed = False
                reasons.append(
                    f"🛑 24h 累计亏损 {daily_loss_pct:.1%} ≥ {self.max_daily_drawdown_pct:.1%}，"
                    f"触发回撤熔断"
                )
                self._trigger_stop()

        # ----- 3. 冷却期检查 -----
        if self._last_stop_time:
            elapsed_hours = (time.time() - self._last_stop_time) / 3600
            if elapsed_hours < self.cooldown_hours:
                allowed = False
                remaining = self.cooldown_hours - elapsed_hours
                reasons.append(
                    f"⏳ 冷却中（剩余 {remaining:.1f}h），上次熔断于 "
                    f"{datetime.fromtimestamp(self._last_stop_time).strftime('%H:%M')}"
                )

        # ----- 4. 波动率异常 / ATR 突变 -----
        if current_atr and avg_atr and avg_atr > 0:
            ratio = current_atr / avg_atr
            if ratio >= self.atr_spike_stop_mult:
                allowed = False
                reasons.append(
                    f"⚡ ATR 突变 {ratio:.1f}x ≥ {self.atr_spike_stop_mult:.0f}x，只平不开"
                )
                self._trigger_stop()
            elif ratio >= self.volatility_mult_threshold:
                position_factor = min(position_factor, self.volatility_position_factor)
                reasons.append(
                    f"⚡ 波动率异常 (ATR={current_atr:.2f}, 均值={avg_atr:.2f}, "
                    f"比值={ratio:.1f}x)，仓位缩至 {self.volatility_position_factor:.0%}"
                )

        # ----- 4b. 价格偏离 MA20 -----
        if (
            current_price
            and ma20
            and ma20 > 0
            and self.ma20_dev_stop_pct > 0
        ):
            dev = abs(float(current_price) - float(ma20)) / float(ma20)
            if dev >= self.ma20_dev_stop_pct:
                allowed = False
                reasons.append(
                    f"价格偏离 MA20 {dev:.1%} ≥ {self.ma20_dev_stop_pct:.0%}，只平不开"
                )
                self._trigger_stop()

        # ----- 5. BNB 黑天鹅哨兵（仅刑事/清仓级熔断全部方向） -----
        rs = bnb_risk or {}
        swan = rs.get("black_swan") or {}
        if swan.get("emergency_liquidate"):
            allowed = False
            position_factor = 0.0
            reasons.insert(0, f"🚨 黑天鹅熔断: {swan.get('interpretation') or swan.get('headline', '监管风险')}")
            self._trigger_stop()
        elif swan.get("triggered"):
            # 哨兵 high 级仅禁止做多，由 trade_advisor 门控；熔断器不拦截做空
            position_factor = min(position_factor, 0.5)
            reasons.append(
                f"⚠️ 黑天鹅哨兵: {swan.get('headline', '')[:60] or swan.get('interpretation', '')[:60]} "
                f"— 禁止做多（做空不受影响）"
            )
        elif rs.get("funding_extreme", {}).get("extreme"):
            position_factor = min(position_factor, 0.5)
            reasons.append(
                f"⚠️ BNB 资金费率极值 {rs['funding_extreme'].get('rate_pct')}%/8h，仓位减半"
            )

        # ----- 确定级别 -----
        if not allowed:
            level = "STOPPED"
            position_factor = 0.0
        elif position_factor < 1.0:
            level = "REDUCED"
        else:
            level = "NORMAL"

        result = {
            "allowed": allowed,
            "position_factor": position_factor,
            "reasons": reasons,
            "level": level,
            "consec_losses": consec_losses,
            "daily_loss_pct": round(daily_loss_pct, 4),
        }

        if reasons:
            logger.warning(f"[CircuitBreaker] {level}: {'; '.join(reasons)}")
        return result

    # ============================================================
    # 内部方法
    # ============================================================
    def _get_current_consec_losses(self) -> int:
        """从最近平仓记录中计算当前连亏笔数"""
        try:
            closed = self._engine.get_closed_positions(limit=20)
        except Exception:
            return 0
        # closed 已按 id DESC 排序（最新在前）
        consec = 0
        for pos in closed:
            reason = str(pos.get("close_reason") or "").strip().upper()
            if reason in self.consec_ignore_reasons:
                continue  # 跳过认错/超时，不打断也不计入
            pnl = pos.get("realized_pnl_usdt", 0)
            try:
                pnl = float(pnl or 0)
            except Exception:
                pnl = 0.0
            if pnl < 0:
                consec += 1
            else:
                break  # 遇到非亏损就停
        return consec

    def _get_daily_loss_pct(self) -> float:
        """计算过去 24h 的累计亏损占账户余额百分比（统一用 UTC）。"""
        try:
            closed = self._engine.get_closed_positions(limit=100)
        except Exception:
            return 0.0

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        daily_loss = 0.0
        for pos in closed:
            closed_at = str(pos.get("closed_at") or "")
            if closed_at and closed_at >= cutoff:
                pnl = pos.get("realized_pnl_usdt", 0)
                try:
                    pnl = float(pnl or 0)
                except Exception:
                    pnl = 0.0
                if pnl < 0:
                    daily_loss += abs(pnl)
        if self.account_balance <= 0:
            return 0.0
        return daily_loss / self.account_balance

    def _in_cooldown(self) -> bool:
        if not self._last_stop_time:
            return False
        return (time.time() - self._last_stop_time) / 3600 < self.cooldown_hours

    def _trigger_stop(self):
        """记录停手时间（用于冷却期计算）。

        已在冷却期内时不刷新时间戳，避免持续条件把冷却永远往后推。
        冷却结束后再次触发市场级条件（ATR/回撤等）可重新武装。
        """
        if self._in_cooldown():
            return
        self._last_stop_time = time.time()
        try:
            from bnb_quant_tool.risk_state import set_circuit_breaker_stop_time
            set_circuit_breaker_stop_time(self._last_stop_time)
        except Exception:
            pass

    def reset_cooldown(self):
        """手动重置冷却期（比如用户主动确认后）"""
        self._last_stop_time = None
        try:
            from bnb_quant_tool.risk_state import clear_circuit_breaker_stop_time
            clear_circuit_breaker_stop_time()
        except Exception:
            pass
        logger.info("[CircuitBreaker] 冷却期已手动重置")

    # ============================================================
    # 格式化输出（GUI 用）
    # ============================================================
    def format_status(self) -> str:
        """返回当前熔断状态的人可读文本"""
        result = self.check()
        if result["level"] == "NORMAL":
            return "✅ 熔断器正常 — 可正常交易"
        elif result["level"] == "REDUCED":
            lines = [f"⚠️ 仓位缩减 (×{result['position_factor']:.0%})"]
            for r in result["reasons"]:
                lines.append(f"  {r}")
            return "\n".join(lines)
        else:  # STOPPED
            lines = ["🛑 已熔断 — 禁止开新单"]
            for r in result["reasons"]:
                lines.append(f"  {r}")
            return "\n".join(lines)
