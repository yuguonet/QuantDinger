#!/usr/bin/env python3
"""龙回头Pro 核心判定逻辑 —— 单一事实源 (single source of truth)

由 test_dragon.py 程序化提取生成 (2026-09-03), 与回测脚本共用同一份判定代码:
  - test_dragon.py (--strategy dragon2)  : 回测
  - app/market_cn/auto/dragon_scan.py    : 盘后全市场扫描 (16:30)
  - app/market_cn/auto/dragon_monitor.py : 盘中状态机 (60s)

本模块保持零 IO / 零 print, 只做纯判定。
修改任何规则后必须重跑回测对数 (基线: 120日 d1open 2437笔 37.2%/-0.35%/+4.62%)。
"""
from __future__ import annotations

def get_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

# ================================================================
# 核心逻辑
# ================================================================

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

# 龙回头Pro (dragon2) — 八因子综合评分版
# ================================================================
# 用户经验特征 → 量化规则映射:
#   特征1 换手率活跃(>5%)   → 锚点日/信号日换手率(成交量/流通股本)评分, 锚点换手>=3%硬门槛
#   特征2 市值20亿~300亿    → 流通市值(circ_shares×close)评分, 10亿~500亿硬门槛
#   特征3 最近涨停/大涨     → 锚点日: 涨停 或 单日涨幅>=7%(主板)/>=12%(创科板)
#   特征4 回调3-11天        → 锚点后连续收盘<锚点收盘 3~11天 (与旧龙回头一致)
#   特征5 MA60持平/向上     → MA60五日斜率>=-1%硬门槛, >=0评分; MA10/20上攻+多头排列评分
#   特征6 回调支撑明显      → 低点触及MA10/MA20且守住 / 不破锚点开盘价 评分; 最大回撤硬门槛
#   特征7 上攻明显放量      → 两种入场: (a)缩量小阴企稳次日买 (b)放量启动日次日买;
#                             D1强确认(涨>=3%且量>=1.5x)延长持仓骑连板, 弱确认D2开盘清仓
#   特征8 龙头少ST          → 股票名称含 ST/退 直接剔除
# 连板特点 → 出场: 涨停日豁免追踪止损(骑板); 连板>=2后断板日尾盘卖;
#                  巨量阴线/天量滞涨尾盘卖(出货一般放巨量); 峰值逃顶保留
# as-of安全: 所有判定只用<=当日收盘数据, 与--today报告共用同一判定函数

DRAGON2_PARAMS = dict(
    # --- 基础形态 ---
    min_pullback_days=3, max_pullback_days=11,
    last_chg_min_abs=0.5,       # (a)信号日小阴下限(绝对值)
    max_last_chg=3.0,           # (a)信号日小阴上限
    vol_r_lo=0.5, vol_r_hi=0.8, # (a)信号日量比区间(缩量)
    b_min_chg=2.0,              # (b)启动日最小涨幅
    b_min_vol_r=1.5,            # (b)启动日最小量比(比前一天明显放量)
    big_gain_main=7.0, big_gain_gem=12.0,   # 大涨锚点阈值
    use_big_gain_anchor=True,
    entry_gap_a=(-3.0, 2.0),    # (a)D1开盘涨幅可买区间(d1open模式)
    entry_gap_b=(-3.5, 5.5),    # (b)D1开盘涨幅可买区间(d1open模式)
    use_mode_a=True, use_mode_b=True,
    # --- 入场模式 ---
    entry_mode='confirm',       # 'confirm'=D1确认后D2开盘买(默认) | 'd1open'=D1开盘直接买(旧口径)
    allow_ok_confirm=False,     # confirm模式: D1中性确认是否也买 (False=只要强确认)
    entry_gap_confirm=(-5.0, 6.0),  # confirm模式: D2开盘涨幅可买区间
    # --- 评分门槛 ---
    min_score=12,
    turnover_hot=10.0, turnover_min=5.0, turnover_hard=3.0,
    mcap_lo=20.0, mcap_hi=300.0, mcap_hard_lo=10.0, mcap_hard_hi=500.0,  # 亿
    ma60_slope_hard=-1.0,
    drawdown_max_main=-15.0, drawdown_max_gem=-20.0,
    anchor_vol_hot=1.5, anchor_vol_min=1.2,
    # --- 出场 ---
    stop_main=-5.0, trail_main=-5.0, stop_gem=-7.0, trail_gem=-7.0,
    hold_days=7, hold_strong=10,
    d1_weak_chg=1.5, d1_weak_vol=1.5,       # D1弱确认: 收阴 或 (涨<1.5%且量<1.5x) → D2开盘清仓
    d1_strong_chg=3.0, d1_strong_vol=1.5,   # D1强确认: 涨>=3%且量>=1.5x → 骑连板模式
    ride_streak=2,              # 持仓中连板数达到2 → 骑板模式(断板尾盘卖)
    big_vol_prev_r=2.8,         # 巨量出货: 量/前一日量
    big_vol_ma5_r=3.5,          # 巨量出货: 量/5日均量 + 滞涨
    big_vol_exit=True,
)


