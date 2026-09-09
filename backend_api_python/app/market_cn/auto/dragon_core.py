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
  - app/market_cn/auto/dragon_scan.py    : 盘后全市场扫描 (16:30)
  - app/market_cn/auto/dragon_monitor.py : 盘中状态机 (60s)

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
    params: 可选, 覆盖 DRAGON_CB_PARAMS 中的键 (dragon_scan 用)。
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
# ================================================================
# V1 / 断板 判定与出场 (2026-09-04 提取, 与 test_dragon.py 共用)
# ================================================================

BOARD_PARAMS = {
    # enhance_filter: 断板增强过滤 (三通道OR, 满足其一即可; 置 False 可整体关闭)
    #   通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)  (企稳)
    #   通道2: 断板期均量比 >= vol_r_or_min                    (换手充分)
    #   通道3: 连板前20日涨幅 >= pre20_min                     (前期热度, 大肉股富集)
    # ma_bull_filter: 均线多头排列过滤 — 已评估: 胜率持平、均收益略增, 作用不大, 默认关闭
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
             "first_break_gap_min": 0, "first_break_chg_min": 0.0},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
                 "first_break_gap_min": 0, "first_break_chg_min": 0.0},
}


# 跌停价原语收编至 common/exec_cn.py (C 阶段); 别名保持引擎内部调用点不变
from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)


def run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    """V1/通用出场模拟 (现实化 2026-09-09, 与 test_dragon.py 逐字同步):

    现实约束: ① T+1 — 买入当日(d=1)不可卖出, 全部出场判定从 d=2 起 (仅更新峰值/估值);
    ② 跳空穿越 — 触发日开盘低于触发价按开盘价成交;
    ③ 跌停无法卖出 — 一字跌停整日跳过 (V1 的 D2 开盘清仓若遇一字跌停顺延次日开盘),
    触发成交触及跌停顺延次日开盘; 到期日一字跌停顺延次日开盘强平。
    V1 日内动量规则 (D1收盘判定→D2开盘执行) 本就满足 T+1, 判定逻辑未改动。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 触发成交触及跌停 / 清仓日一字跌停 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延

    # 如果外部未传入 d1_limit_up, 则在回测内计算 (兼容旧调用)
    # 注意: next_open 模式下 entry_idx=pullback_end+1, d=1 访问的是 D2
    # 因此推荐由调用方预计算并传入
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx + 1 < len(bars):
            d1_bar = bars[entry_idx + 1]
            d1_ret = (d1_bar['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

    # next_open模式: entry_idx=D1(D+1开盘买入)
    # 循环d=1应指向D1(第一个持仓日), d=2指向D2, 以此类推
    # 先用D1的high更新peak
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1), d=2 → entry_idx+1(D2)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            break

        # V1出场 (v3): D1日内动量<3% → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        #   <0: 盘中出货, D2大概率续跌, 100%捕获D2跌>3%的信号
        #   >=0: 盘中有买盘承接, 继续持有
        # 注: -10%止损已移除, 日内动量规则在D2开盘即清仓, 不需要等止损位
        v1_momentum_exit = False
        if is_v1 and d == 2:
            # 日内动量 = D1收盘涨幅 - D1开盘涨幅 = (D1 close - D1 open) / D0 close
            # d1_change 和 d1_gap 由调用方传入, 也可从bars计算
            if d1_change is not None and d1_gap is not None:
                intraday = d1_change - d1_gap
            else:
                # fallback: 从bars计算
                d1_bar = bars[entry_idx]
                d0_close = bars[entry_idx - 1]['close'] if entry_idx > 0 else entry_price
                intraday = (d1_bar['close'] - d1_bar['open']) / d0_close * 100 if d0_close > 0 else 0
            v1_momentum_exit = intraday < 3

        # 一字跌停: 全天无成交可能 (D2开盘清仓同样无法成交 → 顺延次日开盘)
        if is_one_word_limit_dn(b, dn):
            pending_dn = v1_momentum_exit
            last_unfilled = True
            continue
        last_unfilled = False

        if v1_momentum_exit:
            # D2开盘直接清仓, 不等止损位
            exit_p, exit_d = b['open'], d
            break

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 1 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
            if peak_exit:
                ret = (b['close'] / entry_price - 1) * 100
                if ret > 7:
                    bar_range = b['high'] - b['low']
                    upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                    if upper > 30 and b['close'] < b['high'] * 0.98:
                        exit_p, exit_d = b['close'], d
                        break

            # 2/3 追踪+止损 (合并: 价格连续先穿过更高触发线; 跳空按开盘; 触跌停顺延)
            trig_t = peak * (1 + trailing_stop / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            if b['low'] <= trig:
                fill = fill_on_gap(b['open'], trig)
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True   # 成交价触及跌停 → 卖不出
                    continue
                exit_p, exit_d = fill, d
                break

        # 4 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

    # 末日落入无法卖出状态 (一字跌停/触发触跌停) → 顺延下一可交易日开盘强平
    # (连续一字逐日跳过; nxt 指向未成交日的下一日)
    if last_unfilled or pending_dn:
        nxt = entry_idx + exit_d + 1
        while nxt < len(bars):
            nb = bars[nxt]
            pc = bars[nxt - 1]['close']
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb['low'] == nb['high'] and abs(nb['low'] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d = nb['open'], nxt - entry_idx + 1
            break

    result = {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }
    if d1_limit_up:
        result['d1_limit_up'] = d1_limit_up
    return result


def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=7, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main"):
    """断板专用回测: 追踪止损 + 峰值逃顶信号 (收盘价口径, 与v1的low触及口径不同)。

    现实化 (2026-09-09, 与 test_dragon.py 逐字同步):
    ① T+1 — 买入当日(d=1)不可卖出;
    ② 成交价=收盘价 — 原引擎收盘判定却按触发价成交 (触发价高于判定收盘, 不可实现);
    ③ 跌停 — 一字跌停整日跳过; 收盘触及跌停卖不出 → 顺延次日开盘; 到期顺延。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 收盘触跌停卖不出 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延

    # next_open模式: entry_idx=D1, 循环d=1应指向D1
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            break

        # 一字跌停: 全天无成交可能, 持仓顺延
        if is_one_word_limit_dn(b, dn):
            last_unfilled = True
            continue
        last_unfilled = False

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 止损 (收盘判定 → 收盘价成交)
            if ret <= stop_loss:
                if dn is not None and b['close'] <= dn * 1.002:
                    pending_dn = True   # 收盘封死跌停 → 卖不出
                    continue
                exit_p, exit_d = b['close'], d
                break

            # 追踪止损 (盈利时, 收盘判定 → 收盘价成交)
            if ret_from_high <= trailing_stop and ret > 0:
                if dn is not None and b['close'] <= dn * 1.002:
                    pending_dn = True
                    continue
                exit_p, exit_d = b['close'], d
                break

            # 峰值信号: 涨>10%后大上影线(>40%)→收盘逃顶 (收盘>+10%不可能贴跌停)
            if ret > 10:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 40 and b['close'] < b['high'] * 0.98:
                    exit_p, exit_d = b['close'], d
                    break

        exit_p = b['close']; exit_d = d

    # 末日落入无法卖出状态 → 顺延下一可交易日开盘强平 (连续一字逐日跳过)
    if last_unfilled or pending_dn:
        nxt = entry_idx + exit_d + 1
        while nxt < len(bars):
            nb = bars[nxt]
            pc = bars[nxt - 1]['close']
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb['low'] == nb['high'] and abs(nb['low'] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d = nb['open'], nxt - entry_idx + 1
            break

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


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
