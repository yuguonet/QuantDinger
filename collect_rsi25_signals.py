#!/usr/bin/env python3
"""
从DB全量搜集 最低价<BB(20,3.0)下轨 且D1开盘价高于昨日最低价 的信号。

条件:
  - 最低价 < BB下轨 (SMA(20) - 3.0 * STD(20))
  - D1开盘价 > 信号日收盘价
  - 信号后40天内无收盘价 > D1开盘价 * 1.1

用法:
  python collect_rsi25_signals.py
  python collect_rsi25_signals.py --bb-period 20 --bb-std 3.0

输出文件:
  bb_signals.json
"""
from __future__ import annotations
import json, os, sys, argparse, math
from datetime import datetime, timedelta

# ================================================================
# 复用 test_volume_rsi_strategy.py 的 DB 和指标计算
# ================================================================
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_root = os.path.join(_script_dir, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(_script_dir, '.env')]:
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


def get_non_st_codes():
    """获取全市场非ST股票代码（通过 basicinfo_db 的 name 字段过滤）。"""
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    stocks = db.get_all_stocks(status="active")
    return [s["symbol"] for s in stocks if "ST" not in (s.get("name") or "").upper()]

def fetch_kline_db(code, start_time, end_time):
    """从DB获取指定时间范围的K线"""
    try:
        from app.data_sources.provider.adjustment import unadj_to_qfq
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D",
                            start_time=start_time, end_time=end_time, limit=0)
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
    except Exception as e:
        return []


