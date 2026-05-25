#!/usr/bin/env python3
"""
龙回头 - 独立策略

逻辑:
  1. D-N: 股票涨停 (龙)
  2. D-N+1 ~ D-1: 回调至少2天, 不破关键位 (蓄力)
  3. D0: 放量涨≥5%, 弱转强确认 (回头)
  4. V1质量检查: 量比>2x, 上影<0.5%, 排除一字板
  5. D1开盘: 买入

与V1互不干扰: V1看首板次日, 龙回头看回调后再起
"""
from __future__ import annotations
import json, time, argparse, os, sys
from collections import defaultdict
from kline_cache import fetch_kline

# ================================================================
# DB 数据加载 (抄 optimizer/strategy_dragon_v3.py)
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'), os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def fetch_kline_db(code, days=300):
    """从DB加载日线, 返回与fetch_kline兼容的格式(list[dict])"""
    import pandas as pd
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = []
        for r in data:
            bars.append({
                "time": str(r["time"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return bars
    except Exception:
        return []

def ema(values, period):
    """计算EMA (指数移动平均)"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # 初始值用SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    """计算RSI (相对强弱指数)"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 初始SMA
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # EMA平滑
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)

def is_st_stock(code):
    """检查是否为ST股 (ST股涨停5%, 远低于正常涨停阈值, 自然排除)"""
    # ST股涨停5%, 主板阈值9.604% / 创业板科创板阈值19.404%
    # is_limit_up永远不会标记ST股为涨停, 因此自然排除
    # 此函数用于显式过滤, 提升代码可读性
    return False  # 无股票名称数据时依赖阈值自然排除

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
# 预埋信号 (仅保留有效的)
# ================================================================

def detect_signals(bars, idx):
    signals = []
    if idx >= 5:
        avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5
        if avg5 > 0:
            vr = bars[idx]['volume'] / avg5
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            avg3 = sum(b['volume'] for b in bars[idx-3:idx]) / 3
            if vr >= 1.5 and chg >= 1.0 and avg3 < avg5 * 0.9:
                signals.append('volume_breakout')
    if idx >= 5:
        avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5
        if avg5 > 0:
            vr = bars[idx]['volume'] / avg5
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            c5 = bars[idx-5]['close']
            prev5chg = (prev_c / c5 - 1) * 100 if c5 > 0 else 0
            if vr >= 2.0 and chg >= 2.0 and abs(prev5chg) <= 5:
                signals.append('bottom_volume')
    if idx >= 5:
        lb = min(10, idx)
        max_c = max(b['close'] for b in bars[idx-lb:idx])
        if bars[idx]['close'] > max_c * 1.005:
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            if chg <= 5:
                signals.append('break_high')
    return signals

# ================================================================
# 核心逻辑
# ================================================================

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

def find_limit_ups(bars, board_type):
    """找到所有涨停日"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result

def run_backtest(bars, entry_idx, entry_price, hold_days=20, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None):
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

    for d in range(1, hold_days + 1):
        idx = entry_idx + d
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']

        # V1专属: D1没涨停 → D+2快速离场
        if is_v1 and d == 2 and not d1_limit_up:
            d1_bar = bars[entry_idx + 1]
            d1_high = d1_bar['high']
            d1_close = d1_bar['close']
            d2_open_gap = (b['open'] / d1_close - 1) * 100 if d1_close > 0 else 0
            if b['low'] <= entry_price * (1 + stop_loss / 100):
                exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
            if d2_open_gap > 2.0:
                exit_p = b['open']; exit_d = d; break
            exit_trigger = d1_high * 0.99
            if b['low'] <= exit_trigger:
                exit_p = exit_trigger; exit_d = d; break
            exit_p = b['close']; exit_d = d; break

        # ① 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
        if peak_exit:
            ret = (b['close'] / entry_price - 1) * 100
            if ret > 7:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 30 and b['close'] < b['high'] * 0.98:
                    exit_p = b['close']; exit_d = d; break

        # ② 追踪止损
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_limit_up': d1_limit_up,
    }

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=15, stop_loss=-5.0, trailing_stop=-5.0,
                             buy_mode="signal_close"):
    """
    龙回头v4 (优化版):
    D-N涨停 → 回调3-11天 → 末期缩量小阴(-3%~-0.5%)+量比0.5~0.8 → 买入

    出场参数 (stop-5 + trail-5 + peak7/30):
      stop_loss    = -5%  (原-5%, 单笔最大亏损控制)
      trailing_stop = -5% (原-5%, 更早锁利)
      hold_days    = 10   (原15, 时间止损兜底)
      peak_escape : 涨>7%后上影线>30%逃顶 (原10%/40%)

    buy_mode:
      signal_close — 信号日收盘买 (默认)
      next_open    — D+1开盘买
      signal_low   — 信号日最低价买 (理想)
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    limit_ups = find_limit_ups(bars, board_type)
    trades = []
    used_ranges = []

    for lu_idx in limit_ups:
        lu_close = bars[lu_idx]['close']
        lu_vol = bars[lu_idx]['volume']

        # 找回调期: close < lu_close 的最后一天
        pullback_end = None
        for j in range(lu_idx + 1, min(lu_idx + 20, len(bars))):
            if bars[j]['close'] < lu_close:
                pullback_end = j
            elif j >= lu_idx + min_pullback_days:
                break
            else:
                break

        if pullback_end is None:
            continue

        pullback_days = pullback_end - lu_idx
        if pullback_days < min_pullback_days or pullback_days > max_pullback_days:
            continue

        # 弱转强信号: 最后一天十字星/小阳 + 量比<阈值
        last_pb = bars[pullback_end]
        last_pb_prev = bars[pullback_end - 1] if pullback_end > 0 else bars[lu_idx]
        last_pb_prev_c = last_pb_prev['close']
        if last_pb_prev_c <= 0: continue
        last_chg = (last_pb['close'] / last_pb_prev_c - 1) * 100
        last_vol_r = last_pb['volume'] / lu_vol if lu_vol > 0 else 0

        # 排除大阴(跌超过max_last_chg)或放量(>2x)
        if last_chg < -max_last_chg or last_vol_r > 2.0:
            continue

        # 末期量比过滤: 0.5x <= 量比 < 0.8x (温和缩量, 筹码锁定)
        if last_vol_r < 0.5 or last_vol_r >= 0.8:
            continue

        # 末期小阴: -max_last_chg% < 涨跌 < -0.5% (弱转强信号: 缩量下跌, 抛压枯竭)
        is_signal = -max_last_chg < last_chg < -0.5
        if not is_signal:
            continue

        # RSI过滤: 45 < RSI < 70 (偏多但不超买)
        if pullback_end >= 15:
            closes = [bars[j]['close'] for j in range(pullback_end - 15, pullback_end + 1)]
            r = rsi(closes, 14)
            if r is not None and (r <= 45 or r >= 70):
                continue

        # 检查是否已被使用
        skip = False
        for (s, e) in used_ranges:
            if abs(pullback_end - s) <= 4 or abs(pullback_end - e) <= 4:
                skip = True; break
        if skip: continue

        # D+1数据 (用于过滤和回测)
        if pullback_end + 1 >= len(bars): continue
        d1 = bars[pullback_end + 1]
        d1_change = (d1['close'] / last_pb['close'] - 1) * 100

        # 根据buy_mode确定入场价
        if buy_mode == "signal_close":
            entry_price = last_pb['close']
            entry_idx = pullback_end
            entry_date = last_pb['time']
        elif buy_mode == "next_open":
            entry_price = d1['open']
            entry_idx = pullback_end + 1
            entry_date = d1['time']
            # D+1收阴排除 (仅next_open模式)
            if d1_change < 0:
                continue
        elif buy_mode == "signal_low":
            entry_price = last_pb['low']
            entry_idx = pullback_end
            entry_date = last_pb['time']
        else:
            continue
        if entry_price <= 0: continue

        used_ranges.append((lu_idx, pullback_end))

        # 预计算 d1_limit_up: 基于 D1 收盘 vs D0 收盘 (信号日)
        d1_limit_up_val = is_limit_up(d1['close'], last_pb['close'], board_type)

        result = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, peak_exit=True, d1_limit_up=d1_limit_up_val)
        if not result: continue

        trades.append({
            'code': code, 'board': get_board_name(code),
            'path': 'dragon_callback',
            'path_label': '龙回头',
            'lu_date': bars[lu_idx]['time'],
            'pullback_days': pullback_days,
            'signal_date': last_pb['time'],
            'signal_chg': round(last_chg, 2),
            'signal_vol_r': round(last_vol_r, 2),
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'd1_change': round(d1_change, 2),
            **result,
        })

    return trades

def strategy_v1(bars, code, min_vol_ratio=2.0, max_upper_shadow=0.5,
                hold_days=20, stop_loss=-10.0, trailing_stop=-8.0,
                use_preload_filter=True, buy_mode="signal_close",
                no_limit_lookback=10, use_ema_filter=True, use_rsi_filter=True):
    """V1基线策略

    no_limit_lookback: 前N日无涨停过滤 (默认10天, 排除近期有涨停的股票)

    buy_mode:
      signal_close — D0收盘买 (默认)
      next_open    — D+1开盘买
      signal_low   — D0最低价买 (理想)
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    result = []
    for i in range(2, len(bars)):
        prev_c = bars[i-1]['close']
        if prev_c <= 0: continue
        ret = (bars[i]['close'] / prev_c - 1)
        if ret < threshold * 0.98: continue
        # 前N天不是涨停（排除连板中间板+近期有涨停的股票, 只取"干净"第一板）
        skip = False
        for k in range(1, no_limit_lookback + 1):
            if i - k < 1: break
            prev_k_c = bars[i-k-1]['close']
            if prev_k_c > 0 and (bars[i-k]['close'] / prev_k_c - 1) >= threshold * 0.98:
                skip = True; break
        if skip: continue

        fl = bars[i]
        fl_close = fl['close']
        fl_high = fl['high']
        fl_vol = fl['volume']
        fl_prev_close = bars[i-1]['close']
        fl_prev_vol = bars[i-1]['volume']
        ref = bars[i-2]['close'] if i >= 2 else fl_prev_close

        if ref <= 0: continue

        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        upper_shadow = (fl_high - fl_close) / ref * 100
        if upper_shadow >= max_upper_shadow: continue

        # 量比<1.5x
        if vol_ratio >= 1.5:
            continue

        has_preload = False
        preload_type = None
        for k in range(max(0, i - 10), i):
            sigs = detect_signals(bars, k)
            if sigs:
                has_preload = True
                preload_type = sigs[0]
                break

        if use_preload_filter and not has_preload:
            continue

        # EMA趋势过滤: EMA10 > EMA20 (多头排列)
        if use_ema_filter and i >= 20:
            closes = [bars[j]['close'] for j in range(i - 20, i + 1)]
            ema10 = ema(closes, 10)
            ema20 = ema(closes, 20)
            if ema10 is not None and ema20 is not None and ema10 <= ema20:
                continue

        # RSI过滤: 30 < RSI < 70 (排除超买超卖)
        if use_rsi_filter and i >= 15:
            closes = [bars[j]['close'] for j in range(i - 15, i + 1)]
            r = rsi(closes, 14)
            if r is not None and (r <= 30 or r >= 70):
                continue

        # D1数据 (用于过滤和回测)
        if i + 1 >= len(bars): continue
        d1 = bars[i + 1]

        # 根据buy_mode确定入场价
        if buy_mode == "signal_close":
            entry_price = fl['close']
            entry_idx = i
            entry_date = fl['time']
        elif buy_mode == "next_open":
            entry_price = d1['open']
            entry_idx = i + 1
            entry_date = d1['time']
        elif buy_mode == "signal_low":
            entry_price = fl['low']
            entry_idx = i
            entry_date = fl['time']
        else:
            continue
        if entry_price <= 0: continue

        # D1过滤 (仅next_open模式, signal_close已买入不需要)
        d1_change = (d1['close'] / fl_close - 1) * 100
        if buy_mode == "next_open":
            d1_gap = (entry_price / fl_close - 1) * 100
            min_d1_gap = -2.0 if board_type == "main" else -5.0
            if d1_gap < min_d1_gap:
                continue
            if d1_change < 0: continue  # D1收阴排除

        # 预计算 d1_limit_up: 基于 D1 收盘 vs D0 收盘 (涨停日)
        d1_limit_up_val = is_limit_up(d1['close'], fl_close, board_type)

        bt = run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, board_type, is_v1=True, d1_limit_up=d1_limit_up_val)
        if not bt: continue

        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'v1', 'path_label': 'V1',
            'd0_date': fl['time'],
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'vol_ratio': round(vol_ratio, 2),
            'd1_change': round(d1_change, 2),
            'has_preload': has_preload,
            'preload_type': preload_type,
            **bt,
        })

    return result

# ================================================================
# 断板买入策略
# ================================================================

BOARD_PARAMS = {
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15},
}

def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=20, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main"):
    """断板专用回测: 追踪止损 + 峰值逃顶信号"""
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0

    for d in range(1, hold_days + 1):
        idx = entry_idx + d
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

def strategy_break_buy(bars, code, min_streak=2, max_break_gap=5, buy_mode="break_close",
                       override_params=None, use_relay=False, relay_window=7, relay_min_limits=2):
    """断板买入: 连板≥2 → 断板 → 缩量不破位 → 买入 (带止盈+峰值逃顶)

    新增接力模式 (use_relay=True):
      N天M板 → 断板 → 缩量下跌不破位 → 买入
      默认: 7天4板+
    """
    bt = get_board_type(code)
    threshold = 0.098 if bt == "main" else 0.198
    params = dict(BOARD_PARAMS[bt])
    if override_params: params.update(override_params)
    stop_loss, trailing_stop, take_profit = params["stop_loss"], params["trailing_stop"], params["take_profit"]
    hold_days, vol_min, vol_max, drawdown_max = params["hold_days"], params["vol_min"], params["vol_max"], params["drawdown_max"]
    trades, used = [], set()

    # ===== 模式1: 连板后断板 (原有逻辑) =====
    i = 1
    while i < len(bars) - 1:
        if not is_limit_up(bars[i]['close'], bars[i-1]['close'], bt): i += 1; continue
        is_first = True
        for k in range(1, min(11, i + 1)):
            if i-k-1 >= 0 and is_limit_up(bars[i-k]['close'], bars[i-k-1]['close'], bt): is_first = False; break
        if not is_first: i += 1; continue
        streak_start = i; streak_end = i
        while streak_end < len(bars) - 1 and is_limit_up(bars[streak_end+1]['close'], bars[streak_end]['close'], bt): streak_end += 1
        streak_len = streak_end - streak_start + 1
        if streak_len < min_streak: i = streak_end + 1; continue
        break_idx = streak_end + 1
        if break_idx >= len(bars): i = streak_end + 1; continue
        break_days = 0
        for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
            if not is_limit_up(bars[j]['close'], bars[j-1]['close'], bt): break_days += 1
            else: break
        limit_bar = bars[streak_end]; limit_open = float(limit_bar['open']); limit_close = float(limit_bar['close']); limit_vol = float(limit_bar['volume'])
        break_bars = bars[break_idx:break_idx + break_days]
        if not break_bars: i = streak_end + 1; continue
        break_low = min(float(b['low']) for b in break_bars)
        if break_low < limit_open: i = streak_end + 1; continue
        break_vol_avg = sum(float(b['volume']) for b in break_bars) / len(break_bars)
        break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
        if break_vol_r < vol_min or break_vol_r >= vol_max: i = streak_end + 1; continue
        # 断板日涨跌过滤: 涨0-8% (高开低走但不破位)
        break_chg = (bars[break_idx]['close'] / limit_close - 1) * 100 if limit_close > 0 else 0
        if break_chg < 0 or break_chg >= 8: i = streak_end + 1; continue
        # 断板日开盘过滤: 高开0-2%
        break_gap = (bars[break_idx]['open'] / limit_close - 1) * 100 if limit_close > 0 else 0
        if break_gap < 0 or break_gap >= 2: i = streak_end + 1; continue
        break_drawdown = (break_low / limit_close - 1) * 100 if limit_close > 0 else 0
        if break_drawdown < drawdown_max: i = streak_end + 1; continue
        key = (bars[streak_start]['time'], bars[break_idx]['time'])
        if key in used: i = streak_end + 1; continue
        used.add(key)
        if buy_mode == "break_close": entry_price = bars[break_idx]['close']; entry_idx = break_idx
        elif buy_mode == "next_open":
            if break_idx + 1 >= len(bars): i = streak_end + 1; continue
            entry_price = bars[break_idx + 1]['open']; entry_idx = break_idx + 1
        elif buy_mode == "break_low": entry_price = bars[break_idx]['low']; entry_idx = break_idx
        else: i = streak_end + 1; continue
        if entry_price <= 0: i = streak_end + 1; continue
        result = run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, bt)
        if not result: i = streak_end + 1; continue
        break_bar = bars[break_idx]; prev_bar = bars[break_idx - 1]
        break_chg = (break_bar['close'] / prev_bar['close'] - 1) * 100
        break_gap = (break_bar['open'] / prev_bar['close'] - 1) * 100
        trades.append({
            'code': code, 'board': get_board_name(code), 'path': 'break_buy', 'path_label': '断板',
            'mode': 'streak_break',
            'streak_len': streak_len, 'streak_start': bars[streak_start]['time'], 'streak_end': bars[streak_end]['time'],
            'break_date': bars[break_idx]['time'],
            'break_chg': round(break_chg, 2),
            'break_gap': round(break_gap, 2),
            'break_vol_r': round(break_bar['volume'] / prev_bar['volume'] if prev_bar['volume'] > 0 else 0, 2),
            'entry_date': bars[entry_idx]['time'], 'entry_price': round(entry_price, 3), 'buy_mode': buy_mode, **result,
        })
        i = streak_end + 1

    # ===== 模式2: N天M板接力 (新增) =====
    if use_relay:
        for idx in range(relay_window, len(bars) - 1):
            # 统计窗口内涨停数
            limit_count = 0
            limit_indices = []
            for j in range(idx - relay_window + 1, idx + 1):
                if j >= 1 and is_limit_up(bars[j]['close'], bars[j-1]['close'], bt):
                    limit_count += 1
                    limit_indices.append(j)

            if limit_count < relay_min_limits:
                continue

            last_limit = limit_indices[-1]

            # 检查最后涨停后是否断板
            if last_limit + 1 >= len(bars):
                continue
            next_is_limit = is_limit_up(bars[last_limit+1]['close'], bars[last_limit]['close'], bt)
            if next_is_limit:
                continue

            break_bar = bars[last_limit + 1]
            limit_bar = bars[last_limit]
            limit_vol = float(limit_bar['volume'])
            break_chg = (break_bar['close'] / limit_bar['close'] - 1) * 100
            break_vol_r = break_bar['volume'] / limit_vol if limit_vol > 0 else 0

            # 过滤1: 断板日必须跌，但不能跌>8%（洗盘但不破位）
            if break_chg > 0:
                continue
            if break_chg < -8:
                continue

            # 过滤2: 断板日缩量<1.5x（抛压枯竭）
            if break_vol_r > 1.5:
                continue

            # 过滤3: 不破前涨停开盘价（支撑有效）
            if len(limit_indices) >= 2:
                prev_limit = limit_indices[-2]
                prev_open = bars[prev_limit]['open']
                if break_bar['low'] < prev_open:
                    continue

            # 去重
            key = (code, bars[last_limit]['time'])
            if key in used:
                continue
            used.add(key)

            # 买入
            if buy_mode == "next_open":
                if last_limit + 2 >= len(bars):
                    continue
                entry_bar = bars[last_limit + 2]
                entry_price = entry_bar['open']
                entry_idx = last_limit + 2
                # D+1开盘不能高开>5%
                gap = (entry_price / break_bar['close'] - 1) * 100
                if gap > 5:
                    continue
            elif buy_mode == "break_close":
                entry_price = break_bar['close']
                entry_idx = last_limit + 1
            elif buy_mode == "break_low":
                entry_price = break_bar['low']
                entry_idx = last_limit + 1
            else:
                continue

            if entry_price <= 0:
                continue

            result = run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days, stop_loss, trailing_stop, bt)
            if not result:
                continue

            trades.append({
                'code': code, 'board': get_board_name(code), 'path': 'break_buy', 'path_label': '断板',
                'mode': 'relay',
                'relay_window': relay_window,
                'relay_limits': limit_count,
                'last_limit': bars[last_limit]['time'],
                'break_date': break_bar['time'],
                'break_chg': round(break_chg, 2),
                'break_vol_r': round(break_vol_r, 2),
                'entry_date': bars[entry_idx]['time'], 'entry_price': round(entry_price, 3), 'buy_mode': buy_mode, **result,
            })

    return trades

# ================================================================
# 测试列表 (去蓝筹)
# ================================================================

TEST_CODES = [
    "000066","000402","000553","000586","000601","000637","000720","000753","000767","000783",
    "000925","000950","001208","001259","001316","002010","002011","002012","002013","002014",
    "002015","002016","002017","002018","002019","002020","002021","002022","002023","002024",
    "002025","002026","002027","002028","002029","002030","002031","002032","002033","002034",
    "002035","002036","002037","002038","002039","002040","002041","002042","002043","002044",
    "002045","002046","002047","002048","002049","002050","002055","002056","002063","002065",
    "002074","002077","002079","002081","002084","002088","002092","002093","002095","002097",
    "002100","002104","002106","002111","002115","002119","002120","002125","002127","002130",
    "002131","002137","002139","002141","002146","002149","002150","002152","002153","002156",
    "002158","002160","002163","002165","002169","002170","002172","002175","002177","002180",
    "002183","002185","002188","002190","002191","002194","002196","002198","002200","002202",
    "002208","002209","002211","002214","002218","002222","002227","002230","002232","002234",
    "002236","002238","002240","002242","002244","002248","002249","002252","002253","002255",
    "002258","002261","002263","002266","002268","002270","002272","002274","002276","002278",
    "002280","002297","002366","002464","002468","002498","002510","002512","002535","002552",
    "002560","002580","002640","002805","002858","002918","002989","300001","300002","300003",
    "300004","300005","300006","300007","300008","300009","300010","300011","300012","300013",
    "300014","300015","300016","300017","300018","300019","300020","300021","300022","300023",
    "300024","300025","300026","300027","300028","300029","300030","300031","300032","300033",
    "300034","300035","300036","300037","300038","300039","300059","300106","300124","300152",
]

def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号"); return
    wr = sum(1 for t in trades if t['return_pct'] > 0) / len(trades) * 100
    avg = sum(t['return_pct'] for t in trades) / len(trades)
    peak = sum(t['peak_return_pct'] for t in trades) / len(trades)
    ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
    ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
    if ws and ls:
        pl = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls))
    elif ws:
        pl = 999.0
    else:
        pl = 0.0
    print(f"  {label}: {len(trades):>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% 盈亏比{pl:.2f}")

def main():
    parser = argparse.ArgumentParser(description="龙回头 + V1 + 断板 三策略回测")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual=手动指定codes(默认), db=从数据库加载全市场")
    parser.add_argument("--start", type=str, default="2024-01-01", help="DB模式回测开始日期")
    parser.add_argument("--end", type=str, default="2026-05-22", help="DB模式回测结束日期")
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=3, help="龙回头最少回调天数")
    parser.add_argument("--max-pullback", type=int, default=11, help="龙回头最多回调天数")
    parser.add_argument("--max-last-chg", type=float, default=3.0, help="龙回头末期小阳最大涨幅%%")
    parser.add_argument("--strategy", default="all", choices=["all", "dragon", "v1", "break"],
                        help="运行策略: all=全部, dragon=龙回头, v1=V1, break=断板")
    parser.add_argument("--buy-mode", default="signal_close",
                        choices=["signal_close", "next_open", "signal_low"],
                        help="买入模式: signal_close=信号日收盘买(默认), next_open=D+1开盘买, signal_low=信号日最低价(理想)")
    parser.add_argument("--no-limit-lookback", type=int, default=10, help="V1: 前N日无涨停过滤 (默认10)")
    parser.add_argument("--min-vol-ratio", type=float, default=2.0, help="V1: 最小量比 (默认2.0)")
    parser.add_argument("--max-upper-shadow", type=float, default=0.5, help="V1: 最大上影线%% (默认0.5)")
    parser.add_argument("--v1-stop-loss", type=float, default=-10.0, help="V1: 止损%% (默认-10)")
    parser.add_argument("--v1-trailing-stop", type=float, default=-8.0, help="V1: 追踪止损%% (默认-8)")
    parser.add_argument("--no-ema-filter", action="store_true", help="V1: 禁用EMA10>EMA20过滤")
    parser.add_argument("--no-rsi-filter", action="store_true", help="V1: 禁用RSI 30-70过滤")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES

    # DB模式: 从数据库加载全市场代码
    use_db = args.source == "db"
    if use_db:
        print("📊 DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    run_dc = args.strategy in ("all", "dragon")
    run_v1 = args.strategy in ("all", "v1")
    run_bb = args.strategy in ("all", "break")

    mode_label = {"signal_close": "信号日收盘买", "next_open": "D+1开盘买", "signal_low": "信号日最低价(理想)"}[args.buy_mode]

    print(f"{'=' * 80}")
    print(f"龙回头 + V1 + 断板 三策略回测")
    print(f"{'=' * 80}")
    print(f"买入模式: {mode_label}")
    labels = []
    if run_dc: labels.append(f"龙回头(回调{args.pullback}-{args.max_pullback}天)")
    if run_v1: labels.append("V1")
    if run_bb: labels.append(f"断板(连板≥2)")
    print(f"运行: {' + '.join(labels)}")
    print(f"股票: {len(codes)}只\n")

    dc_trades, v1_trades, bb_trades = [], [], []
    success = 0

    for i, code in enumerate(codes):
        # 显式过滤ST股 (ST涨停5%, 远低于正常阈值, 会被自然排除)
        if is_st_stock(code):
            continue
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        parts = []
        if run_dc:
            dc = strategy_dragon_callback(bars, code,
                                           min_pullback_days=args.pullback,
                                           max_pullback_days=args.max_pullback,
                                           max_last_chg=args.max_last_chg,
                                           buy_mode=args.buy_mode)
            dc_trades.extend(dc)
            parts.append(f"龙回头{len(dc)}")
        if run_v1:
            v1 = strategy_v1(bars, code, use_preload_filter=False, buy_mode=args.buy_mode,
                             no_limit_lookback=args.no_limit_lookback,
                             min_vol_ratio=args.min_vol_ratio,
                             max_upper_shadow=args.max_upper_shadow,
                             stop_loss=args.v1_stop_loss,
                             trailing_stop=args.v1_trailing_stop,
                             use_ema_filter=not args.no_ema_filter,
                             use_rsi_filter=not args.no_rsi_filter)
            v1_trades.extend(v1)
            parts.append(f"V1{len(v1)}")
        if run_bb:
            bb_buy_mode = {"signal_close": "break_close", "next_open": "next_open", "signal_low": "break_low"}[args.buy_mode]
            bb = strategy_break_buy(bars, code, buy_mode=bb_buy_mode)
            bb_trades.extend(bb)
            parts.append(f"断板{len(bb)}")

        has_signal = (run_dc and len(dc) > 0) or (run_v1 and len(v1) > 0) or (run_bb and len(bb) > 0)
        if has_signal:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) ✓{len(bars)}根 → {' '.join(parts)}")
        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 独立结果 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只")
    print(f"{'=' * 80}")

    if run_dc:
        print(f"\n📊 龙回头:")
        print_stats(dc_trades, "龙回头")
        if dc_trades:
            print(f"\n  末期量比(vs涨停日):")
            for lo, hi, label in [(0,0.3,"<0.3x"), (0.3,0.5,"0.3-0.5x"), (0.5,0.8,"0.5-0.8x")]:
                seg = [t for t in dc_trades if lo <= t['signal_vol_r'] < hi]
                if seg: print_stats(seg, f"    {label}")
            print(f"\n  回调天数分布:")
            for lo, hi, label in [(3,5,"3-4天"), (5,8,"5-7天"), (8,12,"8-11天")]:
                seg = [t for t in dc_trades if lo <= t['pullback_days'] < hi]
                if seg: print_stats(seg, f"    {label}")
            print(f"\n  🏆 龙回头TOP5:")
            for t in sorted(dc_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
            if len(dc_trades) > 5:
                print(f"\n  💀 龙回头BOTTOM5:")
                for t in sorted(dc_trades, key=lambda x: x['return_pct'])[:5]:
                    print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

    if run_v1:
        print(f"\n📊 V1:")
        print_stats(v1_trades, "V1")

    if run_bb:
        print(f"\n📊 断板:")
        print_stats(bb_trades, "断板")
        if bb_trades:
            # 按模式分
            streak_trades = [t for t in bb_trades if t.get('mode') == 'streak_break']
            relay_trades = [t for t in bb_trades if t.get('mode') == 'relay']
            if streak_trades:
                print(f"\n  连板后断板 ({len(streak_trades)}笔):")
                for sl in sorted(set(t['streak_len'] for t in streak_trades)):
                    seg = [t for t in streak_trades if t['streak_len'] == sl]
                    print_stats(seg, f"    {sl}板后断")
            if relay_trades:
                print(f"\n  接力断板 ({len(relay_trades)}笔):")
                for w in sorted(set(t['relay_window'] for t in relay_trades)):
                    seg = [t for t in relay_trades if t['relay_window'] == w]
                    for m in sorted(set(t['relay_limits'] for t in seg), reverse=True):
                        sub = [t for t in seg if t['relay_limits'] == m]
                        print_stats(sub, f"    {w}天{m}板")

    # ===== 混合结果 =====
    all_trades = dc_trades + v1_trades + bb_trades
    if len(all_trades) > max(len(dc_trades), len(v1_trades), len(bb_trades)):
        print(f"\n{'=' * 80}")
        print(f"📊 三策略合并:")
        print_stats(all_trades, "合并")
        dc_keys = {(t['code'], t['entry_date']) for t in dc_trades}
        v1_keys = {(t['code'], t['entry_date']) for t in v1_trades}
        bb_keys = {(t['code'], t['entry_date']) for t in bb_trades}
        overlap = (dc_keys & v1_keys) | (dc_keys & bb_keys) | (v1_keys & bb_keys)
        if overlap:
            print(f"  ⚠️ 重叠信号: {len(overlap)}笔")
        else:
            print(f"  ✅ 零重叠, 三策略完全互补")

    # 交易明细
    if args.all_trades and dc_trades:
        print(f"\n📋 龙回头交易明细:")
        for t in sorted(dc_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} → 回调{t['pullback_days']}天 → "
                  f"{t['signal_date']}信号{t['signal_chg']:>+5.1f}% 量{t['signal_vol_r']:.2f}x → "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}%")

    # 导出
    all_out = dc_trades + v1_trades + bb_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json ({len(all_out)}笔)")

if __name__ == "__main__":
    main()
