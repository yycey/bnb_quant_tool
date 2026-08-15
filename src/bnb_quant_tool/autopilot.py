"""
Unified Autopilot — 统一定时分析 / AI 全自动 / 信号扫描触发。

模式:
- off: 全部关闭
- scheduled: 仅定时触发 start_analysis
- fullauto: 循环完整决策 + 自动开仓
- unified: 扫描强信号触发单次完整分析；定时/全自动共用同一冷却
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SOURCE_SCHEDULED = "scheduled"
SOURCE_FULLAUTO = "fullauto"
SOURCE_SCANNER = "scanner"
SOURCE_MANUAL = "manual"


class AutopilotController:
    """协调三条自动化链路的触发与冷却。"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._lock = threading.Lock()
        self._last_trigger_ts = 0.0
        self._scanner_pending = False

    def reload_config(self, config: Dict) -> None:
        self.config = config or {}

    def _cfg(self) -> Dict[str, Any]:
        ap = self.config.get("autopilot") or {}
        legacy_fullauto = bool(self.config.get("auto_run", {}).get("enabled"))
        mode = str(ap.get("mode") or "legacy").lower()
        if mode == "legacy":
            mode = "fullauto" if legacy_fullauto else "off"
        return {
            "mode": mode,
            "interval_minutes": int(
                ap.get("interval_minutes")
                or self.config.get("auto_run", {}).get("interval_minutes", 60)
            ),
            "scanner_triggers_analysis": bool(
                ap.get("scanner_triggers_analysis", True)
            ),
            "min_trigger_interval_sec": float(
                ap.get("min_trigger_interval_sec")
                or self.config.get("signal_scanner", {}).get("min_trigger_interval", 120)
            ),
            "respect_analysis_mode": bool(ap.get("respect_analysis_mode", True)),
            "open_paper_on_fullauto": bool(
                ap.get("open_paper_on_fullauto", True)
            ),
        }

    def can_trigger(self, source: str = SOURCE_SCANNER) -> bool:
        cfg = self._cfg()
        now = time.time()
        min_iv = cfg["min_trigger_interval_sec"]
        if now - self._last_trigger_ts < min_iv:
            return False
        if source == SOURCE_SCANNER and not cfg["scanner_triggers_analysis"]:
            return False
        return True

    def mark_triggered(self) -> None:
        self._last_trigger_ts = time.time()

    def request_scanner_cycle(
        self,
        *,
        strength: float,
        min_strength: float,
        on_trigger: Callable[[], None],
        on_skip: Optional[Callable[[str], None]] = None,
        signal_type: str = "",
    ) -> bool:
        """扫描器信号 → 单次完整分析（不重复启动 fullauto 线程）。"""
        cfg = self._cfg()
        if cfg["mode"] not in ("unified", "fullauto", "legacy"):
            if on_skip:
                on_skip("autopilot 模式未启用扫描触发")
            return False
        # 可选：仅大波动类型放行（与 signal_scanner.trigger_on_big_move_only 对齐）
        sc = self.config.get("signal_scanner") or {}
        if bool(sc.get("trigger_on_big_move_only", True)) and signal_type:
            allowed = {
                str(t).strip().upper()
                for t in (sc.get("trigger_signal_types") or [
                    "PRICE_SHOCK", "ATR_SPIKE", "VOLUME_SPIKE", "BREAKOUT"
                ])
            }
            if str(signal_type).upper() not in allowed:
                if on_skip:
                    on_skip(f"非大波动信号 {signal_type}，跳过分析")
                return False
        if strength < min_strength:
            return False
        if not self.can_trigger(SOURCE_SCANNER):
            if on_skip:
                on_skip("冷却中，跳过扫描触发")
            return False
        with self._lock:
            if self._scanner_pending:
                if on_skip:
                    on_skip("已有扫描触发的分析进行中")
                return False
            self._scanner_pending = True
        try:
            self.mark_triggered()
            on_trigger()
            return True
        except Exception as e:
            logger.warning("scanner trigger failed: %s", e)
            return False
        finally:
            with self._lock:
                self._scanner_pending = False

    def describe(self) -> str:
        cfg = self._cfg()
        return (
            f"Autopilot mode={cfg['mode']} "
            f"quiet_interval={cfg['interval_minutes']}m "
            f"scanner→analysis={cfg['scanner_triggers_analysis']} "
            f"(big-move immediate / quiet 2-4h)"
        )
