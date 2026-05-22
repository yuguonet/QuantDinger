"""
连板猎手 — D+1 入场方案对比测试

测试5种入场方式:
  A) 涨停日close (原始, 不可行基准)
  B) D+1 open (最朴素修正)
  C) D+1 close (等一天看走势)
  D) D+1 盘中低吸 (如果D+1有回踩, 买在回踩价; 否则买close)
  E) D+1 open + 高开过滤 (高开太多的跳过)
  F) D+1 低吸+高开过滤 (组合)
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

ENTRY_MODES = [
    "A_same_day_close",    # 基准: 涨停日close
    "B_d1_open",           # D+1 open
    "C_d1_close",          # D+1 close
    "D_d1_pullback",       # D+1 盘中低吸 (买在低点附近)
    "E_d1_open_gap_filter",# D+1 open + 高开过滤
    "F_d1_pullback_gf",    # D+1 低吸 + 高开过滤
]


def get_board_type(code):
    c = str(code)[:3]
    if c.startswith("30") or c.startswith("68"):
        return "gem_star"
    return "main"


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


def backtest_entry(rows, params, mode, extra_params=None):
    """
    对单个连板段回测, 支持不同入场模式

    mode: ENTRY_MODES 中的一种
    extra_params: 额外参数 (如 pullback_pct, max_d1_gap)
    """
    if len(rows) < 5:
        return []

    threshold = params["threshold"]
    ep = extra_params or {}
    pullback_pct = ep.get("pullback_pct", 3.0)  # D+1 回踩幅度%
    max_d1_gap = ep.get("max_d1_gap", 5.0)       # D+1 最大高开%

    trades = []
    position = None

    for i in range(1, len(rows)):
        row = rows[i]
        prev = rows[i - 1]

        close = try_float(row["close"])
        prev_close = try_float(prev["close"])
        open_p = try_float(row["open"])
        high = try_float(row["high"])
        low = try_float(row["low"])

        if prev_close <= 0:
            continue

        if position is None:
            # 检查涨停
            ret = (close / prev_close - 1)
            if ret < threshold * 0.98:
                continue

            # 封板强度 (涨停日必须封死)
            if high > 0:
                seal = (close / high - 1) * 100
                if seal > params["buy_seal_max"]:
                    continue

            # 高开过滤 (涨停日当天)
            gap_pct = (open_p / prev_close - 1) * 100
            if gap_pct > params["buy_max_gap_pct"]:
                continue

            # ===== 根据模式决定买入价 =====
            if mode == "A_same_day_close":
                # 涨停日close买入
                buy_price = close
                buy_idx = i
                buy_date = row["time"]

            elif mode == "B_d1_open":
                # D+1 open买入
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                buy_price = try_float(next_row["open"])
                if buy_price <= 0:
                    continue
                buy_idx = i + 1
                buy_date = next_row["time"]

            elif mode == "C_d1_close":
                # D+1 close买入
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                buy_price = try_float(next_row["close"])
                if buy_price <= 0:
                    continue
                buy_idx = i + 1
                buy_date = next_row["time"]

            elif mode == "D_d1_pullback":
                # D+1 盘中低吸: 如果D+1的low比open低了pullback_pct以上, 买在open*(1-pullback_pct/100)
                # 否则买在D+1 close
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                d1_open = try_float(next_row["open"])
                d1_low = try_float(next_row["low"])
                d1_close = try_float(next_row["close"])
                if d1_open <= 0:
                    continue
                # 低吸目标价
                target = d1_open * (1 - pullback_pct / 100)
                if d1_low <= target:
                    # 有回踩到目标价, 买在目标价
                    buy_price = target
                else:
                    # 没有回踩, 买在close
                    buy_price = d1_close
                if buy_price <= 0:
                    continue
                buy_idx = i + 1
                buy_date = next_row["time"]

            elif mode == "E_d1_open_gap_filter":
                # D+1 open + 高开过滤: D+1 open比涨停日close高开太多就跳过
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                d1_open = try_float(next_row["open"])
                if d1_open <= 0 or close <= 0:
                    continue
                d1_gap = (d1_open / close - 1) * 100
                if d1_gap > max_d1_gap:
                    continue  # 高开太多, 跳过
                buy_price = d1_open
                buy_idx = i + 1
                buy_date = next_row["time"]

            elif mode == "F_d1_pullback_gf":
                # D+1 低吸 + 高开过滤
                if i + 1 >= len(rows):
                    continue
                next_row = rows[i + 1]
                d1_open = try_float(next_row["open"])
                d1_low = try_float(next_row["low"])
                d1_close = try_float(next_row["close"])
                if d1_open <= 0 or close <= 0:
                    continue
                d1_gap = (d1_open / close - 1) * 100
                if d1_gap > max_d1_gap:
                    continue  # 高开太多, 跳过
                target = d1_open * (1 - pullback_pct / 100)
                if d1_low <= target:
                    buy_price = target
                else:
                    buy_price = d1_close
                if buy_price <= 0:
                    continue
                buy_idx = i + 1
                buy_date = next_row["time"]
            else:
                continue

            position = {
                "buy_price": buy_price,
                "buy_date": buy_date,
                "buy_idx": buy_idx,
                "highest": buy_price,
            }
        else:
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
                })
                position = None

    # 数据结束平仓
    if position is not None:
        last = rows[-1]
        close = try_float(last["close"])
        ret_from_buy = (close / position["buy_price"] - 1) * 100
        trades.append({
            "return_pct": round(ret_from_buy, 2),
            "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
            "sell_type": "end_of_data",
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

    # 收集所有交易 (按模式)
    all_trades = {m: [] for m in ENTRY_MODES}
    # 按板块+连板数
    all_trades_bt = {m: defaultdict(list) for m in ENTRY_MODES}
    all_trades_nl = {m: defaultdict(list) for m in ENTRY_MODES}

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

        for mode in ENTRY_MODES:
            extra = {}
            if "pullback" in mode:
                extra["pullback_pct"] = 3.0
            if "gap_filter" in mode or "gf" in mode:
                extra["max_d1_gap"] = 5.0

            trades = backtest_entry(rows, params, mode, extra)
            for t in trades:
                t["code"] = code
                t["board_type"] = board_type
                t["n_limit"] = n_limit
            all_trades[mode].extend(trades)
            for t in trades:
                all_trades_bt[mode][board_type].append(t)
                all_trades_nl[mode][n_limit].append(t)

    print(f"\r   完成: {total} 连板段")

    # ===== 总表 =====
    print(f"\n{'='*90}")
    print(f"  入场方案对比 (全市场)")
    print(f"{'='*90}")
    header = f"  {'方案':<26} {'交易数':>7} {'胜率%':>7} {'均收益%':>9} {'中位%':>7} {'盈亏比':>7} {'最大盈':>8} {'最大亏':>8}"
    print(header)
    print(f"  {'-'*84}")

    mode_labels = {
        "A_same_day_close":     "A 涨停日close(基准)",
        "B_d1_open":            "B D+1 open",
        "C_d1_close":           "C D+1 close",
        "D_d1_pullback":        "D D+1 低吸3%",
        "E_d1_open_gap_filter": "E D+1 open+高开<5%",
        "F_d1_pullback_gf":     "F D+1 低吸+高开<5%",
    }

    for mode in ENTRY_MODES:
        s = calc_stats(all_trades[mode])
        label = mode_labels[mode]
        print(f"  {label:<26} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f} {s['med']:>+7.2f} {s['pnl']:>7.2f} {s['maxw']:>+8.2f} {s['maxl']:>+8.2f}")

    # ===== 按板块 =====
    for bt in ["main", "gem_star"]:
        blabel = "沪深主板" if bt == "main" else "创/科板"
        print(f"\n{'='*90}")
        print(f"  {blabel}")
        print(f"{'='*90}")
        print(header)
        print(f"  {'-'*84}")
        for mode in ENTRY_MODES:
            s = calc_stats(all_trades_bt[mode][bt])
            label = mode_labels[mode]
            print(f"  {label:<26} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f} {s['med']:>+7.2f} {s['pnl']:>7.2f} {s['maxw']:>+8.2f} {s['maxl']:>+8.2f}")

    # ===== 按连板数 (只看2板+, 因为v3.1只做2板+) =====
    print(f"\n{'='*90}")
    print(f"  按连板数 (2板+)")
    print(f"{'='*90}")

    for nl in [2, 3, 4, 5, 6, 7, 8]:
        print(f"\n  --- {nl}板 ---")
        print(f"  {'方案':<26} {'交易数':>7} {'胜率%':>7} {'均收益%':>9}")
        print(f"  {'-'*50}")
        for mode in ENTRY_MODES:
            s = calc_stats(all_trades_nl[mode][nl])
            if s['n'] < 3:
                continue
            label = mode_labels[mode]
            print(f"  {label:<26} {s['n']:>7} {s['wr']:>7.1f} {s['avg']:>+9.2f}")

    # ===== 关键结论 =====
    print(f"\n{'='*90}")
    print(f"  关键结论")
    print(f"{'='*90}")
    s_a = calc_stats(all_trades["A_same_day_close"])
    s_b = calc_stats(all_trades["B_d1_open"])
    s_c = calc_stats(all_trades["C_d1_close"])
    s_d = calc_stats(all_trades["D_d1_pullback"])
    s_e = calc_stats(all_trades["E_d1_open_gap_filter"])
    s_f = calc_stats(all_trades["F_d1_pullback_gf"])

    print(f"""
  A) 涨停日close (不可行基准): {s_a['wr']:.1f}%胜率 {s_a['avg']:+.2f}%均收益
  B) D+1 open (朴素修正):     {s_b['wr']:.1f}%胜率 {s_b['avg']:+.2f}%均收益  ← 买在高开溢价上
  C) D+1 close (等一天):      {s_c['wr']:.1f}%胜率 {s_c['avg']:+.2f}%均收益
  D) D+1 低吸3%:              {s_d['wr']:.1f}%胜率 {s_d['avg']:+.2f}%均收益
  E) D+1 open+高开<5%过滤:    {s_e['wr']:.1f}%胜率 {s_e['avg']:+.2f}%均收益
  F) D+1 低吸+高开<5%过滤:    {s_f['wr']:.1f}%胜率 {s_f['avg']:+.2f}%均收益

  A→B 差距 (高开溢价吃掉的利润): 胜率{s_b['wr']-s_a['wr']:+.1f}% 均收益{s_b['avg']-s_a['avg']:+.2f}%
  E vs B (高开过滤效果): 胜率{s_e['wr']-s_b['wr']:+.1f}% 均收益{s_e['avg']-s_b['avg']:+.2f}%
  F vs B (低吸+过滤效果): 胜率{s_f['wr']-s_b['wr']:+.1f}% 均收益{s_f['avg']-s_b['avg']:+.2f}%
""")


if __name__ == "__main__":
    main()