def _sma(closes, period):
    """最近period根简单均线, 数据不足返回None"""
    if closes is None or len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def _sma_at(closes, end_offset, period):
    """截至 end_offset 天前的period均线 (end_offset=0 → 最近一根)"""
    if closes is None:
        return None
    j = len(closes) - end_offset
    if j < period:
        return None
    return sum(closes[j - period:j]) / period


def run_backtest_dragon2(bars, entry_idx, entry_price, board_type="main",
                         sig_close=0.0, sig_vol=0.0, entry_style="a",
                         params=None, stop_at_idx=None,
                         entry_mode=None, pre_d1_chg=None, pre_d1_vol_r=None,
                         pre_d1_confirm=None):
    """龙回头Pro出场模拟 (as-of安全, 供回测与--today持仓重算共用)

    每日出场判定顺序:
      d=1 收盘: D1确认评估 (仅d1open模式; confirm模式下入场前已完成确认, 跳过)
      d>=2:
        1) D1弱确认 → 开盘清仓 (仅d1open模式)
        2) 追踪止损 (当日涨停豁免 → 骑板; 创科板阈值更宽)
        3) 止损
        4) 收盘可知: 连板>=ride_streak后断板 → 尾盘卖
                      巨量出货(放量阴线/天量滞涨) → 尾盘卖
                      峰值逃顶(涨>7%大上影线) → 尾盘卖
        5) 兕底: 持仓到期收盘卖 / 数据截断返回open=True
    stop_at_idx: 只模拟到该bar索引(--today用); 未触发出场 → open=True
    entry_mode='confirm': 入场前已完成D1确认, pre_d1_*传入确认结果,
                          强确认仍延长持仓(hold_strong), 弱确认不会出现(入场前已过滤)
    """
    p = params or DRAGON2_PARAMS
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    is_gem = board_type == "gem_star"
    stop = p['stop_gem'] if is_gem else p['stop_main']
    trail = p['trail_gem'] if is_gem else p['trail_main']
    hold = p['hold_days']
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ''
    d1_chg = None
    d1_vol_r = None
    d1_confirm = None
    weak_exit = False
    strong = False
    streak = 0
    max_streak = 0
    prev_close = sig_close if sig_close > 0 else entry_price
    vol_hist = []
    capped = False
    mode = entry_mode or p.get('entry_mode', 'confirm')

    d = 1
    while d <= hold:
        idx = entry_idx + d - 1
        if idx >= len(bars):
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']
        pc = bars[idx - 1]['close'] if idx > 0 else b['open']
        day_chg = (b['close'] / pc - 1) * 100 if pc > 0 else 0
        is_lu = is_limit_up(b['close'], pc, board_type)
        vol_hist.append(b['volume'])

        # --- d=1 收盘: D1确认 (仅d1open模式) ---
        if d == 1 and mode != 'confirm':
            d1_chg = (b['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
            d1_vol_r = (b['volume'] / sig_vol) if sig_vol > 0 else None
            vr = d1_vol_r if d1_vol_r is not None else 9.9
            if d1_chg >= p['d1_strong_chg'] and vr >= p['d1_strong_vol']:
                d1_confirm = 'strong'
                strong = True
                hold = max(hold, p['hold_strong'])   # 强确认延长持仓, 给连板空间
            elif d1_chg < 0 or (d1_chg < p['d1_weak_chg'] and vr < p['d1_weak_vol']):
                d1_confirm = 'weak'
                weak_exit = True
            else:
                d1_confirm = 'ok'
        elif d == 1 and mode == 'confirm':
            # 入场前已确认: 回填字段, 强确认延长持仓
            d1_chg = pre_d1_chg
            d1_vol_r = pre_d1_vol_r
            d1_confirm = pre_d1_confirm or 'ok'
            if d1_confirm == 'strong':
                strong = True
                hold = max(hold, p['hold_strong'])

        # --- 弱确认: d=2 开盘清仓 (仅d1open模式) ---
        if weak_exit and d == 2 and mode != 'confirm':
            exit_p, exit_d, exit_reason = b['open'], d, 'D1弱确认,D2开盘清仓'
            break

        # --- 追踪止损 (涨停日豁免, 骑板) ---
        if d > 1 and not is_lu and b['low'] <= peak * (1 + trail / 100):
            exit_p, exit_d, exit_reason = peak * (1 + trail / 100), d, f'追踪止损{trail}%'
            break
        # --- 止损 ---
        if b['low'] <= entry_price * (1 + stop / 100):
            exit_p, exit_d, exit_reason = entry_price * (1 + stop / 100), d, f'止损{stop}%'
            break

        # --- 收盘可知出场 ---
        if is_lu:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            if streak >= p['ride_streak']:
                # 连板后断板 → 尾盘卖 (一致低量结束/开板出货预期)
                exit_p, exit_d, exit_reason = b['close'], d, f'连板{streak}后断板尾盘卖'
                break
            streak = 0
            if p.get('big_vol_exit', True) and d >= 2:
                prev_v = bars[idx - 1]['volume'] if idx > 0 else 0
                prev5 = vol_hist[-6:-1]
                ma5_v = sum(prev5) / len(prev5) if prev5 else 0
                big1 = prev_v > 0 and b['volume'] >= prev_v * p['big_vol_prev_r'] and b['close'] < b['open']
                big2 = ma5_v > 0 and b['volume'] >= ma5_v * p['big_vol_ma5_r'] and day_chg < 3.0
                if big1 or big2:
                    exit_p, exit_d, exit_reason = b['close'], d, '巨量出货尾盘卖'
                    break
            ret_e = (b['close'] / entry_price - 1) * 100
            if ret_e > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p, exit_d, exit_reason = b['close'], d, '峰值逃顶'
                    break

        # 兕底: 记录当日收盘 (无信号时)
        exit_p, exit_d = b['close'], d
        d += 1

    if exit_reason == '' and not capped:
        exit_reason = '持仓到期' if d > hold else '数据结束'
    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_chg': round(d1_chg, 2) if d1_chg is not None else None,
        'd1_vol_r': round(d1_vol_r, 2) if d1_vol_r is not None else None,
        'd1_confirm': d1_confirm,
        'weak_exit': weak_exit,
        'max_streak': max_streak,
        'open': bool(capped),
    }


def dragon2_today_d0_signals(bars, code, stock_info=None, today_str=None, params=None):
    """龙回头Pro 今日(D0)信号: 逐日只用当日收盘可知数据, 与回测共用

    两种入场形态 (同日互斥, 取评分高者):
      (a)缩量企稳: D0=回调末期缩量小阴(-3%~-0.5%), 量比[0.5,0.8) → 次日开盘买
      (b)放量启动: D-1及之前为回调, D0收阳涨>=2%且量比>=1.5x → 次日开盘买
    共同前置: 锚点日(涨停/大涨)后连续回调3~11天 + 八因子评分>=min_score + 硬门槛。
    返回0~1个信号dict。
    """
    result = []
    p = params or DRAGON2_PARAMS
    n = len(bars)
    if n < 66:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1
    if i < 65:
        return result
    board_type = get_board_type(code)
    is_gem = board_type == 'gem_star'
    limit_thr = 0.198 if is_gem else 0.098

    # 特征8: ST/退市风险剔除
    if stock_info:
        _name = (stock_info.get('name') or '')
        if 'ST' in _name.upper() or '退' in _name:
            return result

    info = stock_info or {}
    circ = float(info.get('circ_shares') or 0)

    d0 = bars[i]
    prev_c = bars[i - 1]['close']
    if prev_c <= 0 or d0['close'] <= 0:
        return result
    last_chg = (d0['close'] / prev_c - 1) * 100
    prev_vol = bars[i - 1]['volume']
    sig_vol_r = d0['volume'] / prev_vol if prev_vol > 0 else 0

    closes = [b['close'] for b in bars]
    # --- 特征5: 均线结构 ---
    ma60_now = _sma(closes, 60)
    ma60_5ago = _sma_at(closes, 5, 60)
    ma10_now = _sma(closes, 10)
    ma10_3ago = _sma_at(closes, 3, 10)
    ma20_now = _sma(closes, 20)
    ma20_3ago = _sma_at(closes, 3, 20)
    ma5_now = _sma(closes, 5)
    ma60_slope = None
    if ma60_now and ma60_5ago and ma60_5ago > 0:
        ma60_slope = (ma60_now / ma60_5ago - 1) * 100
        if ma60_slope < p['ma60_slope_hard']:
            return result   # MA60深度下行 → 硬性剔除

    # --- 特征3: 锚点日(涨停或大涨) — 从最近往回扫, 收集全部候选, 取评分最高 ---
    big_gain_thr = p['big_gain_gem'] if is_gem else p['big_gain_main']
    lu_thr = limit_thr * 0.98
    maxpb = p['max_pullback_days']
    lo_idx = max(1, i - 1 - maxpb)
    cands = []
    for a in range(i - 1, lo_idx - 1, -1):
        a_prev = bars[a - 1]['close']
        if a_prev <= 0:
            continue
        anchor_chg = (bars[a]['close'] / a_prev - 1) * 100
        is_lu_day = anchor_chg >= lu_thr
        is_big = p.get('use_big_gain_anchor', True) and anchor_chg >= big_gain_thr
        if not (is_lu_day or is_big):
            continue
        anchor_close = bars[a]['close']
        anchor_open = bars[a]['open']
        anchor_vol = bars[a]['volume']
        if anchor_close <= 0 or anchor_vol <= 0:
            continue
        pb_a = i - a            # (a)回调天数(含今日)
        pb_b = i - 1 - a        # (b)回调天数(到昨日)
        ok_a = (p.get('use_mode_a', True) and p['min_pullback_days'] <= pb_a <= maxpb
                and all(bars[j]['close'] < anchor_close for j in range(a + 1, i + 1)))
        ok_b = (p.get('use_mode_b', True) and p['min_pullback_days'] <= pb_b <= maxpb
                and all(bars[j]['close'] < anchor_close for j in range(a + 1, i)))
        if not ok_a and not ok_b:
            continue
        modes = []
        if ok_a:
            modes.append(('a', pb_a, i))
        if ok_b:
            modes.append(('b', pb_b, i - 1))
        for style, pullback_days, pb_end in modes:
            # 信号日形态
            if style == 'a' and not (-p['max_last_chg'] < last_chg < -p['last_chg_min_abs']
                                     and p['vol_r_lo'] <= sig_vol_r < p['vol_r_hi']):
                continue
            if style == 'b' and not (last_chg >= p['b_min_chg'] and sig_vol_r >= p['b_min_vol_r']
                                     and d0['close'] > d0['open']):
                continue
            # 回调窗口统计
            pb_lows = [bars[j]['low'] for j in range(a + 1, pb_end + 1)]
            pb_vols = [bars[j]['volume'] for j in range(a + 1, pb_end + 1)]
            pb_low = min(pb_lows) if pb_lows else 0
            pb_vol_avg = sum(pb_vols) / len(pb_vols) if pb_vols else 0
            # 回撤硬门槛 (特征6: 支撑明显=回调不深)
            dd = (pb_low / anchor_close - 1) * 100 if anchor_close > 0 else 0
            dd_max = p['drawdown_max_gem'] if is_gem else p['drawdown_max_main']
            if dd < dd_max:
                continue
            score = 0
            # --- 特征1 换手率 (双口径: 流通股本 / 总股本) ---
            turn_anchor = turn_sig = None
            total_sh = float(info.get('total_shares') or 0)
            turn_anchor_t = turn_sig_t = None
            if circ > 0:
                turn_anchor = anchor_vol / circ * 100
                turn_sig = d0['volume'] / circ * 100
                if total_sh > 0:
                    turn_anchor_t = anchor_vol / total_sh * 100
                    turn_sig_t = d0['volume'] / total_sh * 100
                if turn_anchor < p['turnover_hard']:
                    continue        # 锚点换手不足 → 不活跃, 剔除
                if turn_anchor >= p['turnover_hot']:
                    score += 3
                elif turn_anchor >= p['turnover_min']:
                    score += 2
                else:
                    score += 1
                if turn_sig is not None:
                    if turn_sig >= (p['turnover_min'] if style == 'b' else 2.0):
                        score += 1
            else:
                score += 1          # 股本缺失 → 中性
            # --- 特征2 流通市值 ---
            mcap_yi = None
            if circ > 0:
                mcap_yi = circ * d0['close'] / 1e8
                if p['mcap_lo'] <= mcap_yi <= p['mcap_hi']:
                    score += 3
                elif p['mcap_hard_lo'] <= mcap_yi <= p['mcap_hard_hi']:
                    score += 1
                else:
                    continue        # 市值出硬门槛范围 → 剔除
            else:
                score += 1
            # --- 特征5 MA60斜率 + 均线结构 ---
            if ma60_slope is None:
                score += 2
            elif ma60_slope >= 0.3:
                score += 3
            elif ma60_slope >= 0:
                score += 2
            elif ma60_slope >= -0.5:
                score += 1
            ma10_up = ma10_now is not None and ma10_3ago is not None and ma10_now > ma10_3ago
            ma20_up = ma20_now is not None and ma20_3ago is not None and ma20_now > ma20_3ago
            ma10_gt_ma20 = ma10_now is not None and ma20_now is not None and ma10_now > ma20_now
            ma_bull = bool(ma5_now and ma10_now and ma20_now and ma5_now > ma10_now > ma20_now)
            score += (1 if ma10_up else 0) + (1 if ma20_up else 0) \
                + (1 if ma10_gt_ma20 else 0) + (1 if ma_bull else 0)
            # --- 特征6 支撑 ---
            ma_touch = ((ma10_now and pb_low <= ma10_now * 1.02)
                        or (ma20_now and pb_low <= ma20_now * 1.02))
            ma_held = (d0['close'] >= ma20_now * 0.99) if ma20_now else True
            sup_ma = bool(ma_touch and ma_held)
            sup_anchor_open = pb_low >= anchor_open
            if sup_ma:
                score += 2
            if sup_anchor_open:
                score += 2
            # --- 启动放量 (锚点日量 vs 5日均量) ---
            if a >= 5:
                pre5 = [bars[j]['volume'] for j in range(a - 5, a)]
                pre5_avg = sum(pre5) / 5 if pre5 else 0
                anchor_vol_r = anchor_vol / pre5_avg if pre5_avg > 0 else 0
            else:
                anchor_vol_r = 0
            if anchor_vol_r >= p['anchor_vol_hot']:
                score += 2
            elif anchor_vol_r >= p['anchor_vol_min']:
                score += 1
            # --- 回调质量 ---
            if pb_vol_avg > 0 and anchor_vol > 0 and pb_vol_avg <= anchor_vol * 0.7:
                score += 1
            big_red = any(bars[j]['volume'] >= anchor_vol and bars[j]['close'] < bars[j]['open']
                          for j in range(a + 1, pb_end + 1))
            if not big_red:
                score += 1
            # --- 位置 ---
            ratio = d0['close'] / anchor_close if anchor_close > 0 else 0
            if style == 'a' and 0.90 <= ratio <= 0.995:
                score += 1
            if style == 'b' and ratio >= 1.0:
                score += 1
            cands.append({
                'code': code, 'board': get_board_name(code),
                'path': 'dragon2', 'path_label': '龙回头Pro',
                'style': style,
                'entry_style': '(a)缩量企稳' if style == 'a' else '(b)放量启动',
                'lu_date': bars[a]['time'],
                'anchor_chg': round(anchor_chg, 2),
                'anchor_vol_r': round(anchor_vol_r, 2),
                'pullback_days': pullback_days,
                'signal_date': d0['time'],
                'signal_chg': round(last_chg, 2),
                'signal_vol_r': round(sig_vol_r, 2),
                'signal_price': round(d0['close'], 3),
                'sig_vol': d0['volume'],
                'buy_mode': 'next_open',
                'score': score,
                'turnover_anchor': round(turn_anchor, 2) if turn_anchor is not None else None,
                'turnover_anchor_total': round(turn_anchor_t, 2) if turn_anchor_t is not None else None,
                'turnover_sig_total': round(turn_sig_t, 2) if turn_sig_t is not None else None,
                'turnover_sig': round(turn_sig, 2) if turn_sig is not None else None,
                'float_mcap_yi': round(mcap_yi, 1) if mcap_yi is not None else None,
                'ma60_slope': round(ma60_slope, 2) if ma60_slope is not None else None,
                'ma_bull': ma_bull,
                'support_ma': sup_ma,
                'support_anchor_open': sup_anchor_open,
                'pullback_drawdown': round(dd, 2),
                'anchor_type': 'LU' if is_lu_day else 'BIG',
                'sig_vs_anchor': round(ratio * 100, 2),
                'sig_vs_ma10': round((d0['close'] / ma10_now - 1) * 100, 2) if ma10_now else None,
                'sig_vs_ma20': round((d0['close'] / ma20_now - 1) * 100, 2) if ma20_now else None,
                'pullback_close_min_ratio': round(min(bars[j]['close'] for j in range(a + 1, pb_end + 1)) / anchor_close * 100, 2) if pb_end > a else None,
            })
    if not cands:
        return result
    best = max(cands, key=lambda c: c['score'])
    if best['score'] < p['min_score']:
        return result
    result.append(best)
    return result


def _dragon2_d1_confirm(bars, i, sig, p):
    """评估D1确认日强度: strong/ok/weak (只用D1收盘可知数据)"""
    d0c, d1 = bars[i], bars[i + 1]
    if d0c['close'] <= 0:
        return None, None, None
    d1_chg = (d1['close'] / d0c['close'] - 1) * 100
    d1_vol_r = d1['volume'] / sig['sig_vol'] if sig.get('sig_vol', 0) > 0 else None
    vr = d1_vol_r if d1_vol_r is not None else 9.9
    if d1_chg >= p['d1_strong_chg'] and vr >= p['d1_strong_vol']:
        d1_confirm = 'strong'
    elif d1_chg < 0 or (d1_chg < p['d1_weak_chg'] and vr < p['d1_weak_vol']):
        d1_confirm = 'weak'
    else:
        d1_confirm = 'ok'
    return d1_confirm, round(d1_chg, 2), round(d1_vol_r, 2) if d1_vol_r is not None else None


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
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False},
}


