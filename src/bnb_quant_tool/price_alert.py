"""
BNB量化交易工具 - 价格预警 (Price Alert)
==========================================
作用：后台轮询币安最新价格，在以下时刻触发本地弹窗：
- 价格进入入场区间 (entry_zone)
- 价格跌破止损 (stop_loss)
- 价格触及任一止盈档 (tp1 / tp2 / tp3)
- 价格突破有效期取消条件

设计：
- 独立线程，不阻塞主 GUI
- 通过 callback 通知前端（GUI 用 root.after 显示对话框）
- 每个 alert 触发一次后默认禁用，避免反复响铃
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class PriceRule:
    """单条价格规则"""
    rule_id: str
    name: str            # 显示用名字，如 "入场区间下沿"
    direction: str       # 'cross_below' / 'cross_above' / 'enter_range'
    target: float        # 单点价格（cross 用）
    range_high: float = 0.0  # 区间上限（enter_range 用）
    triggered: bool = False
    trigger_time: str = ""
    trigger_price: float = 0.0


class PriceAlertEngine:
    """后台轮询价格 + 触发回调"""

    BINANCE_TICKER_URL = "https://api.binance.me/api/v3/ticker/price"

    def __init__(self, symbol: str = "BNBUSDT", poll_interval: float = 15.0):
        self.symbol = symbol
        self.poll_interval = poll_interval
        self.rules: List[PriceRule] = []
        self.callback: Optional[Callable[[PriceRule, float], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_price: float = 0.0
        self._lock = threading.Lock()

    # ============================================================
    # 规则管理
    # ============================================================
    def set_callback(self, cb: Callable[[PriceRule, float], None]):
        self.callback = cb

    def clear_rules(self):
        with self._lock:
            self.rules = []

    def add_rule(self, rule: PriceRule):
        with self._lock:
            self.rules.append(rule)

    def remove_rule(self, rule_id: str) -> bool:
        """按 rule_id 删除一条规则；未找到返回 False。"""
        rid = str(rule_id or "")
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if str(getattr(r, "rule_id", "")) != rid]
            return len(self.rules) < before

    def load_from_advice(self, advice: Dict):
        """从 trade_advisor 输出的 advice 自动生成规则集"""
        self.clear_rules()
        if not advice or advice.get("action") not in ("LONG", "SHORT"):
            return
        prices = advice.get("prices") or {}
        action = advice["action"]
        # 入场区间提醒
        elow = prices.get("entry_low")
        ehigh = prices.get("entry_high")
        if elow and ehigh:
            self.add_rule(PriceRule(
                rule_id="ENTRY",
                name=f"进入入场区间 [{elow}~{ehigh}]",
                direction="enter_range",
                target=float(elow),
                range_high=float(ehigh),
            ))
        # 止损
        sl = prices.get("stop_loss")
        if sl:
            self.add_rule(PriceRule(
                rule_id="SL",
                name=f"触及止损 {sl}",
                direction="cross_below" if action == "LONG" else "cross_above",
                target=float(sl),
            ))
        # 三档止盈
        for key in ("tp1", "tp2", "tp3"):
            tp = prices.get(key)
            if tp:
                self.add_rule(PriceRule(
                    rule_id=key.upper(),
                    name=f"达到止盈 {key.upper()} = {tp}",
                    direction="cross_above" if action == "LONG" else "cross_below",
                    target=float(tp),
                ))

    # ============================================================
    # 启动/停止
    # ============================================================
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"PriceAlertEngine started: {self.symbol}, interval={self.poll_interval}s")

    def stop(self):
        self._running = False
        logger.info("PriceAlertEngine stopped")

    def is_running(self) -> bool:
        return self._running

    @property
    def last_price(self) -> float:
        return self._last_price

    # ============================================================
    # 主循环
    # ============================================================
    def _loop(self):
        while self._running:
            try:
                price = self._fetch_price()
                if price > 0:
                    self._check_rules(price)
                    self._last_price = price
            except Exception as e:
                logger.warning(f"PriceAlert poll error: {e}")
            # sleep with quick exit support
            for _ in range(int(max(1, self.poll_interval * 2))):
                if not self._running:
                    return
                time.sleep(0.5)

    def _fetch_price(self) -> float:
        try:
            r = requests.get(self.BINANCE_TICKER_URL, params={"symbol": self.symbol}, timeout=8)
            r.raise_for_status()
            return float(r.json().get("price", 0))
        except Exception as e:
            logger.debug(f"Fetch price error: {e}")
            return 0.0

    def _check_rules(self, price: float):
        with self._lock:
            rules = list(self.rules)
        prev = self._last_price if self._last_price > 0 else price
        for rule in rules:
            if rule.triggered:
                continue
            triggered = False
            if rule.direction == "cross_above":
                if prev < rule.target <= price:
                    triggered = True
            elif rule.direction == "cross_below":
                if prev > rule.target >= price:
                    triggered = True
            elif rule.direction == "enter_range":
                if rule.target <= price <= rule.range_high:
                    triggered = True

            if triggered:
                rule.triggered = True
                rule.trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rule.trigger_price = price
                logger.info(f"⚠ Price alert triggered: {rule.name} @ {price}")
                if self.callback:
                    try:
                        self.callback(rule, price)
                    except Exception as e:
                        logger.error(f"Alert callback error: {e}")

    # ============================================================
    # 状态查询
    # ============================================================
    def get_rules_status(self) -> List[Dict]:
        with self._lock:
            return [
                {
                    "id": r.rule_id, "name": r.name,
                    "target": r.target, "range_high": r.range_high,
                    "direction": r.direction,
                    "triggered": r.triggered,
                    "trigger_time": r.trigger_time,
                    "trigger_price": r.trigger_price,
                }
                for r in self.rules
            ]
