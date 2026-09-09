#!/usr/bin/env python3
"""龙回头 + V1/断板 核心判定逻辑 —— 单一事实源 (single source of truth)

2026-09-07: 三策略优化完成 (test_dragon.py 同步):
  - V1: 纯单板过热过滤 (MACD柱<2 + 布林带宽<45%), 136笔79.4%
  - 断板: 首断板gap>=0 + chg>=0%双过滤, 94笔74.5%
  - 龙回头: gap_max=6 (回调5-6天黄金区间), 113笔75.2%

历史规则与验证证据: 龙回头优化分析_20260906/ (滑动窗口找龙 + 拐点或关系过滤)。

使用方:
  - test_dragon.py (--strategy dragon)   : 回测 (dragon_cb_today_d0_signals /
                                           run_backtest_dragon_callback / unified_prefilter)
  - app/market_cn/auto/scan.py      : 盘后全市场扫描 (16:30)
  - app/market_cn/auto/monitor.py    : 盘中状态机 (60s)

本模块保持零 IO / 零 print, 只做纯判定。
2026-09-07 Phase 1: 指标/市场函数/统一预过滤已提取至 common/ (本模块 re-export, 对外 API 不变)。
修改任何规则后必须重跑回测对数 (方案2基线见 龙回头优化分析_20260906/ 分析日志)。

易错点:
  - volume 单位是股, 换手率 = volume/circ_shares*100; 市值用信号日收盘价
  - as-of 安全: 所有判定只用<=当日收盘数据, 回测与 --today 报告共用同一判定函数
"""
from __future__ import annotations

from app.market_cn.auto.common.market import (  # noqa: F401  (re-export, 对外API不变)
    get_board_type, get_board_name, is_limit_up, find_limit_ups,
)
from app.market_cn.auto.common.indicators import (  # noqa: F401
    ema, rsi, calc_macd, calc_bollinger_bw, calc_roc, calc_psy,
    is_macd_golden_cross, is_macd_hist_turning_positive, is_macd_hist_shrinking_negative,
)
from app.market_cn.auto.common.filters import (  # noqa: F401
    PREFILTER_PARAMS, unified_prefilter,
)


# ================================================================
# 龙回头 (dragon_callback, "方案2") —— 2026-09-06 与 test_dragon.py 同步
# ================================================================
# 规则框架: 找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,25] → 拐点OR → 信号质量排除
#           → U1~U4 统一预过滤(@涨停日) → D1开盘gap过滤 → 次日开盘买
# 依据 (龙回头优化分析_20260906/ 分析日志):
#   - 找龙不需要精确识别"龙", 滑动窗口涨停占比>=70%足够 (覆盖连板+断板两种形态)
#   - 回调到位比识别龙更重要: MA20支撑位 / 深跌释放 / 买盘承接 是真正拐点信号
#   - "中间地带"(温和回调+阴线偏多)是亏损重灾区, 用质量排除兜底
#   - 止损-5%太紧截断收益 → 放宽到-8%; 分段追踪止损锁利润

# ================================================================
# Phase 2 facade: 龙回头实现已迁 strategies/dragon_callback.py (DragonCallbackStrategy)。
# 参数/判定/出场模拟全部转发, 对外 API (含 test_dragon.py import) 不变。
from app.market_cn.auto.strategies.dragon_callback import (  # noqa: F401  E402
    DRAGON_CB_PARAMS,
)


def dragon_cb_today_d0_signals(bars, code, min_pullback_days=3, max_pullback_days=11,
                               max_last_chg=3.0, today_str=None, limit_ups=None,
                               use_tech_score=True, params=None):
    """龙回头 今日(D0)入场信号 — Phase 2 facade: 实现已迁 strategies/dragon_callback.py。

    参数 min_pullback_days/max_pullback_days/max_last_chg 为旧版兼容保留, 已不参与判定。
    params: 可选, 覆盖 DRAGON_CB_PARAMS 中的键 (scan 用)。
    """
    from app.market_cn.auto.strategies.dragon_callback import (
        DragonCallbackStrategy, _signal_to_legacy_dict,
    )
    as_of = None
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        as_of = idxs[-1]
    overrides = dict(params or {})
    sigs = DragonCallbackStrategy().scan_signals(
        bars, code, as_of=as_of, limit_ups=limit_ups,
        use_tech_score=use_tech_score, **overrides)
    return [_signal_to_legacy_dict(s, code) for s in sigs]


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None):
    """龙回头出场模拟 — Phase 2 facade: 实现已迁 strategies/dragon_callback.py。"""
    from app.market_cn.auto.strategies.dragon_callback import (
        run_backtest_dragon_callback as _impl,
    )
    return _impl(bars, entry_idx, entry_price, hold_days=hold_days, stop_loss=stop_loss,
                 board_type=board_type, stop_at_idx=stop_at_idx)


