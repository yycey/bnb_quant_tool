"""
BNB量化交易工具 - 技术指标计算模块
计算各种技术指标用于AI分析和交易决策
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算相对强弱指标 (RSI)
        
        Args:
            df: 包含'close'列的DataFrame
            period: RSI周期，默认14
            
        Returns:
            RSI值序列
        """
        close_prices = df['close']
        delta = close_prices.diff()
        
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        logger.debug(f"RSI计算完成，最新值: {rsi.iloc[-1]:.2f}")
        return rsi
    
    @staticmethod
    def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, 
                       signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        计算MACD指标
        
        Args:
            df: 包含'close'列的DataFrame
            fast: 快线周期
            slow: 慢线周期
            signal: 信号线周期
            
        Returns:
            (macd_line, signal_line, histogram) 元组
        """
        close_prices = df['close']
        
        ema_fast = close_prices.ewm(span=fast, adjust=False).mean()
        ema_slow = close_prices.ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        logger.debug(f"MACD计算完成，最新MACD: {macd_line.iloc[-1]:.4f}")
        return macd_line, signal_line, histogram
    
    @staticmethod
    def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, 
                                   std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        计算布林带
        
        Args:
            df: 包含'close'列的DataFrame
            period: 移动平均周期
            std_dev: 标准差倍数
            
        Returns:
            (upper_band, middle_band, lower_band) 元组
        """
        close_prices = df['close']
        
        middle_band = close_prices.rolling(window=period).mean()
        std = close_prices.rolling(window=period).std()
        
        upper_band = middle_band + (std * std_dev)
        lower_band = middle_band - (std * std_dev)
        
        logger.debug(f"布林带计算完成，上轨: {upper_band.iloc[-1]:.2f}, 下轨: {lower_band.iloc[-1]:.2f}")
        return upper_band, middle_band, lower_band
    
    @staticmethod
    def calculate_moving_averages(df: pd.DataFrame, periods: List[int] = [20, 50, 200]) -> Dict[str, pd.Series]:
        """
        计算移动平均线
        
        Args:
            df: 包含'close'列的DataFrame
            periods: 移动平均周期列表
            
        Returns:
            移动平均线字典，key为'MA_{period}'
        """
        close_prices = df['close']
        ma_dict = {}
        
        for period in periods:
            ma_dict[f'MA_{period}'] = close_prices.rolling(window=period).mean()
            logger.debug(f"MA{period}计算完成，最新值: {ma_dict[f'MA_{period}'].iloc[-1]:.2f}")
        
        return ma_dict
    
    @staticmethod
    def calculate_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算成交量简单移动平均
        
        Args:
            df: 包含'volume'列的DataFrame
            period: 移动平均周期
            
        Returns:
            成交量移动平均序列
        """
        volume_sma = df['volume'].rolling(window=period).mean()
        logger.debug(f"成交量SMA计算完成")
        return volume_sma
    
    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算平均真实波幅 (ATR)
        
        Args:
            df: 包含'high', 'low', 'close'列的DataFrame
            period: ATR周期
            
        Returns:
            ATR值序列
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        
        logger.debug(f"ATR计算完成，最新值: {atr.iloc[-1]:.2f}")
        return atr
    
    @staticmethod
    def calculate_stochastic(df: pd.DataFrame, period: int = 14, 
                             smooth_k: int = 3, smooth_d: int = 3) -> Tuple[pd.Series, pd.Series]:
        """
        计算随机指标 (Stochastic Oscillator)
        
        Args:
            df: 包含'high', 'low', 'close'列的DataFrame
            period: %K周期
            smooth_k: %K平滑周期
            smooth_d: %D周期
            
        Returns:
            (%K, %D) 元组
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        lowest_low = low.rolling(window=period).min()
        highest_high = high.rolling(window=period).max()
        
        denom = (highest_high - lowest_low).replace(0, np.nan)
        k = 100 * ((close - lowest_low) / denom)
        k_smooth = k.rolling(window=smooth_k).mean()
        d = k_smooth.rolling(window=smooth_d).mean()
        
        logger.debug(f"随机指标计算完成，%K: {k_smooth.iloc[-1]:.2f}, %D: {d.iloc[-1]:.2f}")
        return k_smooth, d

    @staticmethod
    def calculate_adx(
        df: pd.DataFrame, period: int = 14
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """计算 ADX / +DI / -DI（趋势强度，Moomoo 趋势交易确认）。"""
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = pd.Series(true_range, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * (
            pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
            / atr.replace(0, np.nan)
        )
        minus_di = 100 * (
            pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
            / atr.replace(0, np.nan)
        )
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return adx, plus_di, minus_di

    @staticmethod
    def calculate_obv(df: pd.DataFrame) -> pd.Series:
        """计算能量潮 OBV（量价分析）。"""
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        direction = np.sign(close.diff()).fillna(0.0)
        obv = (direction * volume).cumsum()
        return obv

    @staticmethod
    def calculate_support_resistance(
        df: pd.DataFrame, lookback: int = 50, pivot_window: int = 3
    ) -> Dict[str, float]:
        """用近期枢轴高低点估计支撑/阻力（波段交易）。"""
        lookback = max(10, min(int(lookback), len(df)))
        window = df.tail(lookback)
        highs = window["high"].astype(float)
        lows = window["low"].astype(float)
        close = float(window["close"].iloc[-1])

        piv_highs: List[float] = []
        piv_lows: List[float] = []
        pw = max(1, int(pivot_window))
        for i in range(pw, len(window) - pw):
            h_slice = highs.iloc[i - pw : i + pw + 1]
            l_slice = lows.iloc[i - pw : i + pw + 1]
            h_val = float(highs.iloc[i])
            l_val = float(lows.iloc[i])
            if h_val >= float(h_slice.max()):
                piv_highs.append(h_val)
            if l_val <= float(l_slice.min()):
                piv_lows.append(l_val)

        resistance = min((p for p in piv_highs if p > close), default=float(highs.max()))
        support = max((p for p in piv_lows if p < close), default=float(lows.min()))
        mid = (support + resistance) / 2.0 if resistance > support else close
        range_width = (resistance - support) / close if close > 0 else 0.0
        return {
            "Support": float(support),
            "Resistance": float(resistance),
            "SR_Mid": float(mid),
            "SR_Range_Pct": float(range_width),
            "Price_To_Support": (close - support) / close if close > 0 else 0.0,
            "Price_To_Resistance": (resistance - close) / close if close > 0 else 0.0,
        }
    
    @staticmethod
    def calculate_all_indicators(df: pd.DataFrame) -> Dict:
        """
        计算所有技术指标
        
        Args:
            df: 包含OHLCV数据的DataFrame
            
        Returns:
            所有技术指标的集合字典
        """
        # 同 tick 多策略复用：避免刷屏与重复计算
        try:
            n = len(df)
            c0 = float(df["close"].iloc[-1])
            c1 = float(df["close"].iloc[-2]) if n > 1 else 0.0
            v0 = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
            key = (n, round(c0, 8), round(c1, 8), round(v0, 4))
            cached = getattr(TechnicalIndicators, "_all_ind_cache", None)
            if isinstance(cached, dict) and cached.get("key") == key:
                return dict(cached["val"])
        except Exception:
            key = None

        indicators = {}
        
        # RSI
        indicators['RSI'] = TechnicalIndicators.calculate_rsi(df).iloc[-1]
        
        # MACD
        macd, signal, hist = TechnicalIndicators.calculate_macd(df)
        indicators['MACD'] = macd.iloc[-1]
        indicators['MACD_Signal'] = signal.iloc[-1]
        indicators['MACD_Histogram'] = hist.iloc[-1]
        
        # 布林带
        bb_upper, bb_middle, bb_lower = TechnicalIndicators.calculate_bollinger_bands(df)
        indicators['BB_Upper'] = bb_upper.iloc[-1]
        indicators['BB_Middle'] = bb_middle.iloc[-1]
        indicators['BB_Lower'] = bb_lower.iloc[-1]
        
        # 移动平均线
        ma_dict = TechnicalIndicators.calculate_moving_averages(df)
        for key_ma, value in ma_dict.items():
            indicators[key_ma] = value.iloc[-1]
        
        # ATR
        indicators['ATR'] = TechnicalIndicators.calculate_atr(df).iloc[-1]
        
        # 随机指标
        stoch_k, stoch_d = TechnicalIndicators.calculate_stochastic(df)
        indicators['Stoch_K'] = stoch_k.iloc[-1]
        indicators['Stoch_D'] = stoch_d.iloc[-1]

        # ADX 趋势强度
        adx, plus_di, minus_di = TechnicalIndicators.calculate_adx(df)
        indicators["ADX"] = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0
        indicators["Plus_DI"] = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0
        indicators["Minus_DI"] = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0

        # OBV 量价
        obv = TechnicalIndicators.calculate_obv(df)
        indicators["OBV"] = float(obv.iloc[-1])
        obv_ma = obv.rolling(20).mean()
        indicators["OBV_MA20"] = float(obv_ma.iloc[-1]) if pd.notna(obv_ma.iloc[-1]) else indicators["OBV"]
        if len(obv) >= 6 and pd.notna(obv.iloc[-6]):
            indicators["OBV_Slope"] = float(obv.iloc[-1] - obv.iloc[-6]) / 5.0
        else:
            indicators["OBV_Slope"] = 0.0

        # 支撑 / 阻力
        sr = TechnicalIndicators.calculate_support_resistance(df)
        indicators.update(sr)
        
        # 成交量
        indicators['Volume'] = df['volume'].iloc[-1]
        indicators['Volume_SMA'] = TechnicalIndicators.calculate_volume_sma(df).iloc[-1]
        indicators['Volume_Ratio'] = indicators['Volume'] / indicators['Volume_SMA'] if indicators['Volume_SMA'] > 0 else 1.0
        
        # 价格位置 (相对于布林带)
        current_price = df['close'].iloc[-1]
        bb_width = indicators['BB_Upper'] - indicators['BB_Lower']
        if bb_width and bb_width > 0:
            bb_position = (current_price - indicators['BB_Lower']) / bb_width * 100
        else:
            bb_position = 50.0
        indicators['BB_Position'] = bb_position

        # 黄金/死亡交叉状态（IG MA 交叉框架）
        ma50 = indicators.get("MA_50")
        ma200 = indicators.get("MA_200")
        if ma50 is not None and ma200 is not None and pd.notna(ma50) and pd.notna(ma200):
            indicators["Golden_Cross_State"] = 1.0 if float(ma50) > float(ma200) else -1.0
        else:
            indicators["Golden_Cross_State"] = 0.0
        
        logger.debug("所有技术指标计算完成")
        if key is not None:
            TechnicalIndicators._all_ind_cache = {"key": key, "val": dict(indicators)}
        return indicators


if __name__ == "__main__":
    # 测试代码
    import numpy as np
    
    # 生成模拟数据
    dates = pd.date_range(start='2024-01-01', periods=300, freq='H')
    data = {
        'open_time': dates,
        'open': np.random.uniform(550, 650, 300),
        'high': np.random.uniform(560, 660, 300),
        'low': np.random.uniform(540, 640, 300),
        'close': np.random.uniform(550, 650, 300),
        'volume': np.random.uniform(1000, 5000, 300)
    }
    df = pd.DataFrame(data)
    
    # 计算技术指标
    indicators = TechnicalIndicators.calculate_all_indicators(df)
    
    print("技术指标计算结果:")
    for key, value in indicators.items():
        print(f"{key}: {value:.4f}")
