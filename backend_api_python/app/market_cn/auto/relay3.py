"""relay3.py — 3板接力策略 facade (Phase 2 迁移, 2026-09-07)

实现已迁移至 strategies/relay3.py (StrategyBase 插件); 本文件仅保留旧接口签名转发,
供 dragon_scan / dragon_monitor / test 脚本在 Phase 3 切换前继续使用。
**易错点**: 行为变更必须改 strategies/relay3.py, 此处只允许签名级调整。
"""
from __future__ import annotations

from app.market_cn.auto.strategies.relay3 import (  # noqa: F401  (facade re-export)
    PARAMS,
    SIGNAL_EXTRA_KEYS,
    STRATEGY_KEY,
    STRATEGY_LABEL,
    Relay3Strategy,
    _ma,
    calc_features,
    consecutive_limit_ups,
    entry_stop,
    eval_exit_day_close,
    eval_exit_live,
    gap_buyable,
    ma_bull_arrangement,
    _signal_to_legacy_dict,
)

_strategy = Relay3Strategy()


def relay3_today_d0_signals(bars, code) -> list:
    """D0 扫描 (旧接口): 转发 Relay3Strategy.scan_signals, 输出转回旧 dict 形态。

    bars: 截至当日(含)的日K list[dict]; 返回 list[dict] 与 dragon_cb_today_d0_signals 字段对齐。
    """
    sigs = _strategy.scan_signals(bars, code)
    return [_signal_to_legacy_dict(s) for s in sigs]