# ================================================================
# 统一前置过滤 U1~U4 (2026-09-04, 与 test_dragon.py 共用)
# 依据: tmp/妖股前置过滤分析报告.md (18564涨停事件) + tmp/三策略入口过滤改进报告.md
# 关键设计: 全部条件只用判定日收盘可知数据, 无未来函数。
# 易错点: 龙回头/断板的 U2/U3 必须锚定涨停日评估, 不能用缩量信号日
#         (D0是缩量小阴日, 换手天然低, @D0评估会误杀)。
# ================================================================


# V1/断板 出场引擎与 BOARD_PARAMS 已迁 backtest.py (2026-09-10, 逐字搬运回归验证);
# 判定 facade (v1_today_d0_signals / break_today_d0_signals / _break_signal_at) 保留在此。
# ================================================================
def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """断板期计算+5a~5g确认 — Phase 2 facade: 实现已迁 strategies/break_buy.py。"""
    from app.market_cn.auto.strategies.break_buy import _break_signal_at as _impl
    return _impl(bars, code, streak_start, streak_end, min_streak, max_break_gap, params)


def v1_today_d0_signals(bars, code, ret_20d_min=30.0,
                        d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                        obv_filter=True, d_1_vol_max=1.5, today_str=None,
                        stock_info=None):
    """V1 今日(D0)入场信号 — Phase 2 facade: 实现已迁 strategies/v1.py (V1Strategy)。

    today_str: 指定今日日期(与--today-date一致), 为空则用最后一天。
    返回空list或单元素list, 元素含 d0_date/d0_close/ret_20d/d_1_change。
    """
    from app.market_cn.auto.strategies.v1 import V1Strategy, _signal_to_legacy_dict
    params = dict(ret_20d_min=ret_20d_min,
                  d_1_pullback_min=d_1_pullback_min,
                  d_1_pullback_max=d_1_pullback_max,
                  obv_filter=obv_filter,
                  d_1_vol_max=d_1_vol_max,
                  stock_info=stock_info)
    as_of = None
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        as_of = idxs[-1]  # 与旧实现 idxs[-1] 一致
    sigs = V1Strategy().scan_signals(bars, code, as_of=as_of, **params)
    return [_signal_to_legacy_dict(s, code) for s in sigs]

# ================================================================
# 断板买入策略
# ================================================================


def break_today_d0_signals(bars, code, min_streak=2, max_break_gap=5, today_str=None,
                           limit_ups=None, stock_info=None):
    """断板 今日(D0)信号 — Phase 2 facade: 实现已迁 strategies/break_buy.py (BreakStrategy)。

    原则: --today 时"买入当日(次日D1开盘)由人工判别", 今日(D0)及以前规则与回测
    strategy_break_buy 的"买点之前"完全一致。确认点在断板期最后一天收盘。
    today_str: 指定今日日期, 为空则用最后一天。返回空list或单元素list。
    """
    from app.market_cn.auto.strategies.break_buy import BreakStrategy, _signal_to_legacy_dict
    as_of = None
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b["time"] == today_str]
        if not idxs:
            return []
        as_of = idxs[-1]
    sigs = BreakStrategy().scan_signals(
        bars, code, as_of=as_of, limit_ups=limit_ups,
        min_streak=min_streak, max_break_gap=max_break_gap, stock_info=stock_info)
    return [_signal_to_legacy_dict(s, code) for s in sigs]
