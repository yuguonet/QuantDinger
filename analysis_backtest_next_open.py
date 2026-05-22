"""
连板猎手 v3.1 — 买入时机修正分析 (纯Python, 无第三方依赖)

核心问题:
  原策略在涨停日以收盘价买入,但涨停日封死时实际买不到
  修正: 信号次日(D+1)以开盘价买入

对比: A) 涨停日close买入  B) 次日open买入
"""
import csv
import os
from collections import defaultdict
from datetime import datetime

CSV_PATH = "analysis_output/dragon_ohlcv.csv"

BOARD_PARAMS = {
    "main": {
        "threshold": 0.098,
        "min_streak": 2,
        "buy_max_gap_pct": 8.0,
        "buy_seal_max": 0.5,
        "stop_loss_pct": -8,
        "trailing_stop_pct": -6,
        "take_profit_pct": 15,
    },
    "gem_star": {
        "threshold": 0.198,
        "min_streak": 2,
        "max_streak": 4,
        "buy_max_gap_pct": 12.0,
        "buy_seal_max": 0.5,
        "stop_loss_pct": -12,
        "trailing_stop_pct": -8,
        "take_profit_pct": 20,
    },
}


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


def load_and_group(csv_path):
    """加载CSV并按(code, run_first_limit_date)分组"""
    groups = defaultdict(list)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["code"], row["run_first_limit_date"])
            groups[key].append(row)

    # 每组按time排序
    for key in groups:
        groups[key].sort(key=lambda r: r["time"])

    return groups


def backtest_run(rows, params, buy_on_next_open=False):
    """对单个连板段回测"""
    if len(rows) < 5:
        return []

    threshold = params["threshold"]
    trades = []
    position = None

    for i in range(1, len(rows)):
        row = rows[i]
        prev = rows[i - 1]

        try:
            close = float(row["close"])
            prev_close = float(prev["close"])
            open_p = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
        except (ValueError, KeyError):
            continue

        if prev_close <= 0:
            continue

        if position is None:
            # 检查涨停
            ret = (close / prev_close - 1)
            if ret < threshold * 0.98:
                continue

            # 高开过滤
            gap_pct = (open_p / prev_close - 1) * 100
            if gap_pct > params["buy_max_gap_pct"]:
                continue

            # 封板强度
            if high > 0:
                seal = (close / high - 1) * 100
                if seal > params["buy_seal_max"]:
                    continue

            # 买入
            if buy_on_next_open:
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                try:
                    buy_price = float(next_row["open"])
                except (ValueError, KeyError):
                    continue
                if buy_price <= 0:
                    continue
                buy_idx = i + 1
                buy_date = next_row["time"]
            else:
                buy_price = close
                buy_idx = i
                buy_date = row["time"]

            position = {
                "buy_price": buy_price,
                "buy_date": buy_date,
                "buy_idx": buy_idx,
                "highest": buy_price,
            }
        else:
            # 更新最高价
            if high > position["highest"]:
                position["highest"] = high

            ret_from_buy = (close / position["buy_price"] - 1) * 100
            ret_from_high = (close / position["highest"] - 1) * 100 if position["highest"] > 0 else 0

            sell = False
            sell_type = ""

            if ret_from_buy <= params["stop_loss_pct"]:
                sell = True
                sell_type = "stop_loss"
            elif ret_from_high <= params["trailing_stop_pct"] and ret_from_buy > 0:
                sell = True
                sell_type = "trailing_stop"
            elif ret_from_buy >= params["take_profit_pct"]:
                sell = True
                sell_type = "take_profit"

            if sell:
                trades.append({
                    "buy_date": position["buy_date"],
                    "buy_price": position["buy_price"],
                    "sell_date": row["time"],
                    "sell_price": close,
                    "return_pct": round(ret_from_buy, 2),
                    "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                    "sell_type": sell_type,
                })
                position = None

    # 数据结束平仓
    if position is not None:
        last = rows[-1]
        try:
            close = float(last["close"])
            ret_from_buy = (close / position["buy_price"] - 1) * 100
            trades.append({
                "buy_date": position["buy_date"],
                "buy_price": position["buy_price"],
                "sell_date": last["time"],
                "sell_price": close,
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "end_of_data",
            })
        except (ValueError, KeyError):
            pass

    return trades


