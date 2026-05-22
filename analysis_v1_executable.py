"""
连板猎手 — V1可执行策略: 第一板识别 → D+1开盘买入

流程:
  D0: 某股第一次涨停 (盘后扫描发现)
  D1: 开盘买入
  持有直到卖出条件触发

分析:
  1. D+1开盘溢价 (D0涨停close → D1 open)
  2. D+1是否继续涨停 (续板率)
  3. 最终收益分布
  4. 按板块/连板数分层
  5. 高开过滤效果
"""
import csv
from collections import defaultdict

CSV_PATH = "analysis_output/dragon_ohlcv.csv"

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


def main():
    print("📊 加载数据...")
    groups = load_groups(CSV_PATH)
    total = len(groups)
    print(f"   连板段: {total}")

    # 所有交易 (不同入场方式)
    trades_by_mode = {
        "baseline_close": [],      # D0 close买入 (不可行基准)
        "d1_open": [],             # D1 open买入
        "d1_open_gap_lt3": [],     # D1 open + 高开<3%
        "d1_open_gap_lt5": [],     # D1 open + 高开<5%
        "d1_open_continue": [],    # D1 open + D1继续涨停
    }

    # D+1溢价
    d1_gaps = []
    # D+1是否继续涨停
    d1_continue_count = 0
    d1_not_continue_count = 0

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
            if ret >= params["threshold"] * 0.98:
                if i >= 2:
                    prev2 = rows[i - 2]
                    prev2_close = try_float(prev2["close"])
                    if prev2_close > 0 and prev_close / prev2_close - 1 >= params["threshold"] * 0.98:
                        continue
                first_limit_idx = i
                break

        if first_limit_idx is None:
            continue

        fl_row = rows[first_limit_idx]
        fl_close = try_float(fl_row["close"])
        fl_prev = rows[first_limit_idx - 1]
        fl_prev_close = try_float(fl_prev["close"])

        if fl_prev_close <= 0 or fl_close <= 0:
            continue

        # D+1
        if first_limit_idx + 1 >= len(rows):
            continue
        d1_row = rows[first_limit_idx + 1]
        d1_open = try_float(d1_row["open"])
        d1_close = try_float(d1_row["close"])
        d1_high = try_float(d1_row["high"])
        d1_low = try_float(d1_row["low"])

        if d1_open <= 0:
            continue

        # D+1溢价
        d1_gap = (d1_open / fl_close - 1) * 100
        d1_gaps.append({
            "board_type": board_type,
            "n_limit": n_limit,
            "d1_gap": d1_gap,
            "d1_open": d1_open,
            "d1_close": d1_close,
            "fl_close": fl_close,
        })

        # D+1是否继续涨停
        d1_ret = (d1_close / fl_close - 1)
        if d1_ret >= params["threshold"] * 0.98:
            d1_continue_count += 1
        else:
            d1_not_continue_count += 1

        # ===== 回测不同入场方式 =====

        # 辅助: 从某一天开始持仓回测
        def run_trade(buy_price, buy_idx, buy_date):
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
                    return {
                        "return_pct": round(ret_from_buy, 2),
                        "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                        "sell_type": sell_type,
                        "n_limit": n_limit,
                        "board_type": board_type,
                        "d1_gap": round(d1_gap, 2),
                        "code": code,
                    }
            # 数据结束
            last = rows[-1]
            close = try_float(last["close"])
            return {
                "return_pct": round((close / position["buy_price"] - 1) * 100, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "end_of_data",
                "n_limit": n_limit,
                "board_type": board_type,
                "d1_gap": round(d1_gap, 2),
                "code": code,
            }

        # A: D0 close (基准)
        trades_by_mode["baseline_close"].append(run_trade(fl_close, first_limit_idx, fl_row["time"]))

        # B: D1 open
        trades_by_mode["d1_open"].append(run_trade(d1_open, first_limit_idx + 1, d1_row["time"]))

        # C: D1 open + 高开<3%
        if d1_gap < 3:
            trades_by_mode["d1_open_gap_lt3"].append(run_trade(d1_open, first_limit_idx + 1, d1_row["time"]))

        # D: D1 open + 高开<5%
        if d1_gap < 5:
            trades_by_mode["d1_open_gap_lt5"].append(run_trade(d1_open, first_limit_idx + 1, d1_row["time"]))

        # E: D1 open + D1继续涨停 (确认连板)
        if d1_ret >= params["threshold"] * 0.98:
            trades_by_mode["d1_open_continue"].append(run_trade(d1_open, first_limit_idx + 1, d1_row["time"]))

    print(f"\r   完成: {total} 连板段")

    # ===== D+1 溢价分析 =====
    print(f"\n{'='*80}")
    print(f"  D+1 开盘溢价 (D0涨停close → D1 open)")
    print(f"{'='*80}")
    print(f"  样本: {len(d1_gaps)}")

    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        gaps = [g["d1_gap"] for g in d1_gaps if g["board_type"] == bt]
        if not gaps:
            continue
        avg = sum(gaps) / len(gaps)
        med = sorted(gaps)[len(gaps) // 2]
        print(f"\n  {blabel} (N={len(gaps)}):")
        print(f"    均值: {avg:+.2f}%  中位: {med:+.2f}%")
        brackets = [(-99, -3), (-3, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
        for lo, hi in brackets:
            cnt = sum(1 for g in gaps if lo <= g < hi)
            pct = cnt / len(gaps) * 100
            print(f"    [{lo:+d}%, {hi:+d}%): {cnt} ({pct:.1f}%)")

    # D+1续板率
    total_d1 = d1_continue_count + d1_not_continue_count
    print(f"\n  D+1 续板率: {d1_continue_count}/{total_d1} = {d1_continue_count/total_d1*100:.1f}%")

    # ===== 回测结果 =====
    def print_stats(trades, label):
        if not trades:
            print(f"  {label}: 无交易")
            return
        rets = [t["return_pct"] for t in trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        n = len(rets)
        wr = len(wins) / n * 100
        avg = sum(rets) / n
        med = sorted(rets)[n // 2]
        pnl = (sum(wins)/len(wins)/abs(sum(losses)/len(losses))) if wins and losses else 0
        print(f"  {label}: {n:>5}笔  胜率{wr:>6.1f}%  均收益{avg:>+7.2f}%  中位{med:>+7.2f}%  盈亏比{pnl:>5.2f}")

    print(f"\n{'='*80}")
    print(f"  回测结果对比 (只做2板+)")
    print(f"{'='*80}")

    mode_labels = {
        "baseline_close": "A D0 close (不可行基准)",
        "d1_open":        "B D1 open (第一板次日买入)",
        "d1_open_gap_lt3": "C D1 open + 高开<3%",
        "d1_open_gap_lt5": "D D1 open + 高开<5%",
        "d1_open_continue": "E D1 open + D1续板确认",
    }

    for mode, label in mode_labels.items():
        print_stats(trades_by_mode[mode], label)

    # 按板块
    for bt in ["main", "gem_star"]:
        blabel = "沪深主板" if bt == "main" else "创/科板"
        print(f"\n  --- {blabel} ---")
        for mode, label in mode_labels.items():
            sub = [t for t in trades_by_mode[mode] if t["board_type"] == bt]
            if sub:
                print_stats(sub, label)

    # 按连板数
    print(f"\n  --- 按连板数 ---")
    for nl in [2, 3, 4, 5, 6, 7, 8]:
        print(f"\n  {nl}板:")
        for mode in ["d1_open", "d1_open_gap_lt3", "d1_open_gap_lt5", "d1_open_continue"]:
            sub = [t for t in trades_by_mode[mode] if t["n_limit"] == nl]
            if len(sub) >= 3:
                label = mode_labels[mode]
                print_stats(sub, f"    {label}")

    # ===== 最终对比 =====
    print(f"\n{'='*80}")
    print(f"  最终对比: V1可执行方案")
    print(f"{'='*80}")

    def get_stats(trades):
        if not trades:
            return {"n": 0, "wr": 0, "avg": 0, "med": 0}
        rets = [t["return_pct"] for t in trades]
        wins = [r for r in rets if r > 0]
        return {"n": len(rets), "wr": len(wins)/len(rets)*100, "avg": sum(rets)/len(rets), "med": sorted(rets)[len(rets)//2]}

    s_a = get_stats(trades_by_mode["baseline_close"])
    s_b = get_stats(trades_by_mode["d1_open"])
    s_c = get_stats(trades_by_mode["d1_open_gap_lt3"])
    s_d = get_stats(trades_by_mode["d1_open_gap_lt5"])
    s_e = get_stats(trades_by_mode["d1_open_continue"])

    print(f"""
  A) D0 close (不可行):      {s_a['n']}笔 {s_a['wr']:.1f}% 胜率 {s_a['avg']:+.2f}% 均收益
  B) D1 open (可执行):       {s_b['n']}笔 {s_b['wr']:.1f}% 胜率 {s_b['avg']:+.2f}% 均收益
  C) D1 open + 高开<3%:      {s_c['n']}笔 {s_c['wr']:.1f}% 胜率 {s_c['avg']:+.2f}% 均收益
  D) D1 open + 高开<5%:      {s_d['n']}笔 {s_d['wr']:.1f}% 胜率 {s_d['avg']:+.2f}% 均收益
  E) D1 open + 续板确认:     {s_e['n']}笔 {s_e['wr']:.1f}% 胜率 {s_e['avg']:+.2f}% 均收益

  A→B (溢价损失):  胜率{s_b['wr']-s_a['wr']:+.1f}%  均收益{s_b['avg']-s_a['avg']:+.2f}%
  B→C (高开<3%):   胜率{s_c['wr']-s_b['wr']:+.1f}%  均收益{s_c['avg']-s_b['avg']:+.2f}%  交易数{s_c['n']}/{s_b['n']}
  B→D (高开<5%):   胜率{s_d['wr']-s_b['wr']:+.1f}%  均收益{s_d['avg']-s_b['avg']:+.2f}%  交易数{s_d['n']}/{s_b['n']}
  B→E (续板确认):   胜率{s_e['wr']-s_b['wr']:+.1f}%  均收益{s_e['avg']-s_b['avg']:+.2f}%  交易数{s_e['n']}/{s_b['n']}
""")

    # 按板块最终对比
    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        print(f"  {blabel}:")
        for mode in ["d1_open", "d1_open_gap_lt3", "d1_open_gap_lt5"]:
            sub = [t for t in trades_by_mode[mode] if t["board_type"] == bt]
            st = get_stats(sub)
            label = mode_labels[mode].split("(")[0].strip()
            print(f"    {label}: {st['n']}笔 {st['wr']:.1f}%胜率 {st['avg']:+.2f}%均收益")
        print()


if __name__ == "__main__":
    main()
