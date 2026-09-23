#!/usr/bin/env python3
"""兼容 shim: 旧 app.market_cn.auto.core.indicators -> app.utils.indicators

2026-09-23: 指标库上移为基座共享叶子层 (app/utils/indicators.py), 便于展示层复用同一口径。
本文件保留 re-export ⇒ auto 内全部引用 (gate_stdlib / probe / dragon_callback_legacy / dragon_v2 /
g56 / relay3 / triple_resonance / v1 / common.indicators) 改动 0 行。

新代码请直接 `from app.utils.indicators import ...`。
"""
from app.utils.indicators import (  # noqa: F401
    calc_bollinger_bw,
    calc_macd,
    calc_psy,
    calc_roc,
    ema,
    is_macd_golden_cross,
    is_macd_hist_shrinking_negative,
    is_macd_hist_turning_positive,
    kdj,
    ma,
    rsi,
)

__all__ = [
    "ma", "kdj", "ema", "rsi", "calc_macd", "calc_bollinger_bw", "calc_roc", "calc_psy",
    "is_macd_golden_cross", "is_macd_hist_turning_positive", "is_macd_hist_shrinking_negative",
]
