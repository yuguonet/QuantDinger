"""
连板猎手 — V1思路回测: 第一板开盘埋伏

核心思路:
  不追涨停板, 而是在第一板当天开盘就买入
  此时价格还没涨停, 可以正常买入

测试方案:
  A) 基准: 涨停日close (不可行)
  B) D+1 open (之前测试的朴素修正)
  C) 第一板open (V1思路: 连板段首日开盘买)
  D) 第一板open + 次日确认 (首日open买, 但只在首日确实涨停的情况下)
  E) 第一板open + 盘中低吸 (首日如果有回踩买低点, 否则买open)

进一步分析:
  - 第一板开盘时 vs 涨停价的差距
  - 首日涨停类型: 一字板 vs 开盘后涨停
  - 不同板块/连板数的表现
"""
import csv
from collections import defaultdict

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


def load_groups(csv_path):
    groups = defaultdict(list)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["code"], row["run_first_limit_date"])
            groups[key].append(row)
    for key in groups:
        groups[key].sort(key=lambda r: r["time"])
    return groups


def try_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def backtest_v1_entry(rows, params, mode="C_first_open"):
    """
    V1思路回测: 在第一板开盘买入

    关键: 找到连板段的第一天(第一天涨停), 以当天open买入
    """
    if len(rows) < 5:
        return []

    threshold = params["threshold"]
    trades = []
    position = None

    # 找第一板的位置
    # rows 中, 连板段从某个index开始, 第一天是涨停日
    # 需要找到涨停段的起点
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
            # 检查前一天是否不是涨停(即这是第一板)
            if i >= 2:
                prev2 = rows[i - 2]
                prev2_close = try_float(prev2["close"])
                if prev2_close > 0:
                    prev_ret = (prev_close / prev2_close - 1)
                    if prev_ret < threshold * 0.98:
                        first_limit_idx = i
                        break
            else:
                first_limit_idx = i
                break

    if first_limit_idx is None:
        return []

    # 第一板
    fl_row = rows[first_limit_idx]
    fl_prev = rows[first_limit_idx - 1]
    fl_close = try_float(fl_row["close"])
    fl_open = try_float(fl_row["open"])
    fl_high = try_float(fl_row["high"])
    fl_low = try_float(fl_row["low"])
    fl_prev_close = try_float(fl_prev["close"])

    if fl_prev_close <= 0 or fl_open <= 0:
        return []

    # 确认确实是涨停
    if fl_close / fl_prev_close - 1 < threshold * 0.98:
        return []

    # ===== 决定买入价 =====
    if mode == "A_same_day_close":
        # 基准: 涨停日close
        buy_price = fl_close
        buy_idx = first_limit_idx
        buy_date = fl_row["time"]

    elif mode == "B_d1_open":
        # D+1 open
        if first_limit_idx + 1 >= len(rows):
            return []
        next_row = rows[first_limit_idx + 1]
        buy_price = try_float(next_row["open"])
        if buy_price <= 0:
            return []
        buy_idx = first_limit_idx + 1
        buy_date = next_row["time"]

    elif mode == "C_first_open":
        # V1: 第一板当天open买入
        buy_price = fl_open
        buy_idx = first_limit_idx
        buy_date = fl_row["time"]

    elif mode == "D_first_open_confirmed":
        # V1变体: 第一板open买入, 但只在当日确实涨停的情况下(检查close==high封板)
        seal = (fl_close / fl_high - 1) * 100 if fl_high > 0 else -999
        if seal > params["buy_seal_max"]:
            return []  # 没封死, 不买
        buy_price = fl_open
        buy_idx = first_limit_idx
        buy_date = fl_row["time"]

    elif mode == "E_first_pullback":
        # V1变体: 第一板盘中低吸
        # 如果当天有回踩(open > low, 说明有低于open的价格), 买在low
        # 否则买在open
        if fl_low < fl_open and fl_low > 0:
            buy_price = fl_low
        else:
            buy_price = fl_open
        buy_idx = first_limit_idx
        buy_date = fl_row["time"]

    elif mode == "F_first_pullback_confirmed":
        # 低吸 + 封板确认
        seal = (fl_close / fl_high - 1) * 100 if fl_high > 0 else -999
        if seal > params["buy_seal_max"]:
            return []
        if fl_low < fl_open and fl_low > 0:
            buy_price = fl_low
        else:
            buy_price = fl_open
        buy_idx = first_limit_idx
        buy_date = fl_row["time"]

    else:
        return []

    if buy_price <= 0:
        return []

    # ===== 持仓 + 卖出 =====
    position = {
        "buy_price": buy_price,
        "buy_date": buy_date,
        "buy_idx": buy_idx,
        "highest": buy_price,
    }

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
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": sell_type,
                "buy_open_pct": round((fl_open / fl_prev_close - 1) * 100, 2),
                "buy_close_pct": round((fl_close / fl_prev_close - 1) * 100, 2),
            })
            return trades

    # 数据结束平仓
    last = rows[-1]
    close = try_float(last["close"])
    ret_from_buy = (close / position["buy_price"] - 1) * 100
    trades.append({
        "return_pct": round(ret_from_buy, 2),
        "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
        "sell_type": "end_of_data",
        "buy_open_pct": round((fl_open / fl_prev_close - 1) * 100, 2),
        "buy_close_pct": round((fl_close / fl_prev_close - 1) * 100, 2),
    })
    return trades


