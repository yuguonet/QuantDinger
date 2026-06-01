#!/usr/bin/env python3
"""
BB 超卖策略 - 全市场扫描回测

═══════════════════════════════════════════════════════════════════
  策略说明
═══════════════════════════════════════════════════════════════════

  入场规则:
    ① 非ST股（排除名称含"ST"的股票）
    ② 最低价 < BB(20, 3.0) 下轨
    ②b 收盘价不高于BB下轨5%
    ③ 振幅>8% + 下影线占比<30%（有效波动且以实体为主）
    ④ MA60斜率 >= 0%（排除持续下跌趋势）
    ⑤ (D+1开盘价 > D0收盘价) 或者 D+1开盘价在BB下轨5%以下 → 次日开盘买入

  出场规则:
    ① 跟踪止损: 从持仓期间最高点回撤超过10% → 出场
    ①b 持仓3天内亏损超过-5% → 早期止损
    ② RSI>75 且 量比>2.0 且 未涨停 → 卖出
    ④ RSI>85 且 未涨停 → 极端超买卖出
    ⑤ 最高价突破BB上轨且(上影线>3%或量比(五日)>2.5) → 卖出
    ⑥ 最大持仓天数（默认20天）

  数据源:
    - 全市场股票列表: basicinfo_db（stock_basic_info表）
    - K线数据: kline_cache / db_market
    - ST过滤: 股票名称不含"ST"（含*ST、ST）

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import json, time, argparse, os, sys

# ================================================================
# 路径初始化 — 确保 backend_api_python 在 sys.path 中
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    """加载 .env 环境变量（按优先级查找）"""
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

# ================================================================
# DB K线数据加载（db_market）
# ================================================================
_writer_cache = None
def _get_writer():
    """获取 MarketKlineWriter 单例（延迟初始化）"""
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_all_codes_db():
    """从 db_market 获取全市场股票代码列表（仅K线表中有的）"""
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

# ================================================================
# basicinfo_db 全市场股票加载（带ST过滤）
# ================================================================
_basic_db_cache = None
def _get_basic_db():
    """获取 StockBasicDB 单例（延迟初始化）"""
    global _basic_db_cache
    if _basic_db_cache is not None:
        return _basic_db_cache
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    _basic_db_cache = get_stock_basic_db()
    return _basic_db_cache

def get_all_codes_basicinfo(filter_st=True):
    """
    从 basicinfo_db 获取全市场活跃股票代码列表。
    """
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    if filter_st:
        stocks = [s for s in stocks if "ST" not in s.get("name", "").upper()]
    return [s["symbol"] for s in stocks]

def get_stock_name_map():
    """获取全市场股票 code→name 映射（用于日志输出）"""
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    return {s["symbol"]: s["name"] for s in stocks}

def fetch_kline_db(code, days=300):
    from datetime import datetime, timedelta
    from app.data_sources.provider.adjustment import unadj_to_qfq
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
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

# ================================================================
# kline_cache 数据加载
# ================================================================
from kline_cache import fetch_kline

# ================================================================
# 工具函数
# ================================================================
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
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, sell_signals, extreme_signals,
                 bb_breakout_data=None,
                 trailing_stop_pct=-10.0, take_profit=993.75, board_type="main",
                 max_hold_days=20):
    """
    回测引擎 — 按优先级依次检查出场条件。
    """
    if bb_breakout_data is None:
        bb_breakout_data = [None] * len(bars)
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = ""
    max_d = len(bars) - entry_idx - 1

    for d in range(0, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # 默认记录当前收盘价（循环结束时保留最后一根K线的收盘价）
        exit_p = b['close']
        exit_d = d
        exit_reason = "持仓中"

        # d=0 为入场当天(D+1)，只更新峰值和收盘价，不触发出场条件
        if d == 0:
            continue

        # ① 跟踪止损
        trailing_ref = peak * (1 + trailing_stop_pct / 100)
        if b['low'] <= trailing_ref:
            exit_p = trailing_ref
            exit_d = d
            exit_reason = "跟踪止损"
            break

        # ①b 持仓3天内任意一天跌破-5%止损
        if d <= 3 and entry_price > 0:
            early_stop_price = entry_price * 0.95
            if b['low'] <= early_stop_price:
                exit_p = early_stop_price
                exit_d = d
                exit_reason = "早期止损"
                break

        # ③ RSI>75+量比>2+未涨停
        if sell_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "RSI超买"
            break

        # ④ RSI>85+未涨停
        if extreme_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "RSI极值"
            break

        # ⑤ BB上轨突破（需上影线>3% 或 量比(五日)>2.5）
        bb_data = bb_breakout_data[idx]
        if bb_data is not None:
            if bb_data['upper_shadow'] > 3.0 or bb_data['vol_ratio'] > 2.5:
                exit_p = b['close']
                exit_d = d
                exit_reason = "BB上轨突破"
                break

        # ⑥ 持仓天数上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "持仓天数上限"
            break

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

# ================================================================
# RSI 计算 (Wilder 平滑) — 修复版
# ================================================================
def compute_rsi(closes, rsi_len=14):
    n = len(closes)
    if n < rsi_len + 1:
        return [50.0] * n
    gains = []
    losses = []
    for i in range(1, rsi_len + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / rsi_len
    avg_loss = sum(losses) / rsi_len
    rsi_out = [50.0] * (rsi_len + 1)
    if avg_loss == 0:
        rsi_out[rsi_len] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi_out[rsi_len] = 100.0 - (100.0 / (1.0 + rs))
    alpha = 1.0 / rsi_len
    for i in range(rsi_len + 1, n):
        delta = closes[i] - closes[i - 1]
        g = max(delta, 0.0)
        l = max(-delta, 0.0)
        avg_gain = alpha * g + (1 - alpha) * avg_gain
        avg_loss = alpha * l + (1 - alpha) * avg_loss
        if avg_loss == 0:
            rsi_out.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            rsi_out.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi_out

# ================================================================
# 量比计算
# ================================================================
def compute_volume_ratio(volumes, window=5):
    n = len(volumes)
    vr = [0.0] * n
    for i in range(window, n):
        avg_vol = sum(volumes[i - window:i]) / window
        if avg_vol > 0:
            vr[i] = volumes[i] / avg_vol
    return vr

# ================================================================
# MA60 斜率计算
# ================================================================
def compute_ma_slope(closes, ma_len=60):
    n = len(closes)
    slope = [-999.0] * n
    if n < ma_len + 1:
        return slope
    ma = [0.0] * n
    for i in range(ma_len - 1, n):
        ma[i] = sum(closes[i - ma_len + 1:i + 1]) / ma_len
    for i in range(ma_len, n):
        if ma[i - 1] > 0:
            slope[i] = (ma[i] - ma[i - 1]) / ma[i - 1] * 100
    return slope

# ================================================================
# BB (Bollinger Bands) 计算
# ================================================================
def compute_bb(closes, period=20, num_std=3.0):
    import math
    n = len(closes)
    middle = [None] * n
    upper = [None] * n
    lower = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(variance)
        middle[i] = sma
        upper[i] = sma + num_std * std
        lower[i] = sma - num_std * std
    return middle, upper, lower

# ================================================================
# RSI 策略信号生成 + 回测
# ================================================================
def strategy_rsi(bars, code, rsi_len=14,
                 trailing_stop_pct=-10.0, take_profit=993.75,
                 buy_mode="next_open",
                 ma_slope_threshold=0.0,
                 ma_slope_len=60,
                 max_hold_days=20,
                 bb_period=20, bb_std=3.0):
    if len(bars) < max(rsi_len + 2, bb_period + 2):
        return []

    board_type = get_board_type(code)
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]
    lows = [b['low'] for b in bars]

    rsi_values   = compute_rsi(closes, rsi_len)
    vol_ratios   = compute_volume_ratio(volumes, window=5)
    ma60_slopes  = compute_ma_slope(closes, ma_len=ma_slope_len)
    bb_middle, bb_upper, bb_lower = compute_bb(closes, bb_period, bb_std)

    buy_signal = [False] * len(bars)
    d0_date = [None] * len(bars)
    for i in range(bb_period, len(bars) - 1):
        if bb_lower[i] is None:
            continue
        if lows[i] >= bb_lower[i]:
            continue
        # 收盘价不高于BB下轨5%
        if closes[i] > bb_lower[i] * 1.05:
            continue
        prev_close = bars[i - 1]['close'] if i > 0 else bars[i]['open']
        body_low = min(bars[i]['open'], bars[i]['close'])
        if prev_close > 0:
            lower_shadow = (body_low - bars[i]['low']) / prev_close
            amplitude = (bars[i]['high'] - bars[i]['low']) / prev_close
        else:
            lower_shadow = 0
            amplitude = 0
        if amplitude < 0.08 or (amplitude > 0 and lower_shadow / amplitude >= 0.30):
            continue
        if ma60_slopes[i] != -999.0 and ma60_slopes[i] < ma_slope_threshold:
            continue
        d1_open = bars[i + 1]['open']
        bb_lower_next = bb_lower[i + 1] if i + 1 < len(bb_lower) else bb_lower[i]
        # ⑤ (D+1开盘价 > D0收盘价) 或者 D+1开盘价在BB下轨5%以下 → 次日开盘买入
        if d1_open <= closes[i] and not (bb_lower_next is not None and d1_open < bb_lower_next * 0.95):
            continue
        buy_signal[i] = True
        d0_date[i] = bars[i]['time']

    sell_signal = [False] * len(bars)
    extreme_signal = [False] * len(bars)
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]['close']
        limit_pct = 20.0 if code.startswith("30") or code.startswith("68") else 10.0
        is_limit_up = (bars[i]['close'] / prev_close - 1) * 100 >= limit_pct - 0.5 if prev_close > 0 else False
        if rsi_values[i] > 75 and vol_ratios[i] > 2.0 and not is_limit_up:
            sell_signal[i] = True
        if rsi_values[i] > 85 and not is_limit_up:
            extreme_signal[i] = True

    # ---- ⑤ BB上轨突破卖出信号（信号生成，不做过滤，由回测引擎按持仓天数判断） ----
    bb_breakout_data = [None] * len(bars)  # None=无信号, dict=有信号(含详情)
    for i in range(1, len(bars)):
        if bb_upper[i] is None:
            continue
        if bars[i]['high'] <= bb_upper[i]:
            continue
        body_high = max(bars[i]['open'], bars[i]['close'])
        upper_shadow_pct = (bars[i]['high'] - body_high) / bars[i]['high'] * 100 if bars[i]['high'] > 0 else 0
        vol_avg_5 = sum(bars[j]['volume'] for j in range(max(0, i - 5), i)) / min(5, i)
        vol_ratio_5d = bars[i]['volume'] / vol_avg_5 if vol_avg_5 > 0 else 0
        bb_breakout_data[i] = {
            'upper_shadow': upper_shadow_pct,
            'vol_ratio': vol_ratio_5d,
        }

    trades = []
    used_dates = set()

    for i in range(len(bars)):
        if not buy_signal[i]:
            continue
        signal_date = bars[i]['time']
        if signal_date in used_dates:
            continue
        if buy_mode == "signal_close":
            entry_price = bars[i]['close']
            entry_idx = i
            entry_date = signal_date
        elif buy_mode == "next_open":
            if i + 1 >= len(bars):
                continue
            entry_price = bars[i + 1]['open']
            entry_idx = i + 1
            entry_date = bars[i + 1]['time']
        else:
            continue
        if entry_price <= 0:
            continue
        used_dates.add(signal_date)

        sell_date = None
        for j in range(i + 1, len(bars)):
            if sell_signal[j]:
                sell_date = bars[j]['time']
                break

        body_low = min(bars[i]['open'], bars[i]['close'])
        lower_shadow_pct = round((body_low - bars[i]['low']) / bars[i]['low'] * 100, 2) if bars[i]['low'] > 0 else 0
        amplitude_pct = round((bars[i]['high'] - bars[i]['low']) / bars[i]['low'] * 100, 2) if bars[i]['low'] > 0 else 0

        result = run_backtest(bars, entry_idx, entry_price, sell_signal, extreme_signal,
                              bb_breakout_data,
                              trailing_stop_pct, take_profit, board_type, max_hold_days)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'bb_oversold',
            'path_label': 'BB超卖',
            'signal_rsi': round(rsi_values[i], 2),
            'signal_vol_ratio': round(vol_ratios[i], 3),
            'signal_ma60_slope': round(ma60_slopes[i], 4),
            'signal_bb_lower': round(bb_lower[i], 2) if bb_lower[i] else None,
            'signal_bb_middle': round(bb_middle[i], 2) if bb_middle[i] else None,
            'signal_low': round(lows[i], 2),
            'signal_lower_shadow': lower_shadow_pct,
            'signal_amplitude': amplitude_pct,
            'signal_date': signal_date,
            'd0_date': d0_date[i],
            'turn_day': 0,
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'sell_signal_date': sell_date,
            **result,
        })

    return trades

# ================================================================
# 测试股票列表
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

# ================================================================
# 统计输出
# ================================================================
def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号")
        return
    n = len(trades)
    wr = sum(1 for t in trades if t['return_pct'] > 0) / n * 100
    avg = sum(t['return_pct'] for t in trades) / n
    peak = sum(t['peak_return_pct'] for t in trades) / n
    ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
    ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
    if ws and ls and sum(ls) != 0:
        pl = (sum(ws) / len(ws)) / (abs(sum(ls)) / len(ls))
    elif ws:
        pl = 999.0
    else:
        pl = 0.0
    total_ret = sum(t['return_pct'] for t in trades)
    max_dd = min(t['return_pct'] for t in trades)
    print(f"  {label}: {n:>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% "
          f"盈亏比{pl:.2f} 总收益{total_ret:>+6.2f}% 最大单笔{max_dd:>+6.2f}%")

# ================================================================
# 今日入场扫描（跳过D+1规则，不回测）
# ================================================================
def check_today_entry(bars, code, rsi_len=14,
                      ma_slope_threshold=0.0, ma_slope_len=60,
                      bb_period=20, bb_std=3.0):
    """检查最新一根K线是否满足入场条件（不含D+1），返回None或信号详情dict"""
    if len(bars) < max(rsi_len + 2, bb_period + 2, ma_slope_len + 2):
        return None

    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]
    lows = [b['low'] for b in bars]

    rsi_values  = compute_rsi(closes, rsi_len)
    vol_ratios  = compute_volume_ratio(volumes, window=5)
    ma60_slopes = compute_ma_slope(closes, ma_len=ma_slope_len)
    bb_middle, bb_upper, bb_lower = compute_bb(closes, bb_period, bb_std)

    i = len(bars) - 1  # 最新一根K线

    # ② 最低价 < BB下轨
    if bb_lower[i] is None:
        return None
    if lows[i] >= bb_lower[i]:
        return None
    # 收盘价不高于BB下轨5%
    if closes[i] > bb_lower[i] * 1.05:
        return None

    # ③ 振幅>8% + 下影线占比<30%
    prev_close = bars[i - 1]['close'] if i > 0 else bars[i]['open']
    body_low = min(bars[i]['open'], bars[i]['close'])
    if prev_close <= 0:
        return None
    lower_shadow = (body_low - bars[i]['low']) / prev_close
    amplitude = (bars[i]['high'] - bars[i]['low']) / prev_close
    if amplitude < 0.08 or (amplitude > 0 and lower_shadow / amplitude >= 0.30):
        return None

    # ④ MA60斜率 >= 0%
    if ma60_slopes[i] != -999.0 and ma60_slopes[i] < ma_slope_threshold:
        return None

    # 全部通过，返回信号详情
    return {
        'code': code,
        'board': get_board_name(code),
        'signal_date': bars[i]['time'],
        'close': round(bars[i]['close'], 3),
        'low': round(bars[i]['low'], 3),
        'high': round(bars[i]['high'], 3),
        'signal_rsi': round(rsi_values[i], 2),
        'signal_vol_ratio': round(vol_ratios[i], 3),
        'signal_ma60_slope': round(ma60_slopes[i], 4),
        'signal_bb_lower': round(bb_lower[i], 2) if bb_lower[i] else None,
        'signal_bb_middle': round(bb_middle[i], 2) if bb_middle[i] else None,
        'signal_bb_upper': round(bb_upper[i], 2) if bb_upper[i] else None,
        'signal_amplitude': round(amplitude * 100, 2),
        'signal_lower_shadow': round(lower_shadow * 100, 2),
    }

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="BB超卖策略回测",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认, 测试股票池), db(全市场扫描)")
    parser.add_argument("--filter-st", action="store_true", default=True)
    parser.add_argument("--no-filter-st", action="store_true")
    parser.add_argument("--rsi-len", type=int, default=14)
    parser.add_argument("--trailing-stop", type=float, default=-10.0)
    parser.add_argument("--take-profit", type=float, default=993.75)
    parser.add_argument("--buy-mode", default="next_open",
                        choices=["signal_close", "next_open"])
    parser.add_argument("--ma-slope", type=float, default=0.0,
                        help="MA60斜率阈值%% (默认0.0)")
    parser.add_argument("--max-hold", type=int, default=20)
    parser.add_argument("--bb-period", type=int, default=20)
    parser.add_argument("--bb-std", type=float, default=3.0)
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--today", action="store_true",
                        help="仅扫描今日符合入场条件的股票（跳过D+1规则，不回测）")
    args = parser.parse_args()

    # ---- 确定股票列表 ----
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        use_db = False
        filter_st = False
        stock_source = "手动指定"
    elif args.source == "db":
        use_db = True
        filter_st = not args.no_filter_st
        print("  全市场扫描模式: 从 basicinfo_db 加载股票列表...")
        codes = get_all_codes_basicinfo(filter_st=filter_st)
        stock_source = f"basicinfo_db ({'排除ST' if filter_st else '含ST'})"
        print(f"   {stock_source}: {len(codes)} 只股票")
    else:
        codes = TEST_CODES
        use_db = False
        filter_st = False
        stock_source = "测试股票池"

    mode_label = {"signal_close": "信号日收盘买", "next_open": "D+1开盘买"}[args.buy_mode]

    # ===== --today 模式：仅扫描今日信号，不回测 =====
    if args.today:
        print(f"{'=' * 80}")
        print(f"BB 超卖策略 — 今日入场信号扫描")
        print(f"{'=' * 80}")
        print(f"股票来源: {stock_source} ({len(codes)}只)")
        print(f"         + 收盘价<=BB下轨×1.05 + 振幅>8%% + 下影线占比<30%% + MA60斜率>={args.ma_slope}%%")
        print(f"         + 振幅>8%% + 下影线占比<30%% + MA60斜率>={args.ma_slope}%%")
        print(f"（跳过D+1入场规则）\n")

        name_map = {}
        if use_db:
            try:
                name_map = get_stock_name_map()
            except Exception:
                pass

        hits = []
        for i, code in enumerate(codes):
            bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
            if not bars:
                continue
            sig = check_today_entry(
                bars, code,
                rsi_len=args.rsi_len,
                ma_slope_threshold=args.ma_slope,
                bb_period=args.bb_period,
                bb_std=args.bb_std,
            )
            if sig:
                sname = name_map.get(code, "")
                sig['name'] = sname
                hits.append(sig)
            if (i + 1) % 500 == 0:
                print(f"  已扫描 {i + 1}/{len(codes)} ...")

        print(f"\n{'=' * 80}")
        print(f"扫描完成: {len(codes)}只, 符合条件 {len(hits)} 只")
        print(f"{'=' * 80}")

        if hits:
            # 按振幅降序排列
            hits.sort(key=lambda x: -x['signal_amplitude'])
            print(f"\n{'代码':<10} {'名称':<10} {'板块':<6} {'收盘':>8} {'振幅':>7} {'下影线':>7} "
                  f"{'RSI':>6} {'量比':>6} {'MA60斜率':>9} {'BB下轨':>9}")
            print(f"{'-' * 90}")
            for h in hits:
                print(f"{h['code']:<10} {h['name']:<10} {h['board']:<6} "
                      f"{h['close']:>8.2f} {h['signal_amplitude']:>6.2f}% "
                      f"{h['signal_lower_shadow']:>6.2f}% "
                      f"{h['signal_rsi']:>6.2f} {h['signal_vol_ratio']:>6.3f} "
                      f"{h['signal_ma60_slope']:>9.4f} {h['signal_bb_lower']:>9.2f}")

            # 导出
            out_file = "test_bb_indicator_today.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(hits, f, ensure_ascii=False, indent=2)
            print(f"\n  导出: {out_file}")
        else:
            print("  今日无符合条件的股票。")
        return

    # ===== 常规回测模式 =====

    print(f"{'=' * 80}")
    print(f"BB 超卖策略回测")
    print(f"{'=' * 80}")
    print(f"股票来源: {stock_source} ({len(codes)}只)")
    print(f"入场条件: 非ST股 + 最低价<BB下轨 + 收盘价<=BB下轨×1.05")
    print(f"         + 振幅>8% + 下影线占比<30% + MA60斜率>={args.ma_slope}%")
    print(f"         (D1开盘>D0收盘) 或 (D1开盘<BB下轨×0.95) → {mode_label}")
    print(f"出场规则: 跟踪止损{args.trailing_stop}% / 3天内-5%早期止损 / 止盈{args.take_profit}% / RSI+量比 / BB上轨突破 / 持仓{args.max_hold}天")
    print(f"数据天数: {args.days}天\n")

    all_trades = []
    success = 0
    name_map = {}

    if use_db:
        try:
            name_map = get_stock_name_map()
        except Exception:
            pass

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_rsi(
            bars, code,
            rsi_len=args.rsi_len,
            trailing_stop_pct=args.trailing_stop,
            take_profit=args.take_profit,
            buy_mode=args.buy_mode,
            ma_slope_threshold=args.ma_slope,
            max_hold_days=args.max_hold,
            bb_period=args.bb_period,
            bb_std=args.bb_std,
        )
        all_trades.extend(trades)

        if trades:
            sname = name_map.get(code, "")
            tag = f"({sname})" if sname else ""
            print(f"[{i+1}/{len(codes)}] {code}{tag} ({get_board_name(code)}) "
                  f"  {len(bars)}根 → 信号{len(trades)}笔")

        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 按入场日期过滤 =====
    if args.start_date:
        all_trades = [t for t in all_trades if t['entry_date'] >= args.start_date]
        print(f"  按入场日期过滤: {args.start_date} 起")

    # ===== 汇总 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {stock_source}, {success}只扫描完成")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "全部交易")

        # 出场原因分布
        print(f"\n  --- 出场原因分布 ---")
        reasons = [("trailing_stop", "跟踪止损"), ("take_profit", "止盈"),
                   ("rsi_sell", "RSI>75卖出"), ("rsi_extreme", "RSI>85卖出"),
                   ("bb_breakout", "BB上轨突破"),
                   ("max_hold", "持仓到期"), ("data_end", "数据耗尽")]
        for reason, label in reasons:
            n = len([t for t in all_trades if t.get('exit_reason') == reason])
            if n > 0:
                print(f"    {label:<16} {n:>4}笔")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n  TOP{n} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天 [{t['exit_reason']}]")

        print(f"\n  TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天 [{t['exit_reason']}]")

    # ===== 交易明细 =====
    if args.all_trades and all_trades:
        print(f"\n  全部交易明细:")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"-> 收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天 [{t['exit_reason']}]")

    # ===== 导出 =====
    if all_trades:
        out_file = "test_bb_indicator_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  导出: {out_file}")

if __name__ == "__main__":
    main()
