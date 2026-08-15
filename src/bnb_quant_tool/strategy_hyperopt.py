"""
StrategyLabHyperopt — 类 Freqtrade Hyperopt 的 DSL 参数搜索。

优先使用 Optuna（若已安装）；否则回退到随机搜索 + 局部爬山。
不动态生成 Python 代码，只搜索 FEATURE_SPECS 空间内的规则阈值 / buy_sell_score。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bnb_quant_tool.strategy_lab import (
    FEATURE_SPECS,
    DiscoveryConfig,
    StrategyLab,
    generate_candidate,
)

logger = logging.getLogger(__name__)


@dataclass
class HyperoptConfig:
    n_trials: int = 80
    top_k: int = 5
    seed: Optional[int] = 42
    use_optuna: bool = True
    local_refine_steps: int = 12
    # 与 DiscoveryConfig 对齐的筛选
    min_trades: int = 15
    min_win_rate: float = 0.45
    min_profit_factor: float = 1.2
    min_sharpe: float = 0.0
    max_drawdown_pct: float = -25.0
    walk_forward_oos: bool = True
    oos_train_ratio: float = 0.70


class StrategyLabHyperopt:
    """在 StrategyLab DSL 上做超参搜索。"""

    def __init__(
        self,
        config: Optional[HyperoptConfig] = None,
        lab: Optional[StrategyLab] = None,
        storage_path: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        self.hcfg = config or HyperoptConfig()
        disc = DiscoveryConfig(
            n_candidates=self.hcfg.n_trials,
            top_k=self.hcfg.top_k,
            min_trades=self.hcfg.min_trades,
            min_win_rate=self.hcfg.min_win_rate,
            min_profit_factor=self.hcfg.min_profit_factor,
            min_sharpe=self.hcfg.min_sharpe,
            max_drawdown_pct=self.hcfg.max_drawdown_pct,
            seed=self.hcfg.seed,
            walk_forward_oos=self.hcfg.walk_forward_oos,
            oos_train_ratio=self.hcfg.oos_train_ratio,
        )
        if lab is not None:
            self.lab = lab
        else:
            from bnb_quant_tool.strategy_lab import DEFAULT_DISCOVERED_PATH

            self.lab = StrategyLab(
                config=disc,
                storage_path=storage_path or DEFAULT_DISCOVERED_PATH,
                db_path=db_path,
            )
        if self.hcfg.seed is not None:
            random.seed(self.hcfg.seed)
            np.random.seed(self.hcfg.seed)

    def run(self, df: pd.DataFrame) -> List[Dict]:
        """执行超参搜索，返回 top-K 策略规格（含 metrics / walk_forward）。"""
        if df is None or len(df) < 200:
            raise ValueError("Hyperopt 需要至少 200 根 K 线")

        use_optuna = self.hcfg.use_optuna
        optuna = None
        if use_optuna:
            try:
                import optuna  # type: ignore

                optuna.logging.set_verbosity(optuna.logging.WARNING)
            except ImportError:
                logger.info("Optuna 未安装，回退随机+爬山搜索（pip install optuna 可启用）")
                optuna = None

        if optuna is not None:
            survivors = self._run_optuna(df, optuna)
        else:
            survivors = self._run_random_hillclimb(df)

        survivors.sort(
            key=lambda s: (
                s.get("metrics", {}).get("sharpe_ratio", 0.0),
                s.get("metrics", {}).get("profit_factor", 0.0),
                s.get("metrics", {}).get("total_return_pct", 0.0),
            ),
            reverse=True,
        )
        top = survivors[: self.hcfg.top_k]
        logger.info(
            "StrategyLabHyperopt: trials→%d survivors→%d top-%d",
            self.hcfg.n_trials,
            len(survivors),
            len(top),
        )
        return top

    def run_and_save(self, df: pd.DataFrame, merge_with_existing: bool = True) -> List[Dict]:
        new_top = self.run(df)
        for i, s in enumerate(new_top):
            s["id"] = s.get("id") or f"hyper_{i:04d}"
            s.setdefault("name", f"Hyperopt Strategy #{i + 1}")
            s["source"] = "hyperopt"
        if merge_with_existing:
            old = self.lab.load_discovered(db_path=self.lab.db_path)
            by_id = {s["id"]: s for s in old}
            for s in new_top:
                oid = by_id.get(s["id"])
                if (
                    oid is None
                    or s.get("metrics", {}).get("sharpe_ratio", -1)
                    > oid.get("metrics", {}).get("sharpe_ratio", -1)
                ):
                    by_id[s["id"]] = s
            merged = sorted(
                by_id.values(),
                key=lambda x: x.get("metrics", {}).get("sharpe_ratio", 0.0),
                reverse=True,
            )[: max(self.hcfg.top_k * 2, self.hcfg.top_k)]
            self.lab.save_discovered(merged, db_path=self.lab.db_path)
            return merged
        self.lab.save_discovered(new_top, db_path=self.lab.db_path)
        return new_top

    # ── Optuna path ────────────────────────────────────────────

    def _run_optuna(self, df: pd.DataFrame, optuna: Any) -> List[Dict]:
        survivors: List[Dict] = []

        def objective(trial: Any) -> float:
            spec = self._suggest_spec(trial)
            scored = self._score_spec(df, spec)
            if scored is None:
                return -1e9
            metric = float(scored["metrics"].get("sharpe_ratio") or 0.0)
            # 附加 PF / return 轻微奖励，避免纯夏普尖峰
            metric += 0.05 * float(scored["metrics"].get("profit_factor") or 0.0)
            metric += 0.01 * float(scored["metrics"].get("total_return_pct") or 0.0)
            survivors.append(scored)
            return metric

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=self.hcfg.seed),
        )
        study.optimize(objective, n_trials=self.hcfg.n_trials, show_progress_bar=False)
        # 去重 id，保留最优
        return self._dedupe_best(survivors)

    def _suggest_spec(self, trial: Any) -> Dict[str, Any]:
        n_rules = trial.suggest_int("n_rules", 2, 4)
        features = list(FEATURE_SPECS.keys())
        # 固定顺序采样子集，保证 trial 可复现
        chosen = []
        pool = features[:]
        for i in range(n_rules):
            idx = trial.suggest_int(f"feat_idx_{i}", 0, len(pool) - 1)
            f = pool.pop(idx % len(pool))
            chosen.append(f)
            if not pool:
                break
        rules = []
        for f in chosen:
            fs = FEATURE_SPECS[f]
            op = trial.suggest_categorical(f"op_{f}", list(fs["ops"]))
            # 用特征典型范围做连续采样
            lo, hi = _feature_bounds(f)
            thr = trial.suggest_float(f"thr_{f}", lo, hi)
            action = trial.suggest_categorical(f"act_{f}", [-1, 1])
            # RSI/BB 语义约束
            if f == "RSI" and op == "<" and thr < 50:
                action = 1
            elif f == "RSI" and op == ">" and thr > 50:
                action = -1
            rules.append(
                {"feature": f, "op": op, "threshold": round(thr, 4), "action": int(action)}
            )
        pos_max = sum(r["action"] for r in rules if r["action"] > 0)
        neg_max = sum(r["action"] for r in rules if r["action"] < 0)
        buy_score = trial.suggest_int(
            "buy_score", 1, max(1, pos_max) if pos_max >= 1 else 1
        )
        sell_score = -trial.suggest_int(
            "sell_abs", 1, max(1, abs(neg_max)) if neg_max <= -1 else 1
        )
        return {
            "id": f"hyper_{trial.number:04d}",
            "name": f"Hyperopt Strategy #{trial.number}",
            "rules": rules,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "source": "hyperopt",
        }

    # ── random + hill-climb path ───────────────────────────────

    def _run_random_hillclimb(self, df: pd.DataFrame) -> List[Dict]:
        survivors: List[Dict] = []
        n = self.hcfg.n_trials
        for i in range(n):
            spec = generate_candidate(i)
            spec["id"] = f"hyper_{i:04d}"
            spec["name"] = f"Hyperopt Strategy #{i}"
            spec["source"] = "hyperopt"
            scored = self._score_spec(df, spec)
            if scored is None:
                continue
            refined = self._local_refine(df, scored)
            survivors.append(refined if refined else scored)
        return self._dedupe_best(survivors)

    def _local_refine(self, df: pd.DataFrame, spec: Dict) -> Optional[Dict]:
        best = spec
        best_sharpe = float(spec.get("metrics", {}).get("sharpe_ratio") or -1e9)
        cur = dict(spec)
        cur["rules"] = [dict(r) for r in spec.get("rules") or []]
        for _ in range(self.hcfg.local_refine_steps):
            trial = dict(cur)
            trial["rules"] = [dict(r) for r in cur["rules"]]
            if not trial["rules"]:
                break
            r = random.choice(trial["rules"])
            # 微调阈值 ±5%
            thr = float(r["threshold"])
            r["threshold"] = round(thr * (1.0 + random.uniform(-0.05, 0.05)), 4)
            if random.random() < 0.2:
                trial["buy_score"] = max(1, int(trial.get("buy_score", 2)) + random.choice([-1, 0, 1]))
            scored = self._score_spec(df, trial)
            if scored is None:
                continue
            sh = float(scored["metrics"].get("sharpe_ratio") or -1e9)
            if sh > best_sharpe:
                best = scored
                best_sharpe = sh
                cur = scored
        return best

    def _score_spec(self, df: pd.DataFrame, spec: Dict) -> Optional[Dict]:
        try:
            res = self.lab._backtest_one(df, spec)
        except Exception as e:
            logger.debug("hyperopt backtest fail %s: %s", spec.get("id"), e)
            return None
        if not self.lab._passes_filter(res):
            return None
        out = dict(spec)
        out["metrics"] = self.lab._summarize_metrics(res)
        if self.hcfg.walk_forward_oos:
            wf = self.lab.walk_forward_validate(df, out)
            out["walk_forward"] = wf
            if not wf.get("passed"):
                return None
        return out

    @staticmethod
    def _dedupe_best(items: List[Dict]) -> List[Dict]:
        by_id: Dict[str, Dict] = {}
        for s in items:
            sid = str(s.get("id") or id(s))
            old = by_id.get(sid)
            if old is None or (
                s.get("metrics", {}).get("sharpe_ratio", -1)
                > old.get("metrics", {}).get("sharpe_ratio", -1)
            ):
                by_id[sid] = s
        # 也按规则签名去重
        by_sig: Dict[str, Dict] = {}
        for s in by_id.values():
            sig = _rules_signature(s)
            old = by_sig.get(sig)
            if old is None or (
                s.get("metrics", {}).get("sharpe_ratio", -1)
                > old.get("metrics", {}).get("sharpe_ratio", -1)
            ):
                by_sig[sig] = s
        return list(by_sig.values())


def _feature_bounds(feature: str) -> Tuple[float, float]:
    defaults = {
        "RSI": (15.0, 85.0),
        "MACD_diff": (-2.0, 2.0),
        "MA_ratio": (0.95, 1.05),
        "MA_trend": (-0.03, 0.03),
        "BB_position": (0.02, 0.98),
        "Volume_ratio": (0.5, 2.2),
        "ADX": (12.0, 45.0),
        "Stoch_K": (5.0, 95.0),
        "OBV_slope": (-8000.0, 8000.0),
        "SR_position": (0.02, 0.98),
    }
    return defaults.get(feature, (-1.0, 1.0))


def _rules_signature(spec: Dict) -> str:
    parts = []
    for r in sorted(spec.get("rules") or [], key=lambda x: x.get("feature", "")):
        parts.append(
            f"{r.get('feature')}:{r.get('op')}:{round(float(r.get('threshold', 0)), 3)}:{r.get('action')}"
        )
    return "|".join(parts) + f"|b{spec.get('buy_score')}|s{spec.get('sell_score')}"