def calc_stats(trades):
    """计算统计指标"""
    if not trades:
        return {"count": 0, "win_rate": 0, "avg_ret": 0, "med_ret": 0, "win_avg": 0, "loss_avg": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    return {
        "count": len(trades),
        "win_rate": len(wins) / len(rets) * 100,
        "avg_ret": sum(rets) / len(rets),
        "med_ret": sorted(rets)[len(rets) // 2],
        "win_avg": sum(wins) / len(wins) if wins else 0,
        "loss_avg": abs(sum(losses) / len(losses)) if losses else 0,
        "max_win": max(rets) if rets else 0,
        "max_loss": min(rets) if rets else 0,
    }


def print_stats(stats, label):
    print(f"\n  {label}:")
    print(f"    交易数: {stats['count']}")
    print(f"    胜率: {stats['win_rate']:.1f}%")
    print(f"    均收益: {stats['avg_ret']:.2f}%")
    print(f"    中位收益: {stats['med_ret']:.2f}%")
    if stats['loss_avg'] > 0:
        print(f"    盈亏比: {stats['win_avg'] / stats['loss_avg']:.2f}")
    print(f"    最大盈利: {stats['max_win']:.2f}%  最大亏损: {stats['max_loss']:.2f}%")


def main():
    print("📊 加载数据...")
    groups = load_and_group(CSV_PATH)
    total = len(groups)
    print(f"   连板段: {total}")

    # 收集所有交易
    trades_orig = []  # 涨停日close买入
    trades_next = []  # 次日open买入
    # D+1溢价统计
    d1_gaps = {"main": [], "gem_star": []}
    d1_gaps_by_n = defaultdict(list)

    cnt = 0
    for (code, fl_str), rows in groups.items():
        cnt += 1
        if cnt % 5000 == 0:
            print(f"\r   进度: {cnt}/{total}", end="", flush=True)

        board_type = get_board_type(code)
        params = BOARD_PARAMS[board_type]
        board_name = get_board_name(code)

        try:
            n_limit = int(rows[0].get("run_n_limit_ups", 1))
        except (ValueError, IndexError):
            n_limit = 1

        # 原始
        for t in backtest_run(rows, params, buy_on_next_open=False):
            t["code"] = code
            t["board"] = board_name
            t["board_type"] = board_type
            t["n_limit"] = n_limit
            trades_orig.append(t)

        # 次日开盘
        for t in backtest_run(rows, params, buy_on_next_open=True):
            t["code"] = code
            t["board"] = board_name
            t["board_type"] = board_type
            t["n_limit"] = n_limit
            trades_next.append(t)

        # D+1溢价: 找涨停日,统计次日open vs 涨停日close
        for i in range(1, len(rows)):
            row = rows[i]
            prev = rows[i - 1]
            try:
                close = float(row["close"])
                prev_close = float(prev["close"])
                if prev_close <= 0:
                    continue
                ret = (close / prev_close - 1)
                if ret < params["threshold"] * 0.98:
                    continue
                if i + 1 < len(rows):
                    next_open = float(rows[i + 1]["open"])
                    if close > 0:
                        gap = (next_open / close - 1) * 100
                        d1_gaps[board_type].append(gap)
                        d1_gaps_by_n[n_limit].append(gap)
            except (ValueError, KeyError):
                continue

    print(f"\r   完成: {total} 连板段")

    # === 输出 ===
    stats_orig = calc_stats(trades_orig)
    stats_next = calc_stats(trades_next)

    print(f"\n{'='*70}")
    print(f"  模式A: 涨停日收盘买入 (原始)")
    print(f"{'='*70}")
    print_stats(stats_orig, "全市场")

    # 按板块
    for bt in ["main", "gem_star"]:
        sub = [t for t in trades_orig if t["board_type"] == bt]
        if sub:
            blabel = "沪深主板" if bt == "main" else "创/科板"
            print_stats(calc_stats(sub), blabel)

    # 按连板数
    print(f"\n  按连板数:")
    for nl in sorted(set(t["n_limit"] for t in trades_orig)):
        sub = [t for t in trades_orig if t["n_limit"] == nl]
        if len(sub) < 3:
            continue
        s = calc_stats(sub)
        print(f"    {nl}板: {s['count']}笔 胜率{s['win_rate']:.1f}% 均收益{s['avg_ret']:.2f}%")

    print(f"\n{'='*70}")
    print(f"  模式B: 次日开盘买入 (修正)")
    print(f"{'='*70}")
    print_stats(stats_next, "全市场")

    for bt in ["main", "gem_star"]:
        sub = [t for t in trades_next if t["board_type"] == bt]
        if sub:
            blabel = "沪深主板" if bt == "main" else "创/科板"
            print_stats(calc_stats(sub), blabel)

    print(f"\n  按连板数:")
    for nl in sorted(set(t["n_limit"] for t in trades_next)):
        sub = [t for t in trades_next if t["n_limit"] == nl]
        if len(sub) < 3:
            continue
        s = calc_stats(sub)
        print(f"    {nl}板: {s['count']}笔 胜率{s['win_rate']:.1f}% 均收益{s['avg_ret']:.2f}%")

    # === 对比 ===
    print(f"\n{'='*70}")
    print(f"  关键对比: 次日开盘 vs 涨停日收盘")
    print(f"{'='*70}")
    print(f"  {'指标':<12} {'原始':>10} {'修正':>10} {'变化':>10}")
    print(f"  {'-'*42}")
    print(f"  {'交易数':<12} {stats_orig['count']:>10} {stats_next['count']:>10} {stats_next['count']-stats_orig['count']:>+10}")
    print(f"  {'胜率%':<12} {stats_orig['win_rate']:>10.1f} {stats_next['win_rate']:>10.1f} {stats_next['win_rate']-stats_orig['win_rate']:>+10.1f}")
    print(f"  {'均收益%':<12} {stats_orig['avg_ret']:>10.2f} {stats_next['avg_ret']:>10.2f} {stats_next['avg_ret']-stats_orig['avg_ret']:>+10.2f}")
    print(f"  {'中位收益%':<12} {stats_orig['med_ret']:>10.2f} {stats_next['med_ret']:>10.2f} {stats_next['med_ret']-stats_orig['med_ret']:>+10.2f}")

    # 按板块对比
    for bt in ["main", "gem_star"]:
        o = [t for t in trades_orig if t["board_type"] == bt]
        n = [t for t in trades_next if t["board_type"] == bt]
        if not o or not n:
            continue
        blabel = "沪深主板" if bt == "main" else "创/科板"
        so = calc_stats(o)
        sn = calc_stats(n)
        print(f"\n  {blabel}:")
        print(f"    胜率: {so['win_rate']:.1f}% → {sn['win_rate']:.1f}%  (Δ{sn['win_rate']-so['win_rate']:+.1f}%)")
        print(f"    均收益: {so['avg_ret']:.2f}% → {sn['avg_ret']:.2f}%  (Δ{sn['avg_ret']-so['avg_ret']:+.2f}%)")
        print(f"    交易数: {so['count']} → {sn['count']}")

    # 按连板数对比
    print(f"\n  按连板数对比:")
    all_nl = sorted(set(t["n_limit"] for t in trades_orig) | set(t["n_limit"] for t in trades_next))
    for nl in all_nl:
        o = [t for t in trades_orig if t["n_limit"] == nl]
        n = [t for t in trades_next if t["n_limit"] == nl]
        if len(o) < 3 or len(n) < 3:
            continue
        so = calc_stats(o)
        sn = calc_stats(n)
        print(f"    {nl}板: 胜率 {so['win_rate']:.1f}%→{sn['win_rate']:.1f}%  均收益 {so['avg_ret']:.2f}%→{sn['avg_ret']:.2f}%  ({so['count']}→{sn['count']}笔)")

    # === D+1 开盘溢价 ===
    print(f"\n{'='*70}")
    print(f"  D+1 开盘溢价分析 (涨停日close → 次日open)")
    print(f"{'='*70}")
    for bt in ["main", "gem_star"]:
        gaps = d1_gaps[bt]
        if not gaps:
            continue
        blabel = "主板" if bt == "main" else "创科"
        avg_gap = sum(gaps) / len(gaps)
        med_gap = sorted(gaps)[len(gaps) // 2]
        high_open = sum(1 for g in gaps if g > 0) / len(gaps) * 100
        low_open = sum(1 for g in gaps if g < 0) / len(gaps) * 100
        print(f"  {blabel} (N={len(gaps)}):")
        print(f"    均值: {avg_gap:+.2f}%  中位: {med_gap:+.2f}%")
        print(f"    高开(>0): {high_open:.1f}%  低开(<0): {low_open:.1f}%")
        # 分布
        brackets = [(-99, -3), (-3, -1), (-1, 0), (0, 1), (1, 3), (3, 99)]
        for lo, hi in brackets:
            cnt = sum(1 for g in gaps if lo <= g < hi)
            pct = cnt / len(gaps) * 100
            print(f"    [{lo:+d}%, {hi:+d}%): {cnt} ({pct:.1f}%)")

    print(f"\n  按连板数:")
    for nl in sorted(d1_gaps_by_n.keys()):
        gaps = d1_gaps_by_n[nl]
        if len(gaps) < 5:
            continue
        avg_gap = sum(gaps) / len(gaps)
        med_gap = sorted(gaps)[len(gaps) // 2]
        print(f"    {nl}板 (N={len(gaps)}): 均值{avg_gap:+.2f}% 中位{med_gap:+.2f}%")


if __name__ == "__main__":
    main()
