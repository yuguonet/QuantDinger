"""
连板猎手 V1 — D0第一板深度筛选因子分析

目标: 从每天N个第一板中, 筛出最值得买的2-5只

分析维度:
  1. D0振幅 (越小=封得越死)
  2. D0量比 (放量程度)
  3. D0开盘涨幅 (一字板 vs 低开涨停)
  4. D0上影线 (有无分歧)
  5. D0实体占比 (一字板特征)
  6. D0前一日走势 (前一天涨/跌)
  7. D0换手率 (如果有volume数据, 用成交量变化近似)
  8. D0涨停时间特征 (开盘就封 vs 盘中封)
  9. D0与前5日量能对比
  10. 组合筛选
"""
import csv
from collections import defaultdict
import math

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

        fl = rows[first_limit_idx]
        fl_prev = rows[first_limit_idx - 1]
        fl_close = try_float(fl["close"])
        fl_open = try_float(fl["open"])
        fl_high = try_float(fl["high"])
        fl_low = try_float(fl["low"])
        fl_vol = try_float(fl["volume"])
        fl_prev_close = try_float(fl_prev["close"])
        fl_prev_vol = try_float(fl_prev["volume"])
        fl_prev_open = try_float(fl_prev["open"])
        fl_prev_high = try_float(fl_prev["high"])
        fl_prev_low = try_float(fl_prev["low"])

        if fl_prev_close <= 0 or fl_close <= 0 or fl_open <= 0:
            continue

        # 前2-5日量能
        vols_prev5 = []
        for j in range(max(0, first_limit_idx - 5), first_limit_idx):
            v = try_float(rows[j]["volume"])
            if v > 0:
                vols_prev5.append(v)
        vol_ma5 = sum(vols_prev5) / len(vols_prev5) if vols_prev5 else 0

        # D+1
        if first_limit_idx + 1 >= len(rows):
            continue
        d1 = rows[first_limit_idx + 1]
        d1_open = try_float(d1["open"])
        d1_close = try_float(d1["close"])
        if d1_open <= 0:
            continue

        d1_gap = (d1_open / fl_close - 1) * 100
        d1_is_limit = (d1_close / fl_close - 1) >= params["threshold"] * 0.98

        # V1回测收益
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

        # D0特征
        d0_gap = (fl_open / fl_prev_close - 1) * 100
        d0_return = (fl_close / fl_prev_close - 1) * 100
        d0_range_pct = (fl_high - fl_low) / fl_prev_close * 100  # 振幅
        d0_upper_shadow = (fl_high - fl_close) / fl_prev_close * 100  # 上影线
        d0_lower_shadow = (fl_open - fl_low) / fl_prev_close * 100 if fl_open > fl_low else 0  # 下影线(开盘后回落再拉)
        d0_body = abs(fl_close - fl_open) / fl_prev_close * 100  # 实体
        d0_seal = (fl_close / fl_high - 1) * 100 if fl_high > 0 else -999  # 封板强度

        # 量比
        d0_vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        d0_vol_ratio_5d = fl_vol / vol_ma5 if vol_ma5 > 0 else 0

        # 是否一字板
        d0_is_one_word = (fl_high - fl_low) / fl_prev_close < 0.002

        # 是否开盘就封 (open ≈ close ≈ high, 且low ≈ open)
        d0_open_at_limit = d0_gap >= params["threshold"] * 0.85

        # 前一天涨跌
        d0_prev_return = (fl_prev_close / try_float(rows[first_limit_idx - 2]["close"]) - 1) * 100 if first_limit_idx >= 2 else 0

        # 前一天振幅
        d0_prev_range = (fl_prev_high - fl_prev_low) / try_float(rows[first_limit_idx - 2]["close"]) * 100 if first_limit_idx >= 2 else 0

        first_boards.append({
            "code": code,
            "board_type": board_type,
            "n_limit": n_limit,
            "d0_gap": round(d0_gap, 2),
            "d0_return": round(d0_return, 2),
            "d0_range_pct": round(d0_range_pct, 2),
            "d0_upper_shadow": round(d0_upper_shadow, 4),
            "d0_lower_shadow": round(d0_lower_shadow, 4),
            "d0_body": round(d0_body, 2),
            "d0_seal": round(d0_seal, 4),
            "d0_vol_ratio": round(d0_vol_ratio, 2),
            "d0_vol_ratio_5d": round(d0_vol_ratio_5d, 2),
            "d0_is_one_word": d0_is_one_word,
            "d0_open_at_limit": d0_open_at_limit,
            "d0_prev_return": round(d0_prev_return, 2),
            "d0_prev_range": round(d0_prev_range, 2),
            "d1_gap": round(d1_gap, 2),
            "d1_is_limit": d1_is_limit,
            "final_return": round(final_return, 2),
        })

    print(f"\r   完成: {len(first_boards)} 个第一板")

    def analyze(data, label, indent="  "):
        if not data:
            print(f"{indent}{label}: 无数据")
            return None
        wins = sum(1 for d in data if d["final_return"] > 0)
        n = len(data)
        avg = sum(d["final_return"] for d in data) / n
        return {"n": n, "wr": wins/n*100, "avg": avg}

    def show(data, label, indent="  "):
        r = analyze(data, label, indent)
        if r:
            print(f"{indent}{label}: {r['n']}笔 胜率{r['wr']:.1f}% 均收益{r['avg']:+.2f}%")
        return r

    # 只看D+1涨幅<2%的(可执行)
    fb = [d for d in first_boards if d["d1_gap"] < 2]
    print(f"\n  D+1涨幅<2%的样本: {len(fb)} (全市场{len(first_boards)})")

    # ===== 1. 振幅 =====
    print(f"\n{'='*80}")
    print(f"  D0振幅 vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(0, 1), (1, 3), (3, 5), (5, 8), (8, 99)]
    labels = ["<1%(一字)", "1-3%", "3-5%", "5-8%", ">8%"]
    print(f"\n  {'振幅':<14} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*40}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_range_pct"] < hi]
        show(sub, label)

    # ===== 2. 量比 =====
    print(f"\n{'='*80}")
    print(f"  D0量比 (vs前日) vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 99)]
    labels = ["<1x", "1-2x", "2-3x", "3-5x", ">5x"]
    print(f"\n  {'量比':<14} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*40}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_vol_ratio"] < hi]
        show(sub, label)

    # ===== 3. 5日量比 =====
    print(f"\n{'='*80}")
    print(f"  D0量比 (vs前5日均量) vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 99)]
    labels = ["<1x", "1-2x", "2-3x", "3-5x", ">5x"]
    print(f"\n  {'5日量比':<14} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*40}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_vol_ratio_5d"] < hi]
        show(sub, label)

    # ===== 4. 开盘涨幅 =====
    print(f"\n{'='*80}")
    print(f"  D0开盘涨幅 vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(-99, 0), (0, 2), (2, 5), (5, 8), (8, 99)]
    labels = ["低开(<0%)", "小高开(0-2%)", "中高开(2-5%)", "大高开(5-8%)", "一字(>8%)"]
    print(f"\n  {'开盘涨幅':<18} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*46}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_gap"] < hi]
        show(sub, label)

    # ===== 5. 上影线 =====
    print(f"\n{'='*80}")
    print(f"  D0上影线 vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(-99, 0), (0, 0.5), (0.5, 2), (2, 5), (5, 99)]
    labels = ["无上影", "极小(<0.5%)", "小(0.5-2%)", "中(2-5%)", "大(>5%)"]
    print(f"\n  {'上影线':<18} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*46}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_upper_shadow"] < hi]
        show(sub, label)

    # ===== 6. 前一天涨跌 =====
    print(f"\n{'='*80}")
    print(f"  D0前一天涨跌 vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(-99, -3), (-3, 0), (0, 3), (3, 5), (5, 99)]
    labels = ["大跌(<-3%)", "小跌(-3~0%)", "小涨(0-3%)", "中涨(3-5%)", "大涨(>5%)"]
    print(f"\n  {'前日涨跌':<18} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*46}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_prev_return"] < hi]
        show(sub, label)

    # ===== 7. 封板强度 =====
    print(f"\n{'='*80}")
    print(f"  D0封板强度 vs 收益 (D+1涨幅<2%)")
    print(f"{'='*80}")
    bins = [(-99, -0.5), (-0.5, -0.1), (-0.1, 0), (0, 0.5), (0.5, 99)]
    labels = ["紧封(<-0.5%)", "正常(-0.5~-0.1%)", "完美(-0.1~0%)", "微松(0~0.5%)", "松(>0.5%)"]
    print(f"\n  {'封板强度':<22} {'数量':>6} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*50}")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["d0_seal"] < hi]
        show(sub, label)

    # ===== 8. 一字板 vs 非一字板 =====
    print(f"\n{'='*80}")
    print(f"  一字板 vs 非一字板 (D+1涨幅<2%)")
    print(f"{'='*80}")
    show([d for d in fb if d["d0_is_one_word"]], "一字板")
    show([d for d in fb if not d["d0_is_one_word"]], "非一字板")

    # ===== 9. 开盘就封 vs 盘中封 =====
    print(f"\n{'='*80}")
    print(f"  开盘就封 vs 盘中封 (D+1涨幅<2%)")
    print(f"{'='*80}")
    show([d for d in fb if d["d0_open_at_limit"]], "开盘就封(高开>8.3%)")
    show([d for d in fb if not d["d0_open_at_limit"]], "盘中封(高开<8.3%)")

    # ===== 10. 组合筛选 =====
    print(f"\n{'='*80}")
    print(f"  组合筛选方案 (D+1涨幅<2%)")
    print(f"{'='*80}")

    filters = [
        ("无过滤", lambda d: True),
        ("非一字板", lambda d: not d["d0_is_one_word"]),
        ("非一字板+振幅<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5),
        ("非一字板+振幅<3%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 3),
        ("非一字板+低开/小高开", lambda d: not d["d0_is_one_word"] and d["d0_gap"] < 5),
        ("非一字板+量比2-5x", lambda d: not d["d0_is_one_word"] and 2 <= d["d0_vol_ratio"] < 5),
        ("非一字板+量比2x+", lambda d: not d["d0_is_one_word"] and d["d0_vol_ratio"] >= 2),
        ("非一字板+振幅<5%+量比2x+", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and d["d0_vol_ratio"] >= 2),
        ("非一字板+振幅<5%+量比1-5x", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and 1 <= d["d0_vol_ratio"] < 5),
        ("非一字板+振幅<5%+量比1-5x+高开<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and 1 <= d["d0_vol_ratio"] < 5 and d["d0_gap"] < 5),
        ("非一字板+振幅<3%+量比1-5x+高开<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 3 and 1 <= d["d0_vol_ratio"] < 5 and d["d0_gap"] < 5),
        ("非一字板+振幅<5%+量比2x++高开<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and d["d0_vol_ratio"] >= 2 and d["d0_gap"] < 5),
        ("非一字板+前日小涨0-3%+振幅<5%", lambda d: not d["d0_is_one_word"] and 0 <= d["d0_prev_return"] < 3 and d["d0_range_pct"] < 5),
    ]

    print(f"\n  {'筛选方案':<44} {'N':>5} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*70}")
    for label, fn in filters:
        sub = [d for d in fb if fn(d)]
        if len(sub) < 10:
            continue
        r = analyze(sub, label)
        if r:
            print(f"  {label:<44} {r['n']:>5} {r['wr']:>7.1f} {r['avg']:>+9.2f}")

    # ===== 11. 按板块分 =====
    print(f"\n{'='*80}")
    print(f"  分板块最优组合")
    print(f"{'='*80}")

    for bt in ["main", "gem_star"]:
        blabel = "主板" if bt == "main" else "创科"
        fb_bt = [d for d in fb if d["board_type"] == bt]
        print(f"\n  {blabel} (D+1涨幅<2%, N={len(fb_bt)}):")

        filters_bt = [
            ("无过滤", lambda d: True),
            ("非一字板", lambda d: not d["d0_is_one_word"]),
            ("非一字板+振幅<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5),
            ("非一字板+振幅<3%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 3),
            ("非一字板+量比2x+", lambda d: not d["d0_is_one_word"] and d["d0_vol_ratio"] >= 2),
            ("非一字板+振幅<5%+量比2x+", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and d["d0_vol_ratio"] >= 2),
            ("非一字板+振幅<5%+量比1-5x", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and 1 <= d["d0_vol_ratio"] < 5),
            ("非一字板+振幅<5%+高开<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5 and d["d0_gap"] < 5),
        ]

        print(f"  {'筛选方案':<40} {'N':>5} {'胜率%':>7} {'均收益%':>9}")
        print(f"  {'-'*65}")
        for label, fn in filters_bt:
            sub = [d for d in fb_bt if fn(d)]
            if len(sub) < 10:
                continue
            r = analyze(sub, label)
            if r:
                print(f"  {label:<40} {r['n']:>5} {r['wr']:>7.1f} {r['avg']:>+9.2f}")

    # ===== 12. 创科需要高开过滤(D+1涨幅<5%) =====
    print(f"\n{'='*80}")
    print(f"  创科最优组合 (D+1涨幅<5%)")
    print(f"{'='*80}")

    fb_gem = [d for d in first_boards if d["d1_gap"] < 5 and d["board_type"] == "gem_star"]
    print(f"\n  创科 D+1涨幅<5%: N={len(fb_gem)}")

    filters_gem = [
        ("无过滤", lambda d: True),
        ("非一字板", lambda d: not d["d0_is_one_word"]),
        ("非一字板+振幅<5%", lambda d: not d["d0_is_one_word"] and d["d0_range_pct"] < 5),
        ("非一字板+量比2x+", lambda d: not d["d0_is_one_word"] and d["d0_vol_ratio"] >= 2),
        ("非一字板+量比1x+", lambda d: not d["d0_is_one_word"] and d["d0_vol_ratio"] >= 1),
        ("非一字板+高开<5%", lambda d: not d["d0_is_one_word"] and d["d0_gap"] < 5),
    ]

    print(f"\n  {'筛选方案':<40} {'N':>5} {'胜率%':>7} {'均收益%':>9}")
    print(f"  {'-'*65}")
    for label, fn in filters_gem:
        sub = [d for d in fb_gem if fn(d)]
        if len(sub) < 10:
            continue
        r = analyze(sub, label)
        if r:
            print(f"  {label:<40} {r['n']:>5} {r['wr']:>7.1f} {r['avg']:>+9.2f}")


if __name__ == "__main__":
    main()
