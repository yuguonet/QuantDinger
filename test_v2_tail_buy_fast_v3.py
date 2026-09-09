#!/usr/bin/env python3
"""
V2 尾盘买入策略 — 全市场回测 & 今日信号扫描
  [V3 精掐规则]
    总笔数=23  活跃日=2  日均=11.5笔/天
    胜率=95.7%  均收=+7.393%  盈亏比=8.36
    盈均=+7.771%  亏均=-0.930%
    总收益=+170.03%  最大盈=+10.91%  最大亏=-0.93%
    日胜率=100.0% (2/2)

  --- 收益分布 ---
    <-3%        0笔 (  0.0%) 
    -3~0%       1笔 (  4.3%) ██
    0~1%        0笔 (  0.0%) 
    1~3%        1笔 (  4.3%) ██
    3~5%        0笔 (  0.0%) 
    >=5%       21笔 ( 91.3%) █████████████████████████████████████████████

  --- 按板块 ---

  [沪主板]
    总笔数=12  活跃日=2  日均=6.0笔/天
    胜率=91.7%  均收=+7.082%  盈亏比=8.40
    盈均=+7.811%  亏均=-0.930%
    总收益=+84.99%  最大盈=+10.91%  最大亏=-0.93%
    日胜率=100.0% (2/2)

  [深主板]
    总笔数=11  活跃日=1  日均=11.0笔/天
    胜率=100.0%  均收=+7.731%  盈亏比=inf
    盈均=+7.731%  亏均=+0.000%
    总收益=+85.04%  最大盈=+10.15%  最大亏=+5.57%
    日胜率=100.0% (1/1)
═══════════════════════════════════════════════════════════════════
  策略说明 (基于 V2_TAIL_BUY_STRATEGY.md)
═══════════════════════════════════════════════════════════════════

  核心逻辑: 超卖反弹 — 尾盘大跌+卖压枯竭 → 次日开盘反弹
  数据源:   1分钟K线 (kline_1m_YYYY 表, db_market)

  入场规则 (14:56):
    ① 非ST股, 非北交所
    ② 涨停封板排除 (买不进)
    ③ day_gain   归一化 <= -5  (当日大跌)
    ④ tail_ret   归一化 范围 -2.8% ~ -0.5% (尾盘回落, 均价参考)
    ⑤ pos_range  <= 0.4       (日内低位)
    ⑥ pre5_gain  归一化 <= -10 (近5日深度超卖)
    ⑦ amplitude  归一化 >= 10  (振幅大=弹性足)

  归一化: 创/科板(20%限制)因子×0.5, 主板(10%)不变

  卖出规则:
    D+1 开盘价卖出 (open-to-open)

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import json, time, argparse, os, sys, math
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ================================================================
# 路径初始化
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
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
# DB K线数据加载
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_basic_db_cache = None
def _get_basic_db():
    global _basic_db_cache
    if _basic_db_cache is not None:
        return _basic_db_cache
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    _basic_db_cache = get_stock_basic_db()
    return _basic_db_cache

def get_all_codes_basicinfo(filter_st=True):
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    if filter_st:
        stocks = [s for s in stocks if "ST" not in s.get("name", "").upper()]
    return [s["symbol"] for s in stocks]

def get_stock_name_map():
    db = _get_basic_db()
    stocks = db.get_all_stocks(status="active")
    return {s["symbol"]: s["name"] for s in stocks}

def fetch_1m(code: str, start_date: str, end_date: str) -> List[Dict]:
    """从 db_market 加载1分钟K线 (前复权)"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1m",
                            start_time=start_date, end_time=end_date, limit=0)
        if not data:
            return []
        bars = [{'time': str(r['time']), 'open': float(r['open']), 'high': float(r['high']),
                 'low': float(r['low']), 'close': float(r['close']), 'volume': float(r['volume'])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

def fetch_daily_kline(code: str, days: int = 300) -> List[Dict]:
    """日线 (前复权), 用于 pre5_gain 等日线特征"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"])[:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []

# ================================================================
# 工具函数
# ================================================================
BUY_BAR = 235      # 14:56 (第236根1分钟bar, 0-indexed)
TAIL_START = 199    # 14:20
TAIL_END = 219      # 14:40
MIN_BARS = 230      # 一天最少bar数
MAX_DAY_GAP = 7     # 最大交易日间隔

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"):   return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"):  return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

def day_limit_pct(board_type: str) -> float:
    return 0.20 if board_type == "gem_star" else 0.10

def norm_factor(board_type: str) -> float:
    """归一化系数: 创/科板×0.5, 主板不变"""
    return 0.5 if board_type == "gem_star" else 1.0

# ================================================================
# 1分钟K线按日分组
# ================================================================
def group_days(bars: List[Dict]) -> List[Tuple[str, List[Dict]]]:
    """将连续1分钟bar按日期分组, 过滤bar数不足的日期"""
    days = []
    cur, cur_bars = None, None
    for b in bars:
        d = b['time'][:10]
        if d != cur:
            if cur is not None and cur_bars and len(cur_bars) >= MIN_BARS:
                days.append((cur, cur_bars))
            cur = d
            cur_bars = [b]
        else:
            cur_bars.append(b)
    if cur is not None and cur_bars and len(cur_bars) >= MIN_BARS:
        days.append((cur, cur_bars))
    return days

# ================================================================
# 特征计算 (单日)
# ================================================================
def calc_day_features(day: List[Dict], prev_close: float, limit_pct: float) -> Optional[Dict]:
    """计算单日V2策略所需的全部特征

    返回 None 表示数据不足或无效
    """
    if len(day) <= BUY_BAR:
        return None
    if prev_close <= 0:
        return None

    limit_price = round(prev_close * (1 + limit_pct), 2)
    buy_price = float(day[BUY_BAR]['open'])
    if buy_price <= 0:
        return None

    # 涨停封板排除 (14:56仍封板 → 买不进)
    if buy_price >= limit_price * 0.998:
        return None

    seg = day[:BUY_BAR]

    # --- VWAP (全天) ---
    cum_pv = cum_v = 0.0
    above = up_bars = 0
    prev_c = None
    day_high = day_low = None
    vol_today = 0.0
    touched = False
    morning_close = None

    for m, bb in enumerate(seg):
        o, c, h, l, v = (float(bb['open']), float(bb['close']), float(bb['high']),
                          float(bb['low']), float(bb['volume']))
        tp = (h + l + c) / 3.0
        cum_pv += tp * v
        cum_v += v
        if cum_v > 0 and c > cum_pv / cum_v:
            above += 1
        if prev_c is not None and c > prev_c:
            up_bars += 1
        prev_c = c
        day_high = h if day_high is None else max(day_high, h)
        day_low = l if day_low is None else min(day_low, l)
        vol_today += v
        if h >= limit_price * 0.998:
            touched = True
        if m == 119:
            morning_close = c

    if cum_v <= 0 or not day_high or day_high <= day_low:
        return None

    vwap = cum_pv / cum_v

    # --- tail_ret: 尾盘回落 (14:20~14:40 简单收盘均价 → 14:55 收盘) ---
    # v1版本: 使用简单收盘均价 (与 test_limitnext.py 当前代码一致)
    tail_bars = [float(day[i]['close']) for i in range(TAIL_START, min(TAIL_END + 1, len(day)))]
    tail_avg = sum(tail_bars) / len(tail_bars) if tail_bars else float(seg[-1]['close'])
    tail_ret = (float(seg[-1]['close']) / tail_avg - 1) * 100

    # --- 核心因子 ---
    day_gain = (buy_price / prev_close - 1) * 100
    dist_limit = (limit_price - buy_price) / buy_price * 100
    amplitude = (day_high - day_low) / prev_close * 100
    pos_range = (buy_price - day_low) / (day_high - day_low)

    return {
        'buy_price': buy_price,
        'day_gain': round(day_gain, 2),
        'tail_ret': round(tail_ret, 3),
        'amplitude': round(amplitude, 2),
        'pos_range': round(pos_range, 3),
        'dist_limit': round(dist_limit, 2),
        'touched': touched,
        'vwap': round(vwap, 3),
        'vol_today': vol_today,
        'morning_close': morning_close,
        'day_high': day_high,
        'day_low': day_low,
        'limit_price': limit_price,
    }

# ================================================================
# 日线特征 (pre5_gain, down_streak, lu_recent)
# ================================================================
def calc_daily_features(daily_bars: List[Dict], target_date: str,
                        board_type: str) -> Optional[Dict]:
    """从日线计算近5日涨幅等特征"""
    limit_pct = day_limit_pct(board_type)

    idx = None
    for i, b in enumerate(daily_bars):
        if b['time'] == target_date:
            idx = i
            break
    if idx is None or idx < 6:
        return None

    closes = [float(daily_bars[i]['close']) for i in range(idx + 1)]
    buy_price = closes[-1]

    pre5_close = float(daily_bars[idx - 5]['close'])
    pre5_gain = (buy_price / pre5_close - 1) * 100 if pre5_close > 0 else 0

    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            streak += 1
        else:
            break

    lu_recent = 0
    for dd in range(1, 6):
        if idx - dd < 0:
            break
        cl = float(daily_bars[idx - dd]['close'])
        pc = float(daily_bars[idx - dd - 1]['close']) if idx - dd - 1 >= 0 else 0
        if pc > 0 and cl >= pc * (1 + limit_pct) * 0.998:
            lu_recent += 1

    return {
        'pre5_gain': round(pre5_gain, 2),
        'down_streak': streak,
        'lu_recent': lu_recent,
    }

# ================================================================
# V2 策略信号判定
# ================================================================
def check_v2_signal(feat: Dict, daily_feat: Dict, board_type: str) -> bool:
    """检查是否满足V2精掐规则

    规则 (全部归一化后):
      day_gain  * nf <= -5
      tail_ret  * nf ∈ [-2.8, -0.5]
      pos_range       <= 0.4  (不归一化)
      pre5_gain * nf <= -10
      amplitude * nf >= 10
    """
    nf = norm_factor(board_type)

    if feat['day_gain'] * nf > -5:
        return False
    tail_n = feat['tail_ret'] * nf
    if tail_n < -2.8 or tail_n > -0.5:
        return False
    if feat['pos_range'] > 0.4:
        return False
    if daily_feat['pre5_gain'] * nf > -10:
        return False
    if feat['amplitude'] * nf < 10:
        return False

    return True

# ================================================================
# 第1阶段: 日线预筛 (用D0日线数据, 快速淘汰)
# ================================================================
def daily_prescan(daily_bars: List[Dict], code: str,
                  start_date: str = "", end_date: str = "") -> List[Dict]:
    """日线预筛: 仅用D-1数据, 模拟14:56真实可获取的信息

    14:56时: D-1日线已收盘(可用), D0日线未收盘(不可用)
    """
    board_type = get_board_type(code)
    limit_pct = day_limit_pct(board_type)
    nf = norm_factor(board_type)

    if len(daily_bars) < 7:
        return []

    candidates = []
    for i in range(6, len(daily_bars) - 1):
        d0_date = daily_bars[i]['time']

        if start_date and d0_date < start_date:
            continue
        if end_date and d0_date > end_date:
            continue

        # D-1 = daily_bars[i-1] (14:56时已收盘)
        d1_bar = daily_bars[i - 1]
        prev_close = d1_bar['close']
        if prev_close <= 0:
            continue

        # ---- D-1 日线规则 ----

        # pre5_gain: (D-1 close / D-6 close - 1) * 100
        pre5_close = daily_bars[i - 5]['close']
        pre5_gain = (prev_close / pre5_close - 1) * 100 if pre5_close > 0 else 0
        if pre5_gain * nf > -10:
            continue

        # down_streak: D-1 起连跌天数 (不含D0, D0还没收盘)
        closes_before = [daily_bars[j]['close'] for j in range(i)]
        streak = 0
        for s in range(len(closes_before) - 1, 0, -1):
            if closes_before[s] < closes_before[s - 1]:
                streak += 1
            else:
                break
        if streak < 1:
            continue

        # lu_recent: 近5日涨停次数
        lu_recent = 0
        for dd in range(1, 6):
            if i - dd < 1:
                break
            cl = daily_bars[i - dd]['close']
            pc = daily_bars[i - dd - 1]['close']
            if pc > 0 and cl >= pc * (1 + limit_pct) * 0.998:
                lu_recent += 1
        if lu_recent > 0:
            continue

        # 涨停封板排除 (用D-1收盘价算涨停价)
        limit_price = round(prev_close * (1 + limit_pct), 2)

        next_bar = daily_bars[i + 1]
        candidates.append({
            'date': d0_date,
            'next_date': next_bar['time'],
            'prev_close': round(prev_close, 3),
            'next_open': round(next_bar['open'], 3),
            'limit_price': round(limit_price, 3),
            'pre5_gain': round(pre5_gain, 2),
            'down_streak': streak,
            'lu_recent': lu_recent,
        })

    return candidates


# ================================================================
# 回测: 单股 (两阶段)
# ================================================================
def backtest_stock(code: str, start_date: str, end_date: str,
                   daily_bars: List[Dict]) -> List[Dict]:
    """单股V2策略回测 (两阶段: D-1预筛 → D0 1m验证)"""
    board_type = get_board_type(code)
    limit_pct = day_limit_pct(board_type)
    nf = norm_factor(board_type)

    # ---- 阶段1: D-1 日线预筛 ----
    candidates = daily_prescan(daily_bars, code, start_date, end_date)
    if not candidates:
        return []

    # ---- 阶段2: D0 1m数据验证 ----
    min_date = candidates[0]['date']
    max_date = candidates[-1]['date']
    bars_1m = fetch_1m(code, min_date, max_date)
    if not bars_1m:
        return []

    days_1m = group_days(bars_1m)
    date_to_day = {d: bars for d, bars in days_1m}

    trades = []
    for cand in candidates:
        date = cand['date']
        prev_close = cand['prev_close']
        limit_price = cand['limit_price']

        day = date_to_day.get(date)
        if not day or len(day) <= BUY_BAR:
            continue

        # 从1m数据计算全部D0因子
        buy_price = float(day[BUY_BAR]['open'])
        if buy_price <= 0:
            continue

        # 涨停封板排除
        if buy_price >= limit_price * 0.998:
            continue

        seg = day[:BUY_BAR]
        day_high = max(float(b['high']) for b in seg)
        day_low = min(float(b['low']) for b in seg)
        if day_high <= day_low:
            continue

        # D0 因子 (全部从1m数据算)
        day_gain = (buy_price / prev_close - 1) * 100
        amplitude = (day_high - day_low) / prev_close * 100
        pos_range = (buy_price - day_low) / (day_high - day_low)

        # tail_ret: 简单收盘均价
        tail_bars = [float(day[i]['close']) for i in range(TAIL_START, min(TAIL_END + 1, len(day)))]
        tail_avg = sum(tail_bars) / len(tail_bars) if tail_bars else float(seg[-1]['close'])
        if tail_avg <= 0:
            continue
        tail_ret = (float(seg[-1]['close']) / tail_avg - 1) * 100

        # V2 精掐规则
        if day_gain * nf > -5:
            continue
        if amplitude * nf < 10:
            continue
        if pos_range > 0.4:
            continue
        tail_n = tail_ret * nf
        if tail_n < -2.8 or tail_n > -0.5:
            continue

        # D+1 开盘卖
        next_open = cand['next_open']
        if next_open <= 0:
            continue

        ret = (next_open / buy_price - 1) * 100

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'signal_date': date,
            'entry_price': round(buy_price, 3),
            'exit_date': cand['next_date'],
            'exit_price': round(next_open, 3),
            'return_pct': round(ret, 2),
            'day_gain': round(day_gain, 2),
            'tail_ret': round(tail_ret, 3),
            'pos_range': round(pos_range, 3),
            'amplitude': round(amplitude, 2),
            'pre5_gain': cand['pre5_gain'],
        })

    return trades

# ================================================================
# 今日信号扫描
# ================================================================
def check_today_signal(code: str, daily_bars: List[Dict]) -> Optional[Dict]:
    """检查最新交易日是否满足V2入场条件"""
    board_type = get_board_type(code)
    limit_pct = day_limit_pct(board_type)
    nf = norm_factor(board_type)

    if len(daily_bars) < 7:
        return None

    today_bar = daily_bars[-1]
    today_date = today_bar['time']
    prev_close = float(daily_bars[-2]['close'])
    if prev_close <= 0:
        return None

    close = float(today_bar['close'])
    if close >= prev_close * (1 + limit_pct) * 0.998:
        return None

    bars_1m = fetch_1m(code, today_date, today_date)
    if not bars_1m or len(bars_1m) < MIN_BARS:
        return None

    feat = calc_day_features(bars_1m, prev_close, limit_pct)
    if feat is None:
        return None

    daily_feat = calc_daily_features(daily_bars, today_date, board_type)
    if daily_feat is None:
        return None

    if not check_v2_signal(feat, daily_feat, board_type):
        return None

    return {
        'code': code,
        'board': get_board_name(code),
        'signal_date': today_date,
        'close': round(close, 3),
        'entry_price': feat['buy_price'],
        'day_gain': feat['day_gain'],
        'tail_ret': feat['tail_ret'],
        'pos_range': feat['pos_range'],
        'amplitude': feat['amplitude'],
        'dist_limit': feat['dist_limit'],
        'pre5_gain': daily_feat['pre5_gain'],
        'down_streak': daily_feat['down_streak'],
        'lu_recent': daily_feat['lu_recent'],
        'touched': feat['touched'],
        'norm_day_gain': round(feat['day_gain'] * nf, 2),
        'norm_tail_ret': round(feat['tail_ret'] * nf, 3),
        'norm_amp': round(feat['amplitude'] * nf, 2),
        'norm_pre5': round(daily_feat['pre5_gain'] * nf, 2),
    }

# ================================================================
# 统计输出
# ================================================================
def print_stats(trades: List[Dict], label: str = ""):
    if not trades:
        print(f"  {label}: 无交易")
        return
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    wr = len(wins) / n * 100
    avg_ret = sum(t['return_pct'] for t in trades) / n
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    pl = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    total_ret = sum(t['return_pct'] for t in trades)
    max_loss = min(t['return_pct'] for t in trades)
    max_win = max(t['return_pct'] for t in trades)

    daily = defaultdict(list)
    for t in trades:
        daily[t['signal_date']].append(t['return_pct'])
    active_days = len(daily)
    win_days = sum(1 for rets in daily.values() if sum(rets) / len(rets) > 0)
    day_wr = win_days / active_days * 100 if active_days > 0 else 0

    print(f"\n  [{label}]")
    print(f"    总笔数={n}  活跃日={active_days}  日均={n/max(active_days,1):.1f}笔/天")
    print(f"    胜率={wr:.1f}%  均收={avg_ret:+.3f}%  盈亏比={pl:.2f}")
    print(f"    盈均={avg_win:+.3f}%  亏均={avg_loss:+.3f}%")
    print(f"    总收益={total_ret:+.2f}%  最大盈={max_win:+.2f}%  最大亏={max_loss:+.2f}%")
    print(f"    日胜率={day_wr:.1f}% ({win_days}/{active_days})")

def print_detail(trades: List[Dict]):
    if not trades:
        return
    print(f"\n  {'代码':<8} {'板块':<6} {'信号日':<12} {'入价':>8} {'出价':>8} "
          f"{'收益':>7} {'day_gain':>9} {'tail_ret':>9} {'pos':>5} {'amp':>6} {'pre5':>7}")
    print(f"  {'-' * 100}")
    for t in sorted(trades, key=lambda x: x['signal_date']):
        emoji = '✅' if t['return_pct'] > 0 else '❌'
        print(f"  {t['code']:<8} {t['board']:<6} {t['signal_date']:<12} "
              f"{t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
              f"{t['return_pct']:>+6.2f}%{emoji} "
              f"{t['day_gain']:>+8.2f} {t['tail_ret']:>+8.3f} "
              f"{t['pos_range']:>5.3f} {t['amplitude']:>6.2f} {t['pre5_gain']:>+6.2f}")

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="V2 尾盘买入策略回测 (基于1分钟K线, open-to-open)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test_v2_tail_buy.py --days 60                     # 最近60个交易日回测
  python test_v2_tail_buy.py --days 120 --source db        # 全市场扫描
  python test_v2_tail_buy.py --today                       # 今日信号扫描
  python test_v2_tail_buy.py --today --source db           # 全市场今日扫描
  python test_v2_tail_buy.py --codes 000001,600519 --days 90
        """)
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=60, help="回看交易日数 (默认60)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db(全市场)")
    parser.add_argument("--no-filter-st", action="store_true")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--today", action="store_true",
                        help="仅扫描今日信号 (不回测)")
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    _load_env()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        stock_source = "手动指定"
    elif args.source == "db":
        print("  全市场扫描: 加载股票列表...")
        filter_st = not args.no_filter_st
        codes = get_all_codes_basicinfo(filter_st=filter_st)
        stock_source = f"basicinfo_db ({'排除ST' if filter_st else '含ST'})"
        print(f"   {stock_source}: {len(codes)} 只")
    else:
        codes = [
            "000066", "000402", "000553", "000586", "000601", "000637", "000720", "000753",
            "000767", "000783", "000925", "000950", "001208", "001259", "001316", "002010",
            "002011", "002012", "002013", "002014", "002015", "002016", "002017", "002018",
            "002019", "002020", "002021", "002022", "002023", "002024", "002025", "002026",
            "002027", "002028", "002029", "002030", "002031", "002032", "002033", "002034",
            "002035", "002036", "002037", "002038", "002039", "002040", "002041", "002042",
            "002043", "002044", "002045", "002046", "002047", "002048", "002049", "002050",
            "002055", "002056", "002063", "002065", "002074", "002077", "002079", "002081",
            "002084", "002088", "002092", "002093", "002095", "002097", "002100", "002104",
            "002106", "002111", "002115", "002119", "002120", "002125", "002127", "002130",
            "002131", "002137", "002139", "002141", "002146", "002149", "002150", "002152",
            "002153", "002156", "002158", "002160", "002163", "002165", "002169", "002170",
            "002172", "002175", "002177", "002180", "002183", "002185", "002188", "002190",
            "002191", "002194", "002196", "002198", "002200", "002202", "002208", "002209",
            "002211", "002214", "002218", "002222", "002227", "002230", "002232", "002234",
            "002236", "002238", "002240", "002242", "002244", "002248", "002249", "002252",
            "002253", "002255", "002258", "002261", "002263", "002266", "002268", "002270",
            "002272", "002274", "002276", "002278", "002280", "002297", "002366", "002464",
            "002468", "002498", "002510", "002512", "002535", "002552", "002560", "002580",
            "002640", "002805", "002858", "002918", "002989", "300001", "300002", "300003",
            "300004", "300005", "300006", "300007", "300008", "300009", "300010", "300011",
            "300012", "300013", "300014", "300015", "300016", "300017", "300018", "300019",
            "300020", "300021", "300022", "300023", "300024", "300025", "300026", "300027",
            "300028", "300029", "300030", "300031", "300032", "300033", "300034", "300035",
            "300036", "300037", "300038", "300039", "300059", "300106", "300124", "300152",
        ]
        stock_source = "测试股票池"

    # ================================================================
    # --today 模式
    # ================================================================
    if args.today:
        print(f"{'=' * 80}")
        print(f"V2 尾盘买入策略 — 今日信号扫描")
        print(f"{'=' * 80}")
        print(f"股票来源: {stock_source} ({len(codes)}只)")
        print(f"入场规则: day_gain<=-5 + tail_ret(-2.8~-0.5%) + pos<=0.4")
        print(f"         + pre5<=-10 + amp>=10  (创/科板归一化×0.5)\n")

        name_map = {}
        try:
            name_map = get_stock_name_map()
        except Exception:
            pass

        hits = []
        for i, code in enumerate(codes):
            daily_bars = fetch_daily_kline(code, 300)
            if not daily_bars or len(daily_bars) < 7:
                continue
            sig = check_today_signal(code, daily_bars)
            if sig:
                sig['name'] = name_map.get(code, '')
                hits.append(sig)
            if (i + 1) % 500 == 0:
                print(f"  已扫描 {i + 1}/{len(codes)} ...")

        print(f"\n{'=' * 80}")
        print(f"扫描完成: {len(codes)}只, 符合V2条件 {len(hits)} 只")
        print(f"{'=' * 80}")

        if hits:
            hits.sort(key=lambda x: x['norm_day_gain'])
            print(f"\n{'代码':<8} {'名称':<8} {'板块':<6} {'收盘':>8} "
                  f"{'day_gain':>9} {'tail_ret':>9} {'pos':>5} {'amp':>6} {'pre5':>7}")
            print(f"{'-' * 80}")
            for h in hits:
                print(f"{h['code']:<8} {h['name']:<8} {h['board']:<6} "
                      f"{h['close']:>8.2f} "
                      f"{h['day_gain']:>+8.2f} {h['tail_ret']:>+8.3f} "
                      f"{h['pos_range']:>5.3f} {h['amplitude']:>6.2f} {h['pre5_gain']:>+6.2f}")

            out_file = args.json or "test_v2_tail_buy_today.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(hits, f, ensure_ascii=False, indent=2)
            print(f"\n  导出: {out_file}")
        else:
            print("  今日无符合V2条件的股票。")
        return

    # ================================================================
    # 回测模式
    # ================================================================
    end_date = datetime.now().strftime("%Y-%m-%d")
    if args.start_date:
        start_date = args.start_date
    else:
        start_date = (datetime.now() - timedelta(days=int(args.days * 1.6))).strftime("%Y-%m-%d")

    print(f"{'=' * 80}")
    print(f"V2 尾盘买入策略回测 (1分钟K线, open-to-open)")
    print(f"{'=' * 80}")
    print(f"股票来源: {stock_source} ({len(codes)}只)")
    print(f"回测区间: {start_date} ~ {end_date}")
    print(f"入场规则: day_gain<=-5 + tail_ret(-2.8~-0.5%) + pos<=0.4")
    print(f"         + pre5<=-10 + amp>=10  (创/科板归一化×0.5)")
    print(f"阶段1: D-1日线预筛 → 阶段2: D0 1m验证")
    print(f"卖出规则: D+1 开盘价\n")

    name_map = {}
    try:
        name_map = get_stock_name_map()
    except Exception:
        pass

    all_trades = []
    success = 0
    t0 = time.time()

    for i, code in enumerate(codes):
        daily_bars = fetch_daily_kline(code, 300)
        if not daily_bars or len(daily_bars) < 8:
            continue

        trades = backtest_stock(code, start_date, end_date, daily_bars)
        all_trades.extend(trades)

        if trades:
            sname = name_map.get(code, '')
            tag = f"({sname})" if sname else ""
            print(f"[{i+1}/{len(codes)}] {code}{tag} ({get_board_name(code)}) → {len(trades)}笔信号")

        success += 1

        if args.source != "db":
            time.sleep(0.05)

        if (i + 1) % 200 == 0:
            elapsed = time.time() - t0
            print(f"  进度: {i+1}/{len(codes)}  耗时{elapsed:.0f}s  信号={len(all_trades)}笔")

    elapsed = time.time() - t0

    print(f"\n{'=' * 80}")
    print(f"回测完成: {stock_source}, {success}只, 耗时{elapsed:.0f}s")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "V2 精掐规则")

        n = len(all_trades)
        bins = [(-99, -3), (-3, 0), (0, 1), (1, 3), (3, 5), (5, 99)]
        labels = ['<-3%', '-3~0%', '0~1%', '1~3%', '3~5%', '>=5%']
        print(f"\n  --- 收益分布 ---")
        for (lo, hi), lab in zip(bins, labels):
            cnt = sum(1 for t in all_trades if lo <= t['return_pct'] < hi)
            pct = cnt / n * 100
            bar = '█' * int(pct / 2)
            print(f"    {lab:<8} {cnt:>4}笔 ({pct:>5.1f}%) {bar}")

        by_board = defaultdict(list)
        for t in all_trades:
            by_board[t['board']].append(t)
        if len(by_board) > 1:
            print(f"\n  --- 按板块 ---")
            for board, ts in sorted(by_board.items()):
                print_stats(ts, board)

        n_top = min(args.top, len(all_trades))
        print(f"\n  TOP{n_top} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n_top]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['signal_date']}  入{t['entry_price']:>7.2f} → 出{t['exit_price']:>7.2f}  "
                  f"收益{t['return_pct']:>+6.2f}%  day={t['day_gain']:+.1f}%  tail={t['tail_ret']:+.2f}%")

        print(f"\n  TOP{n_top} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n_top]:
            print(f"    {t['code']:<8} {t['board']:<6} "
                  f"{t['signal_date']}  入{t['entry_price']:>7.2f} → 出{t['exit_price']:>7.2f}  "
                  f"收益{t['return_pct']:>+6.2f}%  day={t['day_gain']:+.1f}%  tail={t['tail_ret']:+.2f}%")

        if args.all_trades:
            print_detail(all_trades)
    else:
        print("  无交易信号。")

    out_file = args.json or "test_v2_tail_buy_result.json"
    if all_trades:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  导出: {out_file}")


if __name__ == "__main__":
    main()