# ================================================================
# BB (Bollinger Bands) 计算
# ================================================================
def compute_bb(closes, period=20, num_std=3.0):
    """返回 (middle, upper, lower) 三个列表，长度与closes相同，前period-1个为None"""
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
# 主逻辑
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="最低价<BB下轨 且D1开盘>昨日最低 信号采集")
    parser.add_argument("--start-date", default="2025-01-01", help="信号起始日期 (默认2025-01-01)")
    parser.add_argument("--bb-period", type=int, default=20, help="BB周期 (默认20)")
    parser.add_argument("--bb-std", type=float, default=3.0, help="BB标准差倍数 (默认3.0)")
    parser.add_argument("--before", type=int, default=0, help="信号前N天 (默认0)")
    parser.add_argument("--after", type=int, default=80, help="信号后N天 (默认80)")
    parser.add_argument("--check-days", type=int, default=40, help="检查突破的天数 (默认40)")
    parser.add_argument("--check-ratio", type=float, default=1.1, help="突破比例 (默认1.1)")
    parser.add_argument("--prefix", default="bb", help="输出文件前缀 (默认bb)")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码(空=全市场)")
    args = parser.parse_args()

    start_date = args.start_date
    bb_period = args.bb_period
    bb_std = args.bb_std
    before_days = args.before
    after_days = args.after
    check_days = args.check_days
    check_ratio = args.check_ratio
    prefix = args.prefix

    # 计算数据加载范围: 需要足够历史来计算BB，加上信号后的窗口
    load_start = (datetime.strptime(start_date, "%Y-%m-%d")
                  - timedelta(days=bb_period + 10)).strftime("%Y-%m-%d")
    load_end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"最低价<BB({bb_period},{bb_std})下轨 且D1开盘>信号日收盘 信号采集")
    print(f"  信号范围: {start_date} ~ 今天")
    print(f"  数据窗口: 前{before_days}天 ~ 后{after_days}天")
    print(f"  突破过滤: 信号后{check_days}天内无收盘>D1开盘×{check_ratio}")
    print(f"  数据加载: {load_start} ~ {load_end}")
    print()

    # 获取股票列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        print("  从DB获取全市场非ST股票列表...")
        codes = get_non_st_codes()
    print(f"  共 {len(codes)} 只股票（已排除ST）\n")

    all_signals = []
    processed = 0

    for idx, code in enumerate(codes):
        bars = fetch_kline_db(code, load_start, load_end)
        if len(bars) < bb_period + 5:
            continue

        closes = [b["close"] for b in bars]
        lows = [b["low"] for b in bars]
        bb_middle, bb_upper, bb_lower = compute_bb(closes, bb_period, bb_std)

        for i in range(bb_period, len(bars) - 1):  # -1: 需要i+1存在(D1)
            if bars[i]["time"] < start_date:
                continue
            if bb_lower[i] is None:
                continue
            # 条件1: 最低价 < BB下轨
            if lows[i] >= bb_lower[i]:
                continue
            # 条件2: D1开盘价 > 信号日收盘价
            d1_open = bars[i + 1]["open"]
            if d1_open <= closes[i]:
                continue
            # 条件3: 信号后check_days天内无收盘价 > D1开盘价 * check_ratio
            d1_idx = i + 1
            check_end = min(len(bars), d1_idx + check_days + 1)
            if any(bars[j]["close"] > d1_open * check_ratio for j in range(d1_idx, check_end)):
                continue

            signal_date = bars[i + 1]["time"]

            # 判断信号后是否有足够80根bar
            remaining_bars = len(bars) - 1 - (i + 1)
            has_enough_data = remaining_bars >= after_days

            win_start_idx = max(0, i + 1 - before_days)
            win_end_idx = min(len(bars) - 1, i + 1 + after_days)
            window_bars = bars[win_start_idx:win_end_idx + 1]

            all_signals.append({
                "symbol": code,
                "signal_date": signal_date,
                "signal_idx_in_bars": i + 1,
                "condition_date": bars[i]["time"],
                "signal_low": lows[i],
                "d1_open": d1_open,
                "bb_lower": round(bb_lower[i], 2),
                "bb_middle": round(bb_middle[i], 2),
                "bb_upper": round(bb_upper[i], 2),
                "has_enough_data": has_enough_data,
                "actual_after_bars": remaining_bars,
                "bars": window_bars,
            })

        processed += 1
        if processed % 100 == 0:
            print(f"  [{processed}/{len(codes)}] 已处理, 累计信号 {len(all_signals)} 条")

    # 按 signal_date 排序
    all_signals.sort(key=lambda x: (x["signal_date"], x["symbol"]))

    # 统计
    all_symbols = sorted(set(s["symbol"] for s in all_signals))
    enough = sum(1 for s in all_signals if s["has_enough_data"])
    print(f"\n{'=' * 60}")
    print(f"  处理: {processed} 只股票")
    print(f"  总信号: {len(all_signals)} 条 ({len(all_symbols)} 个symbol)")
    print(f"  其中数据充足(后>={after_days}bar): {enough} 条")
    print(f"{'=' * 60}")

    # ============================================================
    # 保存单个文件
    # ============================================================
    clean = []
    for s in all_signals:
        clean.append({
            "symbol": s["symbol"],
            "signal_date": s["signal_date"],
            "condition_date": s["condition_date"],
            "signal_low": s["signal_low"],
            "d1_open": s["d1_open"],
            "bb_lower": s["bb_lower"],
            "bb_middle": s["bb_middle"],
            "bb_upper": s["bb_upper"],
            "actual_after_bars": s["actual_after_bars"],
            "bars": s["bars"],
        })

    output = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bb_period": bb_period,
            "bb_std": bb_std,
            "start_date": start_date,
            "before_days": before_days,
            "after_days": after_days,
            "check_days": check_days,
            "check_ratio": check_ratio,
            "total_signals": len(all_signals),
            "total_symbols": len(all_symbols),
        },
        "symbols": all_symbols,
        "signals": clean,
    }

    fname = f"{prefix}_signals.json"
    out_path = os.path.join(_script_dir, fname)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  已保存: {fname} ({os.path.getsize(out_path) / 1024:.0f} KB, {len(clean)} 条)")


if __name__ == "__main__":
    main()
