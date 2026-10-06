"""strategies/dragon_callback.py — 龙回头（**转发层**，2026-10-06 收口）。

递推状态机（`init_state/step/probe/evaluate`）与门/出场/原语已全部下沉进生产类
`app.market_cn.auto.strategies.dragon_callback.DragonCallbackStrategy` —— 该类同时
是生产策略（scan_signals/backtest_stock）与 slice 策略（递推契约）。

本模块只保留旧导入名，与 knife/tail/g56 同一写法。⚠ 不要再往这里加任何实现:
slice 侧曾各抄一份门/出场，实测分叉（use_tech_score=False 使 RSI6 门失效 ⇒
全市场多日 46 笔 vs 生产 30 笔）。
"""

from app.market_cn.auto.strategies.dragon_callback import (  # noqa: F401
    DRAGON_CB_PARAMS,
    RING,
    DragonCallbackStrategy as DragonCallbackSlim,
    run_backtest_dragon_callback,
)
