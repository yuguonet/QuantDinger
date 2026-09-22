# 兼容 shim: 旧 common.indicators -> core.indicators
from app.market_cn.auto.core.indicators import (  # noqa: F401
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
