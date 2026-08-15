"""
市场状态识别 (Market Regime)
根据趋势、波动率、RSI 与新闻情绪识别当前市场状态，
并为机构策略提供动态权重系数。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRENDING = "TRENDING"
RANGING = "RANGING"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
LOW_VOLATILITY = "LOW_VOLATILITY"
NEWS_DRIVEN = "NEWS_DRIVEN"
PANIC = "PANIC"
EUPHORIA = "EUPHORIA"

# 策略 key → 各状态下的权重乘数（与 institutional_strategies 注册名一致）
REGIME_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    TRENDING: {
        "ema_crossover": 1.8,
        "sma_crossover": 1.5,
        "turtle_trading": 1.6,
        "citadel_momentum": 1.5,
        "macd_crossover": 1.3,
        "golden_death_cross": 1.7,
        "adx_trend": 1.8,
        "breakout_volume": 1.6,
        "volume_price_obv": 1.3,
        "bollinger_bands": 0.5,
        "rsi_extreme": 0.6,
        "range_sr_swing": 0.5,
        "stochastic_momentum": 0.7,
        "jump_market_making": 0.7,
    },
    RANGING: {
        "bollinger_bands": 1.7,
        "rsi_extreme": 1.5,
        "jump_market_making": 1.4,
        "fibonacci_retracement": 1.3,
        "renissance_stat_arb": 1.2,
        "range_sr_swing": 1.8,
        "stochastic_momentum": 1.5,
        "ema_crossover": 0.6,
        "turtle_trading": 0.5,
        "citadel_momentum": 0.7,
        "adx_trend": 0.5,
        "breakout_volume": 0.6,
        "golden_death_cross": 0.6,
    },
    HIGH_VOLATILITY: {
        "jump_market_making": 1.6,
        "bridgewater_risk_parity": 1.3,
        "bollinger_bands": 1.2,
        "breakout_volume": 1.3,
        "volume_price_obv": 1.2,
        "turtle_trading": 0.7,
        "ema_crossover": 0.8,
        "range_sr_swing": 0.6,
    },
    LOW_VOLATILITY: {
        "rsi_extreme": 1.5,
        "bollinger_bands": 1.4,
        "renissance_stat_arb": 1.3,
        "aqr_value_momentum": 1.2,
        "range_sr_swing": 1.4,
        "stochastic_momentum": 1.3,
        "citadel_momentum": 0.7,
        "turtle_trading": 0.6,
        "breakout_volume": 0.7,
    },
    NEWS_DRIVEN: {
        "two_sigma_ml": 1.3,
        "citadel_momentum": 1.2,
        "aqr_value_momentum": 1.1,
        "breakout_volume": 1.2,
        "volume_price_obv": 1.15,
    },
    PANIC: {
        "_global": 0.5,
    },
    EUPHORIA: {
        "_global": 0.6,
    },
}


class MarketRegimeDetector:
    """识别市场状态并输出策略权重调整系数。"""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.trend_slope_threshold = float(cfg.get("trend_slope_threshold", 0.0015))
        self.high_vol_atr_ratio = float(cfg.get("high_vol_atr_ratio", 1.35))
        self.low_vol_atr_ratio = float(cfg.get("low_vol_atr_ratio", 0.75))
        self.panic_rsi = float(cfg.get("panic_rsi", 25))
        self.euphoria_rsi = float(cfg.get("euphoria_rsi", 78))
        self.news_confidence_min = float(cfg.get("news_confidence_min", 0.55))
        self._multi_signal_fusion = bool(cfg.get("multi_signal_fusion", True))
        self._fusion_cfg = cfg.get("fusion") or cfg
        self._hmm_enabled = bool(cfg.get("hmm_enabled", True))
        self._hmm_cfg = cfg.get("hmm") or {}
        self._hmm_prefer_on_conflict = bool(cfg.get("hmm_prefer_on_conflict", False))

    def detect(
        self,
        df: pd.DataFrame,
        indicators: Optional[Dict] = None,
        news_summary: Optional[Dict] = None,
        sentiment: Optional[Dict] = None,
        bnb_factors: Optional[Dict] = None,
    ) -> Dict:
        indicators = indicators or {}
        news_summary = news_summary or {}

        regime, reasons = self._classify(df, indicators, news_summary)
        multipliers = self.get_regime_multipliers(regime)

        avg_atr = self._avg_atr(df)
        atr_now = self._f(indicators.get("ATR"), 0.0)
        atr_ratio = (atr_now / avg_atr) if atr_now > 0 and avg_atr > 0 else 1.0

        result = {
            "regime": regime,
            "reasons": reasons,
            "strategy_multipliers": multipliers,
            "position_factor": self._regime_position_factor(regime),
            "description": self._describe(regime),
            "avg_atr": round(avg_atr, 6) if avg_atr else 0.0,
            "atr_ratio": round(atr_ratio, 3),
        }

        if bool(getattr(self, "_multi_signal_fusion", True)):
            try:
                from bnb_quant_tool.regime_fusion import fuse_regime, merge_fusion_into_regime
                fusion = fuse_regime(
                    df,
                    indicators=indicators,
                    sentiment=sentiment,
                    bnb_factors=bnb_factors,
                    config=getattr(self, "_fusion_cfg", {}),
                    legacy_regime=regime,
                )
                result = merge_fusion_into_regime(result, fusion, prefer_fusion=True)
                multipliers = self.get_regime_multipliers(result["regime"])
                result["strategy_multipliers"] = multipliers
                result["position_factor"] = self._regime_position_factor(result["regime"])
            except Exception as e:
                logger.debug("regime fusion: %s", e)

        if bool(getattr(self, "_hmm_enabled", False)):
            try:
                from bnb_quant_tool.regime_hmm import infer_hmm_regime, merge_hmm_into_regime
                hmm = infer_hmm_regime(df, config=getattr(self, "_hmm_cfg", {}))
                result = merge_hmm_into_regime(
                    result,
                    hmm,
                    prefer_hmm_on_conflict=bool(
                        getattr(self, "_hmm_prefer_on_conflict", False)
                    ),
                    min_hmm_confidence=float(
                        getattr(self, "_hmm_cfg", {}).get("min_confidence", 0.65)
                    ),
                )
                if hmm.get("hmm_enabled") and result.get("regime"):
                    multipliers = self.get_regime_multipliers(result["regime"])
                    result["strategy_multipliers"] = multipliers
                    result["position_factor"] = self._regime_position_factor(
                        result["regime"]
                    )
            except Exception as e:
                logger.debug("regime hmm: %s", e)

        return result

    def _classify(
        self,
        df: pd.DataFrame,
        indicators: Dict,
        news_summary: Dict,
    ) -> Tuple[str, list]:
        reasons = []
        rsi = self._f(indicators.get("RSI"), 50)
        atr = self._f(indicators.get("ATR"), 0)
        bb_pos = self._f(indicators.get("BB_Position"), 50)

        # 新闻驱动优先（高置信极端情绪）
        polarity = str(news_summary.get("polarity", "neutral")).lower()
        news_conf = self._f(news_summary.get("confidence"), 0)
        if news_conf >= self.news_confidence_min and polarity in ("bullish", "bearish"):
            reasons.append(f"新闻情绪 {polarity} (置信 {news_conf:.0%})")
            return NEWS_DRIVEN, reasons

        if rsi <= self.panic_rsi:
            reasons.append(f"RSI 恐慌区 ({rsi:.1f})")
            return PANIC, reasons
        if rsi >= self.euphoria_rsi:
            reasons.append(f"RSI 亢奋区 ({rsi:.1f})")
            return EUPHORIA, reasons

        atr_ratio = 1.0
        avg_atr = self._avg_atr(df)
        if atr > 0 and avg_atr > 0:
            atr_ratio = atr / avg_atr

        if atr_ratio >= self.high_vol_atr_ratio:
            reasons.append(f"ATR/均值={atr_ratio:.2f} 高波动")
            return HIGH_VOLATILITY, reasons
        if atr_ratio <= self.low_vol_atr_ratio:
            reasons.append(f"ATR/均值={atr_ratio:.2f} 低波动")
            return LOW_VOLATILITY, reasons

        slope = self._ma_slope(df)
        if abs(slope) >= self.trend_slope_threshold:
            direction = "上涨" if slope > 0 else "下跌"
            reasons.append(f"MA20 斜率 {slope:.4f} ({direction}趋势)")
            return TRENDING, reasons

        reasons.append(f"横盘 (斜率 {slope:.4f}, BB位置 {bb_pos:.0f})")
        return RANGING, reasons

    def get_regime_multipliers(self, regime: str) -> Dict[str, float]:
        base = REGIME_MULTIPLIERS.get(regime, {})
        if "_global" in base:
            return {"_global": base["_global"]}
        return dict(base)

    def _regime_position_factor(self, regime: str) -> float:
        return {
            PANIC: 0.4,
            EUPHORIA: 0.5,
            HIGH_VOLATILITY: 0.7,
            NEWS_DRIVEN: 0.85,
        }.get(regime, 1.0)

    def _describe(self, regime: str) -> str:
        labels = {
            TRENDING: "单边趋势 — 偏重趋势/动量策略",
            RANGING: "横盘震荡 — 偏重均值回归/网格类",
            HIGH_VOLATILITY: "高波动 — 缩仓，偏重做市/风控",
            LOW_VOLATILITY: "低波动 — 偏重震荡策略",
            NEWS_DRIVEN: "新闻驱动 — 降权技术票，关注情绪",
            PANIC: "恐慌 — 全面降权，建议观望",
            EUPHORIA: "亢奋 — 全面降权，谨防追高",
        }
        return labels.get(regime, regime)

    @staticmethod
    def _avg_atr(df: pd.DataFrame, period: int = 14, lookback: int = 50) -> float:
        if df is None or len(df) < period + 5:
            return 0.0
        try:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(period).mean().dropna()
            if len(atr) == 0:
                return 0.0
            return float(atr.tail(min(lookback, len(atr))).mean())
        except Exception:
            return 0.0

    @staticmethod
    def _ma_slope(df: pd.DataFrame, window: int = 20) -> float:
        if df is None or len(df) < window + 5:
            return 0.0
        try:
            ma = df["close"].astype(float).rolling(window).mean().dropna()
            if len(ma) < 5:
                return 0.0
            recent = ma.tail(5).values
            return float((recent[-1] - recent[0]) / max(recent[0], 1e-9))
        except Exception:
            return 0.0

    @staticmethod
    def _f(val, default: float = 0.0) -> float:
        try:
            if val is None:
                return default
            return float(val)
        except (TypeError, ValueError):
            return default
