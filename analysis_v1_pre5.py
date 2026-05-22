"""
连板猎手 V1 — 第一板前5天特征分析

目标: 通过D0前5天走势, 预判后续能涨多少板

分析维度:
  1. 前5天涨跌幅 (蓄势 vs 已拉升)
  2. 前5天量能趋势 (缩量蓄势 vs 放量启动)
  3. 前5天振幅 (窄幅整理 vs 宽幅震荡)
  4. 前5天最大单日涨幅 (有没有提前拉升)
  5. 前1天走势 (小阳/小阴/大阳)
  6. 前5天高低点位置 (底部启动 vs 中途加速)
  7. 组合特征
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

        if first_limit_idx is None or first_limit_idx < 5:
            continue  # 需要前5天数据

        fl = rows[first_limit_idx]
        fl_close = try_float(fl["close"])
        fl_open = try_float(fl["open"])
        fl_high = try_float(fl["high"])
        fl_low = try_float(fl["low"])
        fl_vol = try_float(fl["volume"])
        fl_prev_close = try_float(rows[first_limit_idx - 1]["close"])

        if fl_prev_close <= 0 or fl_close <= 0:
            continue

        # D+1
        if first_limit_idx + 1 >= len(rows):
            continue
        d1 = rows[first_limit_idx + 1]
        d1_open = try_float(d1["open"])
        if d1_open <= 0:
            continue

        d1_gap = (d1_open / fl_close - 1) * 100

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

        # ===== 前5天特征 =====
        pre5 = rows[first_limit_idx - 5:first_limit_idx]  # D-5 ~ D-1

        # 前5天close序列
        pre5_close = [try_float(r["close"]) for r in pre5]
        pre5_open = [try_float(r["open"]) for r in pre5]
        pre5_high = [try_float(r["high"]) for r in pre5]
        pre5_low = [try_float(r["low"]) for r in pre5]
        pre5_vol = [try_float(r["volume"]) for r in pre5]

        # 前5天涨跌幅 (D-5开盘 → D-1收盘)
        pre5_start = pre5_open[0] if pre5_open[0] > 0 else pre5_close[0]
        pre5_end = pre5_close[-1]
        pre5_return = (pre5_end / pre5_start - 1) * 100 if pre5_start > 0 else 0

        # 前5天每日涨跌幅
        pre5_daily_returns = []
        for j in range(len(pre5_close)):
            if j == 0:
                continue
            if pre5_close[j-1] > 0:
                pre5_daily_returns.append((pre5_close[j] / pre5_close[j-1] - 1) * 100)

        # 前5天最大单日涨幅
        pre5_max_daily_gain = max(pre5_daily_returns) if pre5_daily_returns else 0

        # 前5天最大单日跌幅
        pre5_max_daily_loss = min(pre5_daily_returns) if pre5_daily_returns else 0

        # 前5天振幅 (最高-最低) / 最低
        pre5_highest = max(pre5_high)
        pre5_lowest = min(pre5_low)
        pre5_range = (pre5_highest - pre5_lowest) / pre5_lowest * 100 if pre5_lowest > 0 else 0

        # 前5天量能趋势: 后3天均量 / 前2天均量
        vol_early = sum(pre5_vol[:2]) / 2 if len(pre5_vol) >= 2 else pre5_vol[0]
        vol_late = sum(pre5_vol[2:]) / 3 if len(pre5_vol) >= 3 else pre5_vol[-1]
        vol_trend = vol_late / vol_early if vol_early > 0 else 1

        # 前5天平均量比
        vol_avg = sum(pre5_vol) / len(pre5_vol) if pre5_vol else 0

        # 前1天涨跌
        pre1_return = (pre5_close[-1] / pre5_close[-2] - 1) * 100 if len(pre5_close) >= 2 and pre5_close[-2] > 0 else 0

        # 前1天振幅
        pre1_range = (pre5_high[-1] - pre5_low[-1]) / pre5_close[-2] * 100 if len(pre5_close) >= 2 and pre5_close[-2] > 0 else 0

        # 前5天是否连续上涨
        pre5_consecutive_up = all(r > 0 for r in pre5_daily_returns) if pre5_daily_returns else False

        # 前5天阳线数
        pre5_up_days = sum(1 for r in pre5_daily_returns if r > 0)

        # D0特征
        d0_gap = (fl_open / fl_prev_close - 1) * 100
        d0_vol_ratio = fl_vol / try_float(rows[first_limit_idx - 1]["volume"]) if try_float(rows[first_limit_idx - 1]["volume"]) > 0 else 0
        d0_upper_shadow = (fl_high - fl_close) / fl_prev_close * 100 if fl_prev_close > 0 else 0

        # 前5天相对位置: D-1收盘在前5天区间的位置
        if pre5_highest > pre5_lowest:
            pre5_position = (pre5_end - pre5_lowest) / (pre5_highest - pre5_lowest)
        else:
            pre5_position = 0.5

        first_boards.append({
            "code": code,
            "board_type": board_type,
            "n_limit": n_limit,
            "d1_gap": round(d1_gap, 2),
            "final_return": round(final_return, 2),
            # 前5天特征
            "pre5_return": round(pre5_return, 2),
            "pre5_range": round(pre5_range, 2),
            "pre5_max_daily_gain": round(pre5_max_daily_gain, 2),
            "pre5_max_daily_loss": round(pre5_max_daily_loss, 2),
            "pre5_vol_trend": round(vol_trend, 2),
            "pre5_up_days": pre5_up_days,
            "pre5_consecutive_up": pre5_consecutive_up,
            "pre5_position": round(pre5_position, 2),
            "pre1_return": round(pre1_return, 2),
            "pre1_range": round(pre1_range, 2),
            # D0特征
            "d0_gap": round(d0_gap, 2),
            "d0_vol_ratio": round(d0_vol_ratio, 2),
            "d0_upper_shadow": round(d0_upper_shadow, 4),
        })

    print(f"\r   完成: {len(first_boards)} 个第一板")

    # 只分析D+1涨幅<2% + 量比>2x的高置信度样本
    fb = [d for d in first_boards if d["d1_gap"] < 2 and d["d0_vol_ratio"] >= 2]
    print(f"  高置信度样本(D+1<2%+量比>2x): {len(fb)}")

    def show(data, label):
        if len(data) < 5:
            return
        wins = sum(1 for d in data if d["final_return"] > 0)
        avg = sum(d["final_return"] for d in data) / len(data)
        avg_nl = sum(d["n_limit"] for d in data) / len(data)
        # 连板数分布
        nl_dist = defaultdict(int)
        for d in data:
            nl_dist[d["n_limit"]] += 1
        nl_str = " ".join(f"{k}板:{v}" for k, v in sorted(nl_dist.items()))
        print(f"  {label:<36} {len(data):>5}笔 胜率{wins/len(data)*100:>5.1f}% 均收益{avg:>+7.2f}% 均连板{avg_nl:.1f}  [{nl_str}]")

    # ===== 1. 前5天涨跌幅 =====
    print(f"\n{'='*90}")
    print(f"  前5天涨跌幅 vs 后续连板数")
    print(f"{'='*90}")
    bins = [(-99, -5), (-5, -2), (-2, 0), (0, 3), (3, 5), (5, 10), (10, 99)]
    labels = ["<-5%(大跌)", "-5~-2%", "-2~0%", "0~3%", "3~5%", "5~10%", ">10%(大涨)"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_return"] < hi]
        show(sub, label)

    # ===== 2. 前5天振幅 =====
    print(f"\n{'='*90}")
    print(f"  前5天振幅 vs 后续连板数")
    print(f"{'='*90}")
    bins = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 99)]
    labels = ["<5%(窄幅)", "5-10%", "10-15%", "15-20%", ">20%(宽幅)"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_range"] < hi]
        show(sub, label)

    # ===== 3. 前5天最大单日涨幅 =====
    print(f"\n{'='*90}")
    print(f"  前5天最大单日涨幅 vs 后续连板数")
    print(f"{'='*90}")
    bins = [(-99, 2), (2, 4), (4, 6), (6, 8), (8, 99)]
    labels = ["<2%", "2-4%", "4-6%", "6-8%", ">8%(接近涨停)"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_max_daily_gain"] < hi]
        show(sub, label)

    # ===== 4. 前5天量能趋势 =====
    print(f"\n{'='*90}")
    print(f"  前5天量能趋势(后3天均量/前2天均量) vs 后续连板数")
    print(f"{'='*90}")
    bins = [(0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)]
    labels = ["<0.5x(极度缩量)", "0.5-0.8x(缩量)", "0.8-1x(平稳)", "1-1.5x(温和放量)", "1.5-2x(放量)", ">2x(巨量)"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_vol_trend"] < hi]
        show(sub, label)

    # ===== 5. 前5天阳线数 =====
    print(f"\n{'='*90}")
    print(f"  前5天阳线数 vs 后续连板数")
    print(f"{'='*90}")
    for n_up in range(6):
        sub = [d for d in fb if d["pre5_up_days"] == n_up]
        show(sub, f"{n_up}阳")

    # ===== 6. 前1天涨跌 =====
    print(f"\n{'='*90}")
    print(f"  前1天涨跌 vs 后续连板数")
    print(f"{'='*90}")
    bins = [(-99, -3), (-3, 0), (0, 2), (2, 5), (5, 99)]
    labels = ["<-3%(大跌)", "-3~0%", "0~2%", "2~5%", ">5%"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre1_return"] < hi]
        show(sub, label)

    # ===== 7. 前5天相对位置 =====
    print(f"\n{'='*90}")
    print(f"  前5天相对位置(D-1在区间中的位置) vs 后续连板数")
    print(f"{'='*90}")
    bins = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    labels = ["底部(0-20%)", "中下(20-40%)", "中间(40-60%)", "中上(60-80%)", "高位(80-100%)"]
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_position"] < hi]
        show(sub, label)

    # ===== 8. 组合筛选: 预判高连板 =====
    print(f"\n{'='*90}")
    print(f"  组合筛选: 预判3板+ vs 2板")
    print(f"{'='*90}")

    fb_3plus = [d for d in fb if d["n_limit"] >= 3]
    fb_2 = [d for d in fb if d["n_limit"] == 2]
    print(f"\n  3板+: {len(fb_3plus)}笔  2板: {len(fb_2)}笔")

    filters = [
        ("无过滤", lambda d: True),
        ("前5天涨0-5%", lambda d: 0 <= d["pre5_return"] < 5),
        ("前5天涨0-3%", lambda d: 0 <= d["pre5_return"] < 3),
        ("前5天跌0-5%", lambda d: -5 <= d["pre5_return"] < 0),
        ("前5天振幅<10%", lambda d: d["pre5_range"] < 10),
        ("前5天振幅<15%", lambda d: d["pre5_range"] < 15),
        ("前5天缩量(<0.8x)", lambda d: d["pre5_vol_trend"] < 0.8),
        ("前5天放量(>1.2x)", lambda d: d["pre5_vol_trend"] > 1.2),
        ("前5天3+阳", lambda d: d["pre5_up_days"] >= 3),
        ("前1天小涨0-2%", lambda d: 0 <= d["pre1_return"] < 2),
        ("前1天小跌-3~0%", lambda d: -3 <= d["pre1_return"] < 0),
        ("前5天位置中下(20-50%)", lambda d: 0.2 <= d["pre5_position"] < 0.5),
        ("前5天位置底部(0-30%)", lambda d: d["pre5_position"] < 0.3),
        ("前5天涨0-5%+缩量<0.8x", lambda d: 0 <= d["pre5_return"] < 5 and d["pre5_vol_trend"] < 0.8),
        ("前5天涨0-5%+振幅<10%", lambda d: 0 <= d["pre5_return"] < 5 and d["pre5_range"] < 10),
        ("前5天涨0-3%+振幅<10%+缩量", lambda d: 0 <= d["pre5_return"] < 3 and d["pre5_range"] < 10 and d["pre5_vol_trend"] < 0.8),
        ("前5天跌+缩量+位置低", lambda d: d["pre5_return"] < 0 and d["pre5_vol_trend"] < 0.8 and d["pre5_position"] < 0.4),
        ("前5天跌0-3%+缩量+位置低", lambda d: -3 <= d["pre5_return"] < 0 and d["pre5_vol_trend"] < 0.8 and d["pre5_position"] < 0.4),
        ("前1天小跌+前5天缩量", lambda d: -3 <= d["pre1_return"] < 0 and d["pre5_vol_trend"] < 0.8),
        ("前5天涨0-3%+前1天小跌", lambda d: 0 <= d["pre5_return"] < 3 and -3 <= d["pre1_return"] < 0),
    ]

    print(f"\n  {'筛选方案':<40} {'N':>5} {'胜率%':>6} {'均收益%':>8} {'均连板':>6} {'3板+%':>6}")
    print(f"  {'-'*76}")
    for label, fn in filters:
        sub = [d for d in fb if fn(d)]
        if len(sub) < 10:
            continue
        wins = sum(1 for d in sub if d["final_return"] > 0)
        avg_ret = sum(d["final_return"] for d in sub) / len(sub)
        avg_nl = sum(d["n_limit"] for d in sub) / len(sub)
        pct_3plus = sum(1 for d in sub if d["n_limit"] >= 3) / len(sub) * 100
        print(f"  {label:<40} {len(sub):>5} {wins/len(sub)*100:>6.1f} {avg_ret:>+8.2f} {avg_nl:>6.1f} {pct_3plus:>6.1f}")

    # ===== 9. 连板数预测: 按特征分组看均连板 =====
    print(f"\n{'='*90}")
    print(f"  连板数预测: 特征 → 平均连板数")
    print(f"{'='*90}")

    print(f"\n  全样本平均连板数: {sum(d['n_limit'] for d in fb)/len(fb):.2f}")

    # 前5天涨跌幅分组
    print(f"\n  前5天涨跌幅 → 平均连板数:")
    for (lo, hi), label in zip(bins, labels):
        sub = [d for d in fb if lo <= d["pre5_return"] < hi]
        if len(sub) < 5:
            continue
        avg_nl = sum(d["n_limit"] for d in sub) / len(sub)
        pct_3plus = sum(1 for d in sub if d["n_limit"] >= 3) / len(sub) * 100
        print(f"    {label:<20} 均连板{avg_nl:.2f}  3板+占{pct_3plus:.0f}%  ({len(sub)}笔)")


if __name__ == "__main__":
    main()
