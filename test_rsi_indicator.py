#!/usr/bin/env python3
"""
RSI 超买超卖策略 - 独立回测文件

逻辑 (源自 IndicatorStrategy "双均线金叉死叉调优"):
  1. 计算 RSI(14) — Wilder 平滑
  2. RSI < 30 触发买入信号 (边缘触发, 避免重复)
  3. RSI > 70 触发卖出信号
  4. 回测引擎: 止损2% / 止盈3.75% / 持仓20天

按照 STRATEGY_DEV_GUIDE.md 的正向转换:
  IndicatorStrategy 信号 → 独立回测 (DB + kline_cache)
"""
from __future__ import annotations
import json, time, argparse, os, sys

# ================================================================
# DB 数据加载 (复用 test_dragon_callback 的逻辑)
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
    """从DB加载日线, 返回与 fetch_kline 兼容的格式 (list[dict])"""
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
# 回测引擎 (复用 test_dragon_callback 的 run_backtest)
# ================================================================
def run_backtest(bars, entry_idx, entry_price, sell_signals, stop_loss=-2.0, take_profit=3.75, board_type="main"):
    """
    回测引擎 (与 IndicatorStrategy 出场规则一致):
      - 止盈: take_profit% (来自 # @strategy takeProfitPct)
      - 止损: stop_loss% (来自 # @strategy stopLossPct)
      - RSI>70 卖出信号: sell_signals 为 True 的那天收盘平仓
      - 无持仓天数限制, 一直持有直到触发以上三者之一或数据耗尽
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = ""

    max_d = len(bars) - entry_idx - 1  # 最大可持仓天数

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # ① 止盈
        if b['high'] >= entry_price * (1 + take_profit / 100):
            exit_p = entry_price * (1 + take_profit / 100)
            exit_d = d
            exit_reason = "take_profit"
            break

        # ② 止损
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = "stop_loss"
            break

        # ③ RSI 卖出信号 (与原始 IndicatorStrategy df['sell'] 一致)
        if sell_signals[idx]:
            exit_p = b['close']
            exit_d = d
            exit_reason = "rsi_sell"
            break

        # 持仓中, 更新为当日收盘价
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
# RSI 计算 (与 IndicatorStrategy 一致: Wilder 平滑, 纯 Python)
# ================================================================
def compute_rsi(closes, rsi_len=14):
    """
    计算 RSI — 使用 Wilder 平滑 (与 IndicatorStrategy 完全一致)
    返回: list[float], 长度与 closes 相同

    原始 IndicatorStrategy:
      delta = df['close'].diff()        → delta[0] = NaN
      gain = delta.clip(lower=0)        → gain[0] = NaN
      avg_gain = gain.ewm(alpha=1/rsi_len, adjust=False).mean()

    pandas ewm(adjust=False) 对 NaN 的处理:
      NaN 被当作 0 参与递推 (不传播)。
      y[0] = x[0]  (NaN→0)
      y[i] = alpha * x[i] + (1-alpha) * y[i-1]
    """
    n = len(closes)
    if n < 2:
        return [50.0] * n

    alpha = 1.0 / rsi_len
    rsi_out = [50.0]  # index 0: delta=NaN→0, avg_gain=avg_loss=0, RSI=50

    # index 0: NaN→0, avg_gain = 0, avg_loss = 0
    avg_gain = 0.0
    avg_loss = 0.0

    # index 1..n-1: 标准 EWM 递推
    for i in range(1, n):
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
# RSI 策略信号生成 + 回测
# ================================================================
def strategy_rsi(bars, code, rsi_len=14, rsi_buy=30, rsi_sell=70,
                 stop_loss=-2.0, take_profit=3.75,
                 buy_mode="signal_close"):
    """
    RSI 超买超卖策略 (从 IndicatorStrategy 正向转换):

    信号逻辑 (与原 IndicatorStrategy 完全一致):
      - RSI < rsi_buy (30) → 买入信号 (边缘触发)
      - RSI > rsi_sell (70) → 卖出信号

    回测参数 (来自 # @strategy 注释):
      - stopLossPct 0.02  → stop_loss = -2%
      - takeProfitPct 0.0375 → take_profit = +3.75%
      - entryPct 1 → 全仓

    出场规则 (与原 IndicatorStrategy 完全一致):
      - 止盈 +3.75%
      - 止损 -2%
      - RSI>70 卖出信号 (df['sell'])
      - 无持仓天数限制

    buy_mode:
      signal_close — 信号日收盘买 (默认)
      next_open    — D+1 开盘买
      signal_low   — 信号日最低价买 (理想)
    """
    if len(bars) < rsi_len + 2:
        return []

    board_type = get_board_type(code)

    # 计算 RSI
    closes = [b['close'] for b in bars]
    rsi_values = compute_rsi(closes, rsi_len)

    # 生成信号 (与 IndicatorStrategy 完全一致的边缘触发)
    # raw_buy = rsi < rsi_buy, 本次 True & 上次 False → 边缘触发
    buy_signal = [False] * len(bars)
    sell_signal = [False] * len(bars)
    for i in range(1, len(bars)):
        if rsi_values[i] < rsi_buy and rsi_values[i - 1] >= rsi_buy:
            buy_signal[i] = True
        if rsi_values[i] > rsi_sell and rsi_values[i - 1] <= rsi_sell:
            sell_signal[i] = True

    trades = []
    used_dates = set()

    for i in range(len(bars)):
        if not buy_signal[i]:
            continue

        signal_date = bars[i]['time']

        # 去重: 同一天不重复
        if signal_date in used_dates:
            continue

        # 确定入场价
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
        elif buy_mode == "signal_low":
            entry_price = bars[i]['low']
            entry_idx = i
            entry_date = signal_date
        else:
            continue

        if entry_price <= 0:
            continue

        used_dates.add(signal_date)

        # 找对应的卖出信号 (用于记录, 不影响回测)
        sell_date = None
        for j in range(i + 1, len(bars)):
            if sell_signal[j]:
                sell_date = bars[j]['time']
                break

        # 执行回测
        result = run_backtest(bars, entry_idx, entry_price, sell_signal, stop_loss, take_profit, board_type)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'rsi_oversold',
            'path_label': 'RSI超卖',
            'rsi_len': rsi_len,
            'rsi_value': round(rsi_values[i], 2),
            'signal_date': signal_date,
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            'sell_signal_date': sell_date,
            **result,
        })

    return trades

# ================================================================
# 测试股票列表 (复用 test_dragon_callback 的列表)
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
    print(f"  {label}: {n:>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% 盈亏比{pl:.2f} 总收益{total_ret:>+6.2f}% 最大单笔{max_dd:>+6.2f}%")

def print_today_signals(all_trades, today_str):
    """统计今日出现买点的股票"""
    today_trades = [t for t in all_trades if t['entry_date'] == today_str]
    print(f"\n{'=' * 80}")
    print(f"📅 {today_str} 今日买点统计 (RSI超卖)")
    print(f"{'=' * 80}")
    if not today_trades:
        print(f"  今日无买点信号")
        return today_trades

    print(f"  共 {len(today_trades)} 只股票出现 RSI 超卖买点")

    # 按板块分组
    main_today = [t for t in today_trades if t['board'] in ('沪主板', '深主板')]
    gem_today = [t for t in today_trades if t['board'] == '创业板']
    star_today = [t for t in today_trades if t['board'] == '科创板']

    print(f"\n  📊 板块分布:")
    if main_today:
        print(f"    🏛️  主板: {len(main_today)} 只")
    if gem_today:
        print(f"    💎 创业板: {len(gem_today)} 只")
    if star_today:
        print(f"    🚀 科创板: {len(star_today)} 只")

    # 按 RSI 值排序列出
    print(f"\n  📋 今日 RSI 超卖信号 ({len(today_trades)}只):")
    for t in sorted(today_trades, key=lambda x: x['rsi_value']):
        print(f"    {t['code']:<8} {t['board']:<6} RSI={t['rsi_value']:.1f} "
              f"信号{t['signal_date']} 买入{t['entry_price']:.2f}")

    return today_trades

# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="RSI 超买超卖策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码, 留空使用默认列表")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual=缓存+远程(默认), db=从数据库加载")
    parser.add_argument("--rsi-len", type=int, default=14, help="RSI 周期 (默认14)")
    parser.add_argument("--rsi-buy", type=float, default=30, help="RSI 买入阈值 (默认30)")
    parser.add_argument("--rsi-sell", type=float, default=70, help="RSI 卖出阈值 (默认70)")
    parser.add_argument("--stop-loss", type=float, default=-2.0, help="止损%% (默认-2.0)")
    parser.add_argument("--take-profit", type=float, default=3.75, help="止盈%% (默认3.75)")
    parser.add_argument("--buy-mode", default="signal_close",
                        choices=["signal_close", "next_open", "signal_low"],
                        help="买入模式: signal_close=信号日收盘买(默认), next_open=D+1开盘买, signal_low=信号日最低价(理想)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    parser.add_argument("--today", action="store_true", help="仅统计今日买点")
    parser.add_argument("--today-date", type=str, default="", help="指定日期(YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N 交易 (默认10)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("📊 DB模式: 从数据库加载全市场股票...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    mode_label = {
        "signal_close": "信号日收盘买",
        "next_open": "D+1开盘买",
        "signal_low": "信号日最低价(理想)",
    }[args.buy_mode]

    print(f"{'=' * 80}")
    print(f"RSI 超买超卖策略 独立回测")
    print(f"{'=' * 80}")
    print(f"策略参数: RSI({args.rsi_len}) 买入<{args.rsi_buy} 卖出>{args.rsi_sell}")
    print(f"风控参数: 止损{args.stop_loss}% 止盈{args.take_profit}%")
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
            rsi_sell=args.rsi_sell,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            buy_mode=args.buy_mode,
        )
        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) ✓{len(bars)}根 → RSI{len(trades)}笔")

        success += 1
        if not use_db:
            time.sleep(0.15)

    # ===== 结果统计 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    print_stats(all_trades, "RSI超卖策略")

    if all_trades:
        # RSI 值分布
        print(f"\n  📊 RSI 值分布:")
        for lo, hi, label in [(0, 20, "RSI<20 (极度超卖)"), (20, 25, "RSI 20-25"),
                               (25, 30, "RSI 25-30"), (30, 50, "RSI 30-50 (误触发)")]:
            seg = [t for t in all_trades if lo <= t['rsi_value'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # 出场原因分布
        print(f"\n  📊 出场原因分布:")
        for reason, label in [("take_profit", "止盈(+3.75%)"), ("stop_loss", "止损(-2%)"),
                               ("rsi_sell", "RSI>70卖出"), ("data_end", "数据耗尽")]:
            seg = [t for t in all_trades if t.get('exit_reason') == reason]
            if seg:
                print_stats(seg, f"    {label}")

        # 板块分布
        print(f"\n  📊 板块分布:")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, f"    {board}")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n  🏆 TOP{n} 盈利:")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['rsi_value']:.1f} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} → "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天")

        print(f"\n  💀 TOP{n} 亏损:")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"    {t['code']:<8} {t['board']:<6} RSI={t['rsi_value']:.1f} "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} → "
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
            print(f"\n💾 {out_file} ({len(today_trades)}笔)")

    # ===== 交易明细 =====
    if args.all_trades and all_trades:
        print(f"\n📋 全部交易明细:")
        for t in sorted(all_trades, key=lambda x: x['entry_date']):
            sell_tag = f" 卖{t['sell_signal_date']}" if t.get('sell_signal_date') else ""
            print(f"  {t['code']:<8} {t['board']:<6} RSI={t['rsi_value']:.1f} "
                  f"{t['signal_date']}信号 → {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"→ 收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}% "
                  f"持仓{t['exit_day']}天{sell_tag}")

    # ===== 导出 =====
    if all_trades:
        out_file = "test_rsi_indicator_result.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {out_file} ({len(all_trades)}笔)")

if __name__ == "__main__":
    main()
