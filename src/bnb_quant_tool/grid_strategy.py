"""
BNB量化交易工具 - 网格交易策略模块
基于AI历史分析和策略信号，自动计算网格参数
支持：对称网格、上涨网格、下跌网格
"""

import sqlite3
import json
import math
import threading
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GridStrategy:
    """网格交易策略生成器"""

    def __init__(self, db_path: str = None):
        """
        初始化网格策略生成器
        Args:
            db_path: AI学习数据库路径，None则自动查找
        """
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                db_path = str(get_localized_db_path('ai_learning'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent / "data"
                if not base_dir.exists():
                    base_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(base_dir / "ai_learning.db")
        self.db_path = db_path
        self._local = threading.local()

    def _get_conn(self):
        """线程本地连接（WAL模式，避免 database is locked）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self.db_path, timeout=60.0)
        return self._local.conn

    def get_recent_analysis(self, limit: int = 10) -> List[Dict]:
        """从学习系统获取最近的分析记录"""
        if not os.path.exists(self.db_path):
            logger.warning(f"数据库不存在: {self.db_path}")
            return []

        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('''SELECT id, timestamp, symbol, final_signal, current_price,
                                entry_price, stop_loss, take_profit,
                                ai_analysis, ai_confidence
                         FROM analysis_records
                         ORDER BY id DESC LIMIT ?''', (limit,))
            rows = c.fetchall()
        except Exception as e:
            logger.error(f"读取数据库失败: {e}")
            return []

        results = []
        for row in rows:
            (rid, ts, sym, signal, price, entry, sl, tp, ai_json, ai_conf) = row
            item = {
                'id': rid, 'timestamp': ts, 'symbol': sym,
                'signal': signal, 'price': price,
                'entry': entry, 'stop_loss': sl, 'take_profit': tp,
                'ai_confidence': ai_conf or 0.5
            }
            if ai_json:
                try:
                    item['ai_analysis'] = json.loads(ai_json)
                except (json.JSONDecodeError, TypeError):
                    item['ai_analysis'] = {}
            results.append(item)
        return results

    def calculate_grid_params(self, recent_analyses: List[Dict],
                              grid_count: Optional[int] = None,
                              grid_type: str = 'auto') -> Dict:
        """
        根据最近分析计算网格参数

        Args:
            recent_analyses: 最近的分析记录列表
            grid_count: 网格数量（None=自动计算）
            grid_type: 'auto'|'symmetrical'|'upward'|'downward'

        Returns:
            网格参数字典
        """
        if not recent_analyses:
            return {'error': '没有分析记录，请先运行分析'}

        # 收集价格数据
        prices = [a['price'] for a in recent_analyses if a.get('price')]
        entries = [a['entry'] for a in recent_analyses if a.get('entry')]
        sls = [a['stop_loss'] for a in recent_analyses if a.get('stop_loss')]
        tps = [a['take_profit'] for a in recent_analyses if a.get('take_profit')]

        if not prices:
            return {'error': '分析记录中缺少价格数据'}

        # 确定价格区间
        all_lows = prices + sls
        all_highs = prices + tps
        all_entries_prices = prices + entries

        raw_low = min(all_lows) * 0.99   # 留1%缓冲
        raw_high = max(all_highs) * 1.01  # 留1%缓冲
        mid_price = sum(prices) / len(prices)

        # 自动判断网格类型
        if grid_type == 'auto':
            buy_count = sum(1 for a in recent_analyses if a.get('signal') == 'BUY')
            sell_count = sum(1 for a in recent_analyses if a.get('signal') == 'SELL')
            if buy_count > sell_count * 1.5:
                grid_type = 'upward'
            elif sell_count > buy_count * 1.5:
                grid_type = 'downward'
            else:
                grid_type = 'symmetrical'

        # 根据网格类型调整区间
        price_low = raw_low
        price_high = raw_high

        # 自动计算网格数量
        if grid_count is None:
            price_range_pct = (price_high - price_low) / price_low * 100
            if price_range_pct < 5:
                grid_count = 10
            elif price_range_pct < 10:
                grid_count = 15
            elif price_range_pct < 20:
                grid_count = 20
            else:
                grid_count = 30

        # 生成网格线
        grid_levels = []
        if grid_type == 'symmetrical':
            step = (price_high - price_low) / (grid_count - 1)
            for i in range(grid_count):
                price = price_low + step * i
                action = 'BUY' if price < mid_price else 'SELL'
                grid_levels.append({
                    'level': i,
                    'price': round(price, 2),
                    'action': action,
                    'distance_pct': round((price - price_low) / price_low * 100, 2)
                })
        elif grid_type == 'upward':
            # 上涨网格：下方密集（买区），上方稀疏（卖区）
            buy_count = int(grid_count * 0.6)
            sell_count = grid_count - buy_count
            buy_step = (mid_price - price_low) / (buy_count - 1) if buy_count > 1 else 0
            sell_step = (price_high - mid_price) / (sell_count - 1) if sell_count > 1 else 0

            for i in range(buy_count):
                price = price_low + buy_step * i
                grid_levels.append({
                    'level': i, 'price': round(price, 2),
                    'action': 'BUY',
                    'distance_pct': round((price - price_low) / price_low * 100, 2)
                })
            for i in range(sell_count):
                price = mid_price + sell_step * i
                grid_levels.append({
                    'level': buy_count + i, 'price': round(price, 2),
                    'action': 'SELL',
                    'distance_pct': round((price - price_low) / price_low * 100, 2)
                })
        elif grid_type == 'downward':
            # 下跌网格：上方密集（卖区），下方稀疏（买区）
            sell_count = int(grid_count * 0.6)
            buy_count = grid_count - sell_count
            sell_step = (price_high - mid_price) / (sell_count - 1) if sell_count > 1 else 0
            buy_step = (mid_price - price_low) / (buy_count - 1) if buy_count > 1 else 0

            for i in range(sell_count):
                price = mid_price + sell_step * i
                grid_levels.append({
                    'level': i, 'price': round(price, 2),
                    'action': 'SELL',
                    'distance_pct': round((price - price_low) / price_low * 100, 2)
                })
            for i in range(buy_count):
                price = price_low + buy_step * i
                grid_levels.append({
                    'level': sell_count + i, 'price': round(price, 2),
                    'action': 'BUY',
                    'distance_pct': round((price - price_low) / price_low * 100, 2)
                })

        # 统计数据
        signal_stats = {'BUY': 0, 'SELL': 0, 'HOLD': 0}
        for a in recent_analyses:
            sig = a.get('signal', 'HOLD')
            if sig in signal_stats:
                signal_stats[sig] += 1

        result = {
            'grid_type': grid_type,
            'grid_type_cn': self._grid_type_cn(grid_type),
            'price_low': round(price_low, 2),
            'price_high': round(price_high, 2),
            'grid_count': grid_count,
            'mid_price': round(mid_price, 2),
            'total_range_pct': round((price_high - price_low) / price_low * 100, 2),
            'avg_grid_spacing': round((price_high - price_low) / (grid_count - 1), 4),
            'grid_levels': grid_levels,
            'signal_stats': signal_stats,
            'based_on_records': len(recent_analyses),
            'recent_prices': [round(p, 2) for p in prices[:5]],
            'suggested_mode': self._suggest_trading_mode(grid_type, signal_stats)
        }

        logger.info(f"网格策略生成完成: {grid_type}, {grid_count}格, ${price_low:.2f}~${price_high:.2f}")
        return result

    def _grid_type_cn(self, grid_type: str) -> str:
        """网格类型中文名"""
        return {'symmetrical': '对称网格', 'upward': '上涨网格', 'downward': '下跌网格'}.get(grid_type, grid_type)

    def _suggest_trading_mode(self, grid_type: str, signal_stats: Dict) -> str:
        """建议交易模式"""
        total = sum(signal_stats.values())
        if total == 0:
            return '现货网格（低风险）'

        if grid_type == 'upward':
            return '现货网格（低买高卖）'
        elif grid_type == 'downward':
            return '合约网格（高卖低买，需对冲）'
        else:
            return '现货网格（震荡市）'

    def generate_grid_order_plan(self, grid_params: Dict,
                                 total_capital: float = 10000.0,
                                 max_position_pct: float = 0.8) -> Dict:
        """
        生成具体下单计划

        Args:
            grid_params: calculate_grid_params() 的返回结果
            total_capital: 总资金（USDT）
            max_position_pct: 最大仓位占比

        Returns:
            下单计划字典
        """
        if 'error' in grid_params:
            return grid_params

        grid_levels = grid_params['grid_levels']
        capital_per_grid = total_capital * max_position_pct / len(grid_levels)

        orders = []
        for level in grid_levels:
            price = level['price']
            quantity = round(capital_per_grid / price, 4) if price > 0 else 0

            orders.append({
                'grid_level': level['level'],
                'price': price,
                'action': level['action'],
                'quantity': quantity,
                'notional': round(quantity * price, 2),
                'distance_pct': level['distance_pct']
            })

        plan = {
            'grid_type': grid_params['grid_type'],
            'grid_type_cn': grid_params['grid_type_cn'],
            'total_capital': total_capital,
            'max_position_pct': max_position_pct,
            'capital_per_grid': round(capital_per_grid, 2),
            'total_orders': len(orders),
            'buy_orders': sum(1 for o in orders if o['action'] == 'BUY'),
            'sell_orders': sum(1 for o in orders if o['action'] == 'SELL'),
            'orders': orders,
            'price_range': f"${grid_params['price_low']:.2f} ~ ${grid_params['price_high']:.2f}",
            'expected_profit_per_grid_pct': round(grid_params['avg_grid_spacing'] / grid_params['price_low'] * 100, 4),
            'risk_warnings': self._generate_risk_warning(grid_params)
        }

        logger.info(f"下单计划生成: {len(orders)}个订单，单格资金${capital_per_grid:.2f}")
        return plan

    def _generate_risk_warning(self, grid_params: Dict) -> List[str]:
        """生成风险提示"""
        warnings = []
        range_pct = grid_params.get('total_range_pct', 0)

        if range_pct > 15:
            warnings.append(f"价格区间较大（{range_pct:.1f}%），确保有足够资金覆盖全部网格")
        if grid_params.get('grid_count', 0) > 30:
            warnings.append('网格数量较多，频繁交易可能产生较高手续费')
        if grid_params.get('grid_type') == 'downward':
            warnings.append('下跌网格适合趋势下跌市场，现货账户无法卖出做空，建议使用合约（需对冲）')
        warnings.append('网格策略在单边行情中可能亏损，建议设置止损退出机制')
        return warnings

    def format_grid_report(self, grid_params: Dict, plan: Dict = None) -> str:
        """格式化网格报告（用于GUI显示）"""
        if 'error' in grid_params:
            return f"错误: {grid_params['error']}"

        lines = []
        lines.append("=" * 60)
        lines.append("网格交易策略报告")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"网格类型: {grid_params['grid_type_cn']} ({grid_params['grid_type']})")
        lines.append(f"价格区间: ${grid_params['price_low']:.2f} ~ ${grid_params['price_high']:.2f}")
        lines.append(f"总区间: {grid_params['total_range_pct']:.2f}%")
        lines.append(f"网格数量: {grid_params['grid_count']} 格")
        lines.append(f"平均间距: ${grid_params['avg_grid_spacing']:.2f}")
        lines.append(f"建议模式: {grid_params['suggested_mode']}")
        lines.append(f"基于记录数: {grid_params['based_on_records']}")
        lines.append("")

        lines.append("信号统计:")
        stats = grid_params['signal_stats']
        lines.append(f"  买入: {stats['BUY']}  卖出: {stats['SELL']}  持有: {stats['HOLD']}")
        lines.append("")

        if plan:
            lines.append("-" * 60)
            lines.append("下单计划:")
            lines.append(f"  总资金: ${plan['total_capital']:.2f}")
            lines.append(f"  每格资金: ${plan['capital_per_grid']:.2f}")
            lines.append(f"  买单: {plan['buy_orders']}个  卖单: {plan['sell_orders']}个")
            lines.append("")

        lines.append("网格线详情（前10个）:")
        for gl in grid_params['grid_levels'][:10]:
            lines.append(f"  #{gl['level']:2d} | {gl['action']:4s} | ${gl['price']:8.2f} | 距下限: {gl['distance_pct']:.2f}%")
        lines.append("")

        if plan and plan.get('risk_warnings'):
            lines.append("风险提示:")
            for w in plan['risk_warnings']:
                lines.append(f"  ⚠ {w}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


if __name__ == '__main__':
    # 测试代码
    grid = GridStrategy()
    recent = grid.get_recent_analysis(limit=10)

    if not recent:
        print("没有找到分析记录，请先运行几次分析。")
    else:
        print(f"获取到 {len(recent)} 条最近分析记录")
        params = grid.calculate_grid_params(recent, grid_count=None, grid_type='auto')
        plan = grid.generate_grid_order_plan(params, total_capital=10000.0)
        report = grid.format_grid_report(params, plan)
        print(report)
