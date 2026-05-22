"""
连板猎手 — V1深度分析: 第一板可买性

核心问题:
  第一板open买入的前提是: 开盘价低于涨停价, 还有上涨空间
  但如果是一字板(开盘就涨停), 根本买不到

分析:
  1. 第一板开盘类型: 一字板 vs 可买入
  2. 可买入的子集, 收益如何
  3. 过滤一字板后, V1策略的真实表现
"""
import csv
from collections import defaultdict

CSV_PATH = "analysis_output/dragon_ohlcv.csv"

BOARD_PARAMS = {
    "main": {"threshold": 0.098, "min_streak": 2, "stop_loss_pct": -8, "trailing_stop_pct": -6, "take_profit_pct": 15},
    "gem_star": {"threshold": 0.198, "min_streak": 2, "max_streak": 4, "stop_loss_pct": -12, "trailing_stop_pct": -8, "take_profit_pct": 20},
}


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


def main():
    print("📊 加载数据...")
    groups = load_groups(CSV_PATH)
    total = len(groups)
    print(f"   连板段: {total}")

    # 分类统计
    categories = {
        "one_word": {"label": "一字板(买不到)", "trades": [], "by_nl": defaultdict(list)},
        "high_open_sealed": {"label": "大高开封板(难买)", "trades": [], "by_nl": defaultdict(list)},
        "buyable": {"label": "可买入(开盘<涨停)", "trades": [], "by_nl": defaultdict(list)},
    }
    # 按板块
    for bt in ["main", "gem_star"]:
        for cat in categories:
            categories[cat][f"bt_{bt}"] = []

    first_board_stats = []

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
        fl_prev = rows[first_limit_idx - 1]
        fl_close = try_float(fl_row["close"])
        fl_open = try_float(fl_row["open"])
        fl_high = try_float(fl_row["high"])
        fl_low = try_float(fl_row["low"])
        fl_prev_close = try_float(fl_prev["close"])

        if fl_prev_close <= 0 or fl_open <= 0:
            continue

        # 涨停价 (理论)
        limit_price = fl_prev_close * (1 + params["threshold"])

        # 开盘价相对于涨停价的位置
        open_vs_limit = (fl_open / limit_price - 1) * 100  # 正=高开超过涨停价(不可能), 负=低于涨停价
        open_gap_pct = (fl_open / fl_prev_close - 1) * 100  # 相对前日涨幅

        # 分类
        # 一字板: open == high == low == close (几乎没有波动)
        is_one_word = (abs(fl_high - fl_low) / fl_prev_close < 0.002) and (fl_close >= limit_price * 0.98)

        # 大高开封板: open接近涨停价(>threshold*0.9), 且封板
        open_near_limit = open_gap_pct >= params["threshold"] * 0.9

        if is_one_word:
            cat = "one_word"
        elif open_near_limit:
            cat = "high_open_sealed"
        else:
            cat = "buyable"

        first_board_stats.append({
            "code": code,
            "board_type": board_type,
            "n_limit": n_limit,
            "open_gap_pct": open_gap_pct,
            "open_vs_limit_pct": open_vs_limit,
            "category": cat,
            "is_one_word": is_one_word,
        })

        # 回测: 第一板open买入
        buy_price = fl_open
        position = {"buy_price": buy_price, "highest": buy_price}
        trade = None

        for i in range(first_limit_idx + 1, len(rows)):
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
                trade = {
                    "return_pct": round(ret_from_buy, 2),
                    "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                    "sell_type": sell_type,
                    "n_limit": n_limit,
                    "board_type": board_type,
                    "open_gap_pct": round(open_gap_pct, 2),
                }
                break

        if trade is None:
            last = rows[-1]
            close = try_float(last["close"])
            ret_from_buy = (close / position["buy_price"] - 1) * 100
            trade = {
                "return_pct": round(ret_from_buy, 2),
                "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                "sell_type": "end_of_data",
                "n_limit": n_limit,
                "board_type": board_type,
                "open_gap_pct": round(open_gap_pct, 2),
            }

        categories[cat]["trades"].append(trade)
        categories[cat]["by_nl"][n_limit].append(trade)
        categories[cat][f"bt_{board_type}"].append(trade)

    print(f"\r   完成: {total} 连板段")

    # ===== 第一板分类统计 =====
    print(f"\n{'='*80}")
    print(f"  第一板分类统计 (2板+连板段)")
    print(f"{'='*80}")

    total_counted = sum(len(first_board_stats) for _ in [1])
    one_word = [s for s in first_board_stats if s["category"] == "one_word"]
    high_open = [s for s in first_board_stats if s["category"] == "high_open_sealed"]
    buyable = [s for s in first_board_stats if s["category"] == "buyable"]

    print(f"  总计: {len(first_board_stats)} 个连板段")
    print(f"  一字板(买不到): {len(one_word)} ({len(one_word)/len(first_board_stats)*100:.1f}%)")
    print(f"  大高开封板: {len(high_open)} ({len(high_open)/len(first_board_stats)*100:.1f}%)")
    print(f"  可买入: {len(buyable)} ({len(buyable)/len(first_board_stats)*100:.1f}%)")

    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        bt_stats = [s for s in first_board_stats if s["board_type"] == bt]
        bt_ow = [s for s in bt_stats if s["category"] == "one_word"]
        bt_ho = [s for s in bt_stats if s["category"] == "high_open_sealed"]
        bt_buy = [s for s in bt_stats if s["category"] == "buyable"]
        print(f"\n  {blabel} (N={len(bt_stats)}):")
        print(f"    一字板: {len(bt_ow)} ({len(bt_ow)/len(bt_stats)*100:.1f}%)")
        print(f"    大高开: {len(bt_ho)} ({len(bt_ho)/len(bt_stats)*100:.1f}%)")
        print(f"    可买入: {len(bt_buy)} ({len(bt_buy)/len(bt_stats)*100:.1f}%)")

    # ===== 第一板开盘溢价分布(可买入 vs 不可买) =====
    print(f"\n{'='*80}")
    print(f"  第一板开盘溢价分布 (open vs 前日close)")
    print(f"{'='*80}")
    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        bt_stats = [s for s in first_board_stats if s["board_type"] == bt]
        buyable_stats = [s for s in bt_stats if s["category"] == "buyable"]
        if not buyable_stats:
            continue
        gaps = [s["open_gap_pct"] for s in buyable_stats]
        print(f"\n  {blabel} 可买入 (N={len(gaps)}):")
        print(f"    均值: {sum(gaps)/len(gaps):+.2f}%  中位: {sorted(gaps)[len(gaps)//2]:+.2f}%")
        brackets = [(-99, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
        for lo, hi in brackets:
            cnt = sum(1 for g in gaps if lo <= g < hi)
            pct = cnt / len(gaps) * 100
            print(f"    [{lo:+d}%, {hi:+d}%): {cnt} ({pct:.1f}%)")

    # ===== 各类别回测结果 =====
    print(f"\n{'='*80}")
    print(f"  回测对比: 各类别分别表现")
    print(f"{'='*80}")

    def s(trades):
        if not trades:
            return {"n": 0, "wr": 0, "avg": 0, "med": 0}
        rets = sorted([t["return_pct"] for t in trades])
        wins = [r for r in rets if r > 0]
        return {
            "n": len(rets),
            "wr": len(wins) / len(rets) * 100,
            "avg": sum(rets) / len(rets),
            "med": rets[len(rets) // 2],
        }

    print(f"\n  {'类别':<22} {'交易数':>7} {'胜率%':>7} {'均收益%':>9} {'中位%':>7}")
    print(f"  {'-'*55}")
    for cat_key, cat_data in categories.items():
        st = s(cat_data["trades"])
        label = cat_data["label"]
        print(f"  {label:<22} {st['n']:>7} {st['wr']:>7.1f} {st['avg']:>+9.2f} {st['med']:>+7.2f}")

    # 只看可买入的, 按板块
    print(f"\n  可买入类别, 按板块:")
    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        trades = categories["buyable"][f"bt_{bt}"]
        st = s(trades)
        print(f"    {blabel}: {st['n']}笔 胜率{st['wr']:.1f}% 均收益{st['avg']:+.2f}%")

    # 可买入类别, 按连板数
    print(f"\n  可买入类别, 按连板数:")
    for nl in sorted(categories["buyable"]["by_nl"].keys()):
        trades = categories["buyable"]["by_nl"][nl]
        if len(trades) < 3:
            continue
        st = s(trades)
        print(f"    {nl}板: {st['n']}笔 胜率{st['wr']:.1f}% 均收益{st['avg']:+.2f}%")

    # ===== 最终结论 =====
    print(f"\n{'='*80}")
    print(f"  最终结论")
    print(f"{'='*80}")

    buyable_main = s(categories["buyable"]["bt_main"])
    buyable_gem = s(categories["buyable"]["bt_gem_star"])
    all_buyable = s(categories["buyable"]["trades"])

    print(f"""
  V1策略: 第一板开盘买入 (只做2板+)

  可买入率: {len(buyable)}/{len(first_board_stats)} = {len(buyable)/len(first_board_stats)*100:.1f}%
    一字板(买不到): {len(one_word)} ({len(one_word)/len(first_board_stats)*100:.1f}%)
    大高开(难买):   {len(high_open)} ({len(high_open)/len(first_board_stats)*100:.1f}%)
    正常可买:       {len(buyable)} ({len(buyable)/len(first_board_stats)*100:.1f}%)

  可买入部分的表现:
    全市场: {all_buyable['n']}笔 {all_buyable['wr']:.1f}%胜率 {all_buyable['avg']:+.2f}%均收益
    主板:   {buyable_main['n']}笔 {buyable_main['wr']:.1f}%胜率 {buyable_main['avg']:+.2f}%均收益
    创科:   {buyable_gem['n']}笔 {buyable_gem['wr']:.1f}%胜率 {buyable_gem['avg']:+.2f}%均收益

  一字板的表现 (虽然买不到, 但看看):
    {s(categories['one_word']['trades'])['n']}笔 {s(categories['one_word']['trades'])['wr']:.1f}%胜率 {s(categories['one_word']['trades'])['avg']:+.2f}%均收益
""")


if __name__ == "__main__":
    main()
