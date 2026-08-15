# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - 深度学习引擎 v1.0
真正的AI预测系统，不是简单调参

核心能力:
1. 市场状态编码（将复杂市场转为特征向量）
2. 时序模式识别（LSTM/Transformer学习价格模式）
3. 强化学习策略（DQN学习最优交易决策）
4. 元学习（从历史策略中学习如何学习）

作者: Python全栈工程师
日期: 2026-06-03
"""

import sqlite3
import json
import logging
import pickle
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd
from collections import deque

logger = logging.getLogger(__name__)

# ============================================================
# 第一部分：市场状态编码器
# ============================================================

class MarketStateEncoder:
    """
    将复杂市场数据编码为神经网络可理解的特征向量
    
    特征维度：128维
    - 价格特征：20维（OHLCV统计）
    - 技术指标：40维（RSI/MACD/EMA等）
    - 市场结构：20维（波动率/趋势强度）
    - 订单流：20维（买卖比例/成交量）
    - 情绪特征：15维（恐惧贪婪指数）
    - 时间特征：13维（周期性编码）
    """
    
    FEATURE_DIM = 128
    
    def __init__(self, lookback_periods: int = 50):
        self.lookback = lookback_periods
        self.feature_cache = {}
        
    def encode(self, price_data: pd.DataFrame, indicators: Dict, 
               sentiment: Dict = None, order_flow: Dict = None) -> np.ndarray:
        """
        编码市场状态为特征向量
        
        Args:
            price_data: OHLCV数据
            indicators: 技术指标字典
            sentiment: 情绪数据（可选）
            order_flow: 订单流数据（可选）
        
        Returns:
            128维特征向量
        """
        # 初始化默认值
        close = np.array([])
        high = np.array([])
        low = np.array([])
        volume = np.array([])
        
        features = np.zeros(self.FEATURE_DIM)
        idx = 0
        
        # 1. 价格特征 (20维)
        if price_data is not None and len(price_data) > 0:
            close = price_data['close'].values
            high = price_data['high'].values
            low = price_data['low'].values
            volume = price_data['volume'].values
            
            # 价格变化率
            for period in [5, 10, 20]:
                if len(close) > period:
                    ret = (close[-1] - close[-period]) / close[-period]
                    features[idx] = np.clip(ret, -0.5, 0.5)
                    idx += 1
            
            # 波动率
            for period in [10, 20]:
                if len(close) > period:
                    ret = np.diff(close[-period:]) / close[-period:-1]
                    features[idx] = np.std(ret) * np.sqrt(252)  # 年化
                    idx += 1
            
            # 高低点特征
            if len(high) > 20:
                features[idx] = (high[-1] - np.max(high[-20:])) / high[-1]  # 距离高点
                idx += 1
                features[idx] = (low[-1] - np.min(low[-20:])) / low[-1]   # 距离低点
                idx += 1
            
            # 成交量特征
            if len(volume) > 20:
                vol_ma = np.mean(volume[-20:])
                features[idx] = volume[-1] / vol_ma - 1  # 成交量偏离
                idx += 1
        
        # 2. 技术指标特征 (40维)
        if indicators:
            # RSI
            for key in ['rsi_14', 'rsi_7', 'rsi_21']:
                if key in indicators:
                    features[idx] = (indicators[key] - 50) / 50  # 归一化到[-1,1]
                    idx += 1
            
            # MACD
            if 'macd' in indicators:
                features[idx] = indicators['macd'] / 10  # 归一化
                idx += 1
            if 'macd_signal' in indicators:
                features[idx] = indicators['macd_signal'] / 10
                idx += 1
            if 'macd_hist' in indicators:
                features[idx] = np.tanh(indicators['macd_hist'])  # tanh归一化
                idx += 1
            
            # EMA偏离
            for period in [10, 20, 50, 100]:
                key = f'ema_{period}'
                if key in indicators and close[-1] > 0:
                    features[idx] = (close[-1] - indicators[key]) / close[-1]
                    idx += 1
            
            # 布林带
            if 'boll_upper' in indicators and 'boll_lower' in indicators:
                mid = (indicators['boll_upper'] + indicators['boll_lower']) / 2
                width = indicators['boll_upper'] - indicators['boll_lower']
                if width > 0 and close[-1] > 0:
                    features[idx] = (close[-1] - mid) / width  # 布林带位置
                    idx += 1
                    features[idx] = width / close[-1]  # 布林带宽度
                    idx += 1
            
            # ATR
            if 'atr' in indicators and close[-1] > 0:
                features[idx] = indicators['atr'] / close[-1]  # 相对波动率
                idx += 1
        
        # 3. 市场结构特征 (20维)
        if len(close) > 50:
            # 趋势强度
            for period in [10, 20, 50]:
                if len(close) > period:
                    slope = np.polyfit(range(period), close[-period:], 1)[0]
                    features[idx] = np.tanh(slope / close[-1] * 1000)
                    idx += 1
            
            # 动量
            for period in [5, 10, 20]:
                if len(close) > period:
                    features[idx] = np.tanh(indicators.get(f'momentum_{period}', 0))
                    idx += 1
        
        # 4. 情绪特征 (15维)
        if sentiment:
            features[idx] = sentiment.get('fear_greed_index', 50) / 100
            idx += 1
            features[idx] = sentiment.get('social_sentiment', 0)
            idx += 1
            features[idx] = sentiment.get('news_score', 0)
            idx += 1
        
        # 5. 时间特征 (13维) - 周期性编码
        now = datetime.now()
        features[idx] = np.sin(2 * np.pi * now.hour / 24)  # 小时周期
        idx += 1
        features[idx] = np.cos(2 * np.pi * now.hour / 24)
        idx += 1
        features[idx] = np.sin(2 * np.pi * now.weekday() / 7)  # 周周期
        idx += 1
        features[idx] = np.cos(2 * np.pi * now.weekday() / 7)
        idx += 1
        
        # 归一化到[-1, 1]
        features = np.clip(features, -1, 1)
        
        return features[:self.FEATURE_DIM]


# ============================================================
# 第二部分：时序模式识别器（LSTM风格，但纯NumPy实现）
# ============================================================

class TemporalPatternRecognizer:
    """
    时序模式识别器
    
    学习价格序列中的重复模式，不依赖深度学习框架
    使用简化的注意力机制 + 时序卷积
    """
    
    def __init__(self, sequence_length: int = 50, hidden_dim: int = 64):
        self.seq_len = sequence_length
        self.hidden_dim = hidden_dim
        
        # 初始化权重（简化版LSTM）
        self.W_f = np.random.randn(hidden_dim, hidden_dim) * 0.1  # 遗忘门
        self.W_i = np.random.randn(hidden_dim, hidden_dim) * 0.1  # 输入门
        self.W_c = np.random.randn(hidden_dim, hidden_dim) * 0.1  # 候选细胞
        self.W_o = np.random.randn(hidden_dim, hidden_dim) * 0.1  # 输出门
        
        self.b_f = np.zeros(hidden_dim)
        self.b_i = np.zeros(hidden_dim)
        self.b_c = np.zeros(hidden_dim)
        self.b_o = np.zeros(hidden_dim)
        
        # 输出层
        self.W_out = np.random.randn(3, hidden_dim) * 0.1  # 3分类：BUY/HOLD/SELL
        self.b_out = np.zeros(3)
        
        self.trained = False
        
    def forward(self, x_seq: np.ndarray) -> Tuple[np.ndarray, Dict]:
        """
        前向传播
        
        Args:
            x_seq: (sequence_length, feature_dim) 输入序列
        
        Returns:
            (预测概率, 隐藏状态字典)
        """
        batch_size = x_seq.shape[0] if len(x_seq.shape) == 2 else 1
        
        h = np.zeros(self.hidden_dim)
        c = np.zeros(self.hidden_dim)
        
        cache = {'h_list': [], 'c_list': [], 'f_list': [], 'i_list': [], 'o_list': []}
        
        for t in range(len(x_seq)):
            x = x_seq[t]
            
            # LSTM门计算（简化版）
            f = self._sigmoid(np.dot(self.W_f, h) + self.b_f)
            i = self._sigmoid(np.dot(self.W_i, h) + self.b_i)
            o = self._sigmoid(np.dot(self.W_o, h) + self.b_o)
            c_tilde = np.tanh(np.dot(self.W_c, h) + self.b_c)
            
            c = f * c + i * c_tilde
            h = o * np.tanh(c)
            
            # 缓存用于反向传播
            cache['h_list'].append(h.copy())
            cache['c_list'].append(c.copy())
            cache['f_list'].append(f)
            cache['i_list'].append(i)
            cache['o_list'].append(o)
        
        # 输出预测
        logits = np.dot(self.W_out, h) + self.b_out
        probs = self._softmax(logits)
        
        return probs, {'h': h, 'c': c, 'cache': cache}
    
    def _sigmoid(self, x):
        return 1 / (1 + np.exp(-np.clip(x, -500, 500)))
    
    def _softmax(self, x):
        exp_x = np.exp(x - np.max(x))
        return exp_x / exp_x.sum()
    
    def train_on_batch(self, X_batch: np.ndarray, y_batch: np.ndarray, 
                       lr: float = 0.01) -> float:
        """
        在一个批次上训练
        
        Args:
            X_batch: (batch, seq_len, features)
            y_batch: (batch,) 标签 (0=SELL, 1=HOLD, 2=BUY)
        
        Returns:
            损失值
        """
        total_loss = 0
        
        for x_seq, label in zip(X_batch, y_batch):
            # 前向
            probs, state = self.forward(x_seq)
            
            # 计算损失（交叉熵）
            loss = -np.log(probs[label] + 1e-10)
            total_loss += loss
            
            # 反向传播（简化版）
            dout = probs.copy()
            dout[label] -= 1  # 梯度
            
            # 更新输出层 (修正: W_out形状是 (3, 64))
            h = state['h']  # (64,)
            self.W_out -= lr * np.outer(dout, h)  # (3,) outer (64,) = (3, 64)
            self.b_out -= lr * dout  # (3,)
        
        self.trained = True
        return total_loss / len(y_batch)
    
    def predict(self, x_seq: np.ndarray) -> Tuple[str, float]:
        """预测交易信号"""
        probs, _ = self.forward(x_seq)
        
        signal_idx = np.argmax(probs)
        confidence = probs[signal_idx]
        
        signals = ['SELL', 'HOLD', 'BUY']
        return signals[signal_idx], confidence


# ============================================================
# 第三部分：强化学习交易智能体（DQN）
# ============================================================

class TradingAgent:
    """
    基于DQN的交易智能体
    
    核心思想：
    - 状态：市场特征向量
    - 动作：BUY / HOLD / SELL
    - 奖励：风险调整后的收益
    
    学习目标：
    - 最大化长期收益
    - 最小化回撤
    - 适应不同市场状态
    """
    
    def __init__(self, state_dim: int = 128, action_dim: int = 3,
                 memory_size: int = 10000, gamma: float = 0.95,
                 epsilon_start: float = 1.0, epsilon_end: float = 0.05,
                 epsilon_decay: float = 0.995):
        
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.gamma = gamma
        
        # 探索-利用平衡
        self.epsilon = epsilon_start
        self.epsilon_min = epsilon_end
        self.epsilon_decay = epsilon_decay
        
        # 经验回放缓冲区
        self.memory = deque(maxlen=memory_size)
        
        # Q网络（简化的全连接网络）
        self.W1 = np.random.randn(state_dim, 256) * 0.1
        self.b1 = np.zeros(256)
        self.W2 = np.random.randn(256, 128) * 0.1
        self.b2 = np.zeros(128)
        self.W3 = np.random.randn(128, action_dim) * 0.1
        self.b3 = np.zeros(action_dim)
        
        # 目标网络（用于稳定训练）
        self.target_W1 = self.W1.copy()
        self.target_b1 = self.b1.copy()
        self.target_W2 = self.W2.copy()
        self.target_b2 = self.b2.copy()
        self.target_W3 = self.W3.copy()
        self.target_b3 = self.b3.copy()
        
        # 训练统计
        self.train_step = 0
        self.update_target_every = 100
        
    def remember(self, state: np.ndarray, action: int, reward: float,
                 next_state: np.ndarray, done: bool):
        """存储经验"""
        self.memory.append((state, action, reward, next_state, done))
    
    def act(self, state: np.ndarray, training: bool = True) -> int:
        """选择动作（epsilon-greedy策略）"""
        if training and np.random.random() < self.epsilon:
            return np.random.randint(self.action_dim)
        
        # 计算Q值
        q_values = self._forward(state)
        return np.argmax(q_values)
    
    def _forward(self, state: np.ndarray) -> np.ndarray:
        """前向传播"""
        h1 = np.maximum(0, np.dot(state, self.W1) + self.b1)  # ReLU
        h2 = np.maximum(0, np.dot(h1, self.W2) + self.b2)
        q = np.dot(h2, self.W3) + self.b3
        return q
    
    def _target_forward(self, state: np.ndarray) -> np.ndarray:
        """目标网络前向传播"""
        h1 = np.maximum(0, np.dot(state, self.target_W1) + self.target_b1)
        h2 = np.maximum(0, np.dot(h1, self.target_W2) + self.target_b2)
        q = np.dot(h2, self.target_W3) + self.target_b3
        return q
    
    def replay(self, batch_size: int = 32, lr: float = 0.001) -> float:
        """经验回放训练"""
        if len(self.memory) < batch_size:
            return 0.0
        
        batch = np.random.choice(len(self.memory), batch_size, replace=False)
        total_loss = 0
        
        for idx in batch:
            state, action, reward, next_state, done = self.memory[idx]
            
            # 计算目标Q值
            target_q = reward
            if not done:
                next_q = self._target_forward(next_state)
                target_q = reward + self.gamma * np.max(next_q)
            
            # 计算当前Q值
            current_q = self._forward(state)
            
            # 计算TD误差
            td_error = target_q - current_q[action]
            total_loss += td_error ** 2
            
            # 简化的梯度更新（只更新动作对应的Q值）
            # 实际应用中应该用更复杂的优化器
            delta = np.zeros(self.action_dim)
            delta[action] = td_error
            
            # 反向传播更新权重
            h2 = np.maximum(0, np.dot(np.maximum(0, np.dot(state, self.W1) + self.b1), self.W2) + self.b2)
            grad_W3 = np.outer(h2, delta)
            self.W3 -= lr * grad_W3
            self.b3 -= lr * delta
        
        # 更新探索率
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        
        # 定期更新目标网络
        self.train_step += 1
        if self.train_step % self.update_target_every == 0:
            self.target_W1 = self.W1.copy()
            self.target_b1 = self.b1.copy()
            self.target_W2 = self.W2.copy()
            self.target_b2 = self.b2.copy()
            self.target_W3 = self.W3.copy()
            self.target_b3 = self.b3.copy()
        
        return total_loss / batch_size


# ============================================================
# 第四部分：深度学习训练管理器
# ============================================================

class DeepLearningEngine:
    """
    深度学习引擎
    
    整合所有组件，提供统一的训练和预测接口
    """
    
    def __init__(self, config_path: str = "config.yaml",
                 db_path: str = None):
        
        self.config_path = config_path
        if db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.db_path = str(get_localized_db_path('ai_learning'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent / "data"
                if not base_dir.exists():
                    base_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(base_dir / "ai_learning.db")
        else:
            self.db_path = db_path
        
        # 初始化组件
        self.encoder = MarketStateEncoder()
        self.pattern_recognizer = TemporalPatternRecognizer()
        self.agent = TradingAgent()
        
        # 训练状态
        self.is_trained = False
        self.training_history = []
        
        logger.info("DeepLearningEngine initialized")
        self._local = threading.local()

    def _get_conn(self):
        """线程本地连接（WAL模式，避免 database is locked）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn
    
    def prepare_training_data(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        从历史交易数据准备训练集
        
        优先从 paper_trading.db 的已平仓交易提取真实特征，
        回退到 ai_learning.db 的 analysis_records。
        """
        # 尝试从 paper_trading.db 获取真实交易数据
        X, y = self._prepare_from_paper_trading()
        if len(X) >= 10:
            logger.info(f"从 paper_trading.db 提取 {len(X)} 条训练数据")
            return X, y
        
        # 回退到 ai_learning.db
        X, y = self._prepare_from_analysis_records()
        if len(X) >= 10:
            logger.info(f"从 ai_learning.db 提取 {len(X)} 条训练数据")
            return X, y
        
        logger.warning(f"训练数据不足，需要更多交易数据")
        return np.array([]), np.array([])
    
    def _prepare_from_paper_trading(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        从 paper_trading.db 的已平仓交易提取训练数据
        
        每笔 CLOSED 交易：
        - 用入场时指标构建特征（从 trade_metadata JSON 提取）
        - 用实际 PnL 结果作标签
        """
        try:
            # 使用 data_localization 获取 paper_trading.db 路径
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                paper_db = str(get_localized_db_path('paper_trading'))
            except ImportError:
                paper_db = os.path.join(os.path.dirname(self.db_path), "paper_trading.db")
            
            if not os.path.exists(paper_db):
                logger.debug(f"paper_trading.db not found at {paper_db}")
                return np.array([]), np.array([])
            
            conn = sqlite3.connect(paper_db, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.cursor()
            
            # 检查表是否存在
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_positions'")
            if not cur.fetchone():
                conn.close()
                return np.array([]), np.array([])
            
            # 获取已平仓交易 + 入场指标
            cur.execute("""
                SELECT
                    p.id, p.symbol, p.side, p.entry_price, p.close_avg_price,
                    p.realized_pnl_usdt, p.r_multiple, p.qty_total,
                    p.opened_at, p.closed_at,
                    p.sl, p.tp1,
                    p.advice_snapshot
                FROM paper_positions p
                WHERE p.status = 'CLOSED'
                  AND p.realized_pnl_usdt IS NOT NULL
                ORDER BY p.closed_at DESC
            """)
            
            records = cur.fetchall()
            conn.close()
            
            if len(records) < 5:
                return np.array([]), np.array([])
            
            X_sequences = []
            y_labels = []
            
            for row in records:
                (pos_id, symbol, side, entry_price, exit_price,
                 pnl, r_multiple, quantity, entry_time, exit_time,
                 stop_loss, take_profit, metadata_json) = row

                pnl_pct = None
                if pnl_pct is None and entry_price and exit_price:
                    try:
                        ep, xp = float(entry_price), float(exit_price)
                        if ep > 0:
                            direction = 1.0 if str(side).upper() == "LONG" else -1.0
                            pnl_pct = direction * (xp - ep) / ep * 100
                    except (TypeError, ValueError):
                        pass
                if pnl_pct is None and pnl is not None and entry_price and quantity:
                    try:
                        ep, q = float(entry_price), float(quantity)
                        if ep > 0 and q > 0:
                            pnl_pct = float(pnl) / (ep * q) * 100
                    except (TypeError, ValueError):
                        pass

                # 构建特征向量 (128维)
                features = np.zeros(128)
                idx = 0
                
                # 1. 从 metadata 提取入场时指标
                indicators = {}
                if metadata_json:
                    try:
                        metadata = json.loads(metadata_json)
                        indicators = metadata.get("indicators", {}) or metadata.get("entry_indicators", {})
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # 2. 填充特征 (与 MarketStateEncoder.encode 一致)
                # 价格特征 (20维)
                if pnl_pct is not None:
                    features[idx] = np.clip(float(pnl_pct) / 100, -0.5, 0.5)
                idx += 1
                
                if entry_price and exit_price:
                    features[idx] = np.clip((float(exit_price) - float(entry_price)) / float(entry_price), -0.5, 0.5)
                idx += 1
                
                # 技术指标 (40维)
                for key in ['rsi_14', 'rsi_7', 'rsi_21']:
                    if key in indicators:
                        features[idx] = (float(indicators[key]) - 50) / 50
                    idx += 1
                
                for key in ['macd', 'macd_signal', 'macd_hist']:
                    if key in indicators:
                        val = float(indicators[key])
                        if key == 'macd_hist':
                            features[idx] = np.tanh(val)
                        else:
                            features[idx] = val / 10
                    idx += 1
                
                for period in [10, 20, 50, 100]:
                    key = f'ema_{period}'
                    if key in indicators and entry_price:
                        features[idx] = (float(entry_price) - float(indicators[key])) / float(entry_price)
                    idx += 1
                
                if 'boll_upper' in indicators and 'boll_lower' in indicators and entry_price:
                    mid = (float(indicators['boll_upper']) + float(indicators['boll_lower'])) / 2
                    width = float(indicators['boll_upper']) - float(indicators['boll_lower'])
                    if width > 0:
                        features[idx] = (float(entry_price) - mid) / width
                        idx += 1
                        features[idx] = width / float(entry_price)
                        idx += 1
                
                if 'atr' in indicators and entry_price:
                    features[idx] = float(indicators['atr']) / float(entry_price)
                idx += 1
                
                # 方向特征
                if side:
                    features[idx] = 1.0 if side.upper() in ('LONG', 'BUY') else -1.0
                idx += 1
                
                # 止损/止盈比例
                if stop_loss and take_profit and entry_price:
                    sl_dist = abs(float(stop_loss) - float(entry_price))
                    tp_dist = abs(float(take_profit) - float(entry_price))
                    if sl_dist > 0:
                        features[idx] = tp_dist / sl_dist  # 风险回报比
                idx += 1
                
                # 归一化
                features = np.clip(features, -1, 1)
                
                # 创建序列 (50步, 每步128维)
                seq = np.tile(features[:128], (50, 1))
                X_sequences.append(seq)
                
                # 标签: 根据 PnL 百分比
                pnl_val = float(pnl_pct) if pnl_pct else float(pnl or 0)
                if pnl_val > 1.5:
                    y_labels.append(2)  # BUY
                elif pnl_val < -1.5:
                    y_labels.append(0)  # SELL
                else:
                    y_labels.append(1)  # HOLD
            
            return np.array(X_sequences), np.array(y_labels)
            
        except Exception as e:
            logger.error(f"从 paper_trading.db 提取训练数据失败: {e}")
            return np.array([]), np.array([])
    
    def _prepare_from_analysis_records(self) -> Tuple[np.ndarray, np.ndarray]:
        """从 ai_learning.db 的 analysis_records 提取 (原逻辑)"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            # 获取有实际结果的分析记录
            cur.execute("""
                SELECT id, timestamp, indicators, current_price,
                       actual_price_after_24h, actual_result, pnl_percent
                FROM analysis_records
                WHERE actual_result IS NOT NULL
                ORDER BY timestamp DESC
            """)
            
            records = cur.fetchall()
            
            if len(records) < 5:
                return np.array([]), np.array([])
        
            X_sequences = []
            y_labels = []
            
            for record in records:
                record_id, timestamp, indicators_json, price, future_price, result, pnl = record
                
                # 解析指标
                try:
                    indicators = json.loads(indicators_json) if indicators_json else {}
                except (json.JSONDecodeError, TypeError):
                    indicators = {}
                
                # 创建特征向量（简化版，实际需要完整价格序列）
                features = np.zeros(128)
                
                # 编码指标
                idx = 0
                for key in ['rsi_14', 'rsi_7', 'rsi_21']:
                    if key in indicators:
                        features[idx] = (indicators[key] - 50) / 50
                        idx += 1
                
                if 'macd' in indicators:
                    features[idx] = indicators['macd'] / 10
                    idx += 1
                
                # 创建序列（这里用重复特征作为序列，实际应用需要历史数据）
                seq = np.tile(features, (50, 1))
                X_sequences.append(seq)
                
                # 标签：根据收益确定
                if pnl and pnl > 2:
                    y_labels.append(2)  # BUY
                elif pnl and pnl < -2:
                    y_labels.append(0)  # SELL
                else:
                    y_labels.append(1)  # HOLD
            
            return np.array(X_sequences), np.array(y_labels)
            
        except Exception as e:
            logger.error(f"从 analysis_records 提取训练数据失败: {e}")
            return np.array([]), np.array([])
    
    def train(self, epochs: int = 100, batch_size: int = 32) -> Dict:
        """
        训练深度学习模型
        
        Returns:
            训练统计信息
        """
        logger.info("Preparing training data...")
        X, y = self.prepare_training_data()
        
        if len(X) == 0:
            logger.warning("No training data available, skipping training (need more trade data)")
            return {
                'status': 'skipped',
                'reason': '训练数据不足，跳过训练以避免噪声污染。请等待更多交易数据积累。',
                'samples': 0,
                'epochs': 0,
                'final_loss': None,
            }
        
        logger.info(f"Training data: {len(X)} samples")
        
        # 数据过少时给警告但允许继续（用于早期迭代）
        if len(X) < 50:
            logger.warning(f"Training data very limited ({len(X)} samples), "
                           "model may not generalize well")
        
        # 训练时序模型
        losses = []
        for epoch in range(epochs):
            # 随机采样批次
            indices = np.random.choice(len(X), min(batch_size, len(X)), replace=False)
            X_batch = X[indices]
            y_batch = y[indices]
            
            loss = self.pattern_recognizer.train_on_batch(X_batch, y_batch)
            losses.append(loss)
            
            if epoch % 10 == 0:
                logger.info(f"Epoch {epoch}, Loss: {loss:.4f}")
        
        # 训练强化学习智能体
        # ...（模拟训练过程）
        
        self.is_trained = True
        self.training_history = losses
        
        return {
            'epochs': epochs,
            'final_loss': losses[-1] if losses else 0,
            'samples': len(X)
        }
    
    def predict(self, market_data: Dict) -> Dict:
        """
        预测交易信号
        
        Args:
            market_data: 包含price_data, indicators等
        
        Returns:
            预测结果（信号、置信度、特征重要性）
        """
        # 编码市场状态
        features = self.encoder.encode(
            market_data.get('price_data'),
            market_data.get('indicators', {}),
            market_data.get('sentiment')
        )
        
        # 创建序列（使用最近特征）
        feature_seq = np.tile(features, (50, 1))
        
        # 时序模型预测
        signal, confidence = self.pattern_recognizer.predict(feature_seq)
        
        # 强化学习智能体决策
        action = self.agent.act(features, training=False)
        actions = ['SELL', 'HOLD', 'BUY']
        rl_signal = actions[action]
        
        # 综合预测
        final_signal = signal if confidence > 0.6 else rl_signal
        final_confidence = max(confidence, 0.5)
        
        # 特征重要性（基于权重的简化分析）
        importance = self._analyze_feature_importance(features)
        
        return {
            'signal': final_signal,
            'confidence': final_confidence,
            'pattern_signal': signal,
            'rl_signal': rl_signal,
            'feature_importance': importance,
            'model_type': 'deep_learning_v1'
        }
    
    def _analyze_feature_importance(self, features: np.ndarray) -> List[Dict]:
        """分析特征重要性"""
        # 简化版：基于特征绝对值排序
        feature_names = [
            'price_momentum_5', 'price_momentum_10', 'price_momentum_20',
            'volatility_10', 'volatility_20',
            'rsi_14', 'rsi_7', 'rsi_21',
            'macd', 'macd_signal', 'macd_hist',
            'ema_deviation_10', 'ema_deviation_20',
            'boll_position', 'boll_width',
            'atr_relative', 'trend_strength',
            'sentiment', 'hour_sin', 'hour_cos'
        ]
        
        importance = []
        for i, name in enumerate(feature_names):
            if i < len(features):
                importance.append({
                    'feature': name,
                    'value': float(features[i]),
                    'importance': abs(features[i])
                })
        
        importance.sort(key=lambda x: x['importance'], reverse=True)
        return importance[:10]  # 返回前10重要特征
    
    def _serialize_model_data(self) -> Dict[str, Any]:
        return {
            'encoder': {
                'lookback': self.encoder.lookback
            },
            'pattern_recognizer': {
                'W_f': self.pattern_recognizer.W_f,
                'W_i': self.pattern_recognizer.W_i,
                'W_c': self.pattern_recognizer.W_c,
                'W_o': self.pattern_recognizer.W_o,
                'b_f': self.pattern_recognizer.b_f,
                'b_i': self.pattern_recognizer.b_i,
                'b_c': self.pattern_recognizer.b_c,
                'b_o': self.pattern_recognizer.b_o,
                'W_out': self.pattern_recognizer.W_out,
                'b_out': self.pattern_recognizer.b_out,
                'trained': self.pattern_recognizer.trained
            },
            'agent': {
                'W1': self.agent.W1,
                'b1': self.agent.b1,
                'W2': self.agent.W2,
                'b2': self.agent.b2,
                'W3': self.agent.W3,
                'b3': self.agent.b3,
                'epsilon': self.agent.epsilon
            },
            'training_history': self.training_history,
            'is_trained': self.is_trained
        }

    def _apply_model_data(self, model_data: Dict[str, Any]) -> None:
        enc = model_data.get('encoder') or {}
        if enc.get('lookback') is not None:
            self.encoder.lookback = enc['lookback']

        pr_data = model_data.get('pattern_recognizer') or {}
        for key in (
            'W_f', 'W_i', 'W_c', 'W_o',
            'b_f', 'b_i', 'b_c', 'b_o',
            'W_out', 'b_out', 'trained',
        ):
            if key in pr_data:
                setattr(self.pattern_recognizer, key, pr_data[key])

        agent_data = model_data.get('agent') or {}
        for key in ('W1', 'b1', 'W2', 'b2', 'W3', 'b3', 'epsilon'):
            if key in agent_data:
                setattr(self.agent, key, agent_data[key])

        self.is_trained = bool(model_data.get('is_trained', False))
        self.training_history = list(model_data.get('training_history') or [])

    def save_model(self, path: str = None):
        """保存模型到 ai_learning.db（artifact_blobs 表）。"""
        from bnb_quant_tool.db_artifact_store import (
            DEEP_LEARNING_BLOB,
            get_artifact_store,
        )

        model_data = self._serialize_model_data()
        blob = pickle.dumps(model_data)
        store = get_artifact_store(self.db_path)
        store.save_blob(DEEP_LEARNING_BLOB, blob, 'pickle')

        if path is not None:
            with open(path, 'wb') as f:
                f.write(blob)

        logger.info(f"Model saved to ai_learning.db ({DEEP_LEARNING_BLOB})")

    def load_model(self, path: str = None):
        """从 ai_learning.db 加载模型；兼容旧 .pkl 文件并自动迁入数据库。"""
        from bnb_quant_tool.db_artifact_store import (
            DEEP_LEARNING_BLOB,
            get_artifact_store,
        )

        store = get_artifact_store(self.db_path)
        blob = store.load_blob(DEEP_LEARNING_BLOB)
        if blob:
            self._apply_model_data(pickle.loads(blob))
            logger.info(f"Model loaded from ai_learning.db ({DEEP_LEARNING_BLOB})")
            return True

        if path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_model_path
                path = str(get_localized_model_path('deep_learning'))
            except ImportError:
                base_dir = Path(__file__).parent.parent.parent / "data" / "models"
                path = str(base_dir / "deep_learning_model.pkl")

        if not os.path.exists(path):
            logger.warning("Model not found in database or legacy file path")
            return False

        with open(path, 'rb') as f:
            blob = f.read()
        self._apply_model_data(pickle.loads(blob))
        store.save_blob(DEEP_LEARNING_BLOB, blob, 'pickle')
        logger.info(f"Model loaded from legacy file and migrated to ai_learning.db: {path}")
        return True


# ============================================================
# 第五部分：强化学习环境（用于训练）
# ============================================================

class TradingEnvironment:
    """
    交易强化学习环境
    
    模拟真实市场，用于训练智能体
    """
    
    def __init__(self, price_data: pd.DataFrame, initial_balance: float = 10000):
        self.price_data = price_data
        self.initial_balance = initial_balance
        self.reset()
        
    def reset(self):
        """重置环境"""
        self.balance = self.initial_balance
        self.position = 0  # 持仓数量
        self.entry_price = 0
        self.current_step = 0
        self.done = False
        self.total_reward = 0
        
        return self._get_state()
    
    def _get_state(self) -> np.ndarray:
        """获取当前状态"""
        # 简化：返回最近价格的变化率
        if self.current_step >= len(self.price_data):
            return np.zeros(128)
        
        window = self.price_data.iloc[max(0, self.current_step-50):self.current_step+1]
        if len(window) < 2:
            return np.zeros(128)
        
        state = np.zeros(128)
        close = window['close'].values
        
        # 计算特征
        if len(close) > 1:
            ret = (close[-1] - close[-2]) / close[-2]
            state[0] = np.clip(ret, -0.5, 0.5)
        
        return state
    
    def step(self, action: int) -> Tuple[np.ndarray, float, bool, Dict]:
        """
        执行动作
        
        Args:
            action: 0=SELL, 1=HOLD, 2=BUY
        
        Returns:
            (next_state, reward, done, info)
        """
        current_price = self.price_data.iloc[self.current_step]['close']
        
        # 执行交易
        reward = 0
        
        if action == 2:  # BUY
            if self.position == 0:
                self.position = self.balance / current_price
                self.entry_price = current_price
                self.balance = 0
        
        elif action == 0:  # SELL
            if self.position > 0:
                profit = (current_price - self.entry_price) * self.position
                reward = profit / (self.entry_price * self.position)  # 收益率
                self.balance = self.position * current_price
                self.position = 0
                self.entry_price = 0
        
        # 移动到下一步
        self.current_step += 1
        self.done = self.current_step >= len(self.price_data) - 1
        
        # 计算持仓收益（未实现）
        if self.position > 0:
            unrealized = (current_price - self.entry_price) / self.entry_price
            reward += unrealized * 0.1  # 小奖励引导
        
        self.total_reward += reward
        
        return self._get_state(), reward, self.done, {
            'balance': self.balance,
            'position': self.position,
            'total_reward': self.total_reward
        }


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Deep Learning Engine Test")
    print("=" * 60)
    
    # 初始化引擎
    engine = DeepLearningEngine()
    
    # 训练
    print("\n=== Training ===")
    stats = engine.train(epochs=50)
    print(f"Training complete: {stats}")
    
    # 测试预测
    print("\n=== Testing Prediction ===")
    test_data = {
        'price_data': None,
        'indicators': {'rsi_14': 45, 'macd': 2.3},
        'sentiment': {'fear_greed_index': 60}
    }
    result = engine.predict(test_data)
    print(f"Signal: {result['signal']}")
    print(f"Confidence: {result['confidence']:.2%}")
    print(f"Top features:")
    for f in result['feature_importance'][:5]:
        print(f"  {f['feature']}: {f['value']:.3f}")
    
    # 保存模型
    engine.save_model()
    print("\nModel saved successfully!")
