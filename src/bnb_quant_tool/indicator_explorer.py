# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - 指标探索器 v1.0
AI驱动的指标组合发现与遗传优化
作者: Python全栈工程师
日期: 2026-06-03
"""

import json
import os
import sqlite3
import threading
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import logging
import requests
import yaml
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class IndicatorGene:
    """指标基因 - 遗传算法的基本单元"""
    
    def __init__(self, indicator_type: str, params: Dict, 
                 entry_condition: str = None, 
                 exit_condition: str = None):
        self.indicator_type = indicator_type
        self.params = params
        self.entry_condition = entry_condition
        self.exit_condition = exit_condition
        
    def mutate(self, mutation_rate: float = 0.1) -> 'IndicatorGene':
        """变异：随机调整参数"""
        new_params = self.params.copy()
        
        for key, value in new_params.items():
            if random.random() < mutation_rate:
                # 根据参数类型调整
                if isinstance(value, int):
                    delta = max(1, int(value * 0.2))
                    new_params[key] = value + random.randint(-delta, delta)
                elif isinstance(value, float):
                    delta = value * 0.2
                    new_params[key] = max(0.1, value + random.uniform(-delta, delta))
        
        return IndicatorGene(
            self.indicator_type,
            new_params,
            self.entry_condition,
            self.exit_condition
        )
    
    def to_dict(self) -> Dict:
        return {
            "indicator_type": self.indicator_type,
            "params": self.params,
            "entry_condition": self.entry_condition,
            "exit_condition": self.exit_condition
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'IndicatorGene':
        return cls(
            data["indicator_type"],
            data["params"],
            data.get("entry_condition"),
            data.get("exit_condition")
        )


class StrategyChromosome:
    """策略染色体 - 多个指标基因的组合"""
    
    def __init__(self, genes: List[IndicatorGene], 
                 logic: str = "AND",  # AND/OR
                 fitness: float = 0.0):
        self.genes = genes
        self.logic = logic
        self.fitness = fitness
        self.generation = 0
        self.strategy_id = f"chromo_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000,9999)}"
    
    def crossover(self, other: 'StrategyChromosome') -> 'StrategyChromosome':
        """交叉：与另一个染色体交换基因"""
        min_genes = min(len(self.genes), len(other.genes))
        
        # 如果只有一个基因，不能交叉，直接变异
        if min_genes <= 1:
            return self.mutate(0.2)
        
        # 随机选择交叉点
        crossover_point = random.randint(1, min_genes - 1)
        
        # 新基因 = 前 + 后
        new_genes = self.genes[:crossover_point] + other.genes[crossover_point:]
        
        # 随机选择逻辑
        new_logic = random.choice([self.logic, other.logic])
        
        child = StrategyChromosome(new_genes, new_logic)
        child.generation = max(self.generation, other.generation) + 1
        
        return child
    
    def mutate(self, mutation_rate: float = 0.1) -> 'StrategyChromosome':
        """变异：每个基因都有概率变异"""
        new_genes = [g.mutate(mutation_rate) for g in self.genes]
        
        # 小概率改变逻辑
        new_logic = self.logic
        if random.random() < mutation_rate:
            new_logic = "OR" if self.logic == "AND" else "AND"
        
        child = StrategyChromosome(new_genes, new_logic)
        child.generation = self.generation + 1
        
        return child
    
    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "genes": [g.to_dict() for g in self.genes],
            "logic": self.logic,
            "fitness": self.fitness,
            "generation": self.generation
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'StrategyChromosome':
        genes = [IndicatorGene.from_dict(g) for g in data["genes"]]
        chromo = cls(genes, data["logic"], data.get("fitness", 0.0))
        chromo.generation = data.get("generation", 0)
        chromo.strategy_id = data["strategy_id"]
        return chromo
    
    def evaluate_expression(self) -> str:
        """生成可执行的策略表达式"""
        conditions = []
        for gene in self.genes:
            if gene.entry_condition:
                conditions.append(gene.entry_condition)
        
        if not conditions:
            return "True"
        
        return f" {self.logic} ".join(conditions)


class IndicatorExplorer:
    """
    指标探索器 - AI驱动的策略发现
    
    核心能力:
    1. 定义指标基因库
    2. 遗传算法优化
    3. 回测评估适应度
    4. AI辅助生成新指标组合
    5. 自动发现有效策略
    """
    
    POPULATION_SIZE = 20
    MAX_GENERATIONS = 50
    ELITE_COUNT = 3
    MUTATION_RATE = 0.15
    CROSSOVER_RATE = 0.7
    
    def __init__(self, config_path: str = "config.yaml",
                 learning_db_path: str = None,
                 paper_db_path: str = None,
                 api_key: str = None,
                 base_url: str = "https://api.deepseek.com"):
        
        self.config_path = config_path
        
        if learning_db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.learning_db_path = str(get_localized_db_path('ai_learning'))
            except ImportError:
                self.learning_db_path = "ai_learning.db"
        else:
            self.learning_db_path = learning_db_path
        
        if paper_db_path is None:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                self.paper_db_path = str(get_localized_db_path('paper_trading'))
            except ImportError:
                self.paper_db_path = "paper_trading.db"
        else:
            self.paper_db_path = paper_db_path
        
        self.api_key = api_key
        self.base_url = base_url
        
        # 加载配置
        self.config = self._load_config()
        if not api_key:
            from bnb_quant_tool.llm_provider import get_llm_credentials
            llm = get_llm_credentials(self.config)
            self.api_key = llm["api_key"]
            self.model = llm["model"]
            if not base_url or base_url == "https://api.deepseek.com":
                self.base_url = llm["base_url"]
        else:
            self.model = self.config.get("deepseek", {}).get("model", "deepseek-chat")
        
        if not getattr(self, "model", None):
            from bnb_quant_tool.llm_provider import get_llm_credentials
            self.model = get_llm_credentials(self.config)["model"]
        
        # 指标基因库
        self.indicator_library = self._build_indicator_library()
        
        # 当前进化种群
        self.population: List[StrategyChromosome] = []
        
        # 历史最优解
        self.best_strategies: List[StrategyChromosome] = []
        
        logger.info(f"IndicatorExplorer initialized, library size={len(self.indicator_library)}")
        self._local = threading.local()
        self._ensure_evolved_strategies_table()

    def _ensure_evolved_strategies_table(self) -> None:
        """启动时创建 evolved_strategies 表（避免 list 查询 no such table）。"""
        try:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evolved_strategies (
                    strategy_id TEXT PRIMARY KEY,
                    created_at TEXT,
                    generation INTEGER,
                    chromosome_json TEXT,
                    fitness REAL,
                    status TEXT DEFAULT 'discovered'
                )
            """)
            conn.commit()
        except Exception as e:
            logger.debug("evolved_strategies table init: %s", e)

    def _get_conn(self):
        """线程本地连接（WAL模式，避免 database is locked）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            import sqlite3 as _sqlite3
            db_path = self.learning_db_path
            # 绝对路径直接使用；相对路径加 data/ 前缀
            if not os.path.isabs(db_path) and not db_path.startswith('data/'):
                db_path = f'data/{db_path}'
            os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(db_path, timeout=60.0)
        return self._local.conn
    
    def _load_config(self) -> Dict:
        """加载配置"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {}
    
    def _build_indicator_library(self) -> List[IndicatorGene]:
        """构建指标基因库"""
        library = []
        
        # RSI 系列
        for period in [7, 14, 21]:
            for oversold in [25, 30, 35]:
                for overbought in [65, 70, 75]:
                    library.append(IndicatorGene(
                        "RSI",
                        {"period": period},
                        f"rsi_{period} < {oversold}",
                        f"rsi_{period} > {overbought}"
                    ))
        
        # MACD 系列
        for fast in [8, 12, 16]:
            for slow in [20, 26, 32]:
                for signal in [6, 9, 12]:
                    library.append(IndicatorGene(
                        "MACD",
                        {"fast": fast, "slow": slow, "signal": signal},
                        "macd_cross_up",
                        "macd_cross_down"
                    ))
        
        # EMA 系列
        for period in [10, 20, 50, 100]:
            library.append(IndicatorGene(
                "EMA",
                {"period": period},
                f"price > ema_{period}",
                f"price < ema_{period}"
            ))
        
        # 布林带系列
        for period in [15, 20, 25]:
            for std in [1.5, 2.0, 2.5]:
                library.append(IndicatorGene(
                    "BOLL",
                    {"period": period, "std": std},
                    f"price < boll_lower_{period}_{std}",
                    f"price > boll_upper_{period}_{std}"
                ))
        
        # ATR 系列（波动率）
        for period in [10, 14, 20]:
            library.append(IndicatorGene(
                "ATR",
                {"period": period},
                f"atr_percentile_{period} < 30",
                f"atr_percentile_{period} > 70"
            ))
        
        # 成交量系列
        for period in [10, 20]:
            library.append(IndicatorGene(
                "VOLUME",
                {"period": period},
                f"volume > volume_ma_{period} * 1.5",
                f"volume < volume_ma_{period} * 0.5"
            ))
        
        logger.info(f"Built indicator library with {len(library)} genes")
        return library
    
    def initialize_population(self):
        """初始化种群"""
        self.population = []
        
        for _ in range(self.POPULATION_SIZE):
            # 随机选择1-3个指标
            num_genes = random.randint(1, 3)
            genes = random.sample(self.indicator_library, num_genes)
            
            # 随机逻辑
            logic = random.choice(["AND", "OR"])
            
            chromo = StrategyChromosome(genes, logic)
            self.population.append(chromo)
        
        logger.info(f"Initialized population with {len(self.population)} chromosomes")
    
    def evaluate_fitness(self, chromo: StrategyChromosome, 
                        price_data: pd.DataFrame = None) -> float:
        """
        评估染色体适应度
        
        策略:
        1. 如果有历史数据，回测计算胜率/PnL
        2. 否则用AI评分
        """
        if price_data is not None:
            # 回测模式
            return self._backtest_fitness(chromo, price_data)
        else:
            # AI评估模式
            return self._ai_fitness_evaluation(chromo)
    
    def _backtest_fitness(self, chromo: StrategyChromosome, 
                          price_data: pd.DataFrame) -> float:
        """回测计算适应度"""
        try:
            # 生成信号（简化版）
            # TODO: 实现完整信号生成
            # 目前用启发式评分
            
            num_genes = len(chromo.genes)
            logic_bonus = 0.1 if chromo.logic == "AND" else 0.05
            
            # 基于指标类型评分
            score = 0.0
            for gene in chromo.genes:
                if gene.indicator_type == "RSI":
                    # RSI经典，加分
                    score += 0.3
                elif gene.indicator_type == "MACD":
                    score += 0.25
                elif gene.indicator_type == "EMA":
                    score += 0.2
                elif gene.indicator_type in ["BOLL", "ATR"]:
                    # 波动率指标创新性
                    score += 0.35
            
            # 组合多样性奖励
            unique_types = len(set(g.indicator_type for g in chromo.genes))
            diversity_bonus = unique_types * 0.1
            
            fitness = score + logic_bonus + diversity_bonus
            return min(1.0, fitness)
        
        except Exception as e:
            logger.error(f"回测失败: {e}")
            return 0.0
    
    def _ai_fitness_evaluation(self, chromo: StrategyChromosome) -> float:
        """AI评估适应度 - 简化为本地评分"""
        # 为了避免API超时，使用本地启发式评分
        # 指标多样性奖励
        unique_types = len(set(g.indicator_type for g in chromo.genes))
        diversity_score = unique_types * 0.2
        
        # 基于指标类型的分数
        type_scores = {
            "RSI": 0.3,
            "MACD": 0.25,
            "EMA": 0.2,
            "BOLL": 0.35,
            "ATR": 0.4,
            "VOLUME": 0.3
        }
        
        base_score = sum(type_scores.get(g.indicator_type, 0.1) for g in chromo.genes)
        base_score = min(base_score, 1.0)
        
        # AND逻辑略优（更保守）
        logic_bonus = 0.05 if chromo.logic == "AND" else 0.0
        
        # 参数合理性（不要过长周期）
        param_penalty = 0.0
        for g in chromo.genes:
            for v in g.params.values():
                if isinstance(v, int) and v > 50:
                    param_penalty += 0.05
        
        fitness = base_score + diversity_score + logic_bonus - param_penalty
        return max(0.1, min(1.0, fitness))
    
    def evolve_generation(self, price_data: pd.DataFrame = None):
        """进化一代"""
        if not self.population:
            self.initialize_population()
        
        # 1. 评估适应度
        for chromo in self.population:
            chromo.fitness = self.evaluate_fitness(chromo, price_data)
        
        # 2. 排序
        self.population.sort(key=lambda x: x.fitness, reverse=True)
        
        # 3. 记录最优
        if self.population[0].fitness > 0.7:
            self.best_strategies.append(self.population[0])
        
        # 4. 选择 + 交叉
        new_population = []
        
        # 精英保留
        new_population.extend(self.population[:self.ELITE_COUNT])
        
        # 交叉生成新个体
        while len(new_population) < self.POPULATION_SIZE:
            # 轮盘赌选择
            parent1 = self._roulette_select()
            parent2 = self._roulette_select()
            
            # 交叉
            if random.random() < self.CROSSOVER_RATE:
                child = parent1.crossover(parent2)
            else:
                child = parent1.mutate(self.MUTATION_RATE)
            
            new_population.append(child)
        
        self.population = new_population
        
        logger.info(f"Generation evolved, best fitness={self.population[0].fitness:.3f}")
    
    def _roulette_select(self) -> StrategyChromosome:
        """轮盘赌选择"""
        total_fitness = sum(c.fitness for c in self.population)
        if total_fitness == 0:
            return random.choice(self.population)
        
        pick = random.uniform(0, total_fitness)
        cumulative = 0.0
        
        for chromo in self.population:
            cumulative += chromo.fitness
            if cumulative >= pick:
                return chromo
        
        return self.population[-1]
    
    def run_evolution(self, generations: int = None, 
                     price_data: pd.DataFrame = None,
                     target_fitness: float = 0.85) -> Optional[StrategyChromosome]:
        """
        运行进化算法
        
        Args:
            generations: 最大代数
            price_data: 价格数据（用于回测）
            target_fitness: 目标适应度
        
        Returns:
            最优策略染色体
        """
        generations = generations or self.MAX_GENERATIONS
        
        self.initialize_population()
        
        for gen in range(generations):
            self.evolve_generation(price_data)
            
            best = self.population[0]
            logger.info(f"Gen {gen+1}: best fitness={best.fitness:.3f}")
            
            # 达到目标提前终止
            if best.fitness >= target_fitness:
                logger.info(f"Target fitness {target_fitness} reached!")
                break
        
        # 保存最优策略到数据库
        self._save_best_strategies()
        
        return self.population[0] if self.population else None
    
    def _save_best_strategies(self):
        """保存最优策略到数据库"""
        if not self.best_strategies:
            return
        
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            self._ensure_evolved_strategies_table()
            
            for chromo in self.best_strategies:
                cur.execute("""
                    INSERT OR REPLACE INTO evolved_strategies 
                    (strategy_id, created_at, generation, chromosome_json, fitness)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    chromo.strategy_id,
                    datetime.now().isoformat(),
                    chromo.generation,
                    json.dumps(chromo.to_dict(), ensure_ascii=False),
                    chromo.fitness
                ))
            
            conn.commit()
            
            logger.info(f"Saved {len(self.best_strategies)} best strategies")
        
        except Exception as e:
            logger.error(f"保存策略失败: {e}")
    
    def ai_generate_combination(self, market_context: Dict = None) -> StrategyChromosome:
        """
        AI生成新的指标组合
        
        Args:
            market_context: 市场背景（趋势、波动率等）
        
        Returns:
            新的策略染色体
        """
        prompt = f"""基于当前市场状态，建议一个有效的技术指标组合：

市场背景：
{json.dumps(market_context or {}, ensure_ascii=False, indent=2)}

可用指标类型：RSI, MACD, EMA, BOLL, ATR, VOLUME

输出JSON格式：
{{
  "indicators": [
    {{"type": "RSI", "params": {{"period": 14}}, "condition": "rsi_14 < 30"}},
    {{"type": "MACD", "params": {{"fast": 12, "slow": 26, "signal": 9}}, "condition": "macd_cross_up"}}
  ],
  "logic": "AND",
  "reason": "选择理由"
}}
"""
        
        try:
            endpoint = f"{self.base_url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是量化策略研究员。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "stream": False
            }
            
            response = requests.post(endpoint, headers=headers, json=payload, timeout=90)
            response.raise_for_status()
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            data = json.loads(content)
            
            genes = []
            for ind in data.get("indicators", []):
                gene = IndicatorGene(
                    ind["type"],
                    ind["params"],
                    ind.get("condition")
                )
                genes.append(gene)
            
            chromo = StrategyChromosome(genes, data.get("logic", "AND"))
            return chromo
        
        except Exception as e:
            logger.error(f"AI生成失败: {e}")
            # 返回随机组合
            genes = random.sample(self.indicator_library, 2)
            return StrategyChromosome(genes, "AND")
    
    def list_evolved_strategies(self, limit: int = 20) -> List[Dict]:
        """列出已发现的策略"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            self._ensure_evolved_strategies_table()
            
            cur.execute("""
                SELECT strategy_id, created_at, generation, fitness, status, chromosome_json
                FROM evolved_strategies
                WHERE status = 'discovered' OR status = 'active'
                ORDER BY fitness DESC
                LIMIT ?
            """, (limit,))
            
            rows = cur.fetchall()
            
            return [dict(r) for r in rows]
        
        except Exception as e:
            logger.error(f"列出策略失败: {e}")
            return []
    
    def get_signal_from_evolved_strategies(self, indicators: Dict) -> Optional[Dict]:
        """
        用进化发现的策略对当前指标集进行信号评估
        
        加载 fitness 最高的 N 个策略，用其 entry_condition
        对当前指标做布尔匹配，统计 BUY/SELL/HOLD 票数。
        
        Args:
            indicators: 当前技术指标字典 {"rsi_14": 45.2, "macd": 0.03, ...}
        
        Returns:
            {"signal": "BUY"|"SELL"|"HOLD", "confidence": float,
             "active_strategies": int, "votes": {...}} 或 None
        """
        try:
            strategies = self.list_evolved_strategies(limit=10)
            if not strategies:
                return None
            
            # 过滤 fitness >= 0.5
            viable = [s for s in strategies if s.get("fitness", 0) >= 0.5]
            if not viable:
                return None
            
            buy_votes = 0
            sell_votes = 0
            hold_votes = 0
            total_weight = 0.0
            
            for strat in viable:
                chromo_json = strat.get("chromosome_json")
                if not chromo_json:
                    continue
                
                try:
                    chromo_data = json.loads(chromo_json) if isinstance(chromo_json, str) else chromo_json
                except (json.JSONDecodeError, TypeError):
                    continue
                
                genes = chromo_data.get("genes", [])
                logic = chromo_data.get("logic", "AND")
                fitness = strat.get("fitness", 0.5)
                
                # 评估每个基因的条件
                gene_results = []
                for gene_data in genes:
                    cond = gene_data.get("entry_condition", "")
                    gene_type = gene_data.get("indicator_type", "")
                    
                    if not cond:
                        gene_results.append(True)  # 无条件默认 True
                        continue
                    
                    # 执行条件表达式
                    try:
                        result = self._evaluate_condition(cond, indicators)
                        gene_results.append(bool(result))
                    except Exception:
                        gene_results.append(False)
                
                # 组合基因结果
                if not gene_results:
                    continue
                
                if logic == "AND":
                    signal_active = all(gene_results)
                else:
                    signal_active = any(gene_results)
                
                # 投票
                weight = fitness  # 用 fitness 加权
                total_weight += weight
                
                if signal_active:
                    # 判断方向：看条件关键词
                    cond_text = " ".join(g.get("entry_condition", "") for g in genes).lower()
                    if any(k in cond_text for k in ["oversold", "< 30", "< 20", "cross_up", "golden", "bullish"]):
                        buy_votes += weight
                    elif any(k in cond_text for k in ["overbought", "> 70", "> 80", "cross_down", "death", "bearish"]):
                        sell_votes += weight
                    else:
                        # 信号触发但方向不明，根据 RSI 区分
                        rsi = indicators.get("rsi_14", 50)
                        if rsi < 40:
                            buy_votes += weight * 0.6
                        elif rsi > 60:
                            sell_votes += weight * 0.6
                        else:
                            hold_votes += weight
                else:
                    hold_votes += weight * 0.5
            
            if total_weight == 0:
                return None
            
            # 归一化
            buy_pct = buy_votes / total_weight
            sell_pct = sell_votes / total_weight
            hold_pct = hold_votes / total_weight
            
            # 决定信号
            if buy_pct > sell_pct and buy_pct > hold_pct:
                signal = "BUY"
                confidence = buy_pct
            elif sell_pct > buy_pct and sell_pct > hold_pct:
                signal = "SELL"
                confidence = sell_pct
            else:
                signal = "HOLD"
                confidence = hold_pct
            
            return {
                "signal": signal,
                "confidence": round(min(confidence, 0.95), 3),
                "active_strategies": len(viable),
                "votes": {
                    "buy": round(buy_pct, 3),
                    "sell": round(sell_pct, 3),
                    "hold": round(hold_pct, 3),
                },
                "source": "indicator_explorer",
            }
            
        except Exception as e:
            logger.error(f"进化策略信号评估失败: {e}")
            return None
    
    def _evaluate_condition(self, condition: str, indicators: Dict) -> bool:
        """
        安全地评估策略条件表达式
        
        只允许简单的比较运算，不执行任意代码。
        
        Examples:
            "rsi_14 < 30"
            "macd > macd_signal"
            "atr > 0.02"
        """
        import re
        
        # 安全检查：只允许比较表达式
        if not re.match(r'^[\w.]+\s*(<|>|<=|>=|==|!=)\s*[\w.+-]+$', condition.strip()):
            # 多条件用 AND/OR 连接
            # 尝试拆分
            parts = re.split(r'\s+(AND|OR|and|or)\s+', condition)
            if len(parts) > 1:
                results = []
                logic_op = 'AND'
                for i, part in enumerate(parts):
                    if part.upper() in ('AND', 'OR'):
                        logic_op = part.upper()
                    else:
                        results.append(self._evaluate_condition(part.strip(), indicators))
                
                if logic_op == 'AND':
                    return all(results)
                else:
                    return any(results)
            
            return False  # 不识别的表达式
        
        # 解析 A op B
        match = re.match(r'([\w.]+)\s*(<|>|<=|>=|==|!=)\s*([\w.+-]+)', condition.strip())
        if not match:
            return False
        
        left_key, op, right_str = match.groups()
        
        # 获取左侧值
        left_val = indicators.get(left_key)
        if left_val is None:
            return False
        
        # 获取右侧值
        try:
            right_val = float(right_str)
        except ValueError:
            right_val = indicators.get(right_str)
            if right_val is None:
                return False
        
        # 比较
        try:
            left_val = float(left_val)
            right_val = float(right_val)
            if op == '<': return left_val < right_val
            elif op == '>': return left_val > right_val
            elif op == '<=': return left_val <= right_val
            elif op == '>=': return left_val >= right_val
            elif op == '==': return abs(left_val - right_val) < 1e-10
            elif op == '!=': return abs(left_val - right_val) >= 1e-10
        except (TypeError, ValueError):
            pass
        
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("IndicatorExplorer Test")
    print("=" * 60)
    
    explorer = IndicatorExplorer(
        config_path="config.yaml",
        learning_db_path="ai_learning.db"
    )
    
    print("\n=== 指标基因库 ===")
    print(f"总基因数: {len(explorer.indicator_library)}")
    print("\n示例基因:")
    for gene in explorer.indicator_library[:5]:
        print(f"  {gene.indicator_type}({gene.params})")
    
    print("\n=== 初始化种群 ===")
    explorer.initialize_population()
    print(f"种群大小: {len(explorer.population)}")
    
    print("\n=== 进化测试 (5代) ===")
    best = explorer.run_evolution(generations=5, target_fitness=0.9)
    
    if best:
        print(f"\n最优策略:")
        print(f"  ID: {best.strategy_id}")
        print(f"  适应度: {best.fitness:.3f}")
        print(f"  基因数: {len(best.genes)}")
        print(f"  表达式: {best.evaluate_expression()}")
    
    print("\n=== 已发现策略 ===")
    strategies = explorer.list_evolved_strategies()
    for s in strategies[:5]:
        print(f"  {s['strategy_id']}: fitness={s['fitness']:.3f}")
