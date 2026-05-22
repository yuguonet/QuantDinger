"""
连板猎手 V1 — 独立回测版 (最终版, 支持 CSV/DB)

筛选逻辑:
  D0盘后: 量比>2x + 上影线<0.5% + 排除一字板
  D1早盘: 主板涨幅<2% / 创科涨幅<5% → 买入

用法:
  # CSV模式
  python optimizer/strategy_dragon_v1.py --csv analysis_output/dragon_ohlcv.csv

  # DB模式 (全市场)
  python optimizer/strategy_dragon_v1.py --source db
  python optimizer/strategy_dragon_v1.py --source db --start 2024-01-01 --end 2026-05-21
  python optimizer/strategy_dragon_v1.py --source db --quick
  python optimizer/strategy_dragon_v1.py --source db --sample 1000

  # 调参
  python optimizer/strategy_dragon_v1.py --csv analysis_output/dragon_ohlcv.csv --min-vol-ratio 3
  python optimizer/strategy_dragon_v1.py --source db --max-d1-gap 3
"""
from __future__ import annotations
import os, sys
import csv
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

# pandas/numpy 仅 DB 模式需要, 延迟导入
pd = None
np = None

# 路径
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ================================================================
# 通用工具
# ================================================================

def try_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def get_board_type(code):
    c = str(code)[:3]
    if c.startswith("30") or c.startswith("68"):
        return "gem_star"
    return "main"


def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


BOARD_PARAMS = {
    "main": {
        "threshold": 0.098,
        "min_streak": 2,
        "stop_loss_pct": -8,
        "trailing_stop_pct": -6,
        "take_profit_pct": 15,
    },
    "gem_star": {
        "threshold": 0.198,
        "min_streak": 2,
        "max_streak": 4,
        "stop_loss_pct": -12,
        "trailing_stop_pct": -8,
        "take_profit_pct": 20,
    },
}


# ================================================================
# DB 接口
# ================================================================

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, ".env"), os.path.join(_project_root, ".env")]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


def _get_writer():
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []


def load_daily_db(code, start, end):
    import pandas as pd
    writer = _get_writer()
    data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
    if not data:
        return None
    df = pd.DataFrame(data)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ================================================================
# V1 策略核心: D0筛选 → D+1开盘买入
# ================================================================

def backtest_v1_csv(rows, params, max_d1_gap=2.0, min_vol_ratio=2.0, max_upper_shadow=0.5):
    """V1回测 (CSV模式, 单个连板段)"""
    if len(rows) < 6:
        return []

    threshold = params["threshold"]

    # 找第一板
    first_limit_idx = None
    for i in range(1, len(rows)):
        row = rows[i]
        prev = rows[i - 1]
        close = try_float(row["close"])
        prev_close = try_float(prev["close"])
        if prev_close <= 0:
            continue
        ret = (close / prev_close - 1)
        if ret >= threshold * 0.98:
            if i >= 2:
                prev2 = rows[i - 2]
                prev2_close = try_float(prev2["close"])
                if prev2_close > 0 and prev_close / prev2_close - 1 >= threshold * 0.98:
                    continue
            first_limit_idx = i
            break

    if first_limit_idx is None or first_limit_idx < 2:
        return []

    fl = rows[first_limit_idx]
    fl_prev = rows[first_limit_idx - 1]
    fl_prev2 = rows[first_limit_idx - 2]
    fl_close = try_float(fl["close"])
    fl_high = try_float(fl["high"])
    fl_low = try_float(fl["low"])
    fl_vol = try_float(fl["volume"])
    fl_prev_close = try_float(fl_prev["close"])
    fl_prev_vol = try_float(fl_prev["volume"])
    fl_prev2_close = try_float(fl_prev2["close"])

    if fl_prev_close <= 0 or fl_close <= 0 or fl_prev2_close <= 0:
        return []

    # D0筛选
    bar_range = (fl_high - fl_low) / fl_prev2_close * 100
    if bar_range < 0.2:
        return []
    vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
    if vol_ratio < min_vol_ratio:
        return []
    upper_shadow = (fl_high - fl_close) / fl_prev2_close * 100
    if upper_shadow >= max_upper_shadow:
        return []

    # D+1
    if first_limit_idx + 1 >= len(rows):
        return []
    d1 = rows[first_limit_idx + 1]
    d1_open = try_float(d1["open"])
    if d1_open <= 0:
        return []

    d1_gap = (d1_open / fl_close - 1) * 100
    if d1_gap > max_d1_gap:
        return []

    # 持仓回测
    return _run_trade(rows, first_limit_idx + 1, d1_open, params, {
        "d0_vol_ratio": round(vol_ratio, 2),
        "d0_upper_shadow": round(upper_shadow, 4),
        "d1_gap": round(d1_gap, 2),
    })


