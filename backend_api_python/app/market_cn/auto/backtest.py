#!/usr/bin/env python3
"""auto/backtest.py — 框架内全市场回测流水线 (B 阶段, 2026-09-09)

用途: 把 test_dragon.py 的"全市场回测流水线"收进框架 —— 判定/引擎/过滤全部
     import 系统侧权威实现 (strategies 插件 + core facade), 本文件只做**薄枚举编排**:
     逐日切片 → 当日D0判定 → 去重(±4) → U1~U4预过滤 → D1开盘买 → 出场引擎 → trades。

设计点:
  - 与实盘同一份 scan_signals (as_of 切片语义), 对数 PASS 后 test_dragon 双同步约定作废;
  - 枚举层无规则: 所有阈值/规则都在插件 PARAMS 与判定函数内, 本文件不出现魔法数;
  - 数据走 hub.daily (与 test_dragon.fetch_kline_db 逐字等价: 窗口取数+qfq, 已验证);
  - 去重时机在预过滤之前 (与 test_dragon 完全一致, 保证禁用过滤时行为同基线)。

易错点:
  - 枚举终点 n-1: 最后一根无 D+1, 不能做 D0 (与 test_dragon range(2, n-1) 一致);
  - limit_ups 预计算全序列, 逐日传 [j for j in lu_all if j < i] (as_of 内不重算);
  - run 输出 trades 含 tech_score 等字段, 与 tmp/ 基线 JSON 字段对齐供逐笔对数。
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict

from app.market_cn.auto.common.filters import unified_prefilter
from app.market_cn.auto.common.market import find_limit_ups, get_board_type, is_limit_up
from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)
from app.market_cn.auto.core import (
    break_today_d0_signals,
    dragon_cb_today_d0_signals,
    run_backtest_dragon_callback,
    v1_today_d0_signals,
)

# ================================================================
# 出场引擎 (2026-09-10 自 dragon_core.py 迁入 —— 出场模拟属回测流水线, 不属判定核心)
# 迁移为逐字搬运, 行为零差异; 350笔回归基线验证见 tmp/。
# ================================================================

# 跌停价原语收编至 common/exec_cn.py (C 阶段); 别名保持引擎内部调用点不变

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


def is_st_stock(code):
    """显式 ST 过滤 (与 test_dragon 一致): 无股票名称数据时依赖涨停阈值自然排除。"""
    return False


def _find_bar_idx(bars, date_str):
    for i, b in enumerate(bars):
        if b["time"] == date_str:
            return i
    return None


# ================================================================
# 龙回头: 全历史枚举 + 逐笔回测 (编排移植自 test_dragon.strategy_dragon_callback,
# 判定/引擎改调系统侧; 逐字等价由全市场对数 tmp/test_dragon_callback_result_all.json 验证)
# ================================================================

DRAGON_CB_DEFAULTS = dict(hold_days=7, stop_loss=-8.0)


def backtest_dragon_stock(bars, code, hold_days=7, stop_loss=-8.0,
                          stock_info=None, use_prefilter=True):
    """单股龙回头全历史回测, 返回 trades 列表 (字段与基线 JSON 对齐)。"""
    board_type = get_board_type(code)
    n = len(bars)
    if n < 5:
        return []
    lu_all = find_limit_ups(bars, board_type)
    trades = []
    used_ranges = []

    for i in range(2, n - 1):
        # 逐日候选判定: 与实盘 scan 完全同一函数 (切片 as_of 语义)
        sigs = dragon_cb_today_d0_signals(
            bars[:i + 1], code,
            limit_ups=[j for j in lu_all if j < i])

        if not sigs:
            continue
        sig = sigs[0]
        lu_idx = _find_bar_idx(bars, sig["lu_date"])

        # 去重 (±4天内跳过); 注意去重在过滤之前 (对数基线行为)
        skip = False
        for (s, e) in used_ranges:
            if abs(i - s) <= 4 or abs(i - e) <= 4:
                skip = True
                break
        if skip:
            continue
        used_ranges.append((lu_idx, i))

        # U1~U4 预过滤 (锚定涨停日, 无未来函数)
        if use_prefilter and lu_idx > 0:
            ok, fails = unified_prefilter(bars, lu_idx, code, stock_info)
            if not ok:
                continue

        # 入场: 次日(D+1)开盘价
        d0 = bars[i]
        d1 = bars[i + 1]
        d1_gap = (d1["open"] / d0["close"] - 1) * 100 if d0["close"] > 0 else 0
        entry_price = d1["open"]
        if entry_price <= 0:
            continue

        result = run_backtest_dragon_callback(
            bars, i + 1, entry_price, hold_days=hold_days, stop_loss=stop_loss,
            board_type=board_type)
        if not result:
            continue

        trades.append({
            **sig,
            "entry_date": d1["time"],
            "entry_price": round(entry_price, 3),
            "buy_mode": "next_open",
            "d1_gap": round(d1_gap, 2),
            **result,
        })

    return trades


# ================================================================
# V1: 全历史枚举 + 逐笔回测 (编排移植自 test_dragon.strategy_v1)
# ================================================================

V1_DEFAULTS = dict(hold_days=7, stop_loss=-10.0, trailing_stop=-5.0,
                   ret_20d_min=30.0, d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                   obv_filter=True, d_1_vol_max=1.5)


def backtest_v1_stock(bars, code, hold_days=7, stop_loss=-10.0, trailing_stop=-5.0,
                      ret_20d_min=30.0, d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                      obv_filter=True, d_1_vol_max=1.5,
                      stock_info=None, use_prefilter=True):
    """单股 V1 全历史回测 (D0四因子判定, 次日开盘买, D1入场过滤)。"""
    board_type = get_board_type(code)
    n = len(bars)
    if n < 30:
        return []
    trades = []

    for i in range(25, n - 1):
        sigs = v1_today_d0_signals(
            bars[:i + 1], code,
            ret_20d_min=ret_20d_min,
            d_1_pullback_min=d_1_pullback_min,
            d_1_pullback_max=d_1_pullback_max,
            obv_filter=obv_filter,
            d_1_vol_max=d_1_vol_max,
            stock_info=stock_info)
        if not sigs:
            continue
        sig = sigs[0]

        # U1~U4 (信号日D0收盘可知; 20日涨幅>=30%已隐含U4)
        if use_prefilter:
            ok, fails = unified_prefilter(bars, i, code, stock_info)
            if not ok:
                continue

        # 入场: 次日开盘价 + D1当日过滤
        d0 = bars[i]
        d1 = bars[i + 1]
        entry_price = d1["open"]
        if entry_price <= 0:
            continue
        entry_idx = i + 1
        entry_date = d1["time"]
        d1_change = (d1["close"] / d0["close"] - 1) * 100
        d1_gap = (d1["open"] / d0["close"] - 1) * 100
        min_d1_gap = -3.0 if board_type == "main" else -5.0
        if d1_gap < min_d1_gap:
            continue
        if d1_change < 0:
            continue
        if board_type == "gem_star" and d1_gap >= 5.0:
            continue
        # 主板高开3%~5%不入场 (v4数据驱动)
        if board_type == "main" and 3.0 <= d1_gap < 5.0:
            continue

        d1_limit_up_val = is_limit_up(d1["close"], d0["close"], board_type)
        bt = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss,
                          trailing_stop, board_type, is_v1=True,
                          d1_limit_up=d1_limit_up_val, d1_change=d1_change,
                          d1_gap=d1_gap)
        if not bt:
            continue

        trades.append({
            **sig,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 3),
            "buy_mode": "next_open",
            "d1_change": round(d1_change, 2),
            "d1_gap": round(d1_gap, 2),
            "intraday": round(d1_change - d1_gap, 2),
            **bt,
        })

    return trades


# ================================================================
# 断板: 全历史枚举 + 逐笔回测 (编排移植自 test_dragon.strategy_break_buy)
# ================================================================

def backtest_break_stock(bars, code, min_streak=2, max_break_gap=5, override_params=None,
                         stock_info=None, use_prefilter=True):
    """单股断板全历史回测 (断板期确认日判定, 次日开盘买)。"""
    bt_type = get_board_type(code)
    params = dict(BOARD_PARAMS[bt_type])
    if override_params:
        params.update(override_params)
    stop_loss, trailing_stop = params["stop_loss"], params["trailing_stop"]
    hold_days = params["hold_days"]
    n = len(bars)
    if n < 6:
        return []
    lu_all = find_limit_ups(bars, bt_type)
    lu_set = set(lu_all)
    trades = []
    used = set()

    for i in range(4, n - 1):
        # 确认日必为非涨停日 (断板期最后一天)
        if is_limit_up(bars[i]["close"], bars[i - 1]["close"], bt_type):
            continue
        # 廉价预过滤: 断板期结束于i → 必存在距i不超过max_break_gap的涨停日
        if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
            continue
        sigs = break_today_d0_signals(
            bars[:i + 1], code,
            min_streak=min_streak, max_break_gap=max_break_gap,
            limit_ups=[j for j in lu_all if j < i],
            stock_info=stock_info)
        if not sigs:
            continue
        sig = sigs[0]

        # 去重: 同一连板起点+断板日只取一次 (去重在过滤之前, 对数基线行为)
        key = (sig["streak_start"], sig["break_date"])
        if key in used:
            continue
        used.add(key)

        # U1~U4 (确认日D0收盘可知; 连板>=2已隐含U4)
        if use_prefilter:
            ok, fails = unified_prefilter(bars, i, code, stock_info)
            if not ok:
                continue

        # 入场: 次日(D+1)开盘价
        entry_price = bars[i + 1]["open"]
        if entry_price <= 0:
            continue
        result = run_backtest_breakbuy(bars, i + 1, entry_price, hold_days,
                                       stop_loss, trailing_stop, bt_type)
        if not result:
            continue

        prev_close = bars[i]["close"]
        trades.append({
            **sig,
            "signal_date": bars[i]["time"],
            "entry_date": bars[i + 1]["time"],
            "entry_price": round(entry_price, 3),
            "buy_mode": "next_open",
            "d1_change": round((bars[i + 1]["close"] / bars[i + 1]["open"] - 1) * 100, 2)
            if bars[i + 1]["open"] > 0 else 0,
            "d1_gap": round((bars[i + 1]["open"] / prev_close - 1) * 100, 2)
            if prev_close > 0 else 0,
            "intraday": round((bars[i + 1]["close"] - bars[i + 1]["open"]) / prev_close * 100, 2)
            if prev_close > 0 else 0,
            **result,
        })

    return trades


# ================================================================
# 全市场流水线
# ================================================================

def run_all(strategy="dragon", days=300, codes=None, stock_info=None,
            use_prefilter=True, progress_every=500):
    """全市场回测。strategy: dragon (v1/break 待扩)。

    返回 {"trades": [...], "stats": {...}}; trades 直接可 json.dump 与基线对数。
    """
    from app.market_cn.auto.data.hub import all_codes, daily
    from app.market_cn.auto.data.hub import stock_info as _stock_info

    if codes is None:
        codes = all_codes()
    if stock_info is None:
        try:
            stock_info = _stock_info()  # U1~U3 依赖 (缺失则跳过, 会放行)
        except Exception:
            stock_info = {}
    t0 = time.time()
    trades = []
    n_ok = 0
    for k, code in enumerate(codes, 1):
        if is_st_stock(code):
            continue
        bars = daily(code, days)
        if not bars:
            continue
        if strategy == "dragon":
            trades.extend(backtest_dragon_stock(
                bars, code, stock_info=stock_info.get(code) if stock_info else None,
                use_prefilter=use_prefilter))
        elif strategy == "v1":
            trades.extend(backtest_v1_stock(
                bars, code, stock_info=stock_info.get(code) if stock_info else None,
                use_prefilter=use_prefilter))
        elif strategy == "break":
            trades.extend(backtest_break_stock(
                bars, code, stock_info=stock_info.get(code) if stock_info else None,
                use_prefilter=use_prefilter))
        else:
            raise ValueError(f"strategy={strategy} 未实现")
        n_ok += 1
        if progress_every and k % progress_every == 0:
            print(f"[{k}/{len(codes)}] trades={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return {"trades": trades, "stats": _summary(trades), "codes_ok": n_ok,
            "elapsed": round(time.time() - t0, 1)}


def _summary(trades):
    """标准报告 (对齐设计文档 §6.1 + 五高指标映射, 09-09 B-5)。

    字段: 笔数/胜率/均收/盈亏比/收益五分桶/20日峰值分布与均值/月均笔数(可操作性)/
         日均收益(单位时间收益比=均收益÷持有交易日)/前后两段分段稳定性。
    """
    if not trades:
        return {"n": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    buckets = {"≤-10": 0, "-10~-3": 0, "-3~+3": 0, "+3~+10": 0, ">+10": 0}
    for r in rets:
        k = "≤-10" if r <= -10 else "-10~-3" if r <= -3 else \
            "-3~+3" if r < 3 else "+3~+10" if r < 10 else ">+10"
        buckets[k] += 1
    peaks = [t["peak_return_pct"] for t in trades if t.get("peak_return_pct") is not None]
    months = sorted({str(t.get("entry_date", ""))[:7] for t in trades} - {""})
    n_h = len(trades) // 2
    seg = lambda ts: round(sum(1 for t in ts if t["return_pct"] > 0) / len(ts) * 100, 1) if ts else None
    hold_days_avg = sum(t.get("exit_day") or 0 for t in trades) / len(trades)
    return {
        "n": len(trades),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "pl_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
        if wins and losses else None,
        "ret_buckets": buckets,
        "peak": {"mean": round(sum(peaks) / len(peaks), 2) if peaks else None,
                 "lt10": sum(1 for p in peaks if p < 10),
                 "10_20": sum(1 for p in peaks if 10 <= p < 20),
                 "ge20": sum(1 for p in peaks if p >= 20)},
        "monthly_avg": round(len(trades) / len(months), 1) if months else None,
        "ret_per_day": round(sum(rets) / len(rets) / hold_days_avg, 3) if hold_days_avg else None,
        "winrate_1st_half": seg(trades[:n_h]),
        "winrate_2nd_half": seg(trades[n_h:]),
    }


if __name__ == "__main__":
    import argparse
    import json
    import os

    # CLI 直跑时需自行加载 .env (服务进程已由应用加载, 重复加载无害)
    try:
        from dotenv import load_dotenv
        for _p in [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))), ".env"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="框架内全市场回测流水线")
    parser.add_argument("--strategy", default="dragon", choices=["dragon", "v1", "break"])
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--codes", default="", help="逗号分隔, 空则全市场")
    parser.add_argument("--out", default="", help="结果JSON输出路径 (对数用)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    res = run_all(strategy=args.strategy, days=args.days, codes=codes)
    print("统计:", res["stats"], "| codes_ok:", res["codes_ok"],
          "| 耗时:", res["elapsed"], "s")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res["trades"], f, ensure_ascii=False)
        print("已写出:", args.out)
