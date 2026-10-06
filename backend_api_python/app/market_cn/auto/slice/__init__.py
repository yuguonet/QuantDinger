"""slice — 展示层 1 日滑动内核（预处理/回测共用 fold + 实时旁支 + 四条件重建）。

契约见 contract.py；生命周期见 runner.py（advance = 1 日延伸）；实时旁支
见 realtime.py。数学一律转发仓库现有实现（g1/market/minutes 三个 facade）。
"""

from app.market_cn.auto.slice.contract import (  # noqa: F401
    DayInput, InsufficientHistory, Progress, Stage, StrategyBase,
)
from app.market_cn.auto.slice.runner import (  # noqa: F401
    DailyRunner, Record, RebuildNeedsHistory, StateStore,
)
from app.market_cn.auto.slice.realtime import RealtimeBranch  # noqa: F401

__all__ = [
    "DayInput", "InsufficientHistory", "Progress", "Stage", "StrategyBase",
    "DailyRunner", "Record", "RebuildNeedsHistory", "StateStore",
    "RealtimeBranch",
]
