# -*- coding: utf-8 -*-
"""
BNB量化交易工具 - AI自动复盘引擎 v1.0
每N笔交易后自动触发DeepSeek AI复盘，输出参数优化建议
作者: Python全栈工程师
日期: 2026-06-03
"""

import json
import sqlite3
import threading
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import logging

import requests

logger = logging.getLogger(__name__)


class AIReviewEngine:
    """
    AI自动复盘引擎

    核心功能:
    1. 从 paper_trading.db 提取最近N笔交易
    2. 构建复盘Prompt，调用DeepSeek分析
    3. 解析AI建议，输出参数调整方案
    4. 记录复盘结果到 learning_log
    """

    def __init__(self, config: Dict, deepseek_api_key: str,
                 deepseek_model: str = "deepseek-chat",
                 deepseek_base_url: str = "https://api.deepseek.com"):
        self.config = config
        self.api_key = deepseek_api_key
        self.model = deepseek_model
        self.base_url = deepseek_base_url

        # 复盘配置
        review_cfg = config.get('auto_review', {})
        self.trigger_every_n = review_cfg.get('trigger_every_n_trades', 15)
        self.trigger_on_streak = review_cfg.get('trigger_on_streak', 3)
        self.min_trades_to_review = review_cfg.get('min_trades_to_review', 5)
        self.auto_apply = review_cfg.get('auto_apply', True)

        # 防止同一笔计数重复触发
        self._last_review_at_count = 0
        # 连亏复盘防抖（秒）
        self._last_streak_review_ts = 0.0
        self.streak_review_cooldown_sec = float(
            review_cfg.get("streak_review_cooldown_sec", 1800) or 1800
        )
        self._review_lock = threading.Lock()
        self._review_running = False

        # 数据库路径（优先 data/ 子目录）
        try:
            from bnb_quant_tool.data_localization import get_localized_db_path
            self.paper_db_path = str(get_localized_db_path('paper_trading'))
            self.learning_db_path = str(get_localized_db_path('ai_learning'))
        except ImportError:
            base_dir = Path(__file__).parent.parent.parent
            data_dir = base_dir / "data"
            if data_dir.exists():
                self.paper_db_path = str(data_dir / "paper_trading.db")
                self.learning_db_path = str(data_dir / "ai_learning.db")
            else:
                self.paper_db_path = str(base_dir / "paper_trading.db")
                self.learning_db_path = str(base_dir / "ai_learning.db")
        logger.info(f"AIReviewEngine initialized, trigger_every_n={self.trigger_every_n}, "
                    f"db={self.paper_db_path}")
        # 线程本地连接池，避免并发锁和连接泄漏
        self._local = threading.local()

    def _get_paper_conn(self):
        """获取 paper_trading.db 的线程本地连接（WAL模式）"""
        if not hasattr(self._local, 'paper_conn') or self._local.paper_conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.paper_conn = connect_writer(
                self.paper_db_path, timeout=60.0, row_factory=True
            )
        return self._local.paper_conn

    def _get_learning_conn(self):
        """获取 ai_learning.db 的线程本地连接（WAL模式）"""
        if not hasattr(self._local, 'learning_conn') or self._local.learning_conn is None:
            from bnb_quant_tool.sqlite_util import connect_writer
            self._local.learning_conn = connect_writer(
                self.learning_db_path, timeout=60.0
            )
        return self._local.learning_conn

    def reset_connection(self) -> None:
        """关闭当前线程 DB 连接（导入/外部写入后刷新用）。"""
        for attr in ("paper_conn", "learning_conn"):
            conn = getattr(self._local, attr, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                setattr(self._local, attr, None)

    def try_begin_review(self) -> bool:
        """单飞：已有复盘在跑则返回 False。"""
        with self._review_lock:
            if self._review_running:
                return False
            self._review_running = True
            return True

    def end_review(self) -> None:
        with self._review_lock:
            self._review_running = False

    def mark_review_triggered(self, *, total_closed: int = 0, streak: bool = False) -> None:
        if total_closed:
            self._last_review_at_count = int(total_closed)
        if streak:
            import time
            self._last_streak_review_ts = time.time()

    def should_trigger_review(self, paper_engine) -> Tuple[bool, str]:
        """
        判断是否应该触发复盘

        Returns:
            (should_trigger, reason)
        """
        try:
            if self._review_running:
                return False, "复盘进行中"

            stats = paper_engine.get_stats()
            total_closed = stats.get('total_closed_trades', 0)

            # 条件1: 达到触发间隔
            if total_closed > 0 and total_closed % self.trigger_every_n == 0:
                # 防重复：本次阈值和上次复盘时相同则跳过
                if total_closed > self._last_review_at_count:
                    return True, f"达到{total_closed}笔交易（每{self.trigger_every_n}笔复盘，上次在{self._last_review_at_count}笔）"

            # 条件2: 连续亏损（带冷却，避免每笔都刷）
            recent_trades = paper_engine.get_recent_trades(limit=5)
            if len(recent_trades) >= self.trigger_on_streak:
                recent_pnls = [t.get('pnl', 0) or 0 for t in recent_trades[:self.trigger_on_streak]]
                if all(p < 0 for p in recent_pnls):
                    import time
                    now = time.time()
                    if now - float(self._last_streak_review_ts or 0) < self.streak_review_cooldown_sec:
                        return False, "连亏复盘冷却中"
                    return True, f"连续{self.trigger_on_streak}笔亏损"

            return False, ""
        except Exception as e:
            logger.error(f"should_trigger_review error: {e}")
            return False, ""

    def extract_trades_for_review(self, limit: int = 15) -> List[Dict]:
        """从paper_trading.db提取最近N笔已平仓交易"""
        try:
            conn = self._get_paper_conn()
            cur = conn.cursor()

            # 优先查 paper_positions 表（实际使用的表）
            # 兼容旧 trades 表
            table_name = "paper_positions"
            try:
                cur.execute("SELECT count(*) FROM paper_positions WHERE status='CLOSED'")
            except sqlite3.OperationalError:
                try:
                    cur.execute("SELECT count(*) FROM trades WHERE status='closed'")
                    table_name = "trades"
                except sqlite3.OperationalError:
                    return []

            if table_name == "paper_positions":
                cur.execute("""
                    SELECT
                        id, symbol, side, entry_price, close_avg_price,
                        qty_total, realized_pnl_usdt, opened_at, closed_at,
                        sl, close_reason, leverage,
                        mfe_pct, mae_pct, mfe_r, mae_r, r_multiple
                    FROM paper_positions
                        WHERE status = 'CLOSED'
                    ORDER BY closed_at DESC
                    LIMIT ?
                """, (limit,))
            else:
                cur.execute("""
                    SELECT
                        id, symbol, action, entry_price, exit_price,
                        quantity, pnl, pnl_pct, entry_time, exit_time,
                        stop_loss, take_profit, exit_reason
                    FROM trades
                        WHERE status = 'closed'
                    ORDER BY exit_time DESC
                    LIMIT ?
                """, (limit,))

            rows = cur.fetchall()

            trades = []
            for r in rows:
                if table_name == "paper_positions":
                    # 计算 pnl_pct: (close_avg - entry) * qty / (entry * qty)
                    entry = float(r["entry_price"] or 0)
                    close = float(r["close_avg_price"] or 0)
                    qty = float(r["qty_total"] or 0)
                    pnl = float(r["realized_pnl_usdt"] or 0)
                    pnl_pct = ((close - entry) / entry * 100) if entry > 0 else 0
                    # 根据方向修正
                    side = str(r["side"] or "LONG")
                    if side == "SHORT":
                        pnl_pct = -pnl_pct
                    trades.append({
                        "id": r["id"],
                        "symbol": r["symbol"],
                        "action": side,
                        "entry_price": entry,
                        "exit_price": close,
                        "quantity": qty,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "entry_time": r["opened_at"],
                        "exit_time": r["closed_at"],
                        "stop_loss": float(r["sl"] or 0),
                        "take_profit": 0,  # paper_positions 没有 tp 字段
                        "exit_reason": r["close_reason"],
                        "leverage": int(r["leverage"] or 1),
                        # v2.0: MFE/MAE 诊断数据
                        "mfe_pct": round(float(r["mfe_pct"] or 0), 2),
                        "mae_pct": round(float(r["mae_pct"] or 0), 2),
                        "mfe_r": round(float(r["mfe_r"] or 0), 2),
                        "mae_r": round(float(r["mae_r"] or 0), 2),
                        "r_multiple": round(float(r["r_multiple"] or 0), 3),
                    })
                else:
                    trades.append({
                        "id": r["id"],
                        "symbol": r["symbol"],
                        "action": r["action"],
                        "entry_price": float(r["entry_price"] or 0),
                        "exit_price": float(r["exit_price"] or 0),
                        "quantity": float(r["quantity"] or 0),
                        "pnl": float(r["pnl"] or 0),
                        "pnl_pct": float(r["pnl_pct"] or 0),
                        "entry_time": r["entry_time"],
                        "exit_time": r["exit_time"],
                        "stop_loss": float(r["stop_loss"] or 0),
                        "take_profit": float(r["take_profit"] or 0),
                        "exit_reason": r["exit_reason"],
                    })

            logger.info(f"Extracted {len(trades)} trades for review")
            return trades

        except Exception as e:
            logger.error(f"extract_trades_for_review error: {e}")
            return []

    def calculate_trade_statistics(self, trades: List[Dict]) -> Dict:
        """计算交易统计特征，供AI分析"""
        if not trades:
            return {}

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]

        stats = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
            "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
            "total_pnl_pct": sum(t["pnl_pct"] for t in trades),
            "long_trades": sum(1 for t in trades if t["action"] == "LONG"),
            "short_trades": sum(1 for t in trades if t["action"] == "SHORT"),
        }

        # 盈利交易特征
        if wins:
            stats["win_avg_pnl_pct"] = sum(t["pnl_pct"] for t in wins) / len(wins)
            stats["win_avg_hold_hours"] = sum(
                self._hours_between(t["entry_time"], t["exit_time"])
                for t in wins
            ) / len(wins)

        # 亏损交易特征
        if losses:
            stats["loss_avg_pnl_pct"] = sum(t["pnl_pct"] for t in losses) / len(losses)
            stats["loss_avg_hold_hours"] = sum(
                self._hours_between(t["entry_time"], t["exit_time"])
                for t in losses
            ) / len(losses)

        # 出场原因统计
        exit_reasons = {}
        for t in trades:
            reason = t.get("exit_reason") or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        stats["exit_reasons"] = exit_reasons

        # v2.0: MFE/MAE 诊断统计
        mfe_r_values = [t.get("mfe_r", 0) for t in trades if t.get("mfe_r")] 
        mae_r_values = [t.get("mae_r", 0) for t in trades if t.get("mae_r")]
        r_mult_values = [t.get("r_multiple", 0) for t in trades if t.get("r_multiple") is not None]
        if mfe_r_values:
            stats["avg_mfe_r"] = round(sum(mfe_r_values) / len(mfe_r_values), 2)
            stats["max_mfe_r"] = round(max(mfe_r_values), 2)
        if mae_r_values:
            stats["avg_mae_r"] = round(sum(mae_r_values) / len(mae_r_values), 2)
            stats["min_mae_r"] = round(min(mae_r_values), 2)
        if r_mult_values:
            stats["avg_r_multiple"] = round(sum(r_mult_values) / len(r_mult_values), 3)
        # MFE>R 但 R<0 的比例（错失浮盈的交易）
        missed_profit = [t for t in trades if t.get("mfe_r", 0) > 0.5 and (t.get("r_multiple") or 0) < 0]
        if trades:
            stats["missed_profit_pct"] = round(len(missed_profit) / len(trades) * 100, 1)

        return stats

    def _hours_between(self, t1: str, t2: str) -> float:
        """计算两个时间字符串之间的小时数"""
        try:
            dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
            return abs((dt2 - dt1).total_seconds()) / 3600
        except Exception:
            return 0

    def build_review_prompt(self, trades: List[Dict], stats: Dict) -> str:
        """构建DeepSeek复盘提示词 v2.0 — 基于实盘数据分析优化"""

        # 当前配置参数
        trading_cfg = self.config.get("trading", {})
        risk_cfg = self.config.get("risk_management", {})
        advisor_cfg = self.config.get("trade_advisor", {})

        trades_json = json.dumps(trades[:10], ensure_ascii=False, indent=2)

        # 计算额外的诊断指标
        sl_trades = [t for t in trades if t.get('exit_reason') == 'STOP_LOSS']
        tp_trades = [t for t in trades if 'TAKE_PROFIT' in (t.get('exit_reason') or '')]
        sl_but_win = [t for t in sl_trades if t.get('pnl', 0) > 0]
        sl_wr = len(sl_but_win) / len(sl_trades) * 100 if sl_trades else 0
        
        # MFE/MAE 分析
        avg_pnl_pct = stats.get('avg_loss_pct', 0)
        avg_win_pct = stats.get('avg_win_pct', 0)
        avg_mfe_r = stats.get('avg_mfe_r', 0)
        avg_mae_r = stats.get('avg_mae_r', 0)
        avg_r_multiple = stats.get('avg_r_multiple', 0)
        missed_profit_pct = stats.get('missed_profit_pct', 0)
        
        # SL 后反弹统计（mae_r < -0.5 但 mfe_r > 0.5 的比例 = SL 后价格反弹）
        sl_bounced = [t for t in sl_trades if t.get('mae_r', 0) < -0.5 and t.get('mfe_r', 0) > 0.5]
        sl_bounce_rate = len(sl_bounced) / len(sl_trades) * 100 if sl_trades else 0

        prompt = f"""你是一个量化策略研究员。请分析以下{len(trades)}笔交易的表现，找出规律并提出改进方案。

## 交易记录（最近10笔）
{trades_json}

## 统计特征
- 总交易数: {stats.get('total_trades', 0)}
- 盈利笔数: {stats.get('wins', 0)}  亏损笔数: {stats.get('losses', 0)}
- 胜率: {stats.get('win_rate', 0):.1%}
- 平均盈利: {avg_win_pct:+.2f}%  平均亏损: {avg_pnl_pct:+.2f}%
- 累计盈亏: {stats.get('total_pnl_pct', 0):+.2f}%
- 出场原因: {stats.get('exit_reasons', {})}
- SL 触发后盈利: {len(sl_but_win)}/{len(sl_trades)} ({sl_wr:.1f}%) — {'⚠ SL 过紧' if sl_wr > 30 else '正常'}
- SL 后反弹: {len(sl_bounced)}/{len(sl_trades)} ({sl_bounce_rate:.1f}%) — {'⚠ SL 设得太近，价格常反弹' if sl_bounce_rate > 25 else '正常'}

## MFE/MAE 诊断（v2.0）
- 平均 MFE(R): {avg_mfe_r:.2f}R — 价格最多朝有利方向移动多少
- 平均 MAE(R): {avg_mae_r:.2f}R — 价格最多朝不利方向移动多少
- 平均 R-multiple: {avg_r_multiple:.3f} — 实际捕获的盈亏比
- 错失浮盈: {missed_profit_pct:.1f}% 的交易曾有 0.5R 以上浮盈但最终亏损', 

## 已知数据规律（来自 450 笔历史分析）
1. 73.8% 交易以 SL 收场，但其中 39.5% 最终盈利 → SL 过紧
2. 37.8% 交易 MAE<-1R 后才止损 → 需要波动率自适应 SL
3. 仅 14.9% 交易到达 TP3 → TP3 过远
4. UTC 5-11 盈利，13/17/21/22 严重亏损 → 时段过滤必要
5. 历史最大连亏 50 笔 → 需要熔断机制
6. Avg MFE=1.53R, avg R=0.51 → 仅捕获 1/3 浮盈

## 当前策略参数
- RSI超买阈值: 70 (超卖: 30)
- 单笔风险: {trading_cfg.get('risk_per_trade', 0.015):.1%}
- ATR止损倍数: {advisor_cfg.get('atr_sl_mult', 1.5)} (低波动: {advisor_cfg.get('atr_sl_mult_low_vol', 1.3)}, 高波动: {advisor_cfg.get('atr_sl_mult_high_vol', 2.2)})
- ATR止盈倍数: TP1={advisor_cfg.get('atr_tp1_mult', 1.5)} TP2={advisor_cfg.get('atr_tp2_mult', 3.0)} TP3={advisor_cfg.get('atr_tp3_mult', 4.5)}
- 最大仓位: {risk_cfg.get('max_position_pct', 0.2):.0%}
- TP分批比例: TP1=40% TP2=35% TP3=25%

## 任务
1. 分析亏损交易的共同特征（为什么亏？）
2. 分析盈利交易的共同特征（为什么赚？）
3. 针对"出场原因分布"，判断止损是否过紧或过松
4. 提出**具体的参数调整建议**，必须可量化
5. 提出一个新的策略改进思路

输出严格JSON格式，不要有```包裹：
{{
  "loss_patterns": ["亏损特征1", "亏损特征2"],
  "win_patterns": ["盈利特征1", "盈利特征2"],
  "sl_diagnosis": "SL 诊断结论",
  "param_suggestions": [
    {{"param": "risk_per_trade", "old": 0.015, "new": 0.02, "reason": "原因"}},
    {{"param": "atr_sl_mult", "old": 1.5, "new": 1.8, "reason": "原因"}}
  ],
  "new_strategy_idea": "描述一个新的策略思路",
  "confidence": 0.0-1.0
}}
"""
        return prompt

    def call_deepseek(self, prompt: str) -> Dict:
        """调用DeepSeek API"""
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个专业的量化策略研究员，擅长从交易数据中发现规律并提出改进方案。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "stream": False
        }

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]

            # 尝试解析JSON
            # 移除可能的```json包裹
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            ai_output = json.loads(content)
            logger.info("DeepSeek review completed")
            return ai_output

        except requests.exceptions.RequestException as e:
            logger.error(f"DeepSeek API error: {e}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}, content={content[:200]}")
            return {}

    def record_review_result(self, result: Dict, trades_count: int,
                             stats: Optional[Dict] = None):
        """将复盘结果记录到learning_log"""
        try:
            conn = self._get_learning_conn()
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO learning_log (timestamp, event_type, message, details, improvement_score)
                VALUES (?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                "AI_REVIEW",
                f"AI复盘完成，分析了{trades_count}笔交易",
                json.dumps(result, ensure_ascii=False),
                result.get("confidence", 0.5)
            ))

            # 记录参数变更建议到 param_change_log
            for sug in result.get("param_suggestions", []):
                cur.execute("""
                    INSERT INTO param_change_log (timestamp, param_name, old_value, new_value, source, review_summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now().isoformat(),
                    sug.get("param"),
                    sug.get("old"),
                    sug.get("new"),
                    "AI_REVIEW",
                    sug.get("reason")
                ))

            conn.commit()
            logger.info("Review result recorded to learning_log")

            try:
                from bnb_quant_tool.capability_memory import (
                    CapabilityMemory, extract_knowledge_async,
                )
                cm = CapabilityMemory(self.learning_db_path, config=self.config)
                extract_knowledge_async(
                    cm,
                    "extract_and_save_from_review",
                    review_result=result,
                    stats=stats,
                )
            except Exception as cap_e:
                logger.debug(f"knowledge extract from review skipped: {cap_e}")

        except Exception as e:
            logger.error(f"record_review_result error: {e}")

    def run_review(self) -> Dict:
        """执行完整的复盘流程"""
        # 1. 提取交易数据
        trades = self.extract_trades_for_review(limit=15)
        if len(trades) < self.min_trades_to_review:
            logger.info(f"Not enough trades for review ({len(trades)}/{self.min_trades_to_review})")
            return {"status": "skipped", "reason": "数据不足"}

        # 2. 计算统计特征
        stats = self.calculate_trade_statistics(trades)

        # 3. 构建Prompt
        prompt = self.build_review_prompt(trades, stats)

        # 4. 调用DeepSeek
        ai_result = self.call_deepseek(prompt)
        if not ai_result:
            return {"status": "failed", "reason": "AI调用失败"}

        # 5. 记录结果
        self.record_review_result(ai_result, len(trades), stats=stats)

        return {
            "status": "success",
            "trades_analyzed": len(trades),
            "win_rate": stats.get("win_rate", 0),
            "ai_result": ai_result
        }

    def _normalize_review_result(self, ai_result: Dict) -> Dict:
        """将 enriched / 简版复盘 JSON 统一为 record_review_result 可写入格式。"""
        out = dict(ai_result or {})
        normalized_suggestions = []
        for sug in ai_result.get("param_suggestions") or []:
            if not isinstance(sug, dict):
                continue
            normalized_suggestions.append({
                "param": sug.get("param"),
                "old": sug.get("old", sug.get("current")),
                "new": sug.get("new", sug.get("suggest")),
                "reason": sug.get("reason", ""),
            })
        out["param_suggestions"] = normalized_suggestions
        if "confidence" not in out:
            grade = str(out.get("grade", "C")).upper()
            grade_map = {"A": 0.9, "B": 0.75, "C": 0.6, "D": 0.4, "F": 0.2}
            out["confidence"] = grade_map.get(grade, 0.5)
        return out

    def run_enriched_review(self, payload: Dict, symbol: str = "BNB") -> Dict:
        """富数据复盘：DeepSeekAnalyzer.review_paper_trades → 写 param_change_log → 可 auto_apply。"""
        stats = payload.get("stats") or {}
        trades = payload.get("trades") or []
        trades_count = int(
            stats.get("total_trades")
            or stats.get("closed_trades")
            or len(trades)
            or 0
        )
        if trades_count < self.min_trades_to_review:
            return {
                "status": "skipped",
                "reason": f"数据不足 ({trades_count}/{self.min_trades_to_review})",
            }

        try:
            from bnb_quant_tool.ai_analyzer import DeepSeekAnalyzer
            analyzer = DeepSeekAnalyzer(
                api_key=self.api_key,
                model=self.model,
                base_url=self.base_url,
            )
            ai_result = analyzer.review_paper_trades(payload, symbol=symbol)
        except Exception as e:
            logger.error(f"run_enriched_review failed: {e}")
            return {"status": "failed", "reason": str(e)}

        if not ai_result:
            return {"status": "failed", "reason": "AI调用失败"}

        normalized = self._normalize_review_result(ai_result)
        self.record_review_result(normalized, trades_count, stats=stats)

        win_rate = float(stats.get("win_rate") or 0)
        if win_rate > 1:
            win_rate /= 100.0

        return {
            "status": "success",
            "trades_analyzed": trades_count,
            "win_rate": win_rate,
            "ai_result": ai_result,
        }

    def get_pending_param_changes(self) -> List[Dict]:
        """获取待人工确认的参数变更建议"""
        try:
            conn = self._get_learning_conn()
            cur = conn.cursor()

            # 最近24小时内的AI建议
            since = (datetime.now() - timedelta(hours=24)).isoformat()

            cur.execute("""
                SELECT id, timestamp, param_name, old_value, new_value, source, review_summary
                FROM param_change_log
                WHERE source = 'AI_REVIEW' AND timestamp >= ?
                ORDER BY id DESC
            """, (since,))

            rows = cur.fetchall()

            changes = []
            for r in rows:
                changes.append({
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "param": r["param_name"],
                    "old_value": r["old_value"],
                    "new_value": r["new_value"],
                    "reason": r["review_summary"]
                })

            return changes

        except Exception as e:
            logger.error(f"get_pending_param_changes error: {e}")
            return []

    def apply_all_param_changes(self, config_path: str = "config.yaml") -> Dict:
        """自动应用所有待确认的参数变更（无需人工确认）"""
        result = {
            "status": "success",
            "applied": [],
            "failed": [],
            "skipped": []
        }

        try:
            changes = self.get_pending_param_changes()
            if not changes:
                logger.info("没有待应用的参数变更")
                return result

            logger.info(f"开始自动应用 {len(changes)} 项参数变更...")

            # 加载配置
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            modified = False

            for change in changes:
                try:
                    param = change['param']
                    new_value = change['new_value']
                    change_id = change['id']

                    # 根据参数路径应用
                    if self._apply_param_to_config(config, param, new_value):
                        modified = True
                        result['applied'].append({
                            "param": param,
                            "new_value": new_value,
                            "reason": change['reason']
                        })
                        logger.info(f"✅ 应用: {param} = {new_value}")

                        # 标记为已应用
                        self._mark_param_change_applied(change_id)
                    else:
                        result['skipped'].append({
                            "param": param,
                            "reason": f"参数路径不存在: {param}"
                        })
                        logger.warning(f"⚠️ 跳过: {param} (路径不存在)")

                except Exception as e:
                    result['failed'].append({
                        "param": change['param'],
                        "error": str(e)
                    })
                    logger.error(f"❌ 应用失败: {change['param']} - {e}")

            # 保存配置
            if modified:
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.safe_dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                logger.info(f"✅ 配置已保存到 {config_path}")

                # 触发重新加载
                if hasattr(self, '_config_path'):
                    self.config = config

            result['status'] = 'success'
            logger.info(f"自动应用完成: {len(result['applied'])} 成功, {len(result['skipped'])} 跳过, {len(result['failed'])} 失败")

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)
            logger.error(f"自动应用失败: {e}", exc_info=True)

        return result

    def _apply_param_to_config(self, config: Dict, param_path: str, new_value: Any) -> bool:
        """将参数应用到配置字典"""
        # 参数路径映射
        param_mapping = {
            "confidence_threshold": ("trading", "confidence_threshold"),
            "risk_per_trade": ("trading", "risk_per_trade"),
            "max_position_pct": ("risk_management", "max_position_pct"),
            "atr_sl_mult": ("trade_advisor", "atr_sl_mult"),
            "atr_tp_mult": ("backtest", "atr_tp_mult"),
            "news_filter_threshold": ("trade_advisor", "news_filter_threshold"),
            "min_risk_reward_ratio": ("risk_management", "min_risk_reward_ratio"),
            # v2.0 新增参数
            "atr_sl_mult_low_vol": ("trade_advisor", "atr_sl_mult_low_vol"),
            "atr_sl_mult_high_vol": ("trade_advisor", "atr_sl_mult_high_vol"),
            "atr_tp1_mult": ("trade_advisor", "atr_tp1_mult"),
            "atr_tp2_mult": ("trade_advisor", "atr_tp2_mult"),
            "atr_tp3_mult": ("trade_advisor", "atr_tp3_mult"),
        }

        if param_path not in param_mapping:
            return False

        section, key = param_mapping[param_path]
        if section in config and key in config[section]:
            # 类型转换
            current = config[section][key]
            if isinstance(current, float):
                new_value = float(new_value)
            elif isinstance(current, int):
                new_value = int(float(new_value))

            config[section][key] = new_value
            return True

        return False

    def _mark_param_change_shadow_queued(self, change_id: int):
        """标记参数变更已进入影子 A/B 队列。"""
        try:
            conn = self._get_learning_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE param_change_log SET source = 'AI_REVIEW_SHADOW_QUEUED' WHERE id = ?",
                (change_id,),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"mark shadow queued failed: {e}")

    def _mark_param_change_applied(self, change_id: int):
        """标记参数变更已应用"""
        try:
            conn = self._get_learning_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE param_change_log
                SET source = 'AI_REVIEW_APPLIED'
                WHERE id = ?
            """, (change_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"标记失败: {e}")

    def auto_apply_after_review(self, review_result: Dict, config_path: str = "config.yaml") -> Dict:
        """复盘完成后自动应用最优参数（需通过蒙特卡洛压力测试）。"""
        if review_result.get('status') != 'success':
            return {"status": "skipped", "reason": "review_failed"}

        trades_analyzed = review_result.get('trades_analyzed', 0)
        if trades_analyzed < 5:
            return {"status": "skipped", "reason": "insufficient_trades"}

        mc_cfg = (self.config.get("counterfactual") or {}).get("monte_carlo") or {}
        if mc_cfg.get("enabled", True):
            try:
                from bnb_quant_tool.counterfactual_analyzer import CounterfactualAnalyzer
                from bnb_quant_tool.config_access import build_data_fetcher
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator

                trades = self.extract_trades_for_review(limit=int(mc_cfg.get("lookback_trades", 50)))
                cf = CounterfactualAnalyzer(fetcher=build_data_fetcher(self.config))
                ev = LearningEvolutionCoordinator(
                    learner=None,
                    counterfactual=cf,
                    config=self.config,
                )
                gate = ev.promotion_gate(trades, counterfactual=cf)
                if not gate.get("passed"):
                    logger.warning(
                        "晋升守门未通过 (WF/MC)，跳过 auto_apply: %s",
                        gate,
                    )
                    return {
                        "status": "blocked",
                        "reason": "promotion_gate_failed",
                        "gate": gate,
                    }
                stress = gate.get("monte_carlo") or {}
                if not stress.get("passed"):
                    return {
                        "status": "blocked",
                        "reason": "monte_carlo_failed",
                        "stress_test": stress,
                    }
                logger.info("晋升守门通过: %s", gate)
            except Exception as e:
                logger.warning("晋升守门异常，继续 auto_apply: %s", e)

        ev_cfg = (self.config.get("learning_evolution") or {})
        if ev_cfg.get("shadow_param_ab", True):
            try:
                from bnb_quant_tool.learning_evolution import LearningEvolutionCoordinator
                from bnb_quant_tool.ai_learning_system import AILearningSystem

                learner = AILearningSystem(
                    db_path=self.learning_db_path,
                    config=self.config,
                )
                ev = LearningEvolutionCoordinator(learner, config=self.config)
                changes = self.get_pending_param_changes()
                queued = []
                blocked = []
                for change in changes:
                    wgate = ev.learning_writeback_gate(
                        change["param"],
                        new_value=change.get("new_value"),
                        old_value=change.get("old_value"),
                    )
                    if not wgate.get("passed"):
                        blocked.append({
                            "param": change["param"],
                            "reasons": wgate.get("reasons"),
                            "stats": wgate.get("stats"),
                        })
                        continue
                    qid = ev.queue_shadow_param_change(
                        change["param"],
                        float(change.get("old_value") or 0),
                        float(change.get("new_value") or 0),
                        reason=change.get("reason") or "",
                        source="AI_REVIEW_SHADOW",
                    )
                    if qid:
                        queued.append({
                            "param": change["param"],
                            "shadow_value": change["new_value"],
                            "trial_id": qid,
                        })
                        self._mark_param_change_shadow_queued(change["id"])
                if blocked and not queued:
                    return {"status": "blocked", "reason": "writeback_gate", "blocked": blocked}
                if queued:
                    out = {"status": "shadow_queued", "queued": queued}
                    if blocked:
                        out["blocked"] = blocked
                    return out
            except Exception as e:
                logger.warning("shadow param queue failed, fallback direct apply: %s", e)

        return self.apply_all_param_changes(config_path)


if __name__ == "__main__":
    import yaml

    print("=" * 60)
    print("AI Review Engine Test")
    print("=" * 60)

    # 加载配置
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    engine = AIReviewEngine(
        config=config,
        deepseek_api_key=config["deepseek"]["api_key"],
        deepseek_model=config["deepseek"]["model"],
        deepseek_base_url=config["deepseek"]["base_url"]
    )

    result = engine.run_review()
    print(f"\nReview Result: {json.dumps(result, ensure_ascii=False, indent=2)}")

    pending = engine.get_pending_param_changes()
    print(f"\nPending Changes: {len(pending)}")
    for c in pending:
        print(f"  {c['param']}: {c['old_value']} → {c['new_value']} ({c['reason']})")