def run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

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

        # V1出场 (v3): 日内动量<0 → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        #   <0: 盘中出货, D2大概率续跌, 100%捕获D2跌>3%的信号
        #   >=0: 盘中有买盘承接, 继续持有
        # 注: -10%止损已移除, 日内动量规则在D2开盘即清仓, 不需要等止损位
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
            d1_weak = intraday < 3
            if d1_weak:
                # D2开盘直接清仓, 不等止损位
                exit_p = b['open']; exit_d = d; break

        # 1 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
        if peak_exit:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p = b['close']; exit_d = d; break

        # 2 追踪止损
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        # 3 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # 4 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

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
    """断板专用回测: 追踪止损 + 峰值逃顶信号"""
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

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

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0

        # 止损
        if ret <= stop_loss:
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # 追踪止损 (盈利时)
        if ret_from_high <= trailing_stop and ret > 0:
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break

        # 峰值信号: 涨>10%后大上影线(>40%)→收盘逃顶
        if ret > 10:
            bar_range = b['high'] - b['low']
            upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
            if upper > 40 and b['close'] < b['high'] * 0.98:
                exit_p = b['close']; exit_d = d; break

        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


def _ma_bull_at(bars, ci):
    """确认日均线多头排列: MA5>MA10>MA20 (ci=确认日索引); 数据不足(上市<20日)返回None"""
    if ci + 1 < 20:
        return None
    c = [float(b['close']) for b in bars[ci - 19:ci + 1]]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c) / 20
    return ma5 > ma10 > ma20


