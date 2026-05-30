#!/usr/bin/env python3
"""
RSI 超买超卖策略 - 独立回测文件

出场规则:
  - 止盈 / 止损 / RSI>75+量比>2+未涨停 / RSI>85 / 最大持仓天数
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
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, sell_signals, extreme_signals,
                 stop_loss=-12.0, take_profit=993.75, board_type="main",
                 max_hold_days=20):
    """
    出场规则:
      ① 持仓天数上限  ② 止盈  ③ 止损  ④ RSI>75+量比>2+未涨停  ⑤ RSI>85  ⑥ 数据耗尽
    max_hold_days=0 表示不限制
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = ""
    max_d = len(bars) - entry_idx - 1

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # ① 持仓天数上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "max_hold"
            break

        # ② 止盈
        if b['high'] >= entry_price * (1 + take_profit / 100):
            exit_p = entry_price * (1 + take_profit / 100)
            exit_d = d
            exit_reason = "take_profit"
            break

        # ③ 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = "stop_loss"
            break

        # ④ RSI>75+量比>2+未涨停
        if sell_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "rsi_sell"
            break

        # ⑤ RSI>85
        if extreme_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "rsi_extreme"
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
# RSI 计算 (Wilder 平滑) — 修复版
# ================================================================
def compute_rsi(closes, rsi_len=14):
    """
    用前 rsi_len 个变化量的简单平均初始化 avg_gain/avg_loss,
    然后用 Wilder 指数平滑递推。
    """
    n = len(closes)
    if n < rsi_len + 1:
        return [50.0] * n

    # 用前 rsi_len 个 bar 的涨跌初始化
    gains = []
    losses = []
    for i in range(1, rsi_len + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / rsi_len
    avg_loss = sum(losses) / rsi_len

    # 填充 [0, rsi_len] 区间为 50, rsi_len 处算真实值
    rsi_out = [50.0] * (rsi_len + 1)
    if avg_loss == 0:
        rsi_out[rsi_len] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi_out[rsi_len] = 100.0 - (100.0 / (1.0 + rs))

    # Wilder 平滑递推
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
# RSI 策略信号生成 + 回测
# ================================================================
def strategy_rsi(bars, code, rsi_len=14, rsi_buy=23,
                 stop_loss=-12.0, take_profit=993.75,
                 buy_mode="next_open",
                 ma_slope_threshold=-0.5,
                 ma_slope_len=60,
                 max_hold_days=20):
    """
    RSI 超卖策略:

    买入条件 (边缘触发):
      D0: RSI[i] < rsi_buy(23) 且 RSI[i-1] >= rsi_buy 且量比<1.0(缩量) 且 MA60斜率>=-0.5%

    出场: 止盈 / 止损 / RSI>75+量比>2+未涨停 / RSI>85 / 最大持仓天数
    """
    if len(bars) < rsi_len + 2:
        return []

    board_type = get_board_type(code)
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # ---- 计算指标 ----
    rsi_values   = compute_rsi(closes, rsi_len)
    vol_ratios   = compute_volume_ratio(volumes, window=5)
    ma60_slopes  = compute_ma_slope(closes, ma_len=ma_slope_len)

    # ---- 买入信号: D0确认(RSI下穿+缩量+MA60斜率达标) ----
    # D0: RSI 下穿超卖线 + 量比<1.4(缩量) + MA60斜率达标 → 当日买入
    buy_signal = [False] * len(bars)
    d0_date = [None] * len(bars)      # 记录 D0 日期
    for i in range(1, len(bars)):
        # D0: RSI 跌破买入阈值 + 缩量(量比<1.4) + MA60斜率达标
        if (rsi_values[i] < rsi_buy and rsi_values[i - 1] >= rsi_buy
                and vol_ratios[i] < 1.4
                and ma60_slopes[i] >= ma_slope_threshold):
            buy_signal[i] = True
            d0_date[i] = bars[i]['time']

        # RSI 回到超卖线上方, D0 失效
        # (无需额外处理, 下次跌破会重新检测)

    # ---- 卖出信号 ----
    sell_signal = [False] * len(bars)
    extreme_signal = [False] * len(bars)
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]['close']
        # 涨停判断: 主板±10%, 创业板/科创板±20%
        limit_pct = 20.0 if code.startswith("30") or code.startswith("68") else 10.0
        is_limit_up = (bars[i]['close'] / prev_close - 1) * 100 >= limit_pct - 0.5 if prev_close > 0 else False
        # ④ RSI>75 且量比>2.0 且未涨停
        if rsi_values[i] > 75 and vol_ratios[i] > 2.0 and not is_limit_up:
            sell_signal[i] = True
        # ⑤ RSI>85
        if rsi_values[i] > 85:
            extreme_signal[i] = True

    # ---- 生成交易记录 ----
    trades = []
    used_dates = set()

    for i in range(len(bars)):
        if not buy_signal[i]:
            continue

        signal_date = bars[i]['time']
        if signal_date in used_dates:
            continue

        # 入场价
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

        # 找 RSI 卖出日期
        sell_date = None
        for j in range(i + 1, len(bars)):
            if sell_signal[j]:
                sell_date = bars[j]['time']
                break

        # 回测
        result = run_backtest(bars, entry_idx, entry_price, sell_signal, extreme_signal,
                              stop_loss, take_profit, board_type, max_hold_days)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'rsi_oversold',
            'path_label': 'RSI超卖',
            'signal_rsi': round(rsi_values[i], 2),
            'signal_vol_ratio': round(vol_ratios[i], 3),
            'signal_ma60_slope': round(ma60_slopes[i], 4),
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

