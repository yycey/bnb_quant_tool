"""
BNB量化交易工具 - 风险管理模块
负责仓位管理、止损止盈、风险监控
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RiskManager:
    """风险管理系统"""
    
    def __init__(self, config: Dict):
        """
        初始化风险管理器
        
        Args:
            config: 配置字典
        """
        self.max_risk_per_trade = config.get('max_risk_per_trade', 0.02)
        self.max_open_positions = config.get('max_open_positions', 0)
        self.max_daily_loss = config.get('max_daily_loss', 0.05)
        self.min_risk_reward_ratio = config.get('min_risk_reward_ratio', 1.5)
        
        self.open_positions = []
        self.daily_pnl = 0.0
        self.total_balance = config.get('initial_balance', 10000.0)
        
    def validate_trade(self, trade_plan: Dict) -> Tuple[bool, str]:
        """
        验证交易计划是否符合风险管理规则
        
        Args:
            trade_plan: 交易计划字典
            
        Returns:
            (是否通过, 原因)
        """
        # 检查最大持仓数量
        if self.max_open_positions > 0 and len(self.open_positions) >= self.max_open_positions:
            return False, f"已达到最大持仓数量 ({self.max_open_positions})"
        
        # 检查风险回报比（缺失/0 表示尚未形成有效计划，不误杀）
        risk_reward = trade_plan.get('risk_management', {}).get('risk_reward_ratio', 0)
        try:
            risk_reward = float(risk_reward or 0)
        except (TypeError, ValueError):
            risk_reward = 0.0
        if risk_reward <= 0:
            return False, "风险回报比无效（入场/止损/止盈未形成），跳过开仓"
        if risk_reward < self.min_risk_reward_ratio:
            return False, f"风险回报比过低 ({risk_reward:.2f} < {self.min_risk_reward_ratio})"
        
        # 检查单笔风险
        risk_amount = trade_plan.get('risk_management', {}).get('risk_amount', 0)
        risk_ratio = risk_amount / self.total_balance
        if risk_ratio > self.max_risk_per_trade:
            return False, f"单笔风险过高 ({risk_ratio:.2%} > {self.max_risk_per_trade:.2%})"
        
        # 检查当日亏损
        if self.daily_pnl < -self.total_balance * self.max_daily_loss:
            return False, f"当日亏损已达上限 ({self.max_daily_loss:.2%})"
        
        return True, "通过风险检查"
    
    def calculate_position_size(self, entry: float, stop_loss: float, 
                                account_balance: float) -> float:
        """
        计算合理的仓位大小
        
        Args:
            entry: 入场价
            stop_loss: 止损价
            account_balance: 账户余额
            
        Returns:
            仓位大小（BNB数量）
        """
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit == 0:
            return 0
        
        risk_amount = account_balance * self.max_risk_per_trade
        position_size = risk_amount / risk_per_unit
        
        logger.info(f"仓位计算: {position_size:.4f} BNB (风险: ${risk_amount:.2f})")
        return position_size
    
    def update_position(self, position: Dict, current_price: float):
        """
        更新持仓状态和盈亏
        
        Args:
            position: 持仓信息
            current_price: 当前价格
        """
        entry_price = position['entry_price']
        position_size = position['size']
        
        if position['side'] == 'LONG':
            unrealized_pnl = (current_price - entry_price) * position_size
        else:  # SHORT
            unrealized_pnl = (entry_price - current_price) * position_size
        
        position['unrealized_pnl'] = unrealized_pnl
        position['current_price'] = current_price
        position['updated_at'] = datetime.now().isoformat()
        
        logger.debug(f"持仓更新: {position['symbol']} 浮动盈亏: ${unrealized_pnl:.2f}")
        
    def check_stop_loss(self, position: Dict, current_price: float) -> bool:
        """
        检查是否触发止损
        
        Args:
            position: 持仓信息
            current_price: 当前价格
            
        Returns:
            是否触发止损
        """
        stop_loss = position.get('stop_loss')
        if not stop_loss:
            return False
        
        if position['side'] == 'LONG' and current_price <= stop_loss:
            logger.warning(f"触发止损: {position['symbol']} 当前价 ${current_price} <= 止损价 ${stop_loss}")
            return True
        elif position['side'] == 'SHORT' and current_price >= stop_loss:
            logger.warning(f"触发止损: {position['symbol']} 当前价 ${current_price} >= 止损价 ${stop_loss}")
            return True
        
        return False
    
    def check_take_profit(self, position: Dict, current_price: float) -> bool:
        """
        检查是否触发止盈
        
        Args:
            position: 持仓信息
            current_price: 当前价格
            
        Returns:
            是否触发止盈
        """
        take_profit = position.get('take_profit')
        if not take_profit:
            return False
        
        if position['side'] == 'LONG' and current_price >= take_profit:
            logger.info(f"触发止盈: {position['symbol']} 当前价 ${current_price} >= 止盈价 ${take_profit}")
            return True
        elif position['side'] == 'SHORT' and current_price <= take_profit:
            logger.info(f"触发止盈: {position['symbol']} 当前价 ${current_price} <= 止盈价 ${take_profit}")
            return True
        
        return False
    
    def add_position(self, position: Dict):
        """
        添加新持仓
        
        Args:
            position: 持仓信息
        """
        self.open_positions.append(position)
        logger.info(f"新增持仓: {position['symbol']} {position['side']} {position['size']:.4f} BNB")
        
    def close_position(self, position_id: str, exit_price: float, reason: str = ""):
        """
        关闭持仓
        
        Args:
            position_id: 持仓ID
            exit_price: 退出价格
            reason: 平仓原因
        """
        for i, pos in enumerate(self.open_positions):
            if pos.get('id') == position_id:
                # 计算实际盈亏
                entry_price = pos['entry_price']
                size = pos['size']
                
                if pos['side'] == 'LONG':
                    realized_pnl = (exit_price - entry_price) * size
                else:
                    realized_pnl = (entry_price - exit_price) * size
                
                pos['realized_pnl'] = realized_pnl
                pos['exit_price'] = exit_price
                pos['exit_time'] = datetime.now().isoformat()
                pos['close_reason'] = reason
                
                # 更新日盈亏
                self.daily_pnl += realized_pnl
                
                # 从开放持仓中移除
                self.open_positions.pop(i)
                
                logger.info(f"平仓: {pos['symbol']} 盈亏: ${realized_pnl:.2f} 原因: {reason}")
                break
    
    def get_portfolio_summary(self) -> Dict:
        """
        获取投资组合摘要
        
        Returns:
            投资组合信息
        """
        total_unrealized = sum([p.get('unrealized_pnl', 0) for p in self.open_positions])
        
        summary = {
            'total_balance': self.total_balance,
            'open_positions': len(self.open_positions),
            'unrealized_pnl': total_unrealized,
            'daily_pnl': self.daily_pnl,
            'daily_pnl_pct': (self.daily_pnl / self.total_balance * 100) if self.total_balance > 0 else 0,
            'positions': self.open_positions.copy(),
            'risk_exposure': self._calculate_risk_exposure()
        }
        
        return summary
    
    def _calculate_risk_exposure(self) -> float:
        """
        计算当前风险暴露比例
        
        Returns:
            风险暴露比例 (0-1)
        """
        total_risk = 0
        for pos in self.open_positions:
            entry = pos['entry_price']
            stop = pos.get('stop_loss', entry * 0.95)
            size = pos['size']
            risk = abs(entry - stop) * size
            total_risk += risk
        
        return total_risk / self.total_balance if self.total_balance > 0 else 0
    
    def reset_daily_pnl(self):
        """重置当日盈亏（新的一天）"""
        self.daily_pnl = 0.0
        logger.info("当日盈亏已重置")


if __name__ == "__main__":
    # 测试代码
    config = {
        'max_risk_per_trade': 0.02,
        'max_open_positions': 3,
        'max_daily_loss': 0.05,
        'min_risk_reward_ratio': 1.5,
        'initial_balance': 10000.0
    }
    
    rm = RiskManager(config)
    
    # 测试交易计划验证
    test_plan = {
        'risk_management': {
            'risk_reward_ratio': 2.0,
            'risk_amount': 200.0
        }
    }
    
    is_valid, reason = rm.validate_trade(test_plan)
    print(f"交易验证: {is_valid}, 原因: {reason}")
    
    # 测试仓位计算
    position_size = rm.calculate_position_size(
        entry=600.0,
        stop_loss=580.0,
        account_balance=10000.0
    )
    print(f"建议仓位: {position_size:.4f} BNB")
