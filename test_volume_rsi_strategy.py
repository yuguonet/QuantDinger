#!/usr/bin/env python3
"""
量价RSI策略 - 独立回测文件

入场规则:
  ① 今日成交量 > 前两日成交量之和
  ② 当前价格在20日均线之上
  ②b 今日涨幅>3%
  ②c 今日换手率>5%
  ③ 前40日到前5日有一日或多日的RSI<25
  ④ 从最后一个RSI<25的日起到前一日区间涨幅在3%~8%之间

出场规则:
  ① RSI>75 且 量比>2.0
  ② RSI>82
  ③ EMA5<EMA10
"""
from __future__ import annotations
import json, time, argparse, os, sys

# ================================================================
# DB 数据加载
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

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_circ_shares_cache = None
def _get_circ_shares(code):
    """从 stock_basic_info 表读取流通股本(股)"""
    global _circ_shares_cache
    if _circ_shares_cache is not None:
        return _circ_shares_cache.get(code, 0.0)
    _load_env()
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        db.ensure_table()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, circ_shares FROM stock_basic_info WHERE circ_shares > 0")
            _circ_shares_cache = {row[0]: float(row[1]) for row in cur.fetchall()}
        return _circ_shares_cache.get(code, 0.0)
    except Exception:
        _circ_shares_cache = {}
        return 0.0

def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def fetch_kline_db(code, days=300):
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
# RSI 计算 (Wilder 平滑)
# ================================================================
def compute_rsi(closes, rsi_len=14):
    """
    用前 rsi_len 个变化量的简单平均初始化 avg_gain/avg_loss,
    然后用 Wilder 指数平滑递推。
    """
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
# EMA 计算
# ================================================================
def compute_ema(closes, period):
    """计算EMA序列"""
    n = len(closes)
    ema = [0.0] * n
    if n < period:
        return ema

    # 用SMA初始化
    ema[period - 1] = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)

    return ema

