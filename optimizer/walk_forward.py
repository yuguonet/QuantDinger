"""
Walk-Forward 验证模块
防止过拟合的核心机制
"""
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable

import numpy as np


class WalkForwardValidator:
    """
    滚动窗口验证器

    将历史数据分为 N 段，每段：
      - 训练集: 优化参数
      - 测试集: 验证样本外表现

    最终得分 = 所有测试集得分的平均值（而非训练集）
    """

    def __init__(
        self,
        n_splits: int = 5,
        train_ratio: float = 0.7,
        gap_bars: int = 0,       # 训练/测试之间的间隔（防止前瞻偏差）
    ):
        self.n_splits = n_splits
        self.train_ratio = train_ratio
        self.gap_bars = gap_bars

    def split(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict[str, datetime]]:
        """
        生成 N 个 train/test 分割

        Returns:
            [{"train_start": dt, "train_end": dt, "test_start": dt, "test_end": dt}, ...]
        """
        total_days = (end_date - start_date).days
        if total_days < 30:
            raise ValueError(f"数据范围太短 ({total_days} 天)，至少需要 30 天")

        split_size = total_days // self.n_splits
        splits = []

        for i in range(self.n_splits):
            seg_start = start_date + timedelta(days=i * split_size)
            seg_end = start_date + timedelta(days=(i + 1) * split_size)
            if i == self.n_splits - 1:
                seg_end = end_date  # 最后一段包含剩余

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
        objective_fn: Callable[[dict], dict],
        best_params: dict,
        start_date: datetime,
        end_date: datetime,
        score_fn: str = "sharpe",
    ) -> Dict[str, Any]:
        """
        对已优化的参数做 Walk-Forward 验证

        Args:
            objective_fn: 接受 (params, start_date, end_date) 返回 metrics
            best_params: 优化得到的最优参数
            start_date: 数据起始日期
            end_date: 数据结束日期

        Returns:
            {
                "splits": [...],
                "avg_train_score": float,
                "avg_test_score": float,
                "overfitting_ratio": float,  # 越低越好
                "consistency": float,        # 测试集得分一致性
            }
        """
        splits = self.split(start_date, end_date)

        train_scores = []
        test_scores = []
        split_details = []

        for i, s in enumerate(splits):
            # 在训练集上评估
            train_metrics = objective_fn(best_params, s["train_start"], s["train_end"])
            train_score = self._compute_score(train_metrics, score_fn)

            # 在测试集上评估（这是关键：用训练集优化的参数在测试集上跑）
            test_metrics = objective_fn(best_params, s["test_start"], s["test_end"])
            test_score = self._compute_score(test_metrics, score_fn)

            train_scores.append(train_score)
            test_scores.append(test_score)

            split_details.append({
                "fold": i + 1,
                "train_period": f"{s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}",
                "test_period": f"{s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}",
                "train_score": round(train_score, 4),
                "test_score": round(test_score, 4),
                "train_metrics": train_metrics,
                "test_metrics": test_metrics,
            })

        avg_train = np.mean(train_scores) if train_scores else 0
        avg_test = np.mean(test_scores) if test_scores else 0

        # 过拟合比率：训练得分远高于测试得分 → 过拟合
        if avg_train > 0:
            overfitting_ratio = 1 - (avg_test / avg_train)
        else:
            overfitting_ratio = 1.0

        # 一致性：测试集得分的标准差（越低越稳定）
        consistency = 1 - (np.std(test_scores) / max(abs(avg_test), 0.01))
        consistency = max(0, min(1, consistency))

        return {
            "n_splits": len(splits),
            "splits": split_details,
            "avg_train_score": round(float(avg_train), 4),
            "avg_test_score": round(float(avg_test), 4),
            "overfitting_ratio": round(float(overfitting_ratio), 4),
            "consistency": round(float(consistency), 4),
            "verdict": self._verdict(overfitting_ratio, consistency, avg_test),
        }

    def _compute_score(self, metrics: dict, score_fn: str) -> float:
        sharpe = float(metrics.get("sharpeRatio", 0))
        win_rate = float(metrics.get("winRate", 0)) / 100.0
        max_dd = float(metrics.get("maxDrawdown", 0)) / 100.0
        total_return = float(metrics.get("totalReturn", 0)) / 100.0
        total_trades = int(metrics.get("totalTrades", 0))
        profit_factor = float(metrics.get("profitFactor", 0))

        if total_trades < 1:
            return -10.0

        if score_fn == "sharpe":
            return sharpe
        if score_fn == "return_dd_ratio":
            return total_return / max(max_dd, 0.001)
        # composite
        return sharpe * 0.4 + win_rate * 2.0 + min(profit_factor, 5.0) * 0.4 - max_dd * 2.0

    def _verdict(self, overfitting_ratio: float, consistency: float, avg_test: float) -> str:
        if overfitting_ratio > 0.5:
            return "❌ 严重过拟合 — 训练集表现远好于测试集，策略不可用"
        if overfitting_ratio > 0.3:
            return "⚠️ 中度过拟合 — 建议简化策略或增加数据量"
        if avg_test < 0:
            return "❌ 样本外亏损 — 策略在测试集上亏损，不可用"
        if consistency < 0.5:
            return "⚠️ 不稳定 — 测试集得分波动大，策略不够稳健"
        if overfitting_ratio < 0.1 and avg_test > 0:
            return "✅ 通过 — 策略在样本外表现稳定，过拟合风险低"
        return "⚠️ 边缘 — 勉强通过，建议进一步验证"
