"""
BNB 季度销毁 (Auto-Burn) 预期模型
===================================
根据当季度 Binance 生态交易量估算下一期销毁量，
并在销毁窗口前 15 天标记「炒作高点」风控区间。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 币安 BNB 销毁通常在每季度结束后约 2 周内公布并执行
QUARTER_BURN_ANCHOR_DAYS = ((3, 15), (6, 15), (9, 15), (12, 15))


class BNBBurnPredictor:
    """季度 BNB 销毁预期与窗口标记。"""

    def __init__(
        self,
        fetcher=None,
        hype_window_days: int = 15,
        chase_block_days: int = 5,
        fee_to_burn_ratio: float = 0.20,
        binance_fee_rate: float = 0.001,
        cache_seconds: int = 3600,
    ):
        self.fetcher = fetcher
        self.hype_window_days = hype_window_days
        self.chase_block_days = chase_block_days
        self.fee_to_burn_ratio = fee_to_burn_ratio
        self.binance_fee_rate = binance_fee_rate
        self.cache_seconds = cache_seconds
        self._cache: Optional[Tuple[float, Dict]] = None

    def predict(self, symbol: str = "BNBUSDT") -> Dict[str, Any]:
        cached = self._get_cache()
        if cached is not None:
            return cached

        now = datetime.now(timezone.utc)
        next_burn, prev_burn = self._next_burn_dates(now)
        days_to_burn = (next_burn - now).days + (next_burn - now).seconds / 86400.0

        est_burn_bnb, est_volume_usdt = self._estimate_burn_amount(symbol, now)
        in_hype_window = 0 < days_to_burn <= self.hype_window_days
        in_chase_block = 0 < days_to_burn <= self.chase_block_days
        post_burn_cooldown = (now - prev_burn).days <= 7

        block_chase_long = in_chase_block or (
            in_hype_window and days_to_burn <= self.chase_block_days + 3
        )

        result = {
            "next_burn_date": next_burn.date().isoformat(),
            "prev_burn_date": prev_burn.date().isoformat(),
            "days_to_burn": round(days_to_burn, 1),
            "estimated_burn_bnb": round(est_burn_bnb, 0),
            "estimated_quarter_volume_usdt": round(est_volume_usdt, 0),
            "in_hype_window": in_hype_window,
            "in_chase_block_window": in_chase_block,
            "block_chase_long": block_chase_long,
            "post_burn_cooldown": post_burn_cooldown,
            "confidence_penalty": -0.08 if in_hype_window else 0.0,
            "gate_tightening": 0.10 if block_chase_long else (0.05 if in_hype_window else 0.0),
            "interpretation": self._interpret(
                next_burn, days_to_burn, est_burn_bnb, in_hype_window, block_chase_long
            ),
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        self._set_cache(result)
        return result

    def _next_burn_dates(self, now: datetime) -> Tuple[datetime, datetime]:
        year = now.year
        candidates: List[datetime] = []
        for y in (year - 1, year, year + 1):
            for month, day in QUARTER_BURN_ANCHOR_DAYS:
                try:
                    candidates.append(datetime(y, month, day, tzinfo=timezone.utc))
                except ValueError:
                    continue
        candidates.sort()
        future = [d for d in candidates if d > now]
        past = [d for d in candidates if d <= now]
        next_burn = future[0] if future else candidates[-1]
        prev_burn = past[-1] if past else candidates[0]
        return next_burn, prev_burn

    def _estimate_burn_amount(self, symbol: str, now: datetime) -> Tuple[float, float]:
        """用 BNB 24h 成交量 × 手续费率 × 季度天数 × 销毁比例估算。"""
        if self.fetcher is None:
            return 0.0, 0.0
        try:
            ticker = self.fetcher.get_ticker(symbol)
            vol_usdt = float(ticker.get("quoteVolume") or ticker.get("volume") or 0)
            price = float(ticker.get("lastPrice") or 1)
            if vol_usdt <= 0:
                df = self.fetcher.get_klines(symbol=symbol, interval="1d", limit=90)
                if df is not None and len(df) > 0:
                    vol_usdt = float(df["volume"].astype(float).sum() * price)
            quarter_vol = vol_usdt * 90  # 粗估季度 = 90 天 24h 量叠加
            fees_usdt = quarter_vol * self.binance_fee_rate
            burn_usdt = fees_usdt * self.fee_to_burn_ratio
            burn_bnb = burn_usdt / price if price > 0 else 0.0
            return max(0.0, burn_bnb), max(0.0, quarter_vol)
        except Exception as e:
            logger.debug("销毁量估算失败: %s", e)
            return 0.0, 0.0

    @staticmethod
    def _interpret(
        next_burn: datetime,
        days_to_burn: float,
        est_burn: float,
        in_hype: bool,
        block_chase: bool,
    ) -> str:
        parts = [
            f"下一销毁窗口约 {next_burn.date()}（{days_to_burn:.0f} 天后）",
        ]
        if est_burn > 0:
            parts.append(f"估算销毁量 ~{est_burn:,.0f} BNB")
        if block_chase:
            parts.append("⚠ 销毁预期炒作高点：禁止盲目追多")
        elif in_hype:
            parts.append("进入销毁预期窗口（15 天内）：提高门控")
        return "；".join(parts)

    def _get_cache(self) -> Optional[Dict]:
        if self._cache is None:
            return None
        ts, data = self._cache
        if time.time() - ts > self.cache_seconds:
            return None
        return data

    def _set_cache(self, data: Dict) -> None:
        self._cache = (time.time(), data)

    @classmethod
    def format_for_prompt(cls, burn: Dict) -> str:
        if not burn or not burn.get("next_burn_date"):
            return ""
        lines = ["\n【BNB 季度销毁预期】", f"- {burn.get('interpretation', '')}"]
        if burn.get("block_chase_long"):
            lines.append("- ⚠ 销毁炒作窗口：拦截追多")
        lines.append("")
        return "\n".join(lines)
