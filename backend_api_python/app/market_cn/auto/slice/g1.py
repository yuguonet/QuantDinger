"""g1.py — G1 特征/池数学的**单一事实源转发**（零数学副本）。

实现全部在 `core/features/cross_section.py`（增量状态机/特征/mask/聚合）与
`app/utils/indicators.py`（MACD 内核）。本模块只统一命名面，供 slice 策略
与 auto_slim 时代的调用名保持不变 —— **不要在这里加任何公式**。
"""

from app.market_cn.auto.core.features.cross_section import (  # noqa: F401
    ATR_Q5, G1_STATE_STATS, G1_WARMUP, G1_WIN_MIN,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL, MIN_HIST, ROLL,
    _aggregate as aggregate,
    _anchor_step, _atr, _boll_pctb, _g1_arrays, _g1_mask,
    _pctl_roll, _roll_sum, _rsi, _sma_np, _window_of,
    g1_state_features, g1_state_init, g1_state_step, g1_state_window_bars,
)
from app.utils.indicators import (  # noqa: F401
    _ema_naive_series, calc_macd, ema_fwd, macd_core, macd_state,
)