# ================================================================
# MA 计算
# ================================================================
def compute_ma(closes, period):
    """计算简单移动平均线"""
    n = len(closes)
    ma = [0.0] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, exit_signals,
                 max_hold_days=60, ema5=None, ema10=None):
    """
    出场规则:
      ① 出场信号触发  ② EMA5<EMA10  ③ 持仓天数上限  ④ 数据耗尽
    max_hold_days=0 表示不限制
    ema5/ema10=None 表示不启用EMA出场
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = "data_end"
    max_d = len(bars) - entry_idx - 1

    # 买入当天就是最后一天，无法回测
    if max_d <= 0:
        return None

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # ① 出场信号
        if exit_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "signal_exit"
            break

        # ② EMA5 < EMA10 (死叉出场)
        if ema5 is not None and ema10 is not None:
            if ema5[idx] > 0 and ema10[idx] > 0 and ema5[idx] < ema10[idx]:
                exit_p = b['close']
                exit_d = d
                exit_reason = "ema_cross"
                break

        # ③ 持仓天数上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "max_hold"
            break

        exit_p = b['close']
        exit_d = d
        exit_reason = "data_end"

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

# ================================================================
# 量价RSI策略信号生成 + 回测
# ================================================================
def strategy_volume_rsi(bars, code, rsi_len=14, rsi_oversold=25,
                        rsi_overbought=75, rsi_extreme=82,
                        max_hold_days=60, gain_threshold=8.0,
                        circ_shares=0.0, min_turnover=5.0):
    """
    量价RSI策略:

    入场条件:
      ① 今日成交量 > 前两日成交量之和
      ② 当前价格在20日均线之上
      ②b 今日涨幅>3%
      ②c 今日换手率>5%
      ③ 前40日到前5日有一日或多日的RSI<25
      ④ 从最后一个RSI<25的日起到前一日区间涨幅在3%~8%之间

    出场条件:
      ① RSI>75 且 量比>2.0
      ② RSI>82
      ③ EMA5<EMA10
    """
    if len(bars) < 45:  # 需要至少45根K线
        return []

    opens = [b['open'] for b in bars]
    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # ---- 计算指标 ----
    rsi_values = compute_rsi(closes, rsi_len)
    ma20 = compute_ma(closes, 20)
    ema5 = compute_ema(closes, 5)
    ema10 = compute_ema(closes, 10)

    # ---- 出场信号预计算 ----
    exit_signal = [False] * len(bars)
    for i in range(len(bars)):
        # 条件①: RSI>75 且 量比>2.0 (今日成交量 / 前两日成交量之和)
        vol_prev2 = volumes[i - 1] + volumes[i - 2] if i >= 2 else 0
        vol_ratio_i = volumes[i] / vol_prev2 if vol_prev2 > 0 else 0
        if rsi_values[i] > rsi_overbought and vol_ratio_i > 2.0:
            exit_signal[i] = True
        # 条件②: RSI>82
        if rsi_values[i] > rsi_extreme:
            exit_signal[i] = True

    # ---- 入场信号检测 ----
    buy_signal = [False] * len(bars)
    last_rsi30_date = [None] * len(bars)   # 最后一个RSI<25的日期
    last_rsi30_idx_arr = [-1] * len(bars)  # 最后一个RSI<25的索引
    gain_from_rsi30_arr = [0.0] * len(bars)

    for i in range(40, len(bars)):
        # 条件①: 今日成交量 > 前两日成交量之和
        vol_prev2 = volumes[i - 1] + volumes[i - 2]
        if volumes[i] <= vol_prev2:
            continue

        # 条件②: 当前价格在20日均线之上
        if ma20[i] <= 0 or closes[i] <= ma20[i]:
            continue

        # 条件②b: 今日涨幅>3%
        if i >= 1 and closes[i - 1] > 0:
            today_gain = (closes[i] / closes[i - 1] - 1) * 100
            if today_gain <= 3.0:
                continue

        # 条件②c: 今日换手率>min_turnover%
        if circ_shares > 0:
            turnover = volumes[i] / circ_shares * 100
            if turnover < min_turnover:
                continue

        # 条件③: 前40日到前5日有RSI<25的日子(从近到远搜索, 取最后一个)
        last_rsi30_idx = -1
        for j in range(i - 5, max(i - 41, -1), -1):
            if j < 0:
                break
            if rsi_values[j] < rsi_oversold:
                last_rsi30_idx = j
                break

        if last_rsi30_idx < 0:
            continue

        # 条件④: 从最后一个RSI<25日到前一日区间涨幅在3%~8%之间
        price_at_rsi30 = closes[last_rsi30_idx]
        if price_at_rsi30 <= 0:
            continue
        gain_pct = (closes[i - 1] / price_at_rsi30 - 1) * 100
        if gain_pct >= gain_threshold or gain_pct <= 3.0:
            continue

        buy_signal[i] = True
        last_rsi30_date[i] = bars[last_rsi30_idx]['time']
        last_rsi30_idx_arr[i] = last_rsi30_idx
        gain_from_rsi30_arr[i] = round(gain_pct, 2)

    # ---- 生成交易记录 ----
    trades = []
    used_dates = set()

    for i in range(len(bars)):
        if not buy_signal[i]:
            continue

        signal_date = bars[i]['time']
        if signal_date in used_dates:
            continue

        # D+1开盘买
        if i + 1 >= len(bars):
            continue
        entry_price = bars[i + 1]['open']
        entry_idx = i + 1
        entry_date = bars[i + 1]['time']

        if entry_price <= 0:
            continue

        used_dates.add(signal_date)

        # 回测
        result = run_backtest(bars, entry_idx, entry_price, exit_signal, max_hold_days, ema5, ema10)
        if not result:
            continue

        # 计算入场时的各项指标值
        vol_prev2 = volumes[i - 1] + volumes[i - 2]
        vol_ratio = volumes[i] / vol_prev2 if vol_prev2 > 0 else 0
        today_gain = (closes[i] / closes[i - 1] - 1) * 100 if i >= 1 and closes[i - 1] > 0 else 0
        turnover = volumes[i] / circ_shares * 100 if circ_shares > 0 else 0

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'volume_rsi',
            'path_label': '量价RSI',
            'signal_date': signal_date,
            'signal_rsi': round(rsi_values[i], 2),
            'signal_close': closes[i],
            'signal_ma20': round(ma20[i], 3),
            'signal_ema5': round(ema5[i], 3),
            'signal_volume': volumes[i],
            'signal_vol_prev2': vol_prev2,
            'signal_vol_ratio': round(vol_ratio, 3),
            'signal_today_gain': round(today_gain, 2),
            'signal_turnover': round(turnover, 2),
            'last_rsi30_date': last_rsi30_date[i],
            'gain_from_rsi30': gain_from_rsi30_arr[i],
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': 'next_open',
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
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] < 0]
    breakeven = [t for t in trades if t['return_pct'] == 0]
    wr = len(wins) / n * 100 if n > 0 else 0
    avg = sum(t['return_pct'] for t in trades) / n if n > 0 else 0
    peak = sum(t['peak_return_pct'] for t in trades) / n if n > 0 else 0
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    pl = avg_win / abs(avg_loss) if avg_loss != 0 else (999.0 if wins else 0.0)
    total_ret = sum(t['return_pct'] for t in trades)
    worst = min(t['return_pct'] for t in trades) if trades else 0
    print(f"  {label}: {n:>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% "
          f"盈亏比{pl:.2f} 总收益{total_ret:>+6.2f}% 最大单笔{worst:>+6.2f}%")

def print_today_signals(all_trades, today_str):
    today_trades = [t for t in all_trades if t['entry_date'] == today_str]
    print(f"\n{'=' * 80}")
    print(f"  {today_str} 今日买点统计 (量价RSI)")
    print(f"{'=' * 80}")
    if not today_trades:
        print(f"  今日无买点信号")
        return today_trades

    print(f"  共 {len(today_trades)} 只股票出现买入信号")

    main_today = [t for t in today_trades if t['board'] in ('沪主板', '深主板')]
    gem_today  = [t for t in today_trades if t['board'] == '创业板']
    star_today = [t for t in today_trades if t['board'] == '科创板']

    print(f"\n  板块分布:")
    if main_today:  print(f"    主板: {len(main_today)} 只")
    if gem_today:   print(f"    创业板: {len(gem_today)} 只")
    if star_today:  print(f"    科创板: {len(star_today)} 只")

    print(f"\n  今日信号 ({len(today_trades)}只):")
    for t in sorted(today_trades, key=lambda x: x['signal_rsi']):
        print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
              f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
              f"买入{t['entry_price']:.2f} 涨幅={t['gain_from_rsi30']:.1f}%")

    return today_trades

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="量价RSI策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")
    parser.add_argument("--rsi-len", type=int, default=14, help="RSI 周期 (默认14)")
    parser.add_argument("--rsi-oversold", type=float, default=25, help="RSI 超卖阈值 (默认25)")
    parser.add_argument("--rsi-overbought", type=float, default=75, help="RSI 超买阈值 (默认75)")
    parser.add_argument("--rsi-extreme", type=float, default=82, help="RSI 极端超买阈值 (默认82)")
    parser.add_argument("--max-hold", type=int, default=60, help="最大持仓天数 (默认60, 0=不限制)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    parser.add_argument("--today", action="store_true", help="仅统计今日买点")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--gain-threshold", type=float, default=8.0, help="RSI30后涨幅阈值%% (默认8.0)")
    parser.add_argument("--min-turnover", type=float, default=5.0, help="最小换手率%% (默认5.0)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"量价RSI策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 今日成交量 > 前两日成交量之和")
    print(f"  ② 当前价格在20日均线之上")
    print(f"  ②b 今日涨幅>3%")
    print(f"  ②c 今日换手率>{args.min_turnover}%")
    print(f"  ③ 前40日到前5日有RSI<{args.rsi_oversold}")
    print(f"  ④ 从最后一个RSI<{args.rsi_oversold}日起到前一日区间涨幅在3%~{args.gain_threshold}%之间")
    print(f"出场条件:")
    print(f"  ① RSI>{args.rsi_overbought} 且 量比>2.0")
    print(f"  ② RSI>{args.rsi_extreme}")
    print(f"  ③ EMA5<EMA10")
    print(f"风控: 最大持仓{args.max_hold}天")
    print(f"买入模式: D+1开盘买")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_volume_rsi(
            bars, code,
            rsi_len=args.rsi_len,
            rsi_oversold=args.rsi_oversold,
            rsi_overbought=args.rsi_overbought,
            rsi_extreme=args.rsi_extreme,
            max_hold_days=args.max_hold,
            gain_threshold=args.gain_threshold,
            circ_shares=_get_circ_shares(code),
            min_turnover=args.min_turnover,
        )
        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) "
                  f"  {len(bars)}根 -> 信号{len(trades)}笔")

        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 结果统计 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    print_stats(all_trades, "量价RSI策略")

    if all_trades:
        # RSI 分布
        print(f"\n  信号RSI分布:")
        for lo, hi, label in [(0, 20, "RSI<20"), (20, 30, "RSI 20~30"),
                               (30, 40, "RSI 30~40"), (40, 50, "RSI 40~50"),
                               (50, 100, "RSI>=50")]:
            seg = [t for t in all_trades if lo <= t['signal_rsi'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 量比分布
        print(f"\n  信号量比(今日/前2日)分布:")
        for lo, hi, label in [(1.0, 1.5, "量比 1.0~1.5"), (1.5, 2.0, "量比 1.5~2.0"),
                               (2.0, 3.0, "量比 2.0~3.0"), (3.0, 999, "量比 >3.0")]:
            seg = [t for t in all_trades if lo <= t['signal_vol_ratio'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 涨幅分布
        print(f"\n  RSI30以来涨幅分布:")
        for lo, hi, label in [(0, 3, "涨幅 0~3%"), (3, 5, "涨幅 3~5%"),
                               (5, 8, "涨幅 5~8%"), (8, 12, "涨幅 8~12%"),
                               (12, 16, "涨幅 12~16%")]:
            seg = [t for t in all_trades if lo <= t.get('gain_from_rsi30', 0) < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 换手率分布
        print(f"\n  信号换手率分布:")
        for lo, hi, label in [(5, 10, "换手 5~10%"), (10, 15, "换手 10~15%"),
                               (15, 20, "换手 15~20%"), (20, 30, "换手 20~30%"),
                               (30, 999, "换手 >30%")]:
            seg = [t for t in all_trades if lo <= t.get('signal_turnover', 0) < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 出场原因分布
        print(f"\n  出场原因分布:")
        for reason, label in [("signal_exit", "信号出场"), ("ema_cross", "EMA死叉"),
                               ("max_hold", "持仓到期"), ("data_end", "数据耗尽")]:
            seg = [t for t in all_trades if t.get('exit_reason') == reason]
            if seg:
                print_stats(seg, f"    {label}")

        # 板块分布
        print(f"\n  板块分布:")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, f"    {board}")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n  TOP{n} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
                  f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

        print(f"\n  TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
                  f"量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

    # ===== 今日买点 =====
    if args.today:
        from datetime import datetime
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        today_trades = print_today_signals(all_trades, today_str)
        if today_trades:
            out_file = f"volume_rsi_today_signals_{today_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n  {out_file} ({len(today_trades)}笔)")

    # ===== 交易明细 =====
    if args.all_trades and all_trades:
        print(f"\n  全部交易明细:")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} "
                  f"RSI={t['signal_rsi']:.1f} 量比={t['signal_vol_ratio']:.2f} 换手={t['signal_turnover']:.1f}% -> "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"-> 收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

    # ===== 导出 =====
    if all_trades:
        out_file = "test_volume_rsi_strategy_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

if __name__ == "__main__":
    main()
