"""
动态仓位计算
在 TradeAdvisor 基础仓位上乘以增量因子（不再重复 Advisor 已计入的保守度/Regime）。

默认管道：置信度微调 × 波动率 × MTF × 新闻可信度 × 半凯利 × 议会 size_factor
"""

from __future__ import annotations

from typing import Dict, Optional


class DynamicPositionSizer:
    """在 TradeAdvisor 基础仓位上乘以多因子系数。"""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.min_factor = float(cfg.get("min_factor", 0.25))
        self.max_factor = float(cfg.get("max_factor", 1.25))
        self.base_factor = float(cfg.get("base_factor", 1.0))
        self.full_config = cfg
        # Advisor 已计入 conservativeness / regime.position_factor 时默认跳过，避免叠乘
        self.skip_advisor_dupes = bool(cfg.get("skip_advisor_dupes", True))

    def compute_factor(
        self,
        confidence: float = 0.5,
        conservativeness: float = 1.0,
        atr_ratio: float = 1.0,
        multi_timeframe: Optional[Dict] = None,
        news_summary: Optional[Dict] = None,
        market_regime: Optional[Dict] = None,
        win_rate: float = 0.5,
        prices: Optional[Dict] = None,
        council_size_factor: Optional[float] = None,
        *,
        skip_kelly_probe: bool = False,
        already_probed: bool = False,
    ) -> Dict:
        """返回 {'factor': float, 'breakdown': dict, 'note': str}"""
        multi_timeframe = multi_timeframe or {}
        news_summary = news_summary or {}
        market_regime = market_regime or {}

        breakdown = {}
        factor = self.base_factor

        # AI / 综合置信度（轻量缩放，非二次 probe）
        conf_f = 0.7 + 0.6 * max(0.0, min(1.0, confidence))
        breakdown["confidence"] = round(conf_f, 3)
        factor *= conf_f

        # 学习保守度 / Regime：默认跳过（Advisor 已乘）
        if not self.skip_advisor_dupes:
            cons_f = 0.85 + 0.15 * max(0.2, min(1.2, conservativeness))
            breakdown["conservativeness"] = round(cons_f, 3)
            factor *= cons_f
            regime_f = float(market_regime.get("position_factor", 1.0) or 1.0)
            breakdown["market_regime"] = round(regime_f, 3)
            factor *= regime_f
        else:
            breakdown["conservativeness"] = "skipped(advisor)"
            breakdown["market_regime"] = "skipped(advisor)"

        # 波动率：ATR 高于均值则缩仓
        if atr_ratio > 1.5:
            vol_f = 0.65
        elif atr_ratio > 1.2:
            vol_f = 0.8
        elif atr_ratio < 0.8:
            vol_f = 1.05
        else:
            vol_f = 1.0
        breakdown["volatility"] = round(vol_f, 3)
        factor *= vol_f

        # 多周期共振
        mtf_action = (multi_timeframe.get("recommended_action") or "").upper()
        conf_score = str(multi_timeframe.get("confluence", "")).lower()
        resonance = multi_timeframe.get("resonance_score")
        if resonance is not None and abs(float(resonance)) >= 50:
            mtf_f = 1.12
        elif "strong" in conf_score or "强" in conf_score:
            mtf_f = 1.15
        elif mtf_action in ("LONG", "SHORT"):
            mtf_f = 1.05
        else:
            mtf_f = 0.95
        breakdown["multi_timeframe"] = round(mtf_f, 3)
        factor *= mtf_f

        # 新闻风险 / 可信度
        polarity = str(news_summary.get("polarity", "neutral")).lower()
        news_conf = float(news_summary.get("confidence", 0) or 0)
        cred = float(news_summary.get("credibility") or news_summary.get("cred_score") or 1.0)
        if polarity in ("bearish", "bullish") and news_conf >= 0.5:
            news_f = 0.88 * max(0.7, min(1.0, cred))
        else:
            news_f = 1.0
        breakdown["news"] = round(news_f, 3)
        factor *= news_f

        # 半凯利（若 confidence_hard 已 probe，则不再二次 probe）
        try:
            from bnb_quant_tool.kelly_sizing import kelly_position_scale

            kelly_cfg = (
                self.full_config
                if "kelly" in self.full_config
                else {"kelly": self.full_config.get("kelly") or {}}
            )
            kelly = kelly_position_scale(
                win_rate=win_rate,
                prices=prices,
                confidence=confidence,
                config=kelly_cfg,
                skip_confidence_probe=bool(skip_kelly_probe or already_probed),
            )
            if kelly.get("enabled"):
                kf = float(kelly.get("factor") or 1.0)
                breakdown["kelly"] = round(kf, 3)
                factor *= kf
                breakdown["kelly_f"] = kelly.get("kelly_f")
        except Exception:
            pass

        # 议会仓位系数 0~1
        if council_size_factor is not None:
            cs = max(0.15, min(1.0, float(council_size_factor)))
            breakdown["council_size"] = round(cs, 3)
            factor *= cs

        factor = max(self.min_factor, min(self.max_factor, factor))
        note_parts = [
            f"{k}={v}" for k, v in breakdown.items()
            if k != "kelly_f" and v != "skipped(advisor)"
        ]
        note = " × ".join(note_parts)

        return {
            "factor": round(factor, 4),
            "breakdown": breakdown,
            "note": f"动态仓位系数 {factor:.2f} ({note})",
        }

    def apply_to_position(self, position: Dict, factor: float) -> Dict:
        """就地调整 position 字典中的 usdt_amount / quantity。"""
        if not position or factor >= 0.999:
            return position
        pos = dict(position)
        for key in ("usdt_amount", "quantity", "risk_amount"):
            if pos.get(key):
                pos[key] = round(float(pos[key]) * factor, 4 if key == "quantity" else 2)
        pos["dynamic_factor"] = factor
        pos["note"] = (pos.get("note") or "") + f" [动态仓位 ×{factor:.0%}]"
        return pos
