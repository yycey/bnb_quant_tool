"""
BNB量化交易工具 - Strategy Lab（AI 自动发现新策略）
================================================================

设计目标：
- 让系统从 "13 张固定底牌" 进化到 "AI 自动发现新底牌"
- 不动态生成 Python 代码（避免安全风险），用 **规则 DSL** 描述策略
- 每个候选策略 → 独立回测 → 用统计指标筛选 → Top-K 持久化
- 通过率筛选过的策略可被 InstitutionalStrategies 动态注册，进入投票池
  + 享受现有的 AI 学习权重自适应（策略 → 胜率 → 权重）

DSL 形式（一条 rule = 一个加分/扣分条件）：
{
    "id": "auto_xxx",
    "name": "Auto Strategy #1",
    "rules": [
        {"feature": "RSI",          "op": "<",  "threshold": 30,   "action": +1},
        {"feature": "MACD_diff",    "op": ">",  "threshold": 0,    "action": +1},
        {"feature": "BB_position",  "op": "<",  "threshold": 0.2,  "action": +1},
        {"feature": "MA_ratio",     "op": ">",  "threshold": 1.0,  "action": -1},
    ],
    "buy_score":  2,
    "sell_score": -2,
    "metrics": { ... 回测结果摘要 ... }
}

特征池（来自 TechnicalIndicators.calculate_all_indicators）：
- RSI            : 0~100
- MACD_diff      : MACD - MACD_Signal
- MA_ratio       : close / MA_20
- MA_trend       : (MA_20 - MA_50) / MA_50
- BB_position    : (close - BB_Lower) / (BB_Upper - BB_Lower)
- Volume_ratio   : volume / volume_MA_20
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .backtest_engine import BacktestEngine, BacktestResult

logger = logging.getLogger(__name__)


# ============================================================
# 特征池：(name, sampler, op_choices)
# sampler 给出 threshold 的随机采样器
# ============================================================
FEATURE_SPECS: Dict[str, Dict] = {
    "RSI": {
        "sampler": lambda: random.choice(
            [random.uniform(20, 35), random.uniform(60, 80)]
        ),
        "ops": ["<", ">"],
    },
    "MACD_diff": {
        # MACD - Signal 在 BNB 上常落在 [-3, 3]
        "sampler": lambda: random.uniform(-1.5, 1.5),
        "ops": ["<", ">"],
    },
    "MA_ratio": {
        # close / MA_20，常落在 [0.94, 1.06]
        "sampler": lambda: random.uniform(0.96, 1.04),
        "ops": ["<", ">"],
    },
    "MA_trend": {
        # (MA20 - MA50) / MA50，常落在 [-0.05, 0.05]
        "sampler": lambda: random.uniform(-0.02, 0.02),
        "ops": ["<", ">"],
    },
    "BB_position": {
        # 0~1，靠下=接近下轨
        "sampler": lambda: random.uniform(0.05, 0.95),
        "ops": ["<", ">"],
    },
    "Volume_ratio": {
        "sampler": lambda: random.uniform(0.7, 1.8),
        "ops": ["<", ">"],
    },
    "ADX": {
        "sampler": lambda: random.uniform(15, 40),
        "ops": ["<", ">"],
    },
    "Stoch_K": {
        "sampler": lambda: random.choice(
            [random.uniform(10, 30), random.uniform(70, 90)]
        ),
        "ops": ["<", ">"],
    },
    "OBV_slope": {
        "sampler": lambda: random.uniform(-5000, 5000),
        "ops": ["<", ">"],
    },
    "SR_position": {
        # 0=支撑, 1=阻力
        "sampler": lambda: random.uniform(0.05, 0.95),
        "ops": ["<", ">"],
    },
}


# ============================================================
# 特征提取：从指标 dict + window_df 抽取标准化特征
# ============================================================
def extract_features(window_df: pd.DataFrame, indicators: Dict) -> Dict[str, float]:
    """把不同来源的指标统一成 DSL 引用的 feature 名字"""
    close = float(window_df["close"].iloc[-1])
    rsi = indicators.get("RSI")
    macd = indicators.get("MACD")
    macd_sig = indicators.get("MACD_Signal")
    ma20 = indicators.get("MA_20")
    ma50 = indicators.get("MA_50")
    bb_low = indicators.get("BB_Lower")
    bb_up = indicators.get("BB_Upper")

    feats: Dict[str, float] = {}
    if rsi is not None:
        feats["RSI"] = float(rsi)
    if macd is not None and macd_sig is not None:
        feats["MACD_diff"] = float(macd) - float(macd_sig)
    if ma20:
        feats["MA_ratio"] = close / float(ma20)
    if ma20 and ma50:
        feats["MA_trend"] = (float(ma20) - float(ma50)) / float(ma50)
    if bb_low is not None and bb_up is not None and (bb_up - bb_low) > 1e-9:
        feats["BB_position"] = (close - float(bb_low)) / (float(bb_up) - float(bb_low))
    # Volume_ratio：window_df 自算
    try:
        vol = float(window_df["volume"].iloc[-1])
        vol_ma = float(window_df["volume"].tail(20).mean())
        if vol_ma > 0:
            feats["Volume_ratio"] = vol / vol_ma
    except Exception:
        pass

    adx = indicators.get("ADX")
    if adx is not None:
        feats["ADX"] = float(adx)
    stoch_k = indicators.get("Stoch_K")
    if stoch_k is not None:
        feats["Stoch_K"] = float(stoch_k)
    obv_slope = indicators.get("OBV_Slope")
    if obv_slope is not None:
        feats["OBV_slope"] = float(obv_slope)
    support = indicators.get("Support")
    resistance = indicators.get("Resistance")
    if support is not None and resistance is not None:
        span = float(resistance) - float(support)
        if span > 1e-9:
            feats["SR_position"] = (close - float(support)) / span

    return feats


# ============================================================
# DSL → 评分函数（返回 score）
# ============================================================
def evaluate_rules(spec: Dict, feats: Dict[str, float]) -> int:
    score = 0
    for r in spec.get("rules", []):
        f = r["feature"]
        if f not in feats:
            continue
        v = feats[f]
        thr = r["threshold"]
        op = r["op"]
        action = int(r.get("action", 1))
        hit = (v < thr) if op == "<" else (v > thr)
        if hit:
            score += action
    return score


# ============================================================
# DSL → BacktestEngine 用的 signal_func
# ============================================================
def make_signal_func(spec: Dict) -> Callable:
    """返回符合 BacktestEngine.signal_func 协议的函数"""
    buy_thr = int(spec.get("buy_score", 2))
    sell_thr = int(spec.get("sell_score", -2))
    max_score = max(1, sum(abs(int(r.get("action", 1))) for r in spec.get("rules", [])))

    def fn(window_df: pd.DataFrame, indicators: Dict) -> Tuple[str, float]:
        feats = extract_features(window_df, indicators)
        score = evaluate_rules(spec, feats)
        if score >= buy_thr:
            return "LONG", min(1.0, abs(score) / max_score)
        if score <= sell_thr:
            return "SHORT", min(1.0, abs(score) / max_score)
        return "HOLD", 0.0

    return fn


# ============================================================
# DSL → InstitutionalStrategies 用的 strategy_func
# 协议：(df, params=None) -> {'strategy','signal','confidence',...}
# ============================================================
def make_institutional_func(spec: Dict, ti_module=None) -> Callable:
    """生成可挂到 InstitutionalStrategies.strategies 的最终一根 K 线信号函数"""
    name = spec.get("name", spec.get("id", "auto_strategy"))
    buy_thr = int(spec.get("buy_score", 2))
    sell_thr = int(spec.get("sell_score", -2))
    max_score = max(1, sum(abs(int(r.get("action", 1))) for r in spec.get("rules", [])))

    def fn(df: pd.DataFrame, params: Dict = None) -> Dict:
        # 延迟 import，避免顶层循环依赖
        from .technical_indicators import TechnicalIndicators
        try:
            indicators = TechnicalIndicators.calculate_all_indicators(df.tail(200))
        except Exception as e:
            return {
                "strategy": name,
                "signal": "HOLD",
                "confidence": 0.5,
                "error": f"indicator_failed:{e}",
                "description": f"自动发现策略 {name}",
            }
        feats = extract_features(df, indicators)
        score = evaluate_rules(spec, feats)
        if score >= buy_thr:
            sig, conf = "BUY", min(0.9, 0.55 + abs(score) / max_score * 0.35)
        elif score <= sell_thr:
            sig, conf = "SELL", min(0.9, 0.55 + abs(score) / max_score * 0.35)
        else:
            sig, conf = "HOLD", 0.5
        return {
            "strategy": name,
            "signal": sig,
            "confidence": float(conf),
            "score": int(score),
            "rules_n": len(spec.get("rules", [])),
            "description": f"AI 自动发现策略 {spec.get('id','')} score={score}",
        }

    return fn


# ============================================================
# 候选生成器
# ============================================================
def generate_candidate(idx: int) -> Dict:
    """随机生成一个候选策略：2~4 条 rule"""
    n_rules = random.randint(2, 4)
    feature_pool = list(FEATURE_SPECS.keys())
    chosen = random.sample(feature_pool, k=min(n_rules, len(feature_pool)))
    rules: List[Dict] = []
    for f in chosen:
        spec = FEATURE_SPECS[f]
        op = random.choice(spec["ops"])
        thr = round(spec["sampler"](), 4)
        # action 与 op 弱关联：+1 多用于看多侧，-1 多用于看空侧（保留少量噪声）
        if f == "RSI":
            action = +1 if (op == "<" and thr < 50) else -1
        elif f == "BB_position":
            action = +1 if (op == "<" and thr < 0.5) else -1
        elif f in ("MACD_diff", "MA_trend", "MA_ratio"):
            action = +1 if op == ">" else -1
        else:
            action = random.choice([+1, -1])
        rules.append(
            {"feature": f, "op": op, "threshold": thr, "action": int(action)}
        )
    pos_max = sum(r["action"] for r in rules if r["action"] > 0)
    neg_max = sum(r["action"] for r in rules if r["action"] < 0)
    buy_score = max(1, int(round(pos_max * 0.7))) if pos_max >= 1 else 1
    sell_score = min(-1, int(round(neg_max * 0.7))) if neg_max <= -1 else -1
    return {
        "id": f"auto_{idx:04d}",
        "name": f"Auto Strategy #{idx}",
        "rules": rules,
        "buy_score": buy_score,
        "sell_score": sell_score,
    }


# ============================================================
# StrategyLab 主类
# ============================================================
DEFAULT_DISCOVERED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "discovered_strategies.json",
)


def _artifact_store(db_path: Optional[str] = None):
    from bnb_quant_tool.db_artifact_store import get_artifact_store
    return get_artifact_store(db_path)


@dataclass
class DiscoveryConfig:
    n_candidates: int = 120
    top_k: int = 5
    # 筛选门槛
    min_trades: int = 15
    min_win_rate: float = 0.45
    min_profit_factor: float = 1.2
    min_sharpe: float = 0.0
    max_drawdown_pct: float = -25.0   # 不能比这更差
    initial_balance: float = 10000.0
    timeframe_hours: float = 1.0
    seed: Optional[int] = 42
    walk_forward_oos: bool = True
    oos_train_ratio: float = 0.70
    oos_min_test_trades: int = 5
    oos_min_test_wr_ratio: float = 0.70


class StrategyLab:
    """AI 自动发现新策略：随机搜索 + 回测筛选 + 持久化"""

    def __init__(self, config: Optional[DiscoveryConfig] = None,
                 storage_path: str = DEFAULT_DISCOVERED_PATH,
                 db_path: Optional[str] = None):
        self.config = config or DiscoveryConfig()
        self.storage_path = storage_path
        self.db_path = db_path
        if self.config.seed is not None:
            random.seed(self.config.seed)
            np.random.seed(self.config.seed)

    # --------------------------------------------------------
    # 主流程
    # --------------------------------------------------------
    def discover(self, df: pd.DataFrame) -> List[Dict]:
        """对 df 历史数据进行候选生成 + 回测筛选，返回 top-K"""
        cfg = self.config
        if df is None or len(df) < 200:
            raise ValueError("StrategyLab.discover 需要至少 200 根 K 线数据")

        candidates = [generate_candidate(i) for i in range(cfg.n_candidates)]
        logger.info(f"StrategyLab: 生成 {len(candidates)} 个候选策略，开始回测...")

        survivors: List[Dict] = []
        for i, spec in enumerate(candidates):
            try:
                res = self._backtest_one(df, spec)
            except Exception as e:
                logger.debug(f"候选 {spec['id']} 回测失败: {e}")
                continue
            if not self._passes_filter(res):
                continue
            spec["metrics"] = self._summarize_metrics(res)
            if cfg.walk_forward_oos:
                wf = self.walk_forward_validate(df, spec)
                spec["walk_forward"] = wf
                if not wf.get("passed"):
                    continue
            survivors.append(spec)

        # 按 sharpe 降序，profit_factor 次序
        survivors.sort(
            key=lambda s: (
                s["metrics"].get("sharpe_ratio", 0.0),
                s["metrics"].get("profit_factor", 0.0),
                s["metrics"].get("total_return_pct", 0.0),
            ),
            reverse=True,
        )
        top = survivors[: cfg.top_k]
        logger.info(
            f"StrategyLab: 候选 {len(candidates)} → 通过筛选 {len(survivors)} → 取 top-{len(top)}"
        )
        return top

    def discover_and_save(self, df: pd.DataFrame,
                          merge_with_existing: bool = True) -> List[Dict]:
        """发现并写入 ai_learning.db（discovered_strategies 表）。"""
        new_top = self.discover(df)
        merged = new_top
        if merge_with_existing:
            old = self.load_discovered(db_path=self.db_path)
            # 用 id 去重，保留指标更好的
            by_id: Dict[str, Dict] = {s["id"]: s for s in old}
            for s in new_top:
                old_s = by_id.get(s["id"])
                if (old_s is None or
                        s.get("metrics", {}).get("sharpe_ratio", -1)
                        > old_s.get("metrics", {}).get("sharpe_ratio", -1)):
                    by_id[s["id"]] = s
            # 取指标最好的前 top_k * 2 上限保留
            merged = sorted(
                by_id.values(),
                key=lambda s: s.get("metrics", {}).get("sharpe_ratio", 0.0),
                reverse=True,
            )[: max(self.config.top_k * 2, self.config.top_k)]
        self.save_discovered(merged, db_path=self.db_path)
        return merged

    # --------------------------------------------------------
    # 回测包装
    # --------------------------------------------------------
    def _backtest_one(self, df: pd.DataFrame, spec: Dict) -> BacktestResult:
        engine = BacktestEngine(
            initial_balance=self.config.initial_balance,
            timeframe_hours=self.config.timeframe_hours,
            signal_func=make_signal_func(spec),
        )
        return engine.run(df)

    def walk_forward_validate(self, df: pd.DataFrame, spec: Dict) -> Dict:
        """样本内训练 + 样本外测试 — 策略晋升前 OOS 守门。"""
        cfg = self.config
        ratio = float(cfg.oos_train_ratio)
        split = int(len(df) * ratio)
        min_train = 120
        min_test = 40
        if split < min_train or len(df) - split < min_test:
            return {
                "passed": False,
                "reason": f"数据不足 (train={split}, test={len(df) - split})",
            }

        train_df = df.iloc[:split].copy()
        test_df = df.iloc[split:].copy()
        try:
            train_res = self._backtest_one(train_df, spec)
            test_res = self._backtest_one(test_df, spec)
        except Exception as e:
            return {"passed": False, "reason": f"回测失败: {e}"}

        min_test_trades = max(int(cfg.oos_min_test_trades), cfg.min_trades // 4)
        wr_ratio = float(cfg.oos_min_test_wr_ratio)
        test_wr_ok = test_res.win_rate >= max(
            0.38, train_res.win_rate * wr_ratio - 0.05
        )
        pf_ok = (
            test_res.profit_factor == float("inf")
            or test_res.profit_factor >= 1.0
        )
        ret_ok = test_res.total_return_pct > -8.0
        trades_ok = test_res.total_trades >= min_test_trades

        passed = bool(trades_ok and test_wr_ok and pf_ok and ret_ok)
        reason = "OOS 通过" if passed else (
            "OOS 样本外未达标: "
            + ", ".join(
                x for x, ok in [
                    (f"交易数 {test_res.total_trades}<{min_test_trades}", not trades_ok),
                    (f"胜率 {test_res.win_rate:.2f}<{train_res.win_rate * wr_ratio:.2f}", not test_wr_ok),
                    ("PF<1", not pf_ok),
                    (f"收益 {test_res.total_return_pct:.1f}%", not ret_ok),
                ] if ok
            )
        )
        return {
            "passed": passed,
            "reason": reason,
            "train_wr": round(train_res.win_rate, 4),
            "test_wr": round(test_res.win_rate, 4),
            "train_trades": train_res.total_trades,
            "test_trades": test_res.total_trades,
            "test_return_pct": round(test_res.total_return_pct, 3),
            "test_profit_factor": (
                round(test_res.profit_factor, 3)
                if test_res.profit_factor != float("inf")
                else 99.99
            ),
            "split_ratio": ratio,
        }

    def _passes_filter(self, r: BacktestResult) -> bool:
        cfg = self.config
        if r.total_trades < cfg.min_trades:
            return False
        if r.win_rate < cfg.min_win_rate:
            return False
        if r.profit_factor != float("inf") and r.profit_factor < cfg.min_profit_factor:
            return False
        if r.sharpe_ratio < cfg.min_sharpe:
            return False
        if r.max_drawdown_pct < cfg.max_drawdown_pct:
            return False
        if r.total_return_pct <= 0:
            return False
        return True

    @staticmethod
    def _summarize_metrics(r: BacktestResult) -> Dict:
        return {
            "total_return_pct": r.total_return_pct,
            "annual_return_pct": r.annual_return_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "win_rate": r.win_rate,
            "profit_factor": (
                r.profit_factor if r.profit_factor != float("inf") else 99.99
            ),
            "total_trades": r.total_trades,
            "avg_hold_hours": r.avg_hold_hours,
        }

    # --------------------------------------------------------
    # 持久化
    # --------------------------------------------------------
    @staticmethod
    def save_discovered(
        strategies: List[Dict],
        path: str = DEFAULT_DISCOVERED_PATH,
        db_path: Optional[str] = None,
    ):
        store = _artifact_store(db_path)
        store.save_strategies(strategies)
        logger.info(
            f"StrategyLab: 已保存 {len(strategies)} 个策略到 ai_learning.db"
        )

    @staticmethod
    def load_discovered(
        path: str = DEFAULT_DISCOVERED_PATH,
        db_path: Optional[str] = None,
    ) -> List[Dict]:
        store = _artifact_store(db_path)
        strategies = store.load_strategies()
        if strategies:
            return strategies

        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            legacy = list(data.get("strategies", []))
            if legacy:
                store.save_strategies(legacy)
                logger.info(
                    f"StrategyLab: 已从 {path} 迁移 {len(legacy)} 个策略到 ai_learning.db"
                )
            return legacy
        except Exception as e:
            logger.warning(f"StrategyLab: 加载 {path} 失败: {e}")
            return []

    # --------------------------------------------------------
    # 报告
    # --------------------------------------------------------
    @staticmethod
    def format_report(strategies: List[Dict], title: str = "Strategy Lab 发现报告") -> str:
        sep = "=" * 72
        lines = [sep, f"  {title}", sep]
        if not strategies:
            lines.append("  ⚠ 无候选通过筛选 — 试试放宽阈值或增加候选数量")
            lines.append(sep)
            return "\n".join(lines)
        for i, s in enumerate(strategies, 1):
            m = s.get("metrics", {})
            lines.append(
                f"[{i}] {s['name']} ({s['id']})  "
                f"return={m.get('total_return_pct',0):+.2f}%  "
                f"sharpe={m.get('sharpe_ratio',0):.2f}  "
                f"win={m.get('win_rate',0):.1%}  "
                f"PF={m.get('profit_factor',0):.2f}  "
                f"DD={m.get('max_drawdown_pct',0):+.2f}%  "
                f"trades={m.get('total_trades',0)}"
            )
            lines.append(
                f"     buy_score>={s['buy_score']}  sell_score<={s['sell_score']}  "
                f"rules={len(s['rules'])}"
            )
            for r in s["rules"]:
                lines.append(
                    f"       · {r['feature']} {r['op']} {r['threshold']}  → {r['action']:+d}"
                )
        lines.append(sep)
        return "\n".join(lines)


__all__ = [
    "StrategyLab",
    "DiscoveryConfig",
    "DEFAULT_DISCOVERED_PATH",
    "make_signal_func",
    "make_institutional_func",
    "extract_features",
    "evaluate_rules",
    "generate_candidate",
]
