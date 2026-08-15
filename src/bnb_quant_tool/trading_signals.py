"""
BNB量化交易工具 - 交易信号生成模块
结合技术指标和AI分析，生成具体的交易信号
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TradingSignals:
    """交易信号生成器"""
    
    def __init__(self, config: Dict):
        """
        初始化交易信号生成器
        
        Args:
            config: 配置字典，包含交易参数
        """
        self.config = config
        self.symbol = config.get('symbol', 'BNBUSDT')
        self.risk_per_trade = config.get('risk_per_trade', 0.02)
        self.confidence_threshold = config.get('confidence_threshold', 0.7)
        
    def generate_technical_signals(self, df: pd.DataFrame, 
                                    indicators: Dict) -> Dict:
        """
        基于技术指标生成交易信号
        
        Args:
            df: 包含价格和技术指标的DataFrame
            indicators: 技术指标字典
            
        Returns:
            技术信号字典
        """
        signals = {
            'signal': 'HOLD',
            'confidence': 0.5,
            'reasons': []
        }
        
        current_price = df['close'].iloc[-1]
        
        # RSI信号
        rsi = indicators.get('RSI', 50)
        if rsi < 30:
            signals['reasons'].append(f'RSI超卖 ({rsi:.1f} < 30)')
            signals['confidence'] += 0.15
        elif rsi > 70:
            signals['reasons'].append(f'RSI超买 ({rsi:.1f} > 70)')
            signals['confidence'] -= 0.15
        
        # MACD信号
        macd = indicators.get('MACD', 0)
        macd_signal = indicators.get('MACD_Signal', 0)
        macd_hist = indicators.get('MACD_Histogram', 0)
        
        if macd > macd_signal and macd_hist > 0:
            signals['reasons'].append('MACD金叉且柱状图为正')
            signals['confidence'] += 0.2
        elif macd < macd_signal and macd_hist < 0:
            signals['reasons'].append('MACD死叉且柱状图为负')
            signals['confidence'] -= 0.2
        
        # 布林带信号
        bb_position = indicators.get('BB_Position', 50)
        if bb_position < 10:
            signals['reasons'].append(f'价格接近布林带下轨 ({bb_position:.1f}%)')
            signals['confidence'] += 0.15
        elif bb_position > 90:
            signals['reasons'].append(f'价格接近布林带上轨 ({bb_position:.1f}%)')
            signals['confidence'] -= 0.15
        
        # 移动平均线信号
        ma_20 = indicators.get('MA_20', current_price)
        ma_50 = indicators.get('MA_50', current_price)
        
        if current_price > ma_20 > ma_50:
            signals['reasons'].append('价格在所有均线上方，多头排列')
            signals['confidence'] += 0.15
        elif current_price < ma_20 < ma_50:
            signals['reasons'].append('价格在所有均线下方，空头排列')
            signals['confidence'] -= 0.15
        
        # 成交量确认
        volume_ratio = indicators.get('Volume_Ratio', 1.0)
        if volume_ratio > 1.5:
            signals['reasons'].append(f'成交量放大 ({volume_ratio:.1f}x)')
            signals['confidence'] += 0.1
        
        # 确定最终信号
        if signals['confidence'] >= self.confidence_threshold:
            signals['signal'] = 'BUY'
        elif signals['confidence'] <= (1 - self.confidence_threshold):
            signals['signal'] = 'SELL'
        else:
            signals['signal'] = 'HOLD'
        
        signals['confidence'] = max(0.0, min(1.0, signals['confidence']))
        
        logger.info(f"技术信号生成完成: {signals['signal']} (置信度: {signals['confidence']:.2f})")
        return signals
    
    def combine_with_ai_analysis(self, technical_signals: Dict, 
                                  ai_analysis: Dict) -> Dict:
        """
        结合技术信号和AI分析
        
        Args:
            technical_signals: 技术信号
            ai_analysis: AI分析结果
            
        Returns:
            综合交易建议
        """
        combined = {
            'timestamp': datetime.now().isoformat(),
            'symbol': self.symbol,
            'technical_signal': technical_signals['signal'],
            'technical_confidence': technical_signals['confidence'],
            'ai_signal': ai_analysis.get('signal', '持有'),
            'ai_confidence': ai_analysis.get('confidence', 0.5),
            'trend': ai_analysis.get('trend', '未知'),
            'entry_price': ai_analysis.get('entry_price'),
            'stop_loss': ai_analysis.get('stop_loss'),
            'take_profit': ai_analysis.get('take_profit'),
            'risk_reward_ratio': ai_analysis.get('risk_reward_ratio', 0),
            'analysis': ai_analysis.get('analysis', ''),
            'key_levels': ai_analysis.get('key_levels', []),
            'risks': ai_analysis.get('risks', [])
        }
        
        # 综合判断
        tech_weight = 0.4
        ai_weight = 0.6
        
        tech_score = technical_signals['confidence'] if technical_signals['signal'] == 'BUY' else \
                   -technical_signals['confidence'] if technical_signals['signal'] == 'SELL' else 0
        
        # 兼容中英文信号（AI 可能返回 '买入'/'卖出' 或 'BUY'/'SELL'）
        ai_signal = ai_analysis.get('signal', '')
        ai_conf = ai_analysis.get('confidence', 0.5) or 0.5
        if ai_signal in ('买入', 'BUY', 'buy'):
            ai_score = ai_conf
        elif ai_signal in ('卖出', 'SELL', 'sell'):
            ai_score = -ai_conf
        else:
            ai_score = 0
        
        final_score = tech_weight * tech_score + ai_weight * ai_score
        
        if final_score > 0.3:
            combined['final_signal'] = 'BUY'
        elif final_score < -0.3:
            combined['final_signal'] = 'SELL'
        else:
            combined['final_signal'] = 'HOLD'
        
        combined['final_score'] = round(final_score, 3)
        
        logger.info(f"综合信号: {combined['final_signal']} (得分: {combined['final_score']})")
        return combined
    
    def calculate_position_size(self, entry_price: float, stop_loss: float, 
                                account_balance: float) -> Tuple[float, float]:
        """
        计算仓位大小
        
        Args:
            entry_price: 入场价格
            stop_loss: 止损价格
            account_balance: 账户余额
            
        Returns:
            (仓位大小(BNB数量), 风险金额)
        """
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit == 0:
            return 0, 0
        
        risk_amount = account_balance * self.risk_per_trade
        position_size = risk_amount / risk_per_unit
        
        logger.info(f"仓位计算: {position_size:.4f} BNB (风险: ${risk_amount:.2f})")
        return position_size, risk_amount
    
    def generate_trading_plan(self, combined_signal: Dict, 
                              account_balance: float) -> Dict:
        """
        生成完整的交易计划
        
        Args:
            combined_signal: 综合信号
            account_balance: 账户余额
            
        Returns:
            交易计划字典
        """
        if combined_signal['final_signal'] == 'HOLD':
            return {
                'action': 'HOLD',
                'reason': '综合信号不明确，建议观望',
                'timestamp': datetime.now().isoformat()
            }
        
        entry_price = combined_signal.get('entry_price')
        stop_loss = combined_signal.get('stop_loss')
        take_profit = combined_signal.get('take_profit')
        
        if not all([entry_price, stop_loss, take_profit]):
            return {
                'action': 'HOLD',
                'reason': '缺少入场、止损或止盈价格',
                'timestamp': datetime.now().isoformat()
            }
        
        position_size, risk_amount = self.calculate_position_size(
            entry_price, stop_loss, account_balance
        )
        
        plan = {
            'action': combined_signal['final_signal'],
            'symbol': self.symbol,
            'timestamp': datetime.now().isoformat(),
            'entry': {
                'price': entry_price,
                'type': 'LIMIT',
                'time_in_force': 'GTC'
            },
            'risk_management': {
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'position_size': position_size,
                'position_value': position_size * entry_price,
                'risk_amount': risk_amount,
                'risk_reward_ratio': combined_signal.get('risk_reward_ratio', 0)
            },
            'analysis_summary': {
                'trend': combined_signal.get('trend'),
                'confidence': combined_signal.get('ai_confidence', 0),
                'technical_reasons': combined_signal.get('technical_signal'),
                'ai_analysis': combined_signal.get('analysis', '')
            },
            'key_levels': combined_signal.get('key_levels', []),
            'warnings': combined_signal.get('risks', [])
        }
        
        logger.info(f"交易计划生成完成: {plan['action']} {plan['risk_management']['position_size']:.4f} BNB")
        return plan


if __name__ == "__main__":
    # 测试代码
    config = {
        'symbol': 'BNBUSDT',
        'risk_per_trade': 0.02,
        'confidence_threshold': 0.7
    }
    
    generator = TradingSignals(config)
    
    # 模拟数据
    dates = pd.date_range(start='2024-01-01', periods=100, freq='H')
    df = pd.DataFrame({
        'open_time': dates,
        'open': np.random.uniform(550, 650, 100),
        'high': np.random.uniform(560, 660, 100),
        'low': np.random.uniform(540, 640, 100),
        'close': np.random.uniform(550, 650, 100),
        'volume': np.random.uniform(1000, 5000, 100)
    })
    
    indicators = {
        'RSI': 45.5,
        'MACD': 2.3,
        'MACD_Signal': 1.8,
        'MACD_Histogram': 0.5,
        'BB_Position': 50.0,
        'MA_20': 600,
        'MA_50': 590,
        'Volume_Ratio': 1.2
    }
    
    # 生成技术信号
    tech_signals = generator.generate_technical_signals(df, indicators)
    print(f"技术信号: {tech_signals['signal']} (置信度: {tech_signals['confidence']:.2f})")
    print(f"原因: {tech_signals['reasons']}")