def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """给定连板区间[streak_start,streak_end], 计算断板期并执行回测的5a-5e确认。

    与 strategy_break_buy 中"买点之前"的判定完全一致(同一段代码), 供回测和
    break_today 今日检测共用, 保证 --today 与回测逻辑严格对齐。
    返回信号dict(含 break_date/break_days/break_chg/break_gap/break_vol_r)或 None。
    """
    bt = get_board_type(code)
    streak_len = streak_end - streak_start + 1
    if streak_len < min_streak:
        return None

    # 断板期: 涨停日后连续非涨停的天数
    break_idx = streak_end + 1
    if break_idx >= len(bars):
        return None
    limit_bar = bars[streak_end]
    limit_open = float(limit_bar['open'])
    limit_close = float(limit_bar['close'])
    limit_vol = float(limit_bar['volume'])
    break_days = 0
    for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
        if is_limit_up(bars[j]['close'], bars[j - 1]['close'], bt):
            break  # 遇到新涨停, 断板期结束
        break_days += 1

    if break_days == 0:
        # 涨停后直接又是涨停 → 连板加速, 不是断板
        return None

    # 5. 断板期各项检查 (与 strategy_break_buy 完全一致)
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]

    # 5a. 断板期低点不能跌破涨停日开盘价 (支撑有效)
    break_low = min(float(b['low']) for b in break_bars)
    if break_low < limit_open:
        return None

    # 5b. 断板期缩量检查 (vs 涨停日量)
    break_vol_avg = sum(float(b['volume']) for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
    if break_vol_r < params['vol_min'] or break_vol_r >= params['vol_max']:
        return None

    # 5c. 第一个断板日涨跌过滤: vs 涨停日收盘, 允许 -5% ~ +8%
    first_break_chg = (first_break['close'] / limit_close - 1) * 100
    if first_break_chg < -5 or first_break_chg >= 8:
        return None

    # 5d. 第一个断板日开盘过滤: 高开不超过 5%, 低开不超过 3%
    first_break_gap = (first_break['open'] / limit_close - 1) * 100
    if first_break_gap < -3 or first_break_gap >= 5:
        return None

    # 5e. 回撤检查
    break_drawdown = (break_low / limit_close - 1) * 100
    if break_drawdown < params['drawdown_max']:
        return None

    # 5f. 确认日特征 + 增强过滤 (三通道OR, 满足其一即可)
    #     通道1: 确认日涨跌 [0,2)  通道2: 断板期均量比>=1.4  通道3: 连板前20日涨幅>=30 (热度)
    #     确认日 = 断板期最后一天; 特征仅用当日及以前数据, as-of 安全, 回测与 --today 共用本判定
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    _c_prev_close = float(confirm_prev['close'])
    confirm_chg = (float(confirm_bar['close']) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    confirm_gap = (float(confirm_bar['open']) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _pre_ref = float(bars[streak_start - 20]['close'])
        if _pre_ref > 0:
            pre20_gain = (limit_close / _pre_ref - 1) * 100
    if params.get('enhance_filter', True):
        _pass_chg = params.get('confirm_chg_min', 0.0) <= confirm_chg < params.get('confirm_chg_max', 2.0)
        _pass_vol = break_vol_r >= params.get('vol_r_or_min', 1.4)
        _pass_hot = pre20_gain is not None and pre20_gain >= params.get('pre20_min', 30.0)
        if not (_pass_chg or _pass_vol or _pass_hot):
            return None

    # 5g. 均线多头排列 (确认日 MA5>MA10>MA20): 剔除断板期处于均线纠缠/空头的弱信号
    ma_bull = _ma_bull_at(bars, break_idx + break_days - 1)
    if params.get('ma_bull_filter', True) and ma_bull is False:
        return None

    return {
        'break_idx': break_idx, 'break_days': break_days,
        'break_date': bars[break_idx]['time'],
        'streak_len': streak_len, 'streak_start': bars[streak_start]['time'], 'streak_end': bars[streak_end]['time'],
        'break_chg': round(first_break_chg, 2),
        'break_gap': round(first_break_gap, 2),
        'break_vol_r': round(break_vol_r, 2),
        'confirm_chg': round(confirm_chg, 2),
        'confirm_gap': round(confirm_gap, 2),
        'pre20_gain': round(pre20_gain, 2) if pre20_gain is not None else None,
        'ma_bull': ma_bull,
    }


def v1_today_d0_signals(bars, code, ret_20d_min=30.0,
                        d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                        obv_filter=True, d_1_vol_max=1.5, today_str=None,
                        stock_info=None):
    """V1 今日(D0)入场信号: 只检查D0四因子, 不依赖D1数据

    独立于策略回测, 仅用于 --today 报告中的「今日入场」段。
    满足D0四因子 → 下一个交易日开盘买入, D1入场规则(D1开收盘/回踩)开盘后由人工筛选。

    today_str: 指定今日日期(与--today-date一致), 为空则用最后一天。
    返回空list或单元素list, 元素含 d0_date/d0_close/ret_20d/d_1_change。
    """
    result = []
    n = len(bars)
    if n < 26:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1  # 最后一天视为今日(D0)
    if i < 2:
        return result
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    d0 = bars[i]
    d_1 = bars[i-1]
    d_2 = bars[i-2]
    if d_2['close'] <= 0 or d_1['close'] <= 0:
        return result
    if (d0['close'] / d_1['close'] - 1) < threshold * 0.98:
        return result

    # === 因子1: 强趋势 20日涨>ret_20d_min% ===
    if i < 20 or bars[i-20]['close'] <= 0:
        return result
    ret_20d = (d0['close'] / bars[i-20]['close'] - 1) * 100
    if ret_20d < ret_20d_min:
        return result

    # === 因子2: D-1回调 d_1_pullback_min~d_1_pullback_max% ===
    d_1_change = (d_1['close'] / d_2['close'] - 1) * 100
    if d_1_change < d_1_pullback_min or d_1_change >= d_1_pullback_max:
        return result

    # === 因子3: OBV 5日趋势上升 ===
    if obv_filter:
        obv = 0; obv_list = []
        for j in range(max(0, i-20), i+1):
            if j > 0:
                if bars[j]['close'] > bars[j-1]['close']:
                    obv += bars[j]['volume']
                elif bars[j]['close'] < bars[j-1]['close']:
                    obv -= bars[j]['volume']
            obv_list.append(obv)
        if len(obv_list) >= 5 and obv_list[-1] - obv_list[-5] <= 0:
            return result

    # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
    if i >= 6:
        vol_ma5_d1 = sum(bars[j]['volume'] for j in range(i-6, i-1)) / 5
        if vol_ma5_d1 > 0 and d_1['volume'] / vol_ma5_d1 >= d_1_vol_max:
            return result

    circ = float((stock_info or {}).get('circ_shares') or 0)
    total = float((stock_info or {}).get('total_shares') or 0)
    result.append({
        'code': code, 'board': get_board_name(code),
        'path': 'v1', 'path_label': 'V1',
        'd0_date': d0['time'],
        'd0_close': round(d0['close'], 3),
        'ret_20d': round(ret_20d, 2),
        'd_1_change': round(d_1_change, 2),
        'turnover_anchor': round(d0['volume'] / circ * 100, 2) if circ > 0 else None,
        'turnover_anchor_total': round(d0['volume'] / total * 100, 2) if total > 0 else None,
        'buy_mode': 'next_open',
    })
    return result

# ================================================================
# 断板买入策略
# ================================================================


def break_today_d0_signals(bars, code, min_streak=2, max_break_gap=5, today_str=None,
                           limit_ups=None, stock_info=None):
    """断板 今日(D0)信号: 判断今日是否为断板期的确认日, 与回测买点前规则完全一致

    原则: --today 时"买入当日(次日D1开盘)由人工判别", 今日(D0)及以前规则与回测
    strategy_break_buy 的"买点之前"完全一致。断板策略的确认点在断板期最后一天收盘
    (buy_mode=next_open, 买入=次日), 因此今日(D0) = 断板期的最后一天, 而非首断板日。
    断板期指标(5a低点/5b平均量比/5c首日涨跌/5d首日gap/5e回撤)复用 _break_signal_at,
    与回测逐条一致, 绝不引入未来数据。

    返回空list或单元素list。
    """
    result = []
    n = len(bars)
    if n < 3:
        return result
    if today_str:
        idxs = [j for j, b in enumerate(bars) if b['time'] == today_str]
        if not idxs:
            return result
        i = idxs[-1]
    else:
        i = n - 1
    if i < 2:
        return result
    bt = get_board_type(code)
    params = BOARD_PARAMS.get(bt, BOARD_PARAMS['main'])

    # 寻找所有连板结构, 要求断板期最后一天 == 今日(i)
    for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], bt)):
        # 连板第一板确认 (lu_idx 前一日非涨停)
        is_first = True
        for k in range(1, min(11, lu_idx + 1)):
            if lu_idx - k - 1 >= 0 and is_limit_up(bars[lu_idx - k]['close'], bars[lu_idx - k - 1]['close'], bt):
                is_first = False; break
        if not is_first:
            continue
        # 连板结束位置
        streak_start = lu_idx; streak_end = lu_idx
        while streak_end < i - 1 and is_limit_up(bars[streak_end + 1]['close'], bars[streak_end]['close'], bt):
            streak_end += 1
        # 断板期必须且只能在今日结束: break_idx > streak_end 且 break_days 全落在 <=i,
        # 断板期最后一天(break_idx+break_days-1) == i 才意味着今日收盘可确认、明日买入。
        sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params)
        if not sig:
            continue
        if sig['break_idx'] + sig['break_days'] - 1 != i:
            continue
        circ = float((stock_info or {}).get('circ_shares') or 0)
        total = float((stock_info or {}).get('total_shares') or 0)
        turnover_anchor = round(float(bars[streak_end]['volume']) / circ * 100, 2) if circ > 0 else None
        turnover_confirm = round(float(bars[i]['volume']) / circ * 100, 2) if circ > 0 else None
        turnover_anchor_t = round(float(bars[streak_end]['volume']) / total * 100, 2) if total > 0 else None
        turnover_confirm_t = round(float(bars[i]['volume']) / total * 100, 2) if total > 0 else None
        result.append({
            'code': code, 'board': get_board_name(code), 'path': 'break_buy', 'path_label': '断板',
            'mode': 'streak_break',
            'streak_len': sig['streak_len'],
            'streak_start': sig['streak_start'],
            'streak_end': sig['streak_end'],
            'break_date': sig['break_date'],
            'signal_date': bars[i]['time'],
            'break_days': sig['break_days'],
            'break_chg': sig['break_chg'],
            'break_gap': sig['break_gap'],
            'break_vol_r': sig['break_vol_r'],
            'confirm_chg': sig['confirm_chg'],
            'confirm_gap': sig['confirm_gap'],
            'pre20_gain': sig['pre20_gain'],
            'ma_bull': sig['ma_bull'],
            'turnover_anchor': turnover_anchor,
            'turnover_sig': turnover_confirm,
            'turnover_anchor_total': turnover_anchor_t,
            'turnover_sig_total': turnover_confirm_t,
            'entry_price': None, 'buy_mode': 'next_open',
        })
        break  # 只取一个信号
    return result

# ================================================================
# 测试列表 (去蓝筹)
# ================================================================


def find_limit_ups(bars, board_type):
    """找到所有涨停日索引 (断板策略依赖)。"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result
