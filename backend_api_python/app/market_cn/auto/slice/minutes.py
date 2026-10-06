"""minutes.py — 分钟序列标准化**单一事实源转发**（零副本）。

实现全部在 `core/data/hub.py`（prep_minutes / _trading_minute_index）。
"""

from app.market_cn.auto.core.data.hub import (  # noqa: F401
    _trading_minute_index, prep_minutes,
)