def calc_stats(trades):
    if not trades:
        return {"n": 0, "wr": 0, "avg": 0, "med": 0, "pnl": 0, "maxw": 0, "maxl": 0}
    rets = sorted([t["return_pct"] for t in trades])
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    return {
        "n": len(rets),
        "wr": len(wins) / len(rets) * 100,
        "avg": sum(rets) / len(rets),
        "med": rets[len(rets) // 2],
        "pnl": (sum(wins) / len(wins) / abs(sum(losses) / len(losses))) if losses and wins else 0,
        "maxw": max(rets) if rets else 0,
        "maxl": min(rets) if rets else 0,
    }


def main():
    print("📊 加载数据...")
    groups = load_groups(CSV_PATH)
    total = len(groups)
    print(f"   连板段: {total}")

    modes = [
        "A_same_day_close",
        "B_d1_open",
        "C_first_open",
        "D_first_open_confirmed",
        "E_first_pullback",
        "F_first_pullback_confirmed",
    ]

    mode_labels = {
        "A_same_day_close":       "A 涨停日close(基准)",
        "B_d1_open":              "B D+1 open",
        "C_first_open":           "C 第一板open (V1)",
        "D_first_open_confirmed": "D 第一板open+封板确认",
        "E_first_pullback":       "E 第一板盘中低吸",
        "F_first_pullback_confirmed": "F 低吸+封板确认",
    }

    all_trades = {m: [] for m in modes}
    all_bt = {m: defaultdict(list) for m in modes}
    all_nl = {m: defaultdict(list) for m in modes}
    # 首日开盘溢价统计
    first_open_gaps = {"main": [], "gem_star": []}

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

        # 只做2板+
        if n_limit < params["min_streak"]:
            continue

        # 首日开盘溢价
        for i in range(1, len(rows)):
            row = rows[i]
            prev = rows[i - 1]
            close = try_float(row["close"])
            prev_close = try_float(prev["close"])
            open_p = try_float(row["open"])
            if prev_close > 0 and close / prev_close - 1 >= params["threshold"] * 0.98:
                # 这是涨停日
                # 检查是否是第一板
                if i >= 2:
                    prev2 = rows[i - 2]
                    prev2_close = try_float(prev2["close"])
                    if prev2_close > 0 and prev_close / prev2_close - 1 >= params["threshold"] * 0.98:
                        continue  # 不是第一板
                gap = (open_p / prev_close - 1) * 100
                first_open_gaps[board_type].append(gap)
                break

        for mode in modes:
            trades = backtest_v1_entry(rows, params, mode)
            for t in trades:
                t["code"] = code
                t["board_type"] = board_type
                t["n_limit"] = n_limit
            all_trades[mode].extend(trades)
            for t in trades:
                all_bt[mode][board_type].append(t)
                all_nl[mode][n_limit].append(t)

    print(f"\r   完成: {total} 连板段")

    # ===== 总表 =====
    print(f"\n{'='*90}")
    print(f"  V1思路: 第一板开盘埋伏 (只做2板+)")
    print(f"{'='*90}")
    header = f"  {'方案':<28} {'交易数':>7} {'胜率%':>7} {'均收益%':>9} {'中位%':>7} {'盈亏比':>7} {'最大盈':>8} {'最大亏':>8}"
    print(header)
    print(f"  {'-'*86}")

    for mode in modes:
        s = calc_stats(all_trades[mode])
        label = mode_labels[mode]
        print(f"  {label:<28} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f} {s['med']:>+7.2f} {s['pnl']:>7.2f} {s['maxw']:>+8.2f} {s['maxl']:>+8.2f}")

    # ===== 按板块 =====
    for bt in ["main", "gem_star"]:
        blabel = "沪深主板" if bt == "main" else "创/科板"
        print(f"\n{'='*90}")
        print(f"  {blabel}")
        print(f"{'='*90}")
        print(header)
        print(f"  {'-'*86}")
        for mode in modes:
            s = calc_stats(all_bt[mode][bt])
            label = mode_labels[mode]
            print(f"  {label:<28} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f} {s['med']:>+7.2f} {s['pnl']:>7.2f} {s['maxw']:>+8.2f} {s['maxl']:>+8.2f}")

    # ===== 按连板数 =====
    print(f"\n{'='*90}")
    print(f"  按连板数")
    print(f"{'='*90}")

    for nl in [2, 3, 4, 5, 6, 7, 8]:
        print(f"\n  --- {nl}板 ---")
        print(f"  {'方案':<28} {'交易数':>7} {'胜率%':>7} {'均收益%':>9}")
        print(f"  {'-'*52}")
        for mode in modes:
            s = calc_stats(all_nl[mode][nl])
            if s['n'] < 3:
                continue
            label = mode_labels[mode]
            print(f"  {label:<28} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f}")

    # ===== 第一板开盘溢价分析 =====
    print(f"\n{'='*90}")
    print(f"  第一板开盘溢价 (前日close → 第一板open)")
    print(f"{'='*90}")
    for bt in ["main", "gem_star"]:
        gaps = first_open_gaps[bt]
        if not gaps:
            continue
        blabel = "主板" if bt == "main" else "创科"
        avg_gap = sum(gaps) / len(gaps)
        med_gap = sorted(gaps)[len(gaps) // 2]
        print(f"\n  {blabel} (N={len(gaps)}):")
        print(f"    均值: {avg_gap:+.2f}%  中位: {med_gap:+.2f}%")
        # 涨停类型
        one_word = sum(1 for g in gaps if g >= 8)  # 一字板(高开>8%)
        high_open = sum(1 for g in gaps if 3 <= g < 8)
        low_open = sum(1 for g in gaps if 0 <= g < 3)
        down_open = sum(1 for g in gaps if g < 0)
        print(f"    一字板(开>8%): {one_word} ({one_word/len(gaps)*100:.1f}%)")
        print(f"    大高开(3-8%): {high_open} ({high_open/len(gaps)*100:.1f}%)")
        print(f"    小高开(0-3%): {low_open} ({low_open/len(gaps)*100:.1f}%)")
        print(f"    低开(<0%): {down_open} ({down_open/len(gaps)*100:.1f}%)")

        # 分布
        brackets = [(-99, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
        for lo, hi in brackets:
            cnt = sum(1 for g in gaps if lo <= g < hi)
            pct = cnt / len(gaps) * 100
            print(f"    [{lo:+d}%, {hi:+d}%): {cnt} ({pct:.1f}%)")

    # ===== 最终对比 =====
    print(f"\n{'='*90}")
    print(f"  最终对比")
    print(f"{'='*90}")
    s_a = calc_stats(all_trades["A_same_day_close"])
    s_b = calc_stats(all_trades["B_d1_open"])
    s_c = calc_stats(all_trades["C_first_open"])
    s_d = calc_stats(all_trades["D_first_open_confirmed"])
    s_e = calc_stats(all_trades["E_first_pullback"])
    s_f = calc_stats(all_trades["F_first_pullback_confirmed"])

    print(f"""
  A) 涨停日close (不可行基准): {s_a['n']}笔 {s_a['wr']:.1f}%胜率 {s_a['avg']:+.2f}%均收益
  B) D+1 open (朴素修正):     {s_b['n']}笔 {s_b['wr']:.1f}%胜率 {s_b['avg']:+.2f}%均收益
  C) 第一板open (V1):          {s_c['n']}笔 {s_c['wr']:.1f}%胜率 {s_c['avg']:+.2f}%均收益
  D) 第一板open+封板确认:      {s_d['n']}笔 {s_d['wr']:.1f}%胜率 {s_d['avg']:+.2f}%均收益
  E) 第一板盘中低吸:           {s_e['n']}笔 {s_e['wr']:.1f}%胜率 {s_e['avg']:+.2f}%均收益
  F) 低吸+封板确认:            {s_f['n']}笔 {s_f['wr']:.1f}%胜率 {s_f['avg']:+.2f}%均收益

  V1(C) vs 追板(B): 胜率{s_c['wr']-s_b['wr']:+.1f}% 均收益{s_c['avg']-s_b['avg']:+.2f}%
  封板确认(D) vs 不确认(C): 胜率{s_d['wr']-s_c['wr']:+.1f}% 均收益{s_d['avg']-s_c['avg']:+.2f}%
  低吸(E) vs open(C): 胜率{s_e['wr']-s_c['wr']:+.1f}% 均收益{s_e['avg']-s_c['avg']:+.2f}%
""")


if __name__ == "__main__":
    main()
