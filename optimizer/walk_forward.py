"""
Walk-Forward 验证模块（真正版本）

核心区别：
  - ❌ 旧版：固定参数跨期验证（同一组 best_params 评估所有 fold）
  - ✅ 新版：每 fold 独立优化（训练集优化 → 测试集验证 → 滚动）

这才能真正检测参数在不同市场环境下的稳定性。
如果每 fold 优化出的参数差异大 → 策略对参数敏感 → 过拟合风险高。
"""
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable

import numpy as np

from optimizer.scoring import compute_score


class WalkForwardValidator:
    """
    滚动窗口验证器（真正 Walk-Forward）

    将历史数据分为 N 段，每段：
      - 训练集: 独立优化参数
      - 测试集: 用该 fold 优化出的参数验证

    关键指标：
      - avg_test_score: 所有 fold 测试集得分的平均
      - param_stability: 各 fold 最优参数的相似度（越接近 1 越稳定）
      - overfitting_ratio: 训练 vs 测试得分差距
    """

    def __init__(
        self,
        n_splits: int = 5,
        train_ratio: float = 0.7,
        gap_bars: int = 0,
        # 每 fold 的优化配置
        fold_trials: int = 50,       # 每 fold 优化轮数（比全量少，节省时间）
        fold_patience: int = 15,     # 每 fold 早停 patience
        fold_mode: str = "random",   # 每 fold 搜索方式
    ):
        self.n_splits = n_splits
        self.train_ratio = train_ratio
        self.gap_bars = gap_bars
        self.fold_trials = fold_trials
        self.fold_patience = fold_patience
        self.fold_mode = fold_mode

    def split(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict[str, datetime]]:
        total_days = (end_date - start_date).days
        if total_days < 30:
            raise ValueError(f"数据范围太短 ({total_days} 天)，至少需要 30 天")

        split_size = total_days // self.n_splits
        splits = []

        for i in range(self.n_splits):
            seg_start = start_date + timedelta(days=i * split_size)
            seg_end = start_date + timedelta(days=(i + 1) * split_size)
            if i == self.n_splits - 1:
                seg_end = end_date

            seg_days = (seg_end - seg_start).days
            train_days = int(seg_days * self.train_ratio)

            train_start = seg_start
            train_end = seg_start + timedelta(days=train_days)
            test_start = train_end + timedelta(days=self.gap_bars)
            test_end = seg_end

            if test_start >= test_end:
                continue

            splits.append({
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            })

        return splits

    def validate(
        self,
        objective_fn: Callable,          # objective_fn(params, start_date, end_date) → metrics
        template_key: str,               # 策略模板 key（用于创建每 fold 的优化器）
        start_date: datetime,
        end_date: datetime,
        score_fn: str = "composite",
        seed: int = 42,
    ) -> Dict[str, Any]:
        """
        真正 Walk-Forward 验证

        每个 fold：
          1. 用训练集数据独立优化参数
          2. 用优化出的参数在测试集上评估
          3. 记录参数和得分

        Args:
            objective_fn: 接受 (params, start_date, end_date) 返回 metrics
            template_key: 策略模板 key
            start_date: 数据起始日期
            end_date: 数据结束日期
            score_fn: 评分函数名
            seed: 随机种子

        Returns:
            {
                "splits": [...],
                "avg_train_score": float,
                "avg_test_score": float,
                "overfitting_ratio": float,
                "consistency": float,
                "param_stability": float,  # 参数稳定性
            }
        """
        from optimizer.strategy_optimizer import StrategyOptimizer

        splits = self.split(start_date, end_date)

        train_scores = []
        test_scores = []
        split_details = []
        all_fold_params = []

        for i, s in enumerate(splits):
            print(f"\n  {'─'*50}")
            print(f"  Fold {i+1}/{len(splits)}")
            print(f"  训练: {s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}")
            print(f"  测试: {s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}")

            # 1. 在训练集上独立优化
            def train_objective(params):
                return objective_fn(params, s["train_start"], s["train_end"])

            optimizer = StrategyOptimizer(
                template_key=template_key,
                objective_fn=train_objective,
                n_trials=self.fold_trials,
                score_fn=score_fn,
                mode=self.fold_mode,
                seed=seed + i * 1000,
                patience=self.fold_patience,
                min_trials=max(10, self.fold_trials // 3),
            )
            best = optimizer.run()

            if best is None:
                print(f"  ⚠️  Fold {i+1}: 训练集无有效策略，跳过")
                continue

            train_score = best.score
            train_metrics = best.metrics

            # 2. 用训练集优化出的参数在测试集上评估
            try:
                test_metrics = objective_fn(best.params, s["test_start"], s["test_end"])
                test_score = compute_score(test_metrics, score_fn)
            except Exception as e:
                print(f"  ⚠️  Fold {i+1}: 测试集评估失败: {e}")
                test_score = -10.0
                test_metrics = {}

            train_scores.append(train_score)
            test_scores.append(test_score)
            all_fold_params.append(best.params)

            split_details.append({
                "fold": i + 1,
                "train_period": f"{s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}",
                "test_period": f"{s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}",
                "train_score": round(train_score, 4),
                "test_score": round(test_score, 4),
                "best_params": best.params,
                "train_metrics": train_metrics,
                "test_metrics": test_metrics,
                "trials_used": len(optimizer.results),
                "early_stopped": optimizer._early_stopped,
            })

            print(f"  ✓ Fold {i+1}: train={train_score:.4f} → test={test_score:.4f} | "
                  f"trials={len(optimizer.results)}{' ⏹' if optimizer._early_stopped else ''}")

        # 3. 汇总
        avg_train = np.mean(train_scores) if train_scores else 0
        avg_test = np.mean(test_scores) if test_scores else 0

        # 过拟合比率
        if avg_train > 0:
            overfitting_ratio = 1 - (avg_test / avg_train)
        else:
            overfitting_ratio = 1.0

        # 测试集一致性
        consistency = 1 - (np.std(test_scores) / max(abs(avg_test), 0.01))
        consistency = max(0, min(1, consistency))

        # 参数稳定性：各 fold 最优参数的变异系数
        param_stability = self._compute_param_stability(all_fold_params)

        return {
            "n_splits": len(splits),
            "splits": split_details,
            "avg_train_score": round(float(avg_train), 4),
            "avg_test_score": round(float(avg_test), 4),
            "overfitting_ratio": round(float(overfitting_ratio), 4),
            "consistency": round(float(consistency), 4),
            "param_stability": round(float(param_stability), 4),
            "verdict": self._verdict(overfitting_ratio, consistency, avg_test, param_stability),
        }

    def _compute_param_stability(self, all_fold_params: List[dict]) -> float:
        """
        计算参数稳定性：各 fold 最优参数的相似度

        方法：对每个参数计算变异系数(CV)，然后取平均。
        CV 越低 → 参数越稳定 → 返回值越接近 1。

        Returns:
            float: 0~1，越高越稳定
        """
        if len(all_fold_params) < 2:
            return 1.0

        # 收集所有参数名
        all_keys = set()
        for p in all_fold_params:
            all_keys.update(p.keys())

        stabilities = []
        for key in all_keys:
            values = []
            for p in all_fold_params:
                v = p.get(key)
                if v is None:
                    continue
                if isinstance(v, (int, float)):
                    values.append(float(v))
                elif isinstance(v, bool):
                    values.append(1.0 if v else 0.0)
                elif isinstance(v, str):
                    # 字符串参数：看是否一致
                    values.append(hash(v) % 1000)

            if len(values) < 2:
                continue

            mean = np.mean(values)
            std = np.std(values)
            # 避免除零
            if abs(mean) < 1e-10:
                cv = std / 1.0
            else:
                cv = std / abs(mean)

            # CV → stability: CV=0 → 1.0, CV=1 → 0.5, CV>2 → ~0
            stability = max(0, 1.0 / (1.0 + cv))
            stabilities.append(stability)

        return np.mean(stabilities) if stabilities else 1.0

    def _verdict(self, overfitting_ratio: float, consistency: float, avg_test: float, param_stability: float) -> str:
        # 参数不稳定是严重问题
        if param_stability < 0.3:
            return "❌ 参数极不稳定 — 各 fold 最优参数差异巨大，策略对参数敏感，不可用"

        if overfitting_ratio > 0.5:
            return "❌ 严重过拟合 — 训练集表现远好于测试集，策略不可用"
        if overfitting_ratio > 0.3:
            return "⚠️ 中度过拟合 — 建议简化策略或增加数据量"
        if avg_test < 0:
            return "❌ 样本外亏损 — 策略在测试集上亏损，不可用"
        if consistency < 0.5:
            return "⚠️ 不稳定 — 测试集得分波动大，策略不够稳健"
        if overfitting_ratio < 0.1 and avg_test > 0 and param_stability > 0.7:
            return "✅ 通过 — 策略在样本外表现稳定，参数稳健，过拟合风险低"
        if overfitting_ratio < 0.1 and avg_test > 0:
            return "⚠️ 基本通过 — 样本外稳定但参数波动较大，建议关注参数敏感性"
        return "⚠️ 边缘 — 勉强通过，建议进一步验证"
