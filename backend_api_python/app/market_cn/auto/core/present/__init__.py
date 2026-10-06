"""core/present — 展示层（预处理/回测/实时三分支共用的 1 日滑动内核）。

- `contract` : 纯类型 + StrategyProtocol（core 不反向依赖 strategies）
- `runner`   : DailyRunner / StateStore / 四条件重建
- `realtime` : 实时旁支

数学一律转发仓库现有实现（core/market.py、core/features/cross_section.py、
core/data/hub.py）—— 本包**零数学副本**。
"""

from app.market_cn.auto.core.present.contract import (  # noqa: F401
    DayInput, InsufficientHistory, Progress, Stage, StrategyProtocol,
)
from app.market_cn.auto.core.present.runner import (  # noqa: F401
    DailyRunner, Record, RebuildNeedsHistory, StateStore,
)
from app.market_cn.auto.core.present.realtime import RealtimeBranch  # noqa: F401

__all__ = [
    "DayInput", "InsufficientHistory", "Progress", "Stage", "StrategyProtocol",
    "DailyRunner", "Record", "RebuildNeedsHistory", "StateStore",
    "RealtimeBranch",
]
