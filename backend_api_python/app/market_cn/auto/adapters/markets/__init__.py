"""adapters/markets — 市场适配层（MarketSpec）。

一个市场 = 一份 `*.yaml`（声明式取值），core 只定义接口（`core.market.MarketSpec`）。
新增市场 = 新增 YAML（+ 数据源适配器 + 如需执行适配器），**core 零改动**。
"""

from app.market_cn.auto.adapters.markets.registry import (  # noqa: F401
    MarketNotRunnable, load_market, market_keys, require_runnable,
)
