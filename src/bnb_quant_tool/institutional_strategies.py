"""
BNB量化交易工具 - 大机构研究策略模块
集成多种知名机构的量化策略：
1. Renaissance Technologies - 统计套利策略
2. Citadel - 多因子动量策略
3. Bridgewater - 风险平价策略
4. AQR Capital - 价值与动量结合策略
5. Two Sigma - 机器学习预测策略
6. Jump Trading - 高频做市策略
7. 移动平均线策略（SMA/EMA）
8. 布林带突破策略
9. RSI超买超卖策略
10. MACD金叉死叉策略
11. 海龟交易法则
12. 斐波那契回调策略
13. 黄金/死亡交叉（IG MA 交叉）
14. ADX 趋势确认（Moomoo 趋势交易）
15. 随机指标动能（Moomoo Momentum）
16. OBV 量价确认（Volume-Price）
17. 区间波段（Range / Swing S-R）
18. 放量突破（Breakout + Volume）
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InstitutionalStrategies:
    """大机构研究策略集成"""
    
    def __init__(self, config: Dict = None, load_discovered: bool = True):
        """初始化策略参数

        Args:
            config: 配置
            load_discovered: 是否加载 StrategyLab 发现的自动策略（默认 True）
        """
        self.config = config or {}
        self.strategies = {}
        self._register_strategies()
        if load_discovered:
            self._register_discovered_strategies()
    
    def _register_strategies(self):
        """注册所有策略"""
        self.strategies = {
            # 技术指标策略
            'sma_crossover': self.sma_crossover_strategy,
            'ema_crossover': self.ema_crossover_strategy,
            'bollinger_bands': self.bollinger_bands_strategy,
            'rsi_extreme': self.rsi_extreme_strategy,
            'macd_crossover': self.macd_crossover_strategy,
            'fibonacci_retracement': self.fibonacci_retracement_strategy,
            # 经典技术分析（IG / Moomoo / FinLab 框架）
            'golden_death_cross': self.golden_death_cross_strategy,
            'adx_trend': self.adx_trend_strategy,
            'stochastic_momentum': self.stochastic_momentum_strategy,
            'volume_price_obv': self.volume_price_obv_strategy,
            'range_sr_swing': self.range_sr_swing_strategy,
            'breakout_volume': self.breakout_volume_strategy,
            
            # 机构级策略
            'renissance_stat_arb': self.renissance_statistical_arbitrage,
            'citadel_momentum': self.citadel_momentum_strategy,
            'bridgewater_risk_parity': self.bridgewater_risk_parity,
            'aqr_value_momentum': self.aqr_value_momentum_strategy,
            'two_sigma_ml': self.two_sigma_ml_strategy,
            'jump_market_making': self.jump_market_making_strategy,
            'turtle_trading': self.turtle_trading_strategy,
        }

    def _register_discovered_strategies(self):
        """加载 StrategyLab 落盘的自动发现策略，挂到 self.strategies。

        每个发现策略的 id 形如 auto_0001，最终注册名为 auto_<id>。
        加载失败不影响主流程（向后兼容）。
        """
        try:
            from .strategy_lab import StrategyLab, make_institutional_func
            specs = StrategyLab.load_discovered()
            if not specs:
                return 0
            only_promoted = bool(
                (self.config or {}).get("learning_evolution", {}).get(
                    "strategy_lab_only_promoted", False
                )
            )
            count = 0
            for spec in specs:
                if only_promoted and not spec.get("promoted"):
                    continue
                sid = spec.get('id') or f"auto_unknown_{count}"
                key = sid if sid.startswith('auto_') else f"auto_{sid}"
                # 允许覆盖：热加载时刷新同名策略
                self.strategies[key] = make_institutional_func(spec)
                count += 1
            if count > 0:
                logger.info(f"加载 {count} 个 AI 自动发现策略到投票池")
            return count
        except Exception as e:
            logger.warning(f"加载自动发现策略失败（忽略）: {e}")
            return 0

    def reload_discovered(self) -> int:
        """热加载 discovered 策略：先卸下旧 auto_*，再重新注册。"""
        drop = [k for k in list(self.strategies.keys()) if str(k).startswith("auto_")]
        for k in drop:
            self.strategies.pop(k, None)
        return int(self._register_discovered_strategies() or 0)

    # ==================== 技术指标策略 ====================
    
    def sma_crossover_strategy(self, df: pd.DataFrame, 
                                params: Dict = None) -> Dict:
        """
        简单移动平均线交叉策略
        策略逻辑：短期SMA上穿长期SMA买入，下穿卖出
        """
        if params is None:
            params = {'short_window': 20, 'long_window': 50}
        
        short_window = params.get('short_window', 20)
        long_window = params.get('long_window', 50)
        
        signals = []
        df['SMA_Short'] = df['close'].rolling(window=short_window).mean()
        df['SMA_Long'] = df['close'].rolling(window=long_window).mean()
        
        # 计算交叉信号
        signal_values = np.zeros(len(df))
        mask = np.arange(len(df)) >= short_window
        signal_values[mask] = np.where(
            df['SMA_Short'].iloc[mask].values > df['SMA_Long'].iloc[mask].values, 1, -1
        )
        df['Signal'] = signal_values
        df['Position_Change'] = df['Signal'].diff()
        
        latest_signal = 'HOLD'
        if df['Position_Change'].iloc[-1] > 0:
            latest_signal = 'BUY'
        elif df['Position_Change'].iloc[-1] < 0:
            latest_signal = 'SELL'
        
        return {
            'strategy': 'SMA Crossover',
            'signal': latest_signal,
            'confidence': 0.65,
            'sma_short': df['SMA_Short'].iloc[-1],
            'sma_long': df['SMA_Long'].iloc[-1],
            'description': f'SMA{short_window}/{long_window}交叉策略'
        }
    
    def ema_crossover_strategy(self, df: pd.DataFrame,
                                params: Dict = None) -> Dict:
        """
        指数移动平均线交叉策略
        策略逻辑：短期EMA上穿长期EMA买入，下穿卖出
        """
        if params is None:
            params = {'short_window': 12, 'long_window': 26}
        
        short_window = params.get('short_window', 12)
        long_window = params.get('long_window', 26)
        
        df['EMA_Short'] = df['close'].ewm(span=short_window, adjust=False).mean()
        df['EMA_Long'] = df['close'].ewm(span=long_window, adjust=False).mean()
        
        # 计算交叉
        cross_up = (df['EMA_Short'].iloc[-1] > df['EMA_Long'].iloc[-1] and 
                    df['EMA_Short'].iloc[-2] <= df['EMA_Long'].iloc[-2])
        cross_down = (df['EMA_Short'].iloc[-1] < df['EMA_Long'].iloc[-1] and 
                      df['EMA_Short'].iloc[-2] >= df['EMA_Long'].iloc[-2])
        
        if cross_up:
            signal = 'BUY'
            confidence = 0.7
        elif cross_down:
            signal = 'SELL'
            confidence = 0.7
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'EMA Crossover',
            'signal': signal,
            'confidence': confidence,
            'ema_short': df['EMA_Short'].iloc[-1],
            'ema_long': df['EMA_Long'].iloc[-1],
            'description': f'EMA{short_window}/{long_window}交叉策略'
        }
    
    def bollinger_bands_strategy(self, df: pd.DataFrame,
                                  params: Dict = None) -> Dict:
        """
        布林带突破策略
        策略逻辑：价格触及下轨买入，触及上轨卖出
        """
        if params is None:
            params = {'window': 20, 'num_std': 2}
        
        window = params.get('window', 20)
        num_std = params.get('num_std', 2)
        
        df['BB_Middle'] = df['close'].rolling(window=window).mean()
        std = df['close'].rolling(window=window).std()
        df['BB_Upper'] = df['BB_Middle'] + (std * num_std)
        df['BB_Lower'] = df['BB_Middle'] - (std * num_std)
        
        current_price = df['close'].iloc[-1]
        bb_upper = df['BB_Upper'].iloc[-1]
        bb_lower = df['BB_Lower'].iloc[-1]
        bb_middle = df['BB_Middle'].iloc[-1]
        
        # 计算位置百分比
        bb_position = (current_price - bb_lower) / (bb_upper - bb_lower) * 100
        
        if current_price < bb_lower:
            signal = 'BUY'
            confidence = 0.75
        elif current_price > bb_upper:
            signal = 'SELL'
            confidence = 0.75
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'Bollinger Bands',
            'signal': signal,
            'confidence': confidence,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower,
            'bb_middle': bb_middle,
            'bb_position': bb_position,
            'description': f'布林带突破策略 (位置: {bb_position:.1f}%)'
        }
    
    def rsi_extreme_strategy(self, df: pd.DataFrame,
                              params: Dict = None) -> Dict:
        """
        RSI超买超卖策略
        策略逻辑：RSI < 30买入，RSI > 70卖出
        """
        if params is None:
            params = {'oversold': 30, 'overbought': 70, 'period': 14}
        
        oversold = params.get('oversold', 30)
        overbought = params.get('overbought', 70)
        period = params.get('period', 14)
        
        # 计算RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        current_rsi = df['RSI'].iloc[-1]
        
        if current_rsi < oversold:
            signal = 'BUY'
            confidence = 0.7
        elif current_rsi > overbought:
            signal = 'SELL'
            confidence = 0.7
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'RSI Extreme',
            'signal': signal,
            'confidence': confidence,
            'rsi': current_rsi,
            'oversold_threshold': oversold,
            'overbought_threshold': overbought,
            'description': f'RSI超买超卖策略 (RSI: {current_rsi:.1f})'
        }
    
    def macd_crossover_strategy(self, df: pd.DataFrame,
                                 params: Dict = None) -> Dict:
        """
        MACD金叉死叉策略
        策略逻辑：MACD上穿信号线买入，下穿卖出
        """
        if params is None:
            params = {'fast': 12, 'slow': 26, 'signal': 9}
        
        fast = params.get('fast', 12)
        slow = params.get('slow', 26)
        signal_period = params.get('signal', 9)
        
        # 计算MACD
        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        df['MACD'] = ema_fast - ema_slow
        df['MACD_Signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()
        df['MACD_Histogram'] = df['MACD'] - df['MACD_Signal']
        
        # 判断金叉死叉
        cross_up = (df['MACD'].iloc[-1] > df['MACD_Signal'].iloc[-1] and 
                    df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2])
        cross_down = (df['MACD'].iloc[-1] < df['MACD_Signal'].iloc[-1] and 
                      df['MACD'].iloc[-2] >= df['MACD_Signal'].iloc[-2])
        
        if cross_up:
            signal = 'BUY'
            confidence = 0.72
        elif cross_down:
            signal = 'SELL'
            confidence = 0.72
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'MACD Crossover',
            'signal': signal,
            'confidence': confidence,
            'macd': df['MACD'].iloc[-1],
            'macd_signal': df['MACD_Signal'].iloc[-1],
            'macd_histogram': df['MACD_Histogram'].iloc[-1],
            'description': 'MACD金叉死叉策略'
        }
    
    def fibonacci_retracement_strategy(self, df: pd.DataFrame,
                                       params: Dict = None) -> Dict:
        """
        斐波那契回调策略
        策略逻辑：价格在斐波那契回调位获得支撑/阻力时交易
        """
        if params is None:
            params = {'lookback': 100}
        
        lookback = params.get('lookback', 100)
        
        # 找出近期高点和低点
        recent_data = df.tail(lookback)
        high = recent_data['high'].max()
        low = recent_data['low'].min()
        
        # 斐波那契回调位
        diff = high - low
        fib_levels = {
            '0.236': high - diff * 0.236,
            '0.382': high - diff * 0.382,
            '0.5': high - diff * 0.5,
            '0.618': high - diff * 0.618,
            '0.786': high - diff * 0.786,
        }
        
        current_price = df['close'].iloc[-1]
        
        # 判断价格在哪个回调位附近
        tolerance = diff * 0.02  # 2%容忍度
        nearest_level = None
        for level, price in fib_levels.items():
            if abs(current_price - price) < tolerance:
                nearest_level = level
                break
        
        if nearest_level:
            # 在回调位附近，判断是支撑还是阻力
            if current_price > df['close'].iloc[-2]:  # 价格上涨
                signal = 'BUY'
                confidence = 0.68
            else:
                signal = 'SELL'
                confidence = 0.68
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'Fibonacci Retracement',
            'signal': signal,
            'confidence': confidence,
            'high': high,
            'low': low,
            'fib_levels': fib_levels,
            'nearest_level': nearest_level,
            'description': f'斐波那契回调策略 (最近位: {nearest_level})'
        }

    # ==================== 经典技术分析（IG / Moomoo）====================

    def golden_death_cross_strategy(self, df: pd.DataFrame,
                                    params: Dict = None) -> Dict:
        """黄金交叉 / 死亡交叉（IG: 短均线上穿/下穿长均线）。默认 SMA50/200。"""
        if params is None:
            params = {"short_window": 50, "long_window": 200}
        short_w = int(params.get("short_window", 50))
        long_w = int(params.get("long_window", 200))
        if len(df) < long_w + 2:
            return {
                "strategy": "Golden/Death Cross",
                "signal": "HOLD",
                "confidence": 0.5,
                "description": f"数据不足 SMA{short_w}/{long_w}",
            }

        work = df.copy()
        work["SMA_S"] = work["close"].rolling(short_w).mean()
        work["SMA_L"] = work["close"].rolling(long_w).mean()
        s0, s1 = work["SMA_S"].iloc[-2], work["SMA_S"].iloc[-1]
        l0, l1 = work["SMA_L"].iloc[-2], work["SMA_L"].iloc[-1]

        golden = s0 <= l0 and s1 > l1
        death = s0 >= l0 and s1 < l1
        above = s1 > l1

        if golden:
            signal, confidence, reason = "BUY", 0.78, "黄金交叉：短均线上穿长均线"
        elif death:
            signal, confidence, reason = "SELL", 0.78, "死亡交叉：短均线下穿长均线"
        elif above:
            signal, confidence, reason = "BUY", 0.58, "短均线在长均线上方（趋势偏多）"
        else:
            signal, confidence, reason = "SELL", 0.58, "短均线在长均线下方（趋势偏空）"

        return {
            "strategy": "Golden/Death Cross",
            "signal": signal,
            "confidence": confidence,
            "sma_short": float(s1),
            "sma_long": float(l1),
            "reason": reason,
            "description": f"SMA{short_w}/{long_w} 黄金/死亡交叉",
        }

    def adx_trend_strategy(self, df: pd.DataFrame, params: Dict = None) -> Dict:
        """ADX 趋势交易：ADX 确认趋势后跟随 +DI/-DI（Moomoo 趋势交易）。"""
        if params is None:
            params = {"period": 14, "adx_threshold": 25}
        period = int(params.get("period", 14))
        thr = float(params.get("adx_threshold", 25))
        from .technical_indicators import TechnicalIndicators

        adx, plus_di, minus_di = TechnicalIndicators.calculate_adx(df, period=period)
        adx_v = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0
        pdi = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0
        mdi = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0

        if adx_v < thr:
            return {
                "strategy": "ADX Trend",
                "signal": "HOLD",
                "confidence": 0.5,
                "adx": adx_v,
                "plus_di": pdi,
                "minus_di": mdi,
                "reason": f"ADX {adx_v:.1f} < {thr}，趋势未确认，宜观望/区间",
                "description": "ADX 趋势确认策略",
            }

        strength = min(0.9, 0.55 + (adx_v - thr) / 100)
        if pdi > mdi:
            signal, reason = "BUY", f"ADX {adx_v:.1f} 确认上升趋势 (+DI>{mdi:.0f})"
        else:
            signal, reason = "SELL", f"ADX {adx_v:.1f} 确认下降趋势 (-DI>{pdi:.0f})"

        return {
            "strategy": "ADX Trend",
            "signal": signal,
            "confidence": strength,
            "adx": adx_v,
            "plus_di": pdi,
            "minus_di": mdi,
            "reason": reason,
            "description": "ADX 趋势确认策略",
        }

    def stochastic_momentum_strategy(self, df: pd.DataFrame,
                                      params: Dict = None) -> Dict:
        """随机指标动能：超卖金叉买 / 超买死叉卖（Moomoo Momentum）。"""
        if params is None:
            params = {"oversold": 20, "overbought": 80}
        oversold = float(params.get("oversold", 20))
        overbought = float(params.get("overbought", 80))
        from .technical_indicators import TechnicalIndicators

        k, d = TechnicalIndicators.calculate_stochastic(df)
        k0, k1 = float(k.iloc[-2]), float(k.iloc[-1])
        d0, d1 = float(d.iloc[-2]), float(d.iloc[-1])

        cross_up = k0 <= d0 and k1 > d1
        cross_down = k0 >= d0 and k1 < d1

        if cross_up and k1 < oversold + 15:
            signal, confidence, reason = "BUY", 0.74, f"Stoch 金叉且偏弱区 (K={k1:.1f})"
        elif cross_down and k1 > overbought - 15:
            signal, confidence, reason = "SELL", 0.74, f"Stoch 死叉且偏强区 (K={k1:.1f})"
        elif k1 < oversold:
            signal, confidence, reason = "BUY", 0.62, f"Stoch 超卖 (K={k1:.1f})"
        elif k1 > overbought:
            signal, confidence, reason = "SELL", 0.62, f"Stoch 超买 (K={k1:.1f})"
        else:
            signal, confidence, reason = "HOLD", 0.5, f"Stoch 中性 (K={k1:.1f})"

        return {
            "strategy": "Stochastic Momentum",
            "signal": signal,
            "confidence": confidence,
            "stoch_k": k1,
            "stoch_d": d1,
            "reason": reason,
            "description": "随机指标动能策略",
        }

    def volume_price_obv_strategy(self, df: pd.DataFrame,
                                  params: Dict = None) -> Dict:
        """量价 OBV：价格与 OBV 同向确认趋势，背离提示反转（Moomoo VPA）。"""
        if params is None:
            params = {"lookback": 10, "vol_spike": 1.5}
        lookback = int(params.get("lookback", 10))
        vol_spike = float(params.get("vol_spike", 1.5))
        from .technical_indicators import TechnicalIndicators

        if len(df) < lookback + 5:
            return {
                "strategy": "Volume-Price OBV",
                "signal": "HOLD",
                "confidence": 0.5,
                "description": "数据不足",
            }

        obv = TechnicalIndicators.calculate_obv(df)
        price_chg = float(df["close"].iloc[-1] / df["close"].iloc[-lookback] - 1)
        obv_chg = float(obv.iloc[-1] - obv.iloc[-lookback])
        vol = float(df["volume"].iloc[-1])
        vol_ma = float(df["volume"].tail(20).mean())
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0

        # 量价同向 + 放量 → 趋势确认
        if price_chg > 0.01 and obv_chg > 0 and vol_ratio >= vol_spike:
            signal, confidence, reason = (
                "BUY", 0.72, f"放量上涨且 OBV 同步 (+{price_chg:.1%})",
            )
        elif price_chg < -0.01 and obv_chg < 0 and vol_ratio >= vol_spike:
            signal, confidence, reason = (
                "SELL", 0.72, f"放量下跌且 OBV 同步 ({price_chg:.1%})",
            )
        # 背离
        elif price_chg > 0.015 and obv_chg < 0:
            signal, confidence, reason = (
                "SELL", 0.66, "价涨 OBV 跌 — 看跌背离",
            )
        elif price_chg < -0.015 and obv_chg > 0:
            signal, confidence, reason = (
                "BUY", 0.66, "价跌 OBV 涨 — 看涨背离",
            )
        else:
            signal, confidence, reason = "HOLD", 0.5, "量价中性"

        return {
            "strategy": "Volume-Price OBV",
            "signal": signal,
            "confidence": confidence,
            "obv": float(obv.iloc[-1]),
            "obv_change": obv_chg,
            "price_change": price_chg,
            "volume_ratio": vol_ratio,
            "reason": reason,
            "description": "量价 OBV 确认策略",
        }

    def range_sr_swing_strategy(self, df: pd.DataFrame,
                                params: Dict = None) -> Dict:
        """区间波段：接近支撑做多、接近阻力做空（Moomoo Swing / Range）。"""
        if params is None:
            params = {"lookback": 50, "edge_pct": 0.015}
        lookback = int(params.get("lookback", 50))
        edge_pct = float(params.get("edge_pct", 0.015))
        from .technical_indicators import TechnicalIndicators

        sr = TechnicalIndicators.calculate_support_resistance(df, lookback=lookback)
        close = float(df["close"].iloc[-1])
        support = float(sr["Support"])
        resistance = float(sr["Resistance"])
        range_pct = float(sr["SR_Range_Pct"])

        # 区间太窄则不交易
        if range_pct < 0.02:
            return {
                "strategy": "Range S/R Swing",
                "signal": "HOLD",
                "confidence": 0.5,
                "support": support,
                "resistance": resistance,
                "reason": "支撑阻力区间过窄",
                "description": "区间波段策略",
            }

        dist_sup = (close - support) / close if close else 1
        dist_res = (resistance - close) / close if close else 1

        if dist_sup <= edge_pct:
            signal, confidence, reason = (
                "BUY", 0.7, f"接近支撑 {support:.2f}（距 {dist_sup:.2%}）",
            )
        elif dist_res <= edge_pct:
            signal, confidence, reason = (
                "SELL", 0.7, f"接近阻力 {resistance:.2f}（距 {dist_res:.2%}）",
            )
        else:
            signal, confidence, reason = "HOLD", 0.5, "价格在区间中部"

        return {
            "strategy": "Range S/R Swing",
            "signal": signal,
            "confidence": confidence,
            "support": support,
            "resistance": resistance,
            "range_pct": range_pct,
            "reason": reason,
            "description": "支撑阻力区间波段策略",
        }

    def breakout_volume_strategy(self, df: pd.DataFrame,
                                 params: Dict = None) -> Dict:
        """放量突破：突破 N 日高低点且成交量放大（Moomoo Breakout）。"""
        if params is None:
            params = {"lookback": 20, "vol_mult": 1.4}
        lookback = int(params.get("lookback", 20))
        vol_mult = float(params.get("vol_mult", 1.4))
        if len(df) < lookback + 2:
            return {
                "strategy": "Breakout Volume",
                "signal": "HOLD",
                "confidence": 0.5,
                "description": "数据不足",
            }

        recent = df.iloc[-(lookback + 1):-1]
        high = float(recent["high"].max())
        low = float(recent["low"].min())
        close = float(df["close"].iloc[-1])
        vol = float(df["volume"].iloc[-1])
        vol_ma = float(df["volume"].tail(20).mean())
        vol_ok = vol_ma > 0 and vol >= vol_ma * vol_mult

        if close > high and vol_ok:
            pct = (close - high) / high
            signal, confidence, reason = (
                "BUY",
                min(0.88, 0.65 + pct * 40),
                f"放量突破 {lookback}K 高点 {high:.2f}",
            )
        elif close < low and vol_ok:
            pct = (low - close) / low
            signal, confidence, reason = (
                "SELL",
                min(0.88, 0.65 + pct * 40),
                f"放量跌破 {lookback}K 低点 {low:.2f}",
            )
        elif close > high:
            signal, confidence, reason = "BUY", 0.55, "突破高点但量能不足"
        elif close < low:
            signal, confidence, reason = "SELL", 0.55, "跌破低点但量能不足"
        else:
            signal, confidence, reason = "HOLD", 0.5, f"价格在 {low:.2f}-{high:.2f} 内"

        return {
            "strategy": "Breakout Volume",
            "signal": signal,
            "confidence": confidence,
            "breakout_high": high,
            "breakout_low": low,
            "volume_ok": vol_ok,
            "reason": reason,
            "description": f"{lookback}K 放量突破策略",
        }
    
    # ==================== 机构级策略 ====================
    
    def renissance_statistical_arbitrage(self, df: pd.DataFrame,
                                         params: Dict = None) -> Dict:
        """
        Renaissance Technologies - 统计套利策略
        策略逻辑：利用统计学方法找出价格偏离，进行均值回归交易
        """
        if params is None:
            params = {'mean_reversion_window': 20, 'z_score_threshold': 2.0}
        
        window = params.get('mean_reversion_window', 20)
        z_threshold = params.get('z_score_threshold', 2.0)
        
        # 计算价格偏离度（Z-Score）
        df['Price_Mean'] = df['close'].rolling(window=window).mean()
        df['Price_Std'] = df['close'].rolling(window=window).std()
        df['Z_Score'] = (df['close'] - df['Price_Mean']) / df['Price_Std']
        
        current_z = df['Z_Score'].iloc[-1]
        
        if current_z < -z_threshold:
            # 价格显著低于均值，买入
            signal = 'BUY'
            confidence = 0.78
            reason = f'价格偏离均值 {abs(current_z):.2f} 个标准差（超卖）'
        elif current_z > z_threshold:
            # 价格显著高于均值，卖出
            signal = 'SELL'
            confidence = 0.78
            reason = f'价格偏离均值 {abs(current_z):.2f} 个标准差（超买）'
        else:
            signal = 'HOLD'
            confidence = 0.5
            reason = f'价格在正常范围内（Z-Score: {current_z:.2f}）'
        
        return {
            'strategy': 'Renaissance Statistical Arbitrage',
            'signal': signal,
            'confidence': confidence,
            'z_score': current_z,
            'mean_price': df['Price_Mean'].iloc[-1],
            'reason': reason,
            'description': '统计套利均值回归策略'
        }
    
    def citadel_momentum_strategy(self, df: pd.DataFrame,
                                   params: Dict = None) -> Dict:
        """
        Citadel - 多因子动量策略
        策略逻辑：结合价格动量、成交量动量、波动率动量
        """
        if params is None:
            params = {'momentum_window': 20, 'volume_window': 10}
        
        momentum_window = params.get('momentum_window', 20)
        volume_window = params.get('volume_window', 10)
        
        # 价格动量
        df['Price_Momentum'] = df['close'].pct_change(momentum_window)
        
        # 成交量动量
        df['Volume_Momentum'] = df['volume'].pct_change(volume_window)
        
        # 波动率动量
        df['Volatility'] = df['close'].rolling(window=20).std()
        df['Volatility_Momentum'] = df['Volatility'].pct_change(10)
        
        # 综合动量得分
        price_mom = df['Price_Momentum'].iloc[-1]
        volume_mom = df['Volume_Momentum'].iloc[-1]
        vol_mom = df['Volatility_Momentum'].iloc[-1]
        
        # 加权得分（价格50%，成交量30%，波动率20%）
        momentum_score = price_mom * 0.5 + volume_mom * 0.3 + vol_mom * 0.2
        
        if momentum_score > 0.02:
            signal = 'BUY'
            confidence = min(0.85, 0.6 + abs(momentum_score))
        elif momentum_score < -0.02:
            signal = 'SELL'
            confidence = min(0.85, 0.6 + abs(momentum_score))
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'Citadel Multi-Factor Momentum',
            'signal': signal,
            'confidence': confidence,
            'momentum_score': momentum_score,
            'price_momentum': price_mom,
            'volume_momentum': volume_mom,
            'volatility_momentum': vol_mom,
            'description': '多因子动量策略（价格+成交量+波动率）'
        }
    
    def bridgewater_risk_parity(self, df: pd.DataFrame,
                                params: Dict = None) -> Dict:
        """
        Bridgewater - 风险平价策略
        策略逻辑：根据资产波动性分配仓位，平衡风险贡献
        """
        if params is None:
            params = {'volatility_window': 20, 'target_risk': 0.02}
        
        volatility_window = params.get('volatility_window', 20)
        target_risk = params.get('target_risk', 0.02)
        
        # 计算历史波动率
        df['Returns'] = df['close'].pct_change()
        df['Volatility'] = df['Returns'].rolling(window=volatility_window).std() * np.sqrt(252)  # 年化
        
        current_vol = df['Volatility'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # 风险平价仓位计算
        if current_vol > 0:
            risk_based_position = target_risk / current_vol
        else:
            risk_based_position = 0
        
        # 根据波动率判断信号
        vol_ma = df['Volatility'].rolling(window=50).mean().iloc[-1]
        
        if current_vol < vol_ma * 0.8:
            # 低波动环境，可以加仓
            signal = 'BUY'
            confidence = 0.7
            reason = '波动率低于均值，适合加仓'
        elif current_vol > vol_ma * 1.5:
            # 高波动环境，减仓或观望
            signal = 'SELL'
            confidence = 0.7
            reason = '波动率高于均值，风险过大'
        else:
            signal = 'HOLD'
            confidence = 0.5
            reason = '波动率正常，维持当前仓位'
        
        return {
            'strategy': 'Bridgewater Risk Parity',
            'signal': signal,
            'confidence': confidence,
            'current_volatility': current_vol,
            'volatility_ma': vol_ma,
            'risk_based_position': risk_based_position,
            'reason': reason,
            'description': '风险平价策略（基于波动性调整仓位）'
        }
    
    def aqr_value_momentum_strategy(self, df: pd.DataFrame,
                                     params: Dict = None) -> Dict:
        """
        AQR Capital - 价值与动量结合策略
        策略逻辑：结合价值因子（低PE/PB）和动量因子（价格趋势）
        """
        if params is None:
            params = {'value_window': 50, 'momentum_window': 20}
        
        value_window = params.get('value_window', 50)
        momentum_window = params.get('momentum_window', 20)
        
        # 价值因子：价格相对历史均值的折扣
        df['Price_MA'] = df['close'].rolling(window=value_window).mean()
        df['Value_Score'] = (df['Price_MA'] - df['close']) / df['Price_MA']
        
        # 动量因子：过去N天的收益率
        df['Momentum_Score'] = df['close'].pct_change(momentum_window)
        
        # 综合得分（价值50% + 动量50%）
        value_score = df['Value_Score'].iloc[-1]
        momentum_score = df['Momentum_Score'].iloc[-1]
        
        combined_score = value_score * 0.5 + momentum_score * 0.5
        
        if combined_score > 0.05:
            signal = 'BUY'
            confidence = 0.75
            reason = f'价值得分({value_score:.3f})和动量得分({momentum_score:.3f})均向好'
        elif combined_score < -0.05:
            signal = 'SELL'
            confidence = 0.75
            reason = f'价值得分({value_score:.3f})和动量得分({momentum_score:.3f})均向差'
        else:
            signal = 'HOLD'
            confidence = 0.5
            reason = '价值和动量信号不明确'
        
        return {
            'strategy': 'AQR Value + Momentum',
            'signal': signal,
            'confidence': confidence,
            'value_score': value_score,
            'momentum_score': momentum_score,
            'combined_score': combined_score,
            'reason': reason,
            'description': '价值与动量结合策略'
        }
    
    def two_sigma_ml_strategy(self, df: pd.DataFrame,
                               params: Dict = None) -> Dict:
        """
        Two Sigma - 机器学习预测策略
        策略逻辑：使用多因子机器学习模型预测未来价格方向
        """
        if params is None:
            params = {'prediction_window': 5, 'feature_window': 20}
        
        prediction_window = params.get('prediction_window', 5)
        feature_window = params.get('feature_window', 20)
        
        # 构建特征因子
        df['Returns'] = df['close'].pct_change()
        df['MA_Ratio'] = df['close'] / df['close'].rolling(window=feature_window).mean()
        df['Volatility'] = df['Returns'].rolling(window=feature_window).std()
        df['Volume_Change'] = df['volume'].pct_change()
        df['RSI'] = self._calculate_rsi(df['close'], 14)
        
        # 简化版机器学习信号（实际应用中会使用真实ML模型）
        # 这里使用多因子打分作为ML预测概率的替代
        features = pd.DataFrame({
            'ma_ratio': df['MA_Ratio'],
            'volatility': df['Volatility'],
            'volume_change': df['Volume_Change'],
            'rsi': df['RSI']
        })
        
        # 标准化特征
        features_norm = (features - features.mean()) / features.std()
        
        # 模拟ML预测（综合得分）
        ml_score = (
            features_norm['ma_ratio'].iloc[-1] * 0.3 +
            -features_norm['volatility'].iloc[-1] * 0.2 +  # 低波动好
            features_norm['volume_change'].iloc[-1] * 0.2 +
            (features_norm['rsi'].iloc[-1] - 50) / 50 * 0.3  # RSI标准化
        )
        
        # 将得分转换为预测概率
        prediction_prob = 1 / (1 + np.exp(-ml_score))  # Sigmoid
        
        if prediction_prob > 0.6:
            signal = 'BUY'
            confidence = prediction_prob
        elif prediction_prob < 0.4:
            signal = 'SELL'
            confidence = 1 - prediction_prob
        else:
            signal = 'HOLD'
            confidence = 0.5
        
        return {
            'strategy': 'Two Sigma ML Prediction',
            'signal': signal,
            'confidence': confidence,
            'ml_score': ml_score,
            'prediction_probability': prediction_prob,
            'description': '机器学习多因子预测策略'
        }
    
    def jump_market_making_strategy(self, df: pd.DataFrame,
                                     params: Dict = None) -> Dict:
        """
        Jump Trading - 高频做市策略
        策略逻辑：在买卖盘价差中提供流动性，赚取价差收益
        """
        if params is None:
            params = {'spread_window': 20, 'inventory_limit': 0.1}
        
        spread_window = params.get('spread_window', 20)
        inventory_limit = params.get('inventory_limit', 0.1)
        
        # 计算买卖价差（模拟）
        df['Bid_Price'] = df['close'] * (1 - 0.001)  # 买价（低0.1%）
        df['Ask_Price'] = df['close'] * (1 + 0.001)  # 卖价（高0.1%）
        df['Spread'] = df['Ask_Price'] - df['Bid_Price']
        
        # 库存管理（模拟）
        df['Inventory'] = np.random.uniform(-inventory_limit, inventory_limit, len(df))
        
        current_price = df['close'].iloc[-1]
        current_spread = df['Spread'].iloc[-1]
        current_inventory = df['Inventory'].iloc[-1]
        
        # 做市策略：根据库存调整报价
        if current_inventory > inventory_limit * 0.8:
            # 库存过高，倾向于卖出
            signal = 'SELL'
            confidence = 0.6
            reason = '库存过高，需要减仓'
        elif current_inventory < -inventory_limit * 0.8:
            # 库存过低，倾向于买入
            signal = 'BUY'
            confidence = 0.6
            reason = '库存过低，需要加仓'
        else:
            # 库存正常，保持中性
            signal = 'HOLD'
            confidence = 0.5
            reason = '库存平衡，维持做市'
        
        return {
            'strategy': 'Jump Trading Market Making',
            'signal': signal,
            'confidence': confidence,
            'bid_price': df['Bid_Price'].iloc[-1],
            'ask_price': df['Ask_Price'].iloc[-1],
            'spread': current_spread,
            'inventory': current_inventory,
            'reason': reason,
            'description': '高频做市策略（提供流动性赚取价差）'
        }
    
    def turtle_trading_strategy(self, df: pd.DataFrame,
                                params: Dict = None) -> Dict:
        """
        海龟交易法则（Turtle Trading）
        策略逻辑：突破N日高点买入，跌破N日低点卖出
        """
        if params is None:
            params = {'entry_window': 20, 'exit_window': 10}
        
        entry_window = params.get('entry_window', 20)
        exit_window = params.get('exit_window', 10)
        
        # 计算突破位
        df['Entry_High'] = df['high'].rolling(window=entry_window).max().shift(1)
        df['Entry_Low'] = df['low'].rolling(window=entry_window).min().shift(1)
        
        current_price = df['close'].iloc[-1]
        entry_high = df['Entry_High'].iloc[-1]
        entry_low = df['Entry_Low'].iloc[-1]
        
        # 判断突破
        if current_price > entry_high:
            signal = 'BUY'
            confidence = 0.73
            reason = f'价格突破{entry_window}日高点 ({entry_high:.2f})'
        elif current_price < entry_low:
            signal = 'SELL'
            confidence = 0.73
            reason = f'价格跌破{entry_window}日低点 ({entry_low:.2f})'
        else:
            signal = 'HOLD'
            confidence = 0.5
            reason = f'价格在区间内 ({entry_low:.2f} - {entry_high:.2f})'
        
        return {
            'strategy': 'Turtle Trading',
            'signal': signal,
            'confidence': confidence,
            'entry_high': entry_high,
            'entry_low': entry_low,
            'reason': reason,
            'description': f'海龟交易法则（{entry_window}日突破）'
        }
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """计算RSI指标"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    # ==================== 策略集成 ====================
    
    def _resolve_strategy_weight(
        self,
        strategy_key: str,
        display_name: str,
        learning_weights: Optional[Dict[str, float]],
        regime_multipliers: Optional[Dict[str, float]],
        strategy_performance: Optional[Dict[str, Dict]] = None,
        win_rate_strategy_cfg: Optional[Dict[str, float]] = None,
    ) -> float:
        """合并 AI 学习权重、市场状态系数与策略历史胜率。"""
        w = 1.0
        if learning_weights:
            w = float(
                learning_weights.get(display_name)
                or learning_weights.get(strategy_key)
                or 1.0
            )
        if regime_multipliers:
            if "_global" in regime_multipliers:
                w *= float(regime_multipliers["_global"])
            else:
                mult = regime_multipliers.get(strategy_key, 1.0)
                w *= float(mult)
        if strategy_performance:
            try:
                from bnb_quant_tool.win_rate_strategy import adjust_weight_by_performance

                perf = (
                    strategy_performance.get(display_name)
                    or strategy_performance.get(strategy_key)
                )
                w = adjust_weight_by_performance(w, perf, win_rate_strategy_cfg)
            except ImportError:
                pass
        return max(0.0, w)

    def run_all_strategies(
        self,
        df: pd.DataFrame,
        learning_weights: Optional[Dict[str, float]] = None,
        regime_multipliers: Optional[Dict[str, float]] = None,
        strategy_performance: Optional[Dict[str, Dict]] = None,
        win_rate_strategy_cfg: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """
        运行所有策略，生成综合信号（支持学习权重 + 市场状态加权投票）
        
        Returns:
            包含所有策略结果和综合建议的字典
        """
        results = {}
        buy_signals = 0
        sell_signals = 0
        hold_signals = 0
        total_confidence_buy = 0.0
        total_confidence_sell = 0.0
        weighted_buy = 0.0
        weighted_sell = 0.0
        total_weight = 0.0
        
        for strategy_name, strategy_func in self.strategies.items():
            try:
                result = strategy_func(df)
                results[strategy_name] = result
                display = result.get("strategy", strategy_name)
                sw = self._resolve_strategy_weight(
                    strategy_name, display, learning_weights, regime_multipliers,
                    strategy_performance=strategy_performance,
                    win_rate_strategy_cfg=win_rate_strategy_cfg,
                )
                result["vote_weight"] = round(sw, 4)
                if sw <= 0:
                    hold_signals += 1
                    continue
                total_weight += sw

                if result['signal'] == 'BUY':
                    buy_signals += 1
                    total_confidence_buy += result['confidence']
                    weighted_buy += sw * float(result.get('confidence', 0.5))
                elif result['signal'] == 'SELL':
                    sell_signals += 1
                    total_confidence_sell += result['confidence']
                    weighted_sell += sw * float(result.get('confidence', 0.5))
                else:
                    hold_signals += 1
            except Exception as e:
                logger.error(f"策略 {strategy_name} 执行失败: {e}")
                results[strategy_name] = {
                    'strategy': strategy_name,
                    'signal': 'ERROR',
                    'confidence': 0,
                    'error': str(e)
                }
        
        total_strategies = buy_signals + sell_signals + hold_signals
        use_weighted = bool(learning_weights or regime_multipliers) and total_weight > 0

        if total_strategies == 0:
            consensus_signal = 'HOLD'
            consensus_confidence = 0.5
        elif use_weighted:
            buy_ratio = weighted_buy / total_weight
            sell_ratio = weighted_sell / total_weight
            if buy_ratio > 0.35 and buy_ratio > sell_ratio * 1.15:
                consensus_signal = 'BUY'
                consensus_confidence = min(0.95, buy_ratio)
            elif sell_ratio > 0.35 and sell_ratio > buy_ratio * 1.15:
                consensus_signal = 'SELL'
                consensus_confidence = min(0.95, sell_ratio)
            else:
                consensus_signal = 'HOLD'
                consensus_confidence = 0.5
        else:
            buy_weight = buy_signals / total_strategies
            sell_weight = sell_signals / total_strategies
            if buy_weight > 0.6:
                consensus_signal = 'BUY'
                consensus_confidence = total_confidence_buy / buy_signals if buy_signals > 0 else 0.5
            elif sell_weight > 0.6:
                consensus_signal = 'SELL'
                consensus_confidence = total_confidence_sell / sell_signals if sell_signals > 0 else 0.5
            else:
                consensus_signal = 'HOLD'
                consensus_confidence = 0.5
        
        summary = {
            'timestamp': datetime.now().isoformat(),
            'total_strategies': total_strategies,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'hold_signals': hold_signals,
            'consensus_signal': consensus_signal,
            'consensus_confidence': consensus_confidence,
            'weighted_voting': use_weighted,
            'strategy_details': results
        }
        
        logger.info(f"策略集成完成: {consensus_signal} (置信度: {consensus_confidence:.2f})")
        logger.info(f"看多: {buy_signals}, 看空: {sell_signals}, 观望: {hold_signals}")
        
        return summary
    
    def get_strategy_weights(self) -> Dict:
        """
        获取各策略的权重（可用于调整策略重要性）
        
        Returns:
            策略权重字典
        """
        # 默认等权重，可以根据历史表现调整
        weights = {}
        for strategy_name in self.strategies.keys():
            weights[strategy_name] = 1.0 / len(self.strategies)
        return weights
    
    def optimize_strategy_weights(self, df: pd.DataFrame, 
                                  lookback_periods: int = 100) -> Dict:
        """
        根据近期表现优化策略权重（简化版）
        
        Args:
            df: 价格数据
            lookback_periods: 回看期数
            
        Returns:
            优化后的策略权重
        """
        # 简化版：根据策略稳定性分配权重
        # 实际应用中会使用更复杂的优化算法（如马科维茨均值方差优化）
        
        performance_scores = {}
        
        for strategy_name, strategy_func in self.strategies.items():
            try:
                # 回测策略表现（简化）
                results = strategy_func(df.tail(lookback_periods))
                # 根据置信度和信号一致性打分
                score = results.get('confidence', 0.5)
                performance_scores[strategy_name] = score
            except Exception:
                performance_scores[strategy_name] = 0.5
        
        # 归一化为权重
        total_score = sum(performance_scores.values())
        if total_score > 0:
            weights = {k: v / total_score for k, v in performance_scores.items()}
        else:
            weights = self.get_strategy_weights()
        
        return weights


if __name__ == "__main__":
    # 测试代码
    import numpy as np
    
    # 生成模拟数据
    dates = pd.date_range(start='2024-01-01', periods=200, freq='H')
    np.random.seed(42)
    prices = 600 + np.cumsum(np.random.randn(200) * 5)
    
    df = pd.DataFrame({
        'open_time': dates,
        'open': prices + np.random.randn(200),
        'high': prices + abs(np.random.randn(200) * 2),
        'low': prices - abs(np.random.randn(200) * 2),
        'close': prices,
        'volume': np.random.uniform(1000, 5000, 200)
    })
    
    # 初始化策略
    strategies = InstitutionalStrategies()
    
    # 运行所有策略
    print("=" * 60)
    print("大机构研究策略分析")
    print("=" * 60)
    
    results = strategies.run_all_strategies(df)
    
    print(f"\n综合信号: {results['consensus_signal']}")
    print(f"置信度: {results['consensus_confidence']:.2f}")
    print(f"\n策略分布:")
    print(f"  看多: {results['buy_signals']}")
    print(f"  看空: {results['sell_signals']}")
    print(f"  观望: {results['hold_signals']}")
    
    print(f"\n详细策略信号:")
    for strategy_name, result in results['strategy_details'].items():
        print(f"  {result['strategy']}: {result['signal']} (置信度: {result['confidence']:.2f})")
        print(f"     {result.get('description', '')}")
    
    print("\n" + "=" * 60)
