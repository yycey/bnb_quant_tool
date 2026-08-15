# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - 策略变异器 v1.0
AI驱动的策略生成与影子测试验证
作者: Python全栈工程师
日期: 2026-06-03
"""

import json
import sqlite3
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import logging
import requests
import yaml

logger = logging.getLogger(__name__)


class StrategyCandidate:
    """策略候选 - 影子测试中的新策略"""
    
    def __init__(self, strategy_id: str, strategy_def: Dict, 
                 parent_id: Optional[str] = None,
                 mutation_reason: str = ""):
        self.strategy_id = strategy_id
        self.strategy_def = strategy_def
        self.parent_id = parent_id
        self.mutation_reason = mutation_reason
        self.created_at = datetime.now()
        
        # 影子测试统计
        self.shadow_trades = []  # 记录每笔影子交易
        self.total_trades = 0
        self.wins = 0
        self.total_pnl = 0.0
        
    def add_shadow_trade(self, pnl: float, win: bool, details: Dict = None):
        """添加一笔影子交易"""
        self.shadow_trades.append({
            "pnl": pnl,
            "win": win,
            "timestamp": datetime.now().isoformat(),
            "details": details or {}
        })
        self.total_trades += 1
        if win:
            self.wins += 1
        self.total_pnl += pnl
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0
    
    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0.0
    
    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "strategy_def": self.strategy_def,
            "parent_id": self.parent_id,
            "mutation_reason": self.mutation_reason,
            "created_at": self.created_at.isoformat(),
            "stats": {
                "total_trades": self.total_trades,
                "wins": self.wins,
                "win_rate": self.win_rate,
                "total_pnl": self.total_pnl,
                "avg_pnl": self.avg_pnl
            }
        }


class StrategyMutator:
    """
    策略变异器 - AI驱动的策略进化
    
    核心能力:
    1. 从复盘结果提取策略模式
    2. 生成策略变异（参数调整、条件组合）
    3. 管理影子测试队列
    4. 验证新策略有效性
    5. 自动启用通过验证的策略
    """
    
    MIN_SHADOW_TRADES = 10  # 最少影子交易数
    WIN_RATE_THRESHOLD = 0.55  # 胜率阈值（需超过基准）
    MIN_CONFIDENCE = 0.6  # AI置信度阈值
    
    def __init__(self, config_path: str = "config.yaml",
                 learning_db_path: str = None,
                 api_key: str = None,
                 base_url: str = "https://api.deepseek.com"):
        
        self.config_path = config_path
        from bnb_quant_tool.data_localization import resolve_db_path
        self.learning_db_path = resolve_db_path(learning_db_path, "ai_learning")
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
            # 模型配置
            self.model = self.config.get("deepseek", {}).get("model", "deepseek-chat")

        if not getattr(self, "model", None):
            from bnb_quant_tool.llm_provider import get_llm_credentials
            self.model = get_llm_credentials(self.config)["model"]
        
        # 活跃的影子策略
        self.shadow_strategies: Dict[str, StrategyCandidate] = {}
        
        # 策略模板库
        self.strategy_templates = self._init_strategy_templates()
        
        logger.info(f"StrategyMutator initialized, model={self.model}")
        self._local = threading.local()

    def _get_conn(self):
        """线程本地连接（WAL模式，避免 database is locked）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.conn = connect_writer(self.learning_db_path, timeout=60.0)
        return self._local.conn

    def reset_connection(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
    def _load_config(self) -> Dict:
        """加载配置"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {}
    
    def _init_strategy_templates(self) -> List[Dict]:
        """初始化策略模板库"""
        return [
            # 趋势跟踪类
            {
                "category": "trend_following",
                "name": "RSI趋势",
                "conditions": ["rsi < 30", "price > ema_20"],
                "params": {
                    "rsi_oversold": {"min": 20, "max": 35, "default": 30},
                    "rsi_overbought": {"min": 65, "max": 80, "default": 70}
                }
            },
            {
                "category": "trend_following",
                "name": "MACD趋势",
                "conditions": ["macd_cross_up", "price > ema_50"],
                "params": {
                    "macd_fast": {"min": 8, "max": 16, "default": 12},
                    "macd_slow": {"min": 20, "max": 30, "default": 26}
                }
            },
            # 均值回归类
            {
                "category": "mean_reversion",
                "name": "布林带回归",
                "conditions": ["price < boll_lower", "rsi < 40"],
                "params": {
                    "boll_period": {"min": 15, "max": 25, "default": 20},
                    "boll_std": {"min": 1.5, "max": 2.5, "default": 2.0}
                }
            },
            # 共振类（新策略方向）
            {
                "category": "confluence",
                "name": "RSI+MACD共振",
                "conditions": ["rsi < 35", "macd_cross_up"],
                "params": {
                    "rsi_threshold": {"min": 25, "max": 40, "default": 35},
                    "volume_factor": {"min": 1.0, "max": 2.0, "default": 1.5}
                }
            },
            {
                "category": "confluence",
                "name": "波动率自适应",
                "conditions": ["atr_percentile < 30", "rsi_oversold"],
                "params": {
                    "atr_sl_mult": {"min": 1.0, "max": 3.0, "default": 1.5},
                    "atr_window": {"min": 10, "max": 20, "default": 14}
                }
            }
        ]
    
    def generate_mutations(self, review_result: Dict) -> List[StrategyCandidate]:
        """
        基于AI复盘结果生成策略变异
        
        Args:
            review_result: AI复盘输出
        
        Returns:
            策略候选列表
        """
        ai_result = review_result.get("ai_result", {})
        candidates = []
        
        # 1. 参数调整型变异
        param_suggestions = ai_result.get("param_suggestions", [])
        for sug in param_suggestions[:2]:  # 最多取2个参数建议
            candidate = self._create_param_mutation(sug)
            if candidate:
                candidates.append(candidate)
        
        # 2. 条件组合型变异
        new_idea = ai_result.get("new_strategy_idea", "")
        if new_idea and ai_result.get("confidence", 0) >= self.MIN_CONFIDENCE:
            candidate = self._create_condition_mutation(new_idea, ai_result)
            if candidate:
                candidates.append(candidate)
        
        # 3. 从亏损模式提取防御规则
        loss_patterns = ai_result.get("loss_patterns", [])
        if loss_patterns:
            candidate = self._create_defense_mutation(loss_patterns)
            if candidate:
                candidates.append(candidate)
        
        logger.info(f"生成 {len(candidates)} 个策略候选")
        return candidates
    
    def _create_param_mutation(self, param_sug: Dict) -> Optional[StrategyCandidate]:
        """创建参数调整型变异"""
        param_name = param_sug.get("param")
        new_value = param_sug.get("new")
        
        if not param_name or new_value is None:
            return None
        
        # 生成策略ID
        strategy_id = f"param_{param_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # 构建策略定义
        strategy_def = {
            "type": "param_adjustment",
            "target_param": param_name,
            "new_value": new_value,
            "reason": param_sug.get("reason", ""),
            "expected_impact": f"调整{param_name}以改善表现"
        }
        
        return StrategyCandidate(
            strategy_id=strategy_id,
            strategy_def=strategy_def,
            mutation_reason=f"AI建议: {param_sug.get('reason', '')}"
        )
    
    def _create_condition_mutation(self, idea: str, ai_result: Dict) -> Optional[StrategyCandidate]:
        """创建条件组合型变异"""
        # 尝试匹配模板
        matched_template = self._match_strategy_template(idea)
        
        if matched_template:
            template = matched_template["template"]
            conditions = template["conditions"].copy()
            
            # 根据AI建议微调参数
            params = {}
            for param_name, param_def in template.get("params", {}).items():
                params[param_name] = param_def["default"]
            
            strategy_id = f"confluence_{matched_template['index']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            strategy_def = {
                "type": "condition_combination",
                "category": template["category"],
                "name": template["name"],
                "conditions": conditions,
                "params": params,
                "idea_source": idea
            }
            
            return StrategyCandidate(
                strategy_id=strategy_id,
                strategy_def=strategy_def,
                mutation_reason=f"AI新策略思路: {idea[:50]}..."
            )
        
        return None
    
    def _match_strategy_template(self, idea: str) -> Optional[Dict]:
        """匹配策略模板"""
        idea_lower = idea.lower()
        
        # 关键词匹配
        keywords = {
            "rsi": ["rsi"],
            "macd": ["macd"],
            "共振": ["confluence"],
            "波动率": ["atr", "volatility"],
            "布林": ["boll"]
        }
        
        for idx, template in enumerate(self.strategy_templates):
            template_name = template["name"].lower()
            category = template["category"]
            
            # 检查关键词
            matched = False
            for key, words in keywords.items():
                if any(w in idea_lower for w in words):
                    if key in template_name or category == key:
                        matched = True
                        break
            
            # 检查模板名是否在想法中
            if any(word in template_name for word in idea_lower.split()):
                matched = True
            
            if matched:
                return {"template": template, "index": idx}
        
        # 默认返回第一个共振类模板
        for idx, t in enumerate(self.strategy_templates):
            if t["category"] == "confluence":
                return {"template": t, "index": idx}
        
        return None
    
    def _create_defense_mutation(self, loss_patterns: List[str]) -> Optional[StrategyCandidate]:
        """创建防御型变异"""
        # 提取关键特征
        filter_rules = []
        
        for pattern in loss_patterns[:3]:
            pattern_lower = pattern.lower()
            
            # RSI过热不过开仓
            if "rsi" in pattern_lower and ("接近" in pattern or "未到" in pattern):
                filter_rules.append({
                    "type": "entry_filter",
                    "condition": "rsi_reject_range",
                    "params": {"min": 65, "max": 75},
                    "reason": f"避免在RSI {65}-{75}区间开单"
                })
            
            # 高波动期不过开仓
            if "波动" in pattern or "震出" in pattern:
                filter_rules.append({
                    "type": "entry_filter",
                    "condition": "atr_high_filter",
                    "params": {"atr_percentile_max": 80},
                    "reason": "避免在高波动期开单"
                })
        
        if not filter_rules:
            return None
        
        strategy_id = f"defense_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        strategy_def = {
            "type": "defense_rules",
            "filter_rules": filter_rules,
            "loss_patterns": loss_patterns
        }
        
        return StrategyCandidate(
            strategy_id=strategy_id,
            strategy_def=strategy_def,
            mutation_reason=f"防御性规则: {len(filter_rules)}条过滤条件"
        )
    
    def add_to_shadow_queue(self, candidate: StrategyCandidate) -> bool:
        """添加策略到影子测试队列"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            # 创建影子策略表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shadow_strategies (
                    strategy_id TEXT PRIMARY KEY,
                    created_at TEXT,
                    strategy_def TEXT,
                    parent_id TEXT,
                    mutation_reason TEXT,
                    status TEXT DEFAULT 'active',
                    shadow_trades TEXT DEFAULT '[]',
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0.0
                )
            """)
            
            cur.execute("""
                INSERT INTO shadow_strategies 
                (strategy_id, created_at, strategy_def, parent_id, mutation_reason, status)
                VALUES (?, ?, ?, ?, ?, 'active')
            """, (
                candidate.strategy_id,
                candidate.created_at.isoformat(),
                json.dumps(candidate.strategy_def, ensure_ascii=False),
                candidate.parent_id or "",
                candidate.mutation_reason
            ))
            
            conn.commit()
            
            self.shadow_strategies[candidate.strategy_id] = candidate
            logger.info(f"策略 {candidate.strategy_id} 已加入影子测试队列")
            return True
        
        except Exception as e:
            logger.error(f"添加影子策略失败: {e}")
            return False
    
    def record_shadow_trade(self, strategy_id: str, pnl: float, 
                           win: bool, market_data: Dict = None):
        """记录影子交易"""
        if strategy_id not in self.shadow_strategies:
            self._load_shadow_strategy(strategy_id)
        
        if strategy_id not in self.shadow_strategies:
            logger.warning(f"影子策略不存在: {strategy_id}")
            return
        
        candidate = self.shadow_strategies[strategy_id]
        candidate.add_shadow_trade(pnl, win, market_data)
        
        # 更新数据库
        try:
            from bnb_quant_tool.sqlite_util import begin_immediate, run_db

            payload = (
                json.dumps(candidate.shadow_trades, ensure_ascii=False),
                candidate.total_trades,
                candidate.wins,
                candidate.total_pnl,
                strategy_id,
            )

            def _op():
                conn = self._get_conn()
                cur = conn.cursor()
                try:
                    begin_immediate(conn)
                    cur.execute("""
                        UPDATE shadow_strategies 
                        SET shadow_trades = ?, total_trades = ?, wins = ?, total_pnl = ?
                        WHERE strategy_id = ?
                    """, payload)
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    self.reset_connection()
                    raise

            run_db(_op, label=f"shadow_trade:{strategy_id}", on_locked=self.reset_connection)
            # 检查是否达到验证条件
            self._check_validation(strategy_id)
        except Exception as e:
            logger.error(f"记录影子交易失败: {e}")
            try:
                self.reset_connection()
            except Exception:
                pass
    def _load_shadow_strategy(self, strategy_id: str):
        """从数据库加载影子策略"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute("""
                SELECT * FROM shadow_strategies WHERE strategy_id = ?
            """, (strategy_id,))
            
            row = cur.fetchone()
            
            if row:
                candidate = StrategyCandidate(
                    strategy_id=row["strategy_id"],
                    strategy_def=json.loads(row["strategy_def"]),
                    parent_id=row["parent_id"] or None,
                    mutation_reason=row["mutation_reason"]
                )
                candidate.created_at = datetime.fromisoformat(row["created_at"])
                candidate.shadow_trades = json.loads(row["shadow_trades"])
                candidate.total_trades = row["total_trades"]
                candidate.wins = row["wins"]
                candidate.total_pnl = row["total_pnl"]
                
                self.shadow_strategies[strategy_id] = candidate
        
        except Exception as e:
            logger.error(f"加载影子策略失败: {e}")
    
    def _check_validation(self, strategy_id: str):
        """检查策略是否通过验证"""
        candidate = self.shadow_strategies.get(strategy_id)
        if not candidate:
            return
        
        # 需要足够样本
        if candidate.total_trades < self.MIN_SHADOW_TRADES:
            return
        
        # 获取基准策略胜率
        baseline_wr = self._get_baseline_winrate()
        
        # 检查是否超过阈值
        if candidate.win_rate >= baseline_wr + self.WIN_RATE_THRESHOLD:
            self._promote_strategy(strategy_id, "win_rate_passed")
            logger.info(f"策略 {strategy_id} 通过验证，胜率 {candidate.win_rate:.1%}")
        
        # 如果表现很差，直接淘汰
        elif candidate.win_rate < 0.3 and candidate.total_trades >= 5:
            self._retire_strategy(strategy_id, "poor_performance")
            logger.info(f"策略 {strategy_id} 已淘汰，胜率 {candidate.win_rate:.1%}")
    
    def _get_baseline_winrate(self) -> float:
        """获取基准胜率"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) as winrate
                FROM analysis_records
                WHERE created_at > datetime('now', '-30 days')
            """)
            
            row = cur.fetchone()
            
            return float(row[0]) if row and row[0] else 0.5
        
        except Exception:
            return 0.5
    
    def _promote_strategy(self, strategy_id: str, reason: str):
        """升级策略到正式使用 + 自动应用参数"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE shadow_strategies SET status = 'promoted' WHERE strategy_id = ?
            """, (strategy_id,))
            
            # 记录到学习日志
            cur.execute("""
                INSERT INTO learning_log (event_type, event_data, created_at)
                VALUES ('strategy_promoted', ?, ?)
            """, (json.dumps({
                "strategy_id": strategy_id,
                "reason": reason,
                "stats": self.shadow_strategies[strategy_id].to_dict()["stats"]
            }, ensure_ascii=False), datetime.now().isoformat()))
            
            conn.commit()
            
            # ===== 闭环：自动应用 promoted 策略 =====
            candidate = self.shadow_strategies.get(strategy_id)
            if candidate:
                applied = self._apply_promoted_strategy(candidate)
                if applied:
                    logger.info(f"🧠 策略 {strategy_id} 已升级并自动应用: {applied}")
                else:
                    logger.info(f"策略 {strategy_id} 已升级，但无可直接应用的参数变更")
            else:
                logger.info(f"策略 {strategy_id} 已升级为正式策略")
        
        except Exception as e:
            logger.error(f"升级策略失败: {e}")
    
    def _apply_promoted_strategy(self, candidate: StrategyCandidate) -> Optional[List[str]]:
        """
        将 promoted 策略自动应用到 config.yaml
        
        支持3种策略类型:
        1. param_adjustment → 直接修改 config 参数
        2. condition_combination → 写入 config 的 active_strategies 列表
        3. defense_rules → 写入 config 的 defense_filters 列表
        
        Returns:
            应用的参数列表，或 None
        """
        sdef = candidate.strategy_def
        stype = sdef.get("type", "")
        applied = []
        
        try:
            import yaml
            config_path = self.config_path
            
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            
            if stype == "param_adjustment":
                # 直接修改 config 中的参数
                param_name = sdef.get("target_param")
                new_value = sdef.get("new_value")
                if param_name and new_value is not None:
                    # 递归设置嵌套 key（如 risk_management.atr_sl_mult）
                    keys = param_name.split(".")
                    obj = config
                    for k in keys[:-1]:
                        if k not in obj or not isinstance(obj[k], dict):
                            obj[k] = {}
                        obj = obj[k]
                    old_value = obj.get(keys[-1])
                    obj[keys[-1]] = new_value
                    applied.append(f"{param_name}: {old_value} → {new_value}")
                    logger.info(f"  ✅ {param_name} = {new_value} (旧值={old_value})")
            
            elif stype == "condition_combination":
                # 写入 active_strategies
                if "active_strategies" not in config:
                    config["active_strategies"] = []
                entry = {
                    "id": candidate.strategy_id,
                    "name": sdef.get("name", "unknown"),
                    "category": sdef.get("category", "confluence"),
                    "conditions": sdef.get("conditions", []),
                    "params": sdef.get("params", {}),
                    "source": "shadow_promoted",
                    "win_rate": round(candidate.win_rate, 3),
                    "total_trades": candidate.total_trades,
                    "promoted_at": datetime.now().isoformat(),
                }
                config["active_strategies"].append(entry)
                applied.append(f"active_strategies += {sdef.get('name', candidate.strategy_id)}")
                logger.info(f"  ✅ 添加活跃策略: {sdef.get('name', candidate.strategy_id)}")
            
            elif stype == "defense_rules":
                # 写入 defense_filters
                if "defense_filters" not in config:
                    config["defense_filters"] = []
                for rule in sdef.get("filter_rules", []):
                    rule_entry = rule.copy()
                    rule_entry["source"] = candidate.strategy_id
                    rule_entry["promoted_at"] = datetime.now().isoformat()
                    config["defense_filters"].append(rule_entry)
                    applied.append(f"defense: {rule.get('condition', rule.get('type', '?'))}")
                    logger.info(f"  ✅ 添加防御规则: {rule.get('condition', rule.get('type'))}")
            
            if applied:
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                logger.info(f"config.yaml 已更新 ({len(applied)}项)")
            
            return applied if applied else None
            
        except Exception as e:
            logger.error(f"自动应用策略失败: {e}")
            return None
    
    def _retire_strategy(self, strategy_id: str, reason: str):
        """淘汰策略"""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE shadow_strategies SET status = 'retired' WHERE strategy_id = ?
            """, (strategy_id,))
            
            conn.commit()
        
        except Exception as e:
            logger.error(f"淘汰策略失败: {e}")
    
    def list_shadow_strategies(self, status: str = "active") -> List[Dict]:
        """列出影子策略"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute("""
                SELECT strategy_id, created_at, mutation_reason, total_trades, 
                       wins, total_pnl, status
                FROM shadow_strategies
                WHERE status = ?
                ORDER BY created_at DESC
            """, (status,))
            
            rows = cur.fetchall()
            
            strategies = []
            for r in rows:
                win_rate = r["wins"] / r["total_trades"] if r["total_trades"] > 0 else 0
                strategies.append({
                    "strategy_id": r["strategy_id"],
                    "created_at": r["created_at"],
                    "mutation_reason": r["mutation_reason"],
                    "total_trades": r["total_trades"],
                    "wins": r["wins"],
                    "win_rate": win_rate,
                    "total_pnl": r["total_pnl"],
                    "status": r["status"]
                })
            
            return strategies
        
        except Exception as e:
            logger.error(f"列出影子策略失败: {e}")
            return []
    
    def get_promoted_strategies(self) -> List[Dict]:
        """获取已升级的策略"""
        return self.list_shadow_strategies(status="promoted")
    
    def call_deepseek_for_mutation(self, prompt: str) -> Dict:
        """调用DeepSeek生成策略变异"""
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个量化策略研究员，擅长从交易数据中发现规律并提出创新策略。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,  # 稍高的温度鼓励创新
            "stream": False
        }
        
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            # 尝试解析JSON
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"raw_content": content}
        
        except Exception as e:
            logger.error(f"调用DeepSeek失败: {e}")
            return {"error": str(e)}


if __name__ == "__main__":
    print("=" * 60)
    print("StrategyMutator Test")
    print("=" * 60)
    
    mutator = StrategyMutator(
        config_path="config.yaml",
        learning_db_path="ai_learning.db"
    )
    
    # 测试生成变异
    test_review = {
        "ai_result": {
            "param_suggestions": [
                {"param": "atr_sl_mult", "old": 1.5, "new": 2.0, "reason": "止损过紧"}
            ],
            "new_strategy_idea": "RSI和MACD共振，RSI低于35且MACD金叉时开多",
            "confidence": 0.75,
            "loss_patterns": ["RSI接近70但未到超买时开空容易止损"]
        }
    }
    
    print("\n生成策略变异:")
    candidates = mutator.generate_mutations(test_review)
    
    for c in candidates:
        print(f"\n  {c.strategy_id}")
        print(f"    类型: {c.strategy_def.get('type')}")
        print(f"    原因: {c.mutation_reason}")
    
    # 测试影子策略列表
    print("\n\n当前影子策略:")
    shadows = mutator.list_shadow_strategies()
    for s in shadows[:5]:
        print(f"  {s['strategy_id']}: {s['mutation_reason'][:40]}... (胜率{s['win_rate']:.1%})")
