"""strategies/tail_oversold.py — **转发层**（门逻辑单一事实源在生产策略类）。

2026-10-06: 递推状态机与触发门已下沉进 `auto/strategies/tail_oversold.py`
(`TailOversoldStrategy`)，本模块只保留旧导入名 `TailOversoldSlim` —— 零数学副本。
"""

from app.market_cn.auto.strategies.tail_oversold import (  # noqa: F401
    PARAMS, STRATEGY_KEY, TailOversoldStrategy as TailOversoldSlim,
)

__all__ = ["TailOversoldSlim", "PARAMS", "STRATEGY_KEY"]