def backtest_v1_db(df, code, params, max_d1_gap=2.0, min_vol_ratio=2.0, max_upper_shadow=0.5):
    """V1回测 (DB模式, 单只股票全量扫描)"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)

    if n < 6:
        return []

    threshold = params["threshold"]
    trades = []

    i = 1
    while i < n - 1:
        # 找涨停日
        ret = (close[i] / close[i - 1] - 1) if close[i - 1] > 0 else 0
        if ret < threshold * 0.98:
            i += 1
            continue

        # 检查是否第一板 (前一日非涨停)
        if i >= 2:
            prev_ret = (close[i - 1] / close[i - 2] - 1) if close[i - 2] > 0 else 0
            if prev_ret >= threshold * 0.98:
                i += 1
                continue

        # 是第一板, D0筛选
        fl_close = close[i]
        fl_high = high[i]
        fl_low = low[i]
        fl_vol = volume[i]
        fl_prev_close = close[i - 1]
        fl_prev_vol = volume[i - 1]
        fl_prev2_close = close[i - 2] if i >= 2 else close[i - 1]

        if fl_prev_close <= 0 or fl_prev2_close <= 0:
            i += 1
            continue

        # 排除一字板
        bar_range = (fl_high - fl_low) / fl_prev2_close * 100
        if bar_range < 0.2:
            i += 1
            continue

        # 量比
        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        if vol_ratio < min_vol_ratio:
            i += 1
            continue

        # 上影线
        upper_shadow = (fl_high - fl_close) / fl_prev2_close * 100
        if upper_shadow >= max_upper_shadow:
            i += 1
            continue

        # D+1
        if i + 1 >= n:
            i += 1
            continue
        d1_open = df["open"].values[i + 1]
        if d1_open <= 0:
            i += 1
            continue

        d1_gap = (d1_open / fl_close - 1) * 100
        if d1_gap > max_d1_gap:
            i += 1
            continue

        # 持仓回测
        trade = _run_trade_db(df, i + 1, d1_open, params)
        if trade:
            trade["d0_vol_ratio"] = round(vol_ratio, 2)
            trade["d0_upper_shadow"] = round(upper_shadow, 4)
            trade["d1_gap"] = round(d1_gap, 2)
            trade["buy_date"] = str(df.index[i + 1])
            trades.append(trade)

        # 跳过这个连板段
        i += 2

    return trades


def _run_trade(rows, buy_idx, buy_price, params, extra=None):
    """持仓回测 (CSV模式)"""
    position = {"buy_price": buy_price, "highest": buy_price}
    for i in range(buy_idx + 1, len(rows)):
        row = rows[i]
        close = try_float(row["close"])
        high = try_float(row["high"])
        if high > position["highest"]:
            position["highest"] = high
        ret_from_buy = (close / position["buy_price"] - 1) * 100
        ret_from_high = (close / position["highest"] - 1) * 100 if position["highest"] > 0 else 0

        sell = False
        sell_type = ""
        if ret_from_buy <= params["stop_loss_pct"]:
            sell = True; sell_type = "stop_loss"
        elif ret_from_high <= params["trailing_stop_pct"] and ret_from_buy > 0:
            sell = True; sell_type = "trailing_stop"
        elif ret_from_buy >= params["take_profit_pct"]:
            sell = True; sell_type = "take_profit"

        if sell:
            result = {
                "buy_date": rows[buy_idx]["time"] if "time" in rows[buy_idx] else "",
                "buy_price": buy_price,
                "sell_date": row["time"] if "time" in row else "",
                "sell_price": close,
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": sell_type,
            }
            if extra:
                result.update(extra)
            return [result]

    last = rows[-1]
    close = try_float(last["close"])
    result = {
        "buy_date": rows[buy_idx]["time"] if "time" in rows[buy_idx] else "",
        "buy_price": buy_price,
        "sell_date": last["time"] if "time" in last else "",
        "sell_price": close,
        "return_pct": round((close / position["buy_price"] - 1) * 100, 2),
        "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
        "sell_type": "end_of_data",
    }
    if extra:
        result.update(extra)
    return [result]


def _run_trade_db(df, buy_idx, buy_price, params):
    """持仓回测 (DB模式, 返回单个trade dict或None)"""
    position = {"buy_price": buy_price, "highest": buy_price}
    close_arr = df["close"].values
    high_arr = df["high"].values
    n = len(close_arr)

    for i in range(buy_idx + 1, n):
        close = float(close_arr[i])
        high = float(high_arr[i])
        if high > position["highest"]:
            position["highest"] = high
        ret_from_buy = (close / position["buy_price"] - 1) * 100
        ret_from_high = (close / position["highest"] - 1) * 100 if position["highest"] > 0 else 0

        if ret_from_buy <= params["stop_loss_pct"]:
            return {
                "sell_date": str(df.index[i]),
                "sell_price": close,
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "stop_loss",
            }
        elif ret_from_high <= params["trailing_stop_pct"] and ret_from_buy > 0:
            return {
                "sell_date": str(df.index[i]),
                "sell_price": close,
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "trailing_stop",
            }
        elif ret_from_buy >= params["take_profit_pct"]:
            return {
                "sell_date": str(df.index[i]),
                "sell_price": close,
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "take_profit",
            }

    # 数据结束
    close = float(close_arr[-1])
    return {
        "sell_date": str(df.index[-1]),
        "sell_price": close,
        "return_pct": round((close / position["buy_price"] - 1) * 100, 2),
        "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
        "sell_type": "end_of_data",
    }


# ================================================================
# 全量回测
# ================================================================

def run_csv(csv_path, max_d1_gap, min_vol_ratio, max_upper_shadow):
    """CSV模式全量回测"""
    print("📊 加载数据...")
    groups = defaultdict(list)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["code"], row["run_first_limit_date"])
            groups[key].append(row)
    for key in groups:
        groups[key].sort(key=lambda r: r["time"])

    total = len(groups)
    print(f"   连板段: {total}")
    print(f"   筛选: 量比>={min_vol_ratio}x + 上影线<{max_upper_shadow}% + D+1涨幅<{max_d1_gap}%")

    all_trades = []
    cnt = 0
    for (code, fl_str), rows in groups.items():
        cnt += 1
        if cnt % 5000 == 0:
            print(f"\r   进度: {cnt}/{total}", end="", flush=True)

        board_type = get_board_type(code)
        params = BOARD_PARAMS[board_type]
        try:
            n_limit = int(rows[0].get("run_n_limit_ups", 1))
        except (ValueError, IndexError):
            n_limit = 1
        if n_limit < params["min_streak"]:
            continue

        trades = backtest_v1_csv(rows, params, max_d1_gap, min_vol_ratio, max_upper_shadow)
        for t in trades:
            t["code"] = code
            t["board"] = get_board_name(code)
            t["board_type"] = board_type
            t["n_limit"] = n_limit
        all_trades.extend(trades)

    print(f"\r   完成: {total} 连板段")
    return all_trades


def run_db(start_date, end_date, max_d1_gap, min_vol_ratio, max_upper_shadow, quick, sample):
    """DB模式全量回测"""
    global pd, np
    import pandas as _pd
    import numpy as _np
    pd = _pd
    np = _np

    print("📊 DB 模式: 从 db_market 加载数据...")
    codes = get_all_codes_db()
    print(f"   全市场: {len(codes)} 只股票")

    if quick:
        codes = codes[:500]
    elif sample > 0:
        codes = codes[:sample]

    # 查询区间: 前置30天, 后置20天
    buffer_pre = 30
    buffer_post = 20
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer_pre)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=buffer_post)).strftime("%Y-%m-%d")
    sd = pd.Timestamp(start_date)
    ed = pd.Timestamp(end_date)

    all_trades = []
    total = len(codes)
    loaded = 0
    signal_count = 0

    for code_idx, code in enumerate(codes):
        if (code_idx + 1) % 500 == 0 or code_idx == 0:
            print(f"\r   扫描: {code_idx+1}/{total}  已加载: {loaded}  信号: {signal_count}", end="", flush=True)
        try:
            df = load_daily_db(code, query_start, query_end)
            if df is None or len(df) < 15:
                continue
            loaded += 1

            board_type = get_board_type(code)
            params = BOARD_PARAMS[board_type]

            trades = backtest_v1_db(df, code, params, max_d1_gap, min_vol_ratio, max_upper_shadow)
            for t in trades:
                # 过滤: 买入日期必须在回测区间内
                buy_date = pd.Timestamp(t.get("buy_date", ""))
                if buy_date < sd or buy_date > ed:
                    continue
                t["code"] = code
                t["board"] = get_board_name(code)
                t["board_type"] = board_type
                # 连板数 (DB模式下简单估算: 从买入日往后数涨停天数)
                t["n_limit"] = _count_streak_db(df, buy_date, params["threshold"])
                all_trades.append(t)
                signal_count += 1
        except Exception:
            continue

    print(f"\r   扫描完成: {loaded}/{total} 只股票  交易信号: {signal_count}")
    return all_trades


def _count_streak_db(df, buy_date, threshold):
    """从D+1开始数连板天数 (用于DB模式)"""
    import pandas as pd
    try:
        idx = df.index.get_loc(pd.Timestamp(buy_date))
        if isinstance(idx, slice):
            idx = idx.start
    except KeyError:
        return 2

    close = df["close"].values
    n = len(close)
    count = 0
    # buy_date是D+1, 从D+1开始数涨停天数
    for i in range(idx, n):
        if i == 0:
            continue
        ret = (close[i] / close[i - 1] - 1) if close[i - 1] > 0 else 0
        if ret >= threshold * 0.98:
            count += 1
        else:
            break
    return max(count, 2)  # 至少2板 (因为D0是第一板)


# ================================================================
# 输出
# ================================================================

def print_summary(trades):
    if not trades:
        print("❌ 无交易信号")
        return

    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]

    print(f"\n{'='*70}")
    print(f"  连板猎手 V1 回测结果")
    print(f"{'='*70}")
    print(f"  总交易数: {len(trades)}")
    print(f"  胜率: {len(wins)/len(rets)*100:.1f}%")
    print(f"  平均收益: {sum(rets)/len(rets):.2f}%")
    print(f"  中位收益: {sorted(rets)[len(rets)//2]:.2f}%")
    if wins and losses:
        print(f"  盈亏比: {sum(wins)/len(wins) / abs(sum(losses)/len(losses)):.2f}")

    # 按板块
    by_bt = defaultdict(list)
    for t in trades:
        by_bt[t.get("board_type", "unknown")].append(t)
    for bt in ["main", "gem_star"]:
        sub = by_bt.get(bt, [])
        if not sub:
            continue
        blabel = "沪深主板" if bt == "main" else "创/科板"
        sub_rets = [t["return_pct"] for t in sub]
        sub_wins = [r for r in sub_rets if r > 0]
        print(f"\n  --- {blabel} ({len(sub)}笔) ---")
        print(f"    胜率: {len(sub_wins)/len(sub_rets)*100:.1f}%")
        print(f"    均收益: {sum(sub_rets)/len(sub_rets):.2f}%")
        if sub_wins and len(sub_wins) < len(sub_rets):
            sub_loss = [r for r in sub_rets if r < 0]
            print(f"    盈亏比: {sum(sub_wins)/len(sub_wins) / abs(sum(sub_loss)/len(sub_loss)):.2f}")

    # 按细分板块
    by_board = defaultdict(list)
    for t in trades:
        by_board[t.get("board", "?")].append(t)
    print(f"\n  --- 按板块 ---")
    for board in ["沪主板", "深主板", "创业板", "科创板"]:
        sub = by_board.get(board, [])
        if not sub:
            continue
        sub_rets = [t["return_pct"] for t in sub]
        sub_wins = [r for r in sub_rets if r > 0]
        print(f"    {board}: {len(sub)}笔 胜率{len(sub_wins)/len(sub_rets)*100:.1f}% 均收益{sum(sub_rets)/len(sub_rets):.2f}%")

    # 按连板数
    by_nl = defaultdict(list)
    for t in trades:
        by_nl[t.get("n_limit", 0)].append(t)
    print(f"\n  --- 按连板数 ---")
    for nl in sorted(by_nl.keys()):
        sub = by_nl[nl]
        if len(sub) < 3:
            continue
        sub_rets = [t["return_pct"] for t in sub]
        sub_wins = [r for r in sub_rets if r > 0]
        print(f"    {nl}板: {len(sub)}笔 胜率{len(sub_wins)/len(sub_rets)*100:.1f}% 均收益{sum(sub_rets)/len(sub_rets):.2f}%")

    # 卖出类型
    print(f"\n  --- 卖出类型 ---")
    by_st = defaultdict(list)
    for t in trades:
        by_st[t.get("sell_type", "unknown")].append(t)
    for st in ["take_profit", "trailing_stop", "stop_loss", "end_of_data"]:
        st_trades = by_st.get(st, [])
        if not st_trades:
            continue
        sub_rets = [t["return_pct"] for t in st_trades]
        sub_wins = [r for r in sub_rets if r > 0]
        print(f"    {st}: {len(st_trades)}笔 均收益{sum(sub_rets)/len(sub_rets):.2f}% 胜率{len(sub_wins)/len(sub_rets)*100:.1f}%")


# ================================================================
# 入口
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="连板猎手V1 — D0筛选→D+1开盘买入 (支持 CSV/DB)")
    parser.add_argument("--source", choices=["csv", "db"], default="csv",
                        help="数据源: csv (默认) 或 db (PostgreSQL)")
    parser.add_argument("--csv", default="analysis_output/dragon_ohlcv.csv",
                        help="CSV 文件路径")
    parser.add_argument("--start", type=str, default="2024-01-01",
                        help="回测开始日期 (DB模式)")
    parser.add_argument("--end", type=str, default="2026-05-21",
                        help="回测结束日期 (DB模式)")
    parser.add_argument("--quick", action="store_true",
                        help="抽样500只 (DB模式)")
    parser.add_argument("--sample", type=int, default=0,
                        help="抽样N只 (DB模式)")
    parser.add_argument("--max-d1-gap", type=float, default=2.0,
                        help="D+1最大高开%% (默认2.0)")
    parser.add_argument("--min-vol-ratio", type=float, default=2.0,
                        help="D0最小量比 (默认2.0)")
    parser.add_argument("--max-upper-shadow", type=float, default=0.5,
                        help="D0最大上影线%% (默认0.5)")
    args = parser.parse_args()

    if args.source == "db":
        all_trades = run_db(args.start, args.end,
                           args.max_d1_gap, args.min_vol_ratio, args.max_upper_shadow,
                           args.quick, args.sample)
    else:
        all_trades = run_csv(args.csv,
                            args.max_d1_gap, args.min_vol_ratio, args.max_upper_shadow)

    print_summary(all_trades)

    # 保存
    out_path = "analysis_output/backtest_v1_trades.csv"
    if all_trades:
        # 确定字段
        fieldnames = list(all_trades[0].keys())
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_trades)
        print(f"\n💾 交易明细: {out_path}")


if __name__ == "__main__":
    main()