def print_today_signals(all_trades, today_str):
    today_trades = [t for t in all_trades if t['entry_date'] == today_str]
    print(f"\n{'=' * 80}")
    print(f"  {today_str} 今日买点统计 (RSI超卖)")
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
              f"量比={t['signal_vol_ratio']:.2f} 买入{t['entry_price']:.2f}")

    return today_trades

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="RSI 超卖策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")
    parser.add_argument("--rsi-len", type=int, default=14, help="RSI 周期 (默认14)")
    parser.add_argument("--rsi-buy", type=float, default=23, help="RSI 买入阈值 (默认25)")
    parser.add_argument("--rsi-sell", type=float, default=70, help="RSI 卖出阈值 (默认70)")
    parser.add_argument("--stop-loss", type=float, default=-12.0, help="止损%% (默认-12.0)")
    parser.add_argument("--take-profit", type=float, default=993.75, help="止盈%% (默认993.75)")
    parser.add_argument("--buy-mode", default="next_open",
                        choices=["signal_close", "next_open"],
                        help="买入模式")
    parser.add_argument("--ma-slope", type=float, default=-0.5,
                        help="MA60斜率阈值%% (默认-0.5)")
    parser.add_argument("--max-hold", type=int, default=20,
                        help="最大持仓天数 (默认20, 0=不限制)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    parser.add_argument("--today", action="store_true", help="仅统计今日买点")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    mode_label = {
        "signal_close": "信号日收盘买",
        "next_open": "D+1开盘买",
    }[args.buy_mode]

    print(f"{'=' * 80}")
    print(f"RSI 超卖策略 独立回测")
    print(f"{'=' * 80}")
    print(f"买入条件: RSI({args.rsi_len})<{args.rsi_buy} + D0缩量(量比<1.4) + MA60斜率>={args.ma_slope}%")
    print(f"风控: 止损{args.stop_loss}% 止盈{args.take_profit}% 最大持仓{args.max_hold}天")
    print(f"出场: RSI>75+量比>2+未涨停 / RSI>85")
    print(f"买入模式: {mode_label}")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        trades = strategy_rsi(
            bars, code,
            rsi_len=args.rsi_len,
            rsi_buy=args.rsi_buy,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            buy_mode=args.buy_mode,
            ma_slope_threshold=args.ma_slope,
            max_hold_days=args.max_hold,
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

    print_stats(all_trades, "RSI超卖策略")

    if all_trades:
        # RSI 分布
        print(f"\n  信号RSI分布:")
        for lo, hi, label in [(0, 20, "RSI<20 (极度超卖)"), (20, 23, "RSI 20~23"),
                               (23, 25, "RSI 23~25")]:
            seg = [t for t in all_trades if lo <= t['signal_rsi'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 量比分布
        print(f"\n  信号量比分布:")
        for lo, hi, label in [(1.0, 1.3, "量比 1.0~1.3 (温和)"), (1.3, 1.6, "量比 1.3~1.6"),
                               (1.6, 2.0, "量比 1.6~2.0"), (2.0, 999, "量比 >2.0")]:
            seg = [t for t in all_trades if lo <= t['signal_vol_ratio'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 出场原因分布
        print(f"\n  出场原因分布:")
        for reason, label in [("take_profit", "止盈"), ("stop_loss", "止损"),
                               ("rsi_sell", "RSI>75卖出"), ("rsi_extreme", "RSI>85卖出"),
                               ("max_hold", "持仓到期"),
                               ("data_end", "数据耗尽")]:
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
                  f"量比={t['signal_vol_ratio']:.2f} D{t.get('turn_day',0)} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

        print(f"\n  TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['signal_rsi']:.1f} "
                  f"量比={t['signal_vol_ratio']:.2f} D{t.get('turn_day',0)} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} -> "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

    # ===== 今日买点 =====
    if args.today:
        from datetime import datetime
        today_str = args.today_date if args.today_date else datetime.now().strftime("%Y-%m-%d")
        today_trades = print_today_signals(all_trades, today_str)
        if today_trades:
            out_file = f"rsi_today_signals_{today_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(today_trades, f, ensure_ascii=False, indent=2)
            print(f"\n  {out_file} ({len(today_trades)}笔)")

    # ===== 交易明细 =====
    if args.all_trades and all_trades:
        print(f"\n  全部交易明细:")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            sell_tag = f" 卖{t['sell_signal_date']}" if t.get('sell_signal_date') else ""
            print(f"  {t['code']:<8} {t['board']:<6} "
                  f"RSI={t['signal_rsi']:.1f} 量比={t['signal_vol_ratio']:.2f} -> "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"-> 收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天{sell_tag}")

    # ===== 导出 =====
    if all_trades:
        out_file = "test_rsi_indicator_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")

if __name__ == "__main__":
    main()
