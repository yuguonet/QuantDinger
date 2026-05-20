"""
参数优化引擎
内置两种搜索策略：
  1. Random Search（无额外依赖）
  2. Bayesian Optimization（需要 optuna，可选）

早停机制：
  - patience: 连续 N 轮无提升则停止（默认 25）
  - min_trials: 最少跑多少轮再判断早停（默认 20）
  - bad_threshold: 前 K 轮最高分仍低于此值则提前终止（默认 -1.0）
"""
import json
import math
import random
import time
import traceback
from typing import Dict, Any, List, Optional, Callable

import numpy as np

from optimizer.param_space import STRATEGY_TEMPLATES, get_template
from optimizer.scoring import compute_score


class TrialResult:
    """单次试验结果"""
    def __init__(self, params: dict, score: float, metrics: dict, trial_id: int):
        self.params = params
        self.score = score
        self.metrics = metrics
        self.trial_id = trial_id

    def to_dict(self) -> dict:
        return {
            "trial_id": self.trial_id,
            "params": self.params,
            "score": round(self.score, 4),
            "metrics": self.metrics,
        }


class StrategyOptimizer:
    """
    策略参数优化器（带早停）

    用法:
        optimizer = StrategyOptimizer(
            template_key="dual_ma_volume",
            objective_fn=my_backtest_fn,
            n_trials=100,
        )
        best = optimizer.run()
    """

    def __init__(
        self,
        template_key: str,
        objective_fn: Callable[[dict], dict],
        n_trials: int = 100,
        mode: str = "auto",       # "random" | "optuna" | "auto"
        score_fn: str = "sharpe",  # "sharpe" | "return_dd_ratio" | "composite"
        seed: int = 42,
        patience: int = 25,        # 早停：连续 N 轮无提升
        min_trials: int = 20,      # 早停：最少跑多少轮
        bad_threshold: float = -1.0,  # 早停：前 min_trials 轮最高分低于此值则终止
    ):
        self.template = get_template(template_key)
        self.template_key = template_key
        self.objective_fn = objective_fn
        self.n_trials = n_trials
        self.mode = mode
        self.score_fn = score_fn
        self.seed = seed
        self.patience = patience
        self.min_trials = min_trials
        self.bad_threshold = bad_threshold

        self.results: List[TrialResult] = []
        self.best_result: Optional[TrialResult] = None
        self._early_stopped = False
        self._early_stop_reason = ""

    # ============================================================
    # 主入口
    # ============================================================

    def run(self) -> Optional[TrialResult]:
        random.seed(self.seed)
        np.random.seed(self.seed)

        if self.mode == "auto":
            try:
                import optuna
                self.mode = "optuna"
            except ImportError:
                self.mode = "random"

        print(f"\n{'='*60}")
        print(f"  策略优化器启动")
        print(f"  模板: {self.template['name']}")
        print(f"  搜索方式: {self.mode}")
        print(f"  试验次数: {self.n_trials}")
        print(f"  早停: patience={self.patience}, min_trials={self.min_trials}")
        print(f"{'='*60}\n")

        if self.mode == "optuna":
            return self._run_optuna()
        return self._run_random()

    # ============================================================
    # 早停检查
    # ============================================================

    def _check_early_stop(self, trial_idx: int) -> bool:
        """检查是否应该早停"""
        # 1. 最少试验次数
        if trial_idx < self.min_trials:
            return False

        # 2. 前 min_trials 轮全低于阈值 → 策略本身不行
        if len(self.results) >= self.min_trials and self.best_result is not None:
            if self.best_result.score < self.bad_threshold:
                self._early_stopped = True
                self._early_stop_reason = (
                    f"前 {self.min_trials} 轮最高分 {self.best_result.score:.4f} "
                    f"< 阈值 {self.bad_threshold}，策略不可行"
                )
                return True

        # 3. 连续 patience 轮无提升
        if self.best_result is not None:
            best_trial = self.best_result.trial_id
            trials_since_best = trial_idx - best_trial
            if trials_since_best >= self.patience:
                self._early_stopped = True
                self._early_stop_reason = (
                    f"连续 {trials_since_best} 轮无提升 "
                    f"(best={self.best_result.score:.4f} @ trial {best_trial})"
                )
                return True

        return False

    # ============================================================
    # Random Search + 早停
    # ============================================================

    def _run_random(self) -> Optional[TrialResult]:
        param_space = self.template["params"]
        constraints = self.template.get("constraints", [])

        for i in range(self.n_trials):
            # 采样参数
            params = self._sample_params(param_space, constraints)
            if params is None:
                continue

            # 评估
            try:
                metrics = self.objective_fn(params)
                score = self._compute_score(metrics)
            except Exception as e:
                print(f"  Trial {i+1}/{self.n_trials} FAILED: {e}")
                continue

            result = TrialResult(params, score, metrics, i + 1)
            self.results.append(result)

            if self.best_result is None or score > self.best_result.score:
                self.best_result = result
                print(f"  ★ Trial {i+1}/{self.n_trials} | NEW BEST | score={score:.4f} | "
                      f"sharpe={metrics.get('sharpeRatio', 0):.2f} | "
                      f"winRate={metrics.get('winRate', 0):.1f}% | "
                      f"maxDD={metrics.get('maxDrawdown', 0):.1f}%")
            elif (i + 1) % 10 == 0:
                print(f"  · Trial {i+1}/{self.n_trials} | score={score:.4f} | "
                      f"best={self.best_result.score:.4f}")

            # 早停检查
            if self._check_early_stop(i + 1):
                print(f"\n  ⏹ 早停 @ Trial {i+1}: {self._early_stop_reason}")
                break

        self._print_summary()
        return self.best_result

    def _sample_params(self, param_space: dict, constraints: list, max_retries: int = 50) -> Optional[dict]:
        """从参数空间采样一组参数，满足约束条件"""
        for _ in range(max_retries):
            params = {}
            for name, spec in param_space.items():
                if spec["type"] == "int":
                    params[name] = random.randint(spec["low"], spec["high"])
                elif spec["type"] == "float":
                    raw = random.uniform(spec["low"], spec["high"])
                    step = spec.get("step", 0.001)
                    params[name] = round(round(raw / step) * step, 6)
                elif spec["type"] == "choice":
                    params[name] = random.choice(spec["choices"])

            if self._check_constraints(params, constraints):
                return params

        print("  WARNING: 无法在约束条件下采样，跳过")
        return None

    def _check_constraints(self, params: dict, constraints: list) -> bool:
        for left, op, right in constraints:
            l_val = params.get(left)
            r_val = params.get(right)
            if l_val is None or r_val is None:
                continue
            if op == "<" and not (l_val < r_val):
                return False
            if op == "<=" and not (l_val <= r_val):
                return False
            if op == ">" and not (l_val > r_val):
                return False
        return True

    # ============================================================
    # Optuna + 早停
    # ============================================================

    def _run_optuna(self) -> Optional[TrialResult]:
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)
        except ImportError:
            print("  optuna 不可用，回退到 random search")
            return self._run_random()

        param_space = self.template["params"]
        constraints = self.template.get("constraints", [])

        def optuna_objective(trial):
            params = {}
            for name, spec in param_space.items():
                if spec["type"] == "int":
                    params[name] = trial.suggest_int(name, spec["low"], spec["high"], step=spec.get("step", 1))
                elif spec["type"] == "float":
                    params[name] = trial.suggest_float(name, spec["low"], spec["high"], step=spec.get("step"))
                elif spec["type"] == "choice":
                    params[name] = trial.suggest_categorical(name, spec["choices"])

            if not self._check_constraints(params, constraints):
                raise optuna.exceptions.TrialPruned()

            try:
                metrics = self.objective_fn(params)
                score = self._compute_score(metrics)
            except Exception:
                raise optuna.exceptions.TrialPruned()

            result = TrialResult(params, score, metrics, len(self.results) + 1)
            self.results.append(result)
            if self.best_result is None or score > self.best_result.score:
                self.best_result = result
            return score

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=self.seed),
        )

        # Optuna 内置早停：连续 N 轮无提升
        early_stop_callback = None
        if self.patience > 0:
            class EarlyStopCallback:
                def __init__(self, patience, min_trials, threshold):
                    self.patience = patience
                    self.min_trials = min_trials
                    self.threshold = threshold
                def __call__(self, study, trial):
                    n = len(study.trials)
                    if n < self.min_trials:
                        return
                    if study.best_trial.number < n - self.patience:
                        study.stop()
                    if study.best_value < self.threshold and n >= self.min_trials:
                        study.stop()
            early_stop_callback = EarlyStopCallback(self.patience, self.min_trials, self.bad_threshold)

        study.optimize(
            optuna_objective,
            n_trials=self.n_trials,
            show_progress_bar=True,
            callbacks=[early_stop_callback] if early_stop_callback else None,
        )

        if len(study.trials) < self.n_trials:
            self._early_stopped = True
            self._early_stop_reason = f"Optuna 早停 @ {len(study.trials)} trials"
            print(f"\n  ⏹ 早停 @ {len(study.trials)} trials")

        self._print_summary()
        return self.best_result

    # ============================================================
    # 评分函数
    # ============================================================

    def _compute_score(self, metrics: dict) -> float:
        """调用统一评分模块"""
        return compute_score(metrics, self.score_fn)

    # ============================================================
    # 输出
    # ============================================================

    def _print_summary(self):
        if not self.results:
            print("\n  ❌ 没有有效的试验结果")
            return

        actual = len(self.results)
        planned = self.n_trials
        stopped = " ⏹ 早停" if self._early_stopped else ""

        print(f"\n{'='*60}")
        print(f"  优化完成 | 有效试验: {actual}/{planned}{stopped}")
        if self._early_stopped:
            print(f"  原因: {self._early_stop_reason}")
        print(f"{'='*60}")

        if self.best_result:
            m = self.best_result.metrics
            print(f"\n  ★ 最优结果:")
            print(f"    Score:        {self.best_result.score:.4f}")
            print(f"    Sharpe:       {m.get('sharpeRatio', 0):.2f}")
            print(f"    Total Return: {m.get('totalReturn', 0):.2f}%")
            print(f"    Win Rate:     {m.get('winRate', 0):.1f}%")
            print(f"    Max Drawdown: {m.get('maxDrawdown', 0):.1f}%")
            print(f"    Profit Factor:{m.get('profitFactor', 0):.2f}")
            print(f"    Total Trades: {m.get('totalTrades', 0)}")
            print(f"\n  最优参数:")
            for k, v in self.best_result.params.items():
                print(f"    {k}: {v}")

        # Top 5
        sorted_results = sorted(self.results, key=lambda r: r.score, reverse=True)
        print(f"\n  Top 5 策略:")
        print(f"  {'Rank':<5} {'Score':<10} {'Sharpe':<10} {'WinRate':<10} {'MaxDD':<10} {'Trades':<8}")
        print(f"  {'-'*53}")
        for rank, r in enumerate(sorted_results[:5], 1):
            m = r.metrics
            print(f"  {rank:<5} {r.score:<10.4f} {m.get('sharpeRatio',0):<10.2f} "
                  f"{m.get('winRate',0):<10.1f} {m.get('maxDrawdown',0):<10.1f} "
                  f"{m.get('totalTrades',0):<8}")

    def get_top_n(self, n: int = 10) -> List[TrialResult]:
        return sorted(self.results, key=lambda r: r.score, reverse=True)[:n]

    def export_results(self, path: str):
        data = {
            "template": self.template_key,
            "n_trials": self.n_trials,
            "mode": self.mode,
            "score_fn": self.score_fn,
            "total_valid": len(self.results),
            "early_stopped": self._early_stopped,
            "early_stop_reason": self._early_stop_reason,
            "best": self.best_result.to_dict() if self.best_result else None,
            "top_10": [r.to_dict() for r in self.get_top_n(10)],
            "all_results": [r.to_dict() for r in sorted(self.results, key=lambda r: r.score, reverse=True)],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n  结果已保存到: {path}")
