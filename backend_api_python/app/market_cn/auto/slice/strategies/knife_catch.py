"""strategies/knife_catch.py — **转发层**（门逻辑单一事实源在生产策略类）。

2026-10-06: 递推状态机与触发门已下沉进 `auto/strategies/knife_catch.py`
(`KnifeCatchStrategy`)，本模块只保留旧导入名 `KnifeCatchSlim` —— 零数学副本。
改名动机: slice/strategies 曾是生产门逻辑的第二份实现，改一侧必须回头对账
另一侧（"两套实现可给出相反结果"）。现在两者共用 `_gates` 一个函数。
"""

from app.market_cn.auto.strategies.knife_catch import (  # noqa: F401
    KnifeCatchStrategy as KnifeCatchSlim, PARAMS, STRATEGY_KEY,
)

__all__ = ["KnifeCatchSlim", "PARAMS", "STRATEGY_KEY"]
