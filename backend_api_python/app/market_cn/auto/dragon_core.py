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
            # --- 特征1 换手率 ---
            turn_anchor = turn_sig = None
            if circ > 0:
                turn_anchor = anchor_vol / circ * 100
                turn_sig = d0['volume'] / circ * 100
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
