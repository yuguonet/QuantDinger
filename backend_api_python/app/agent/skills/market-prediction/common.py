# -*- coding: utf-8 -*-
"""
market-prediction/common.py — 共享工具与基类
"""

from typing import Any, Dict


class SkillResult(dict):
    """
    技能标准返回包装。
    继承 dict，直接可序列化；run_skill 会自动解包。
    """

    def __init__(self, data: Dict[str, Any]):
        super().__init__(data)
        # 标准元数据
        self.setdefault("skill", "market-prediction")
        self.setdefault("version", "1.0.0")
        self.setdefault("timestamp", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def clamp(x: float, lo: float, hi: float) -> float:
    """数值钳制"""
    return max(lo, min(hi, x))


def percentile_rank(value: float, history: list) -> float:
    """
    计算值在历史序列中的百分位（0-100）。
    用于 PE/PB/股债利差分位计算。
    """
    if not history:
        return 50.0
    sorted_hist = sorted(history)
    n = len(sorted_hist)
    # 线性插值百分位
    import bisect
    pos = bisect.bisect_left(sorted_hist, value)
    if pos == 0:
        return 0.0
    if pos == n:
        return 100.0
    # 在 pos-1 和 pos 之间插值
    lo_val = sorted_hist[pos - 1]
    hi_val = sorted_hist[pos]
    if hi_val == lo_val:
        return (pos / n) * 100
    frac = (value - lo_val) / (hi_val - lo_val)
    return ((pos - 1 + frac) / n) * 100


def sigmoid(x: float) -> float:
    """Sigmoid 函数"""
    import math
    if x >= 30:
        return 1.0
    if x <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))