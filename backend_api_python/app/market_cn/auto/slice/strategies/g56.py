"""strategies/g56.py — **转发层**（门/信号构造单一事实源在生产策略类）。

2026-10-06: 递推状态机与池台账已下沉进 `auto/strategies/g56.py` (`G56Strategy`)；
门委托生产唯一实现 `_g56_gate`、信号构造委托 `_mk_signal`。本模块只保留旧导入名。
"""

from app.market_cn.auto.strategies.g56 import (  # noqa: F401
    G56_WIN, PoolLedger, STRATEGY_KEY, G56Strategy as G56Slim,
)

__all__ = ["G56Slim", "PoolLedger", "G56_WIN", "STRATEGY_KEY"]
