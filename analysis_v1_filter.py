"""
连板猎手 V1 — 第一板筛选因子分析

问题: 每天有很多第一板涨停, 哪些值得买?
目标: 找到D0(第一板涨停日)的特征, 预测D+1能否继续涨

分析维度:
  1. 连板数 (后续是2板段 vs 3板+段)
  2. D0涨停类型: 一字板 vs 开盘后涨停
  3. D0封板强度
  4. D0量能
  5. D0开盘涨幅
  6. 板块
  7. D+1开盘涨幅对收益的影响
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


def try_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def main():
    print("📊 加载数据...")
    groups = defaultdict(list)
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["code"], row["run_first_limit_date"])
            groups[key].append(row)
    for key in groups:
        groups[key].sort(key=lambda r: r["time"])

    total = len(groups)
    print(f"   连板段: {total}")

    # 收集所有第一板的特征 + 后续表现
    first_boards = []

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
        fl_volume = try_float(fl_row["volume"])
        fl_prev_close = try_float(fl_prev["close"])

        if fl_prev_close <= 0 or fl_close <= 0 or fl_open <= 0:
            continue

        # 前一天的量 (用于算量比)
        fl_prev_volume = try_float(fl_prev["volume"])

        # 前2天的量
        if first_limit_idx >= 2:
            fl_prev2 = rows[first_limit_idx - 2]
            fl_prev2_volume = try_float(fl_prev2["volume"])
        else:
            fl_prev2_volume = 0

        # D0特征
        d0_gap = (fl_open / fl_prev_close - 1) * 100  # 开盘涨幅
        d0_return = (fl_close / fl_prev_close - 1) * 100  # 收盘涨幅
        d0_seal = (fl_close / fl_high - 1) * 100 if fl_high > 0 else -999  # 封板强度
        d0_range = (fl_high - fl_low) / fl_prev_close * 100  # 振幅
        d0_vol_ratio = fl_volume / fl_prev_volume if fl_prev_volume > 0 else 0  # 量比
        d0_is_one_word = abs(fl_high - fl_low) / fl_prev_close < 0.002  # 一字板

        # D+1
        if first_limit_idx + 1 >= len(rows):
            continue
        d1_row = rows[first_limit_idx + 1]
        d1_open = try_float(d1_row["open"])
        d1_close = try_float(d1_row["close"])

        if d1_open <= 0:
            continue

        d1_gap = (d1_open / fl_close - 1) * 100  # D+1高开幅度

        # D+1续板?
        d1_is_limit = (d1_close / fl_close - 1) >= params["threshold"] * 0.98

        # V1回测收益 (D+1 open买入, 追踪止损/止盈)
        buy_price = d1_open
        position = {"buy_price": buy_price, "highest": buy_price}
        final_return = 0
        for i in range(first_limit_idx + 2, len(rows)):
            row = rows[i]
            close = try_float(row["close"])
            high = try_float(row["high"])
            if high > position["highest"]:
                position["highest"] = high
            ret_from_buy = (close / position["buy_price"] - 1) * 100
            ret_from_high = (close / position["highest"] - 1) * 100 if position["highest"] > 0 else 0
            if ret_from_buy <= params["stop_loss_pct"]:
                final_return = ret_from_buy; break
            elif ret_from_high <= params["trailing_stop_pct"] and ret_from_buy > 0:
                final_return = ret_from_buy; break
            elif ret_from_buy >= params["take_profit_pct"]:
                final_return = ret_from_buy; break
        else:
            last = rows[-1]
            final_return = (try_float(last["close"]) / position["buy_price"] - 1) * 100

        first_boards.append({
            "code": code,
            "board_type": board_type,
            "n_limit": n_limit,
            "d0_gap": round(d0_gap, 2),
            "d0_return": round(d0_return, 2),
            "d0_seal": round(d0_seal, 4),
            "d0_range": round(d0_range, 2),
            "d0_vol_ratio": round(d0_vol_ratio, 2),
            "d0_is_one_word": d0_is_one_word,
            "d1_gap": round(d1_gap, 2),
            "d1_is_limit": d1_is_limit,
            "final_return": round(final_return, 2),
        })

    print(f"\r   完成: {len(first_boards)} 个第一板")

    # ===== 分析 =====

    def analyze_group(data, label, indent="  "):
        if not data:
            print(f"{indent}{label}: 无数据")
            return
        wins = [d for d in data if d["final_return"] > 0]
        n = len(data)
        wr = len(wins) / n * 100
        avg = sum(d["final_return"] for d in data) / n
        d1_cont = sum(1 for d in data if d["d1_is_limit"]) / n * 100
        print(f"{indent}{label}: {n}笔 胜率{wr:.1f}% 均收益{avg:+.2f}% 续板率{d1_cont:.1f}%")

    # ===== 1. 总览 =====
    print(f"\n{'='*80}")
    print(f"  第一板特征 → D+1买入收益分析")
    print(f"{'='*80}")

    print(f"\n  总样本: {len(first_boards)}")
    analyze_group(first_boards, "全市场")
    for bt in ["main", "gem_star"]:
        analyze_group([d for d in first_boards if d["board_type"] == bt],
                     "主板" if bt == "main" else "创科")

    # ===== 2. D+1开盘涨幅 vs 收益 =====
    print(f"\n{'='*80}")
    print(f"  D+1开盘涨幅 vs 后续收益 (核心筛选维度)")
    print(f"{'='*80}")

    gap_bins = [(-99, -3), (-3, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
    gap_labels = ["<-3%", "-3~0%", "0~2%", "2~5%", "5~8%", ">8%"]

    print(f"\n  {'D+1开盘涨幅':<14} {'数量':>6} {'胜率%':>7} {'均收益%':>9} {'续板率%':>8}")
    print(f"  {'-'*50}")
    for (lo, hi), label in zip(gap_bins, gap_labels):
        sub = [d for d in first_boards if lo <= d["d1_gap"] < hi]
        if not sub:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        cont = sum(1 for d in sub if d["d1_is_limit"]) / len(sub) * 100
        print(f"  {label:<14} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f} {cont:>8.1f}")

    # 按板块
    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        print(f"\n  {blabel}:")
        print(f"  {'D+1开盘涨幅':<14} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
        print(f"  {'-'*42}")
        for (lo, hi), label in zip(gap_bins, gap_labels):
            sub = [d for d in first_boards if lo <= d["d1_gap"] < hi and d["board_type"] == bt]
            if not sub:
                continue
            wins = sum(1 for d in sub if d["final_return"] > 0)
            avg = sum(d["final_return"] for d in sub) / len(sub)
            print(f"  {label:<14} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f}")

    # ===== 3. D0开盘涨幅 vs 收益 =====
    print(f"\n{'='*80}")
    print(f"  D0(第一板)开盘涨幅 vs D+1买入收益")
    print(f"{'='*80}")

    d0_gap_bins = [(-99, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
    d0_gap_labels = ["低开(<0%)", "小高开(0-2%)", "中高开(2-5%)", "大高开(5-8%)", "一字板(>8%)"]

    print(f"\n  {'D0开盘涨幅':<18} {'数量':>6} {'胜率%':>7} {'均收益%':>9} {'D+1续板%':>8}")
    print(f"  {'-'*55}")
    for (lo, hi), label in zip(d0_gap_bins, d0_gap_labels):
        sub = [d for d in first_boards if lo <= d["d0_gap"] < hi]
        if not sub:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        cont = sum(1 for d in sub if d["d1_is_limit"]) / len(sub) * 100
        print(f"  {label:<18} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f} {cont:>8.1f}")

    # ===== 4. 封板强度 vs 收益 =====
    print(f"\n{'='*80}")
    print(f"  D0封板强度 vs 收益")
    print(f"{'='*80}")

    seal_bins = [(-99, -0.5), (-0.5, -0.1), (-0.1, 0), (0, 0.5), (0.5, 99)]
    seal_labels = ["紧封(<-0.5%)", "正常(-0.5~-0.1%)", "完美(-0.1~0%)", "微松(0~0.5%)", "松封(>0.5%)"]

    print(f"\n  {'封板强度':<22} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*50}")
    for (lo, hi), label in zip(seal_bins, seal_labels):
        sub = [d for d in first_boards if lo <= d["d0_seal"] < hi]
        if not sub:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        print(f"  {label:<22} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f}")

    # ===== 5. 量比 vs 收益 =====
    print(f"\n{'='*80}")
    print(f"  D0量比 vs 收益")
    print(f"{'='*80}")

    vol_bins = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 99)]
    vol_labels = ["<1x(缩量)", "1-2x", "2-3x", "3-5x", ">5x(巨量)"]

    print(f"\n  {'量比':<16} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*44}")
    for (lo, hi), label in zip(vol_bins, vol_labels):
        sub = [d for d in first_boards if lo <= d["d0_vol_ratio"] < hi]
        if not sub:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        print(f"  {label:<16} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f}")

    # ===== 6. 按连板数 =====
    print(f"\n{'='*80}")
    print(f"  后续连板数 vs D+1买入收益")
    print(f"{'='*80}")

    print(f"\n  {'连板数':<8} {'数量':>6} {'胜率%':>7} {'均收益%':>9} {'D+1续板%':>8}")
    print(f"  {'-'*44}")
    for nl in sorted(set(d["n_limit"] for d in first_boards)):
        sub = [d for d in first_boards if d["n_limit"] == nl]
        if len(sub) < 5:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        cont = sum(1 for d in sub if d["d1_is_limit"]) / len(sub) * 100
        print(f"  {nl}板{'':<5} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f} {cont:>8.1f}")

    # ===== 7. 综合筛选: D+1涨幅<2% =====
    print(f"\n{'='*80}")
    print(f"  综合筛选: D+1开盘涨幅<2% 的第一板")
    print(f"{'='*80}")

    filtered = [d for d in first_boards if d["d1_gap"] < 2]
    analyze_group(filtered, "D+1涨幅<2%")
    for bt in ["main", "gem_star"]:
        sub = [d for d in filtered if d["board_type"] == bt]
        blabel = "主板" if bt == "main" else "创科"
        analyze_group(sub, blabel)

    print(f"\n  D+1涨幅<2% + 主板:")
    main_filtered = [d for d in first_boards if d["d1_gap"] < 2 and d["board_type"] == "main"]
    analyze_group(main_filtered, "主板D+1<2%")

    # D+1涨幅<0%
    print(f"\n  D+1低开(<0%):")
    d1_low = [d for d in first_boards if d["d1_gap"] < 0]
    analyze_group(d1_low, "D+1低开")
    for bt in ["main", "gem_star"]:
        sub = [d for d in d1_low if d["board_type"] == bt]
        blabel = "主板" if bt == "main" else "创科"
        analyze_group(sub, blabel)

    # ===== 8. 最优组合筛选 =====
    print(f"\n{'='*80}")
    print(f"  最优组合筛选方案")
    print(f"{'='*80}")

    filters = [
        ("无过滤", lambda d: True),
        ("D+1涨幅<2%", lambda d: d["d1_gap"] < 2),
        ("D+1涨幅<3%", lambda d: d["d1_gap"] < 3),
        ("D+1涨幅<5%", lambda d: d["d1_gap"] < 5),
        ("D+1低开", lambda d: d["d1_gap"] < 0),
        ("D+1涨幅<2%+主板", lambda d: d["d1_gap"] < 2 and d["board_type"] == "main"),
        ("D+1涨幅<3%+主板", lambda d: d["d1_gap"] < 3 and d["board_type"] == "main"),
        ("D+1涨幅<5%+创科+续板", lambda d: d["d1_gap"] < 5 and d["board_type"] == "gem_star" and d["d1_is_limit"]),
    ]

    print(f"\n  {'筛选方案':<28} {'数量':>6} {'胜率%':>7} {'均收益%':>9} {'续板率%':>8}")
    print(f"  {'-'*62}")
    for label, fn in filters:
        sub = [d for d in first_boards if fn(d)]
        if not sub:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in sub) / len(sub)
        cont = sum(1 for d in sub if d["d1_is_limit"]) / len(sub) * 100
        print(f"  {label:<28} {len(sub):>6} {wins/len(sub)*100:>7.1f} {avg:>+9.2f} {cont:>8.1f}")


if __name__ == "__main__":
    main()
