#!/usr/bin/env python3
"""
连板信号横向过滤 — 同日涨停股之间比对，去噪提纯

核心思路：
    某天有 N 只股票涨停，其中大部分明天就结束（噪声），
    少数会继续连板（信号）。横向比较这 N 只股票的特征，
    过滤掉噪声，提高信号浓度。

过滤维度：
    1. 第一板当天：高开幅度、封板强度、振幅、量比
    2. 第一板次日：振幅、收盘位置、是否涨停
    3. 前置趋势：前几天涨幅
"""
import csv
import os
import argparse
from collections import defaultdict, Counter
from datetime import datetime


def load_groups(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    groups = defaultdict(list)
    for r in rows:
        groups[(r["code"], r["run_first_limit_date"])].append(r)
    for k in groups:
        groups[k].sort(key=lambda x: x["time"])
    return groups


def extract_features(group):
    """提取一组股票的特征（只用第一板及之前数据）"""
    n = len(group)
    board = group[0].get("board", "")
    threshold = 0.198 if board in ("创业板", "科创板") else 0.098
    streak = int(group[0]["run_n_limit_ups"])

    fld = group[0]["run_first_limit_date"]
    fl_pos = None
    for i, r in enumerate(group):
        if r["time"] == fld:
            fl_pos = i
            break
    if fl_pos is None or fl_pos < 1:
        return None

    fl = group[fl_pos]
    prev = group[fl_pos - 1]
    fl_open = float(fl["open"])
    fl_close = float(fl["close"])
    fl_high = float(fl["high"])
    fl_low = float(fl["low"])
    fl_vol = float(fl["volume"])
    prev_close = float(prev["close"])
    prev_vol = float(prev["volume"])

    # 前5天均量
    vol_window = [float(group[j]["volume"]) for j in range(max(0, fl_pos - 5), fl_pos)]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else fl_vol

    # 前5天涨幅
    prev5_return = None
    if fl_pos >= 5:
        prev5_close = float(group[fl_pos - 5]["close"])
        prev5_return = (prev_close / prev5_close - 1) * 100

    feat = {
        "code": group[0]["code"],
        "streak": streak,
        "board": board,
        "first_limit_date": fld,
        # 第一板当天（T日收盘前可观测）
        "fl_gap_pct": (fl_open / prev_close - 1) * 100,
        "fl_return_pct": (fl_close / prev_close - 1) * 100,
        "fl_amplitude_pct": (fl_high - fl_low) / prev_close * 100,
        "fl_seal_pct": (fl_close - fl_low) / fl_close * 100 if fl_close > 0 else 0,
        "fl_vol_ratio": fl_vol / avg_vol if avg_vol > 0 else 0,
        "fl_body_ratio": abs(fl_close - fl_open) / (fl_high - fl_low) if (fl_high - fl_low) > 0 else 0,
        "prev5_return_pct": prev5_return,
    }

    # 前一天量比
    if fl_pos >= 2:
        prev_vol_window = [float(group[j]["volume"]) for j in range(max(0, fl_pos - 6), fl_pos - 1)]
        prev_avg_vol = sum(prev_vol_window) / len(prev_vol_window) if prev_vol_window else prev_vol
        feat["prev_vol_ratio"] = prev_vol / prev_avg_vol if prev_avg_vol > 0 else 0

    return feat


def extract_pre_daily_features(group, max_days=10):
    """提取D0前逐日特征 (D-1, D-2, ... D-max_days 各自独立)"""
    board = group[0].get("board", "")
    threshold = 0.198 if board in ("创业板", "科创板") else 0.098
    streak = int(group[0]["run_n_limit_ups"])

    fld = group[0]["run_first_limit_date"]
    fl_pos = None
    for i, r in enumerate(group):
        if r["time"] == fld:
            fl_pos = i
            break
    if fl_pos is None or fl_pos < 1:
        return None

    daily = {}
    for offset in range(1, max_days + 1):
        idx = fl_pos - offset
        if idx < 1:
            break
        row = group[idx]
        prev = group[idx - 1]
        close = float(row["close"])
        prev_close = float(prev["close"])
        vol = float(row["volume"])
        prev_vol = float(prev["volume"])
        high = float(row["high"])
        low = float(row["low"])
        if prev_close <= 0 or prev_vol <= 0:
            continue

        daily[offset] = {
            "ret": (close / prev_close - 1) * 100,
            "range": (high - low) / prev_close * 100,
            "vol_ratio": vol / prev_vol,
            "is_up": 1 if close > prev_close else 0,
            "body_pct": abs(close - float(row["open"])) / prev_close * 100,
        }
    return {"code": group[0]["code"], "streak": streak, "daily": daily}


def analyze_pre_daily(groups):
    """D0前逐日分析: D-1, D-2, ... 各自独立统计, 续板vs断板"""
    print(f"\n{'='*70}")
    print(f"  D0前逐日分析 (续板D0 vs 断板D0)")
    print(f"{'='*70}")

    daily_data = defaultdict(lambda: {"cont": [], "stop": []})
    for key, group in groups.items():
        info = extract_pre_daily_features(group)
        if not info:
            continue
        bucket = "cont" if info["streak"] >= 2 else "stop"
        for offset, feat in info["daily"].items():
            daily_data[offset][bucket].append(feat)

    print(f"\n  {'日期':<8} {'续板均涨幅':>10} {'断板均涨幅':>10} {'差异':>8} {'续板上涨%':>10} {'断板上涨%':>10} {'续板量比':>10} {'断板量比':>10}")
    print(f"  {'─'*76}")
    for offset in sorted(daily_data.keys()):
        data = daily_data[offset]
        cont, stop = data["cont"], data["stop"]
        if len(cont) < 10 or len(stop) < 10:
            continue
        c_ret = sum(x["ret"] for x in cont) / len(cont)
        s_ret = sum(x["ret"] for x in stop) / len(stop)
        c_up = sum(x["is_up"] for x in cont) / len(cont) * 100
        s_up = sum(x["is_up"] for x in stop) / len(stop) * 100
        c_vol = sum(x["vol_ratio"] for x in cont) / len(cont)
        s_vol = sum(x["vol_ratio"] for x in stop) / len(stop)
        diff = c_ret - s_ret
        star = "⭐" if abs(diff) > 0.5 else "  "
        print(f"  {star}D-{offset:<5} {c_ret:>+9.2f}% {s_ret:>+9.2f}% {diff:>+7.2f}% {c_up:>9.1f}% {s_up:>9.1f}% {c_vol:>10.2f} {s_vol:>10.2f}")

    # 振幅
    print(f"\n  振幅%对比:")
    print(f"  {'日期':<8} {'续板均振幅':>10} {'断板均振幅':>10} {'差异':>8}")
    print(f"  {'─'*38}")
    for offset in sorted(daily_data.keys()):
        data = daily_data[offset]
        cont, stop = data["cont"], data["stop"]
        if len(cont) < 10 or len(stop) < 10:
            continue
        c_rng = sum(x["range"] for x in cont) / len(cont)
        s_rng = sum(x["range"] for x in stop) / len(stop)
        diff = c_rng - s_rng
        star = "⭐" if abs(diff) > 0.3 else "  "
        print(f"  {star}D-{offset:<5} {c_rng:>9.2f}% {s_rng:>9.2f}% {diff:>+7.2f}%")


def analyze_per_level(groups):
    """逐板分析: 每个板数平台独立, 续板vs断板横向共性"""
    print(f"\n{'='*70}")
    print(f"  逐板横向共性分析")
    print(f"{'='*70}")

    # 按板数分组
    platforms = defaultdict(lambda: {"cont": [], "stop": []})
    for key, group in groups.items():
        feat = extract_features(group)
        if not feat:
            continue
        streak = feat["streak"]
        # 续板 = streak >= 当前板数+1, 即还能继续
        # 但这里我们只有最终streak, 需要按"如果在第N板, 是否会续板"来分析
        # 简化: 对于每个板数level, streak > level = 续板, streak == level = 断板
        for level in range(1, min(streak + 1, 8)):
            continued = streak > level
            bucket = "cont" if continued else "stop"
            entry = dict(feat)
            entry["at_level"] = level
            platforms[level][bucket].append(entry)

    bool_features = [
        ("fl_gap_pct", "高开>=3%", lambda v: v >= 3),
        ("fl_gap_pct", "高开>=5%", lambda v: v >= 5),
        ("fl_gap_pct", "高开>=8%", lambda v: v >= 8),
        ("fl_amplitude_pct", "窄幅(振幅<5%)", lambda v: v < 5),
        ("fl_amplitude_pct", "窄幅(振幅<8%)", lambda v: v < 8),
        ("fl_vol_ratio", "缩量(量比<1x)", lambda v: v < 1.0),
        ("fl_vol_ratio", "缩量(量比<1.5x)", lambda v: v < 1.5),
        ("fl_body_ratio", "小实体(<0.5)", lambda v: v < 0.5),
        ("fl_body_ratio", "小实体(<0.7)", lambda v: v < 0.7),
    ]

    for level in sorted(platforms.keys()):
        if level > 6:
            break
        cont = platforms[level]["cont"]
        stop = platforms[level]["stop"]
        if len(cont) < 10 or len(stop) < 10:
            continue

        total = len(cont) + len(stop)
        cont_rate = len(cont) / total * 100

        print(f"\n  ── 第{level}板 ──  续板={len(cont)}  断板={len(stop)}  续板率={cont_rate:.1f}%")
        print(f"  {'特征':<22} {'续板中占比':>10} {'断板中占比':>10} {'差异':>8}")
        print(f"  {'─'*52}")

        results = []
        for feat_name, label, fn in bool_features:
            c_pct = sum(1 for x in cont if feat_name in x and fn(x[feat_name])) / len(cont) * 100
            s_pct = sum(1 for x in stop if feat_name in x and fn(x[feat_name])) / len(stop) * 100
            diff = c_pct - s_pct
            results.append((label, c_pct, s_pct, diff))

        results.sort(key=lambda x: -abs(x[3]))
        for label, c_pct, s_pct, diff in results:
            signal = "✅" if diff > 5 else "❌" if diff < -5 else "  "
            print(f"  {signal} {label:<20} {c_pct:>9.1f}% {s_pct:>9.1f}% {diff:>+7.1f}%")

        # 数值特征
        num_features = [
            ("fl_return_pct", "涨幅%"),
            ("fl_gap_pct", "高开%"),
            ("fl_amplitude_pct", "振幅%"),
            ("fl_vol_ratio", "量比"),
            ("prev5_return_pct", "前5日涨幅%"),
        ]
        print(f"\n  {'特征':<22} {'续板均值':>10} {'断板均值':>10} {'区分度':>8}")
        print(f"  {'─'*52}")
        from math import sqrt
        for feat_name, label in num_features:
            c_vals = [x[feat_name] for x in cont if feat_name in x and x[feat_name] is not None]
            s_vals = [x[feat_name] for x in stop if feat_name in x and x[feat_name] is not None]
            if len(c_vals) < 5 or len(s_vals) < 5:
                continue
            c_mean = sum(c_vals) / len(c_vals)
            s_mean = sum(s_vals) / len(s_vals)
            c_var = sum((v - c_mean) ** 2 for v in c_vals) / len(c_vals)
            s_var = sum((v - s_mean) ** 2 for v in s_vals) / len(s_vals)
            pooled = sqrt((c_var + s_var) / 2) if (c_var + s_var) > 0 else 1
            d = (c_mean - s_mean) / pooled
            star = "⭐" if abs(d) >= 0.3 else "  " if abs(d) >= 0.15 else "  "
            print(f"  {star}{label:<20} {c_mean:>10.2f} {s_mean:>10.2f} {d:>+8.3f}")


def analyze_day_groups(csv_path):
    """按日期分组，同一天涨停的股票横向比较"""
    print(f"\n{'='*70}")
    print(f"  横向过滤分析：同日涨停股比对")
    print(f"{'='*70}")

    groups = load_groups(csv_path)

    # 按第一板日期分组
    day_groups = defaultdict(list)
    for key, group in groups.items():
        feat = extract_features(group)
        if feat and feat["first_limit_date"]:
            day_groups[feat["first_limit_date"]].append(feat)

    print(f"\n   交易日数: {len(day_groups)}")
    total_stocks = sum(len(v) for v in day_groups.values())
    print(f"   涨停股总数: {total_stocks}")

    # ── 分析：同日涨停股中，3+板的比例 ──
    all_ratios = []
    for date, feats in day_groups.items():
        n_total = len(feats)
        n_high = sum(1 for f in feats if f["streak"] >= 3)
        all_ratios.append(n_high / n_total if n_total > 0 else 0)

    avg_ratio = sum(all_ratios) / len(all_ratios)
    print(f"   3+板占同日涨停股比例: 均值={avg_ratio:.1%}")

    # ── 逐维度过滤，看提纯效果 ──
    print(f"\n{'─'*70}")
    print(f"  逐维度过滤效果（过滤后3+板浓度）")
    print(f"{'─'*70}")

    # 收集所有特征值
    all_feats = []
    for feats in day_groups.values():
        all_feats.extend(feats)

    n_total = len(all_feats)
    n_high_total = sum(1 for f in all_feats if f["streak"] >= 3)
    baseline = n_high_total / n_total
    print(f"\n  基准: {n_total}只涨停股, 3+板={n_high_total}只, 浓度={baseline:.1%}")

    # 对每个特征，尝试不同过滤阈值
    feature_list = [
        ("fl_gap_pct", "第一板高开%"),
        ("fl_return_pct", "第一板涨幅%"),
        ("fl_seal_pct", "封板强度%"),
        ("fl_amplitude_pct", "第一板振幅%"),
        ("fl_vol_ratio", "第一板量比"),
        ("fl_body_ratio", "实体比"),
        ("prev5_return_pct", "前5天涨幅%"),
        ("prev_vol_ratio", "前一天量比"),
    ]

    best_filters = []

    for fname, fdesc in feature_list:
        vals = [f[fname] for f in all_feats if fname in f and f[fname] is not None]
        if not vals:
            continue

        vals_sorted = sorted(vals)
        n = len(vals_sorted)

        # 尝试不同百分位阈值
        for pct in [10, 20, 30, 40, 50, 60, 70, 80, 90]:
            thresh = vals_sorted[int(n * pct / 100)]

            # 方向1: 保留 >= thresh
            filtered_high = [f for f in all_feats
                            if fname in f and f[fname] is not None and f[fname] >= thresh]
            if len(filtered_high) >= 10:
                n_high = sum(1 for f in filtered_high if f["streak"] >= 3)
                conc = n_high / len(filtered_high)
                lift = conc / baseline if baseline > 0 else 0
                if lift > 1.2:
                    best_filters.append({
                        "feature": fname,
                        "desc": fdesc,
                        "dir": ">=",
                        "thresh": thresh,
                        "pct": pct,
                        "n_filtered": len(filtered_high),
                        "n_high": n_high,
                        "concentration": conc,
                        "lift": lift,
                    })

            # 方向2: 保留 <= thresh
            filtered_low = [f for f in all_feats
                           if fname in f and f[fname] is not None and f[fname] <= thresh]
            if len(filtered_low) >= 10:
                n_high = sum(1 for f in filtered_low if f["streak"] >= 3)
                conc = n_high / len(filtered_low)
                lift = conc / baseline if baseline > 0 else 0
                if lift > 1.2:
                    best_filters.append({
                        "feature": fname,
                        "desc": fdesc,
                        "dir": "<=",
                        "thresh": thresh,
                        "pct": pct,
                        "n_filtered": len(filtered_low),
                        "n_high": n_high,
                        "concentration": conc,
                        "lift": lift,
                    })

    # 按提升倍数排序
    best_filters.sort(key=lambda x: -x["lift"])

    print(f"\n  有效过滤规则（提升>1.2x）:")
    print(f"  {'特征':20s}  {'条件':>12s}  {'过滤后':>6s}  {'3+板':>5s}  {'浓度':>6s}  {'提升':>5s}")
    print(f"  {'─'*60}")

    for f in best_filters[:15]:
        print(f"  {f['desc']:20s}  {f['dir']:>2s} {f['thresh']:>8.2f}  "
              f"{f['n_filtered']:>5d}  {f['n_high']:>4d}  {f['concentration']:>5.1%}  {f['lift']:>4.1f}x")

    # ── 组合过滤 ──
    print(f"\n{'─'*70}")
    print(f"  组合过滤（AND）")
    print(f"{'─'*70}")

    # 取前4个最好的单特征
    top_single = []
    seen_features = set()
    for f in best_filters:
        if f["feature"] not in seen_features:
            top_single.append(f)
            seen_features.add(f["feature"])
        if len(top_single) >= 5:
            break

    combo_results = []
    for i in range(len(top_single)):
        for j in range(i + 1, len(top_single)):
            r1 = top_single[i]
            r2 = top_single[j]

            filtered = []
            for f in all_feats:
                v1 = f.get(r1["feature"])
                v2 = f.get(r2["feature"])
                if v1 is None or v2 is None:
                    continue

                pass1 = (v1 >= r1["thresh"]) if r1["dir"] == ">=" else (v1 <= r1["thresh"])
                pass2 = (v2 >= r2["thresh"]) if r2["dir"] == ">=" else (v2 <= r2["thresh"])

                if pass1 and pass2:
                    filtered.append(f)

            if len(filtered) >= 5:
                n_high = sum(1 for f in filtered if f["streak"] >= 3)
                conc = n_high / len(filtered)
                lift = conc / baseline if baseline > 0 else 0
                combo_results.append({
                    "r1": r1, "r2": r2,
                    "n_filtered": len(filtered),
                    "n_high": n_high,
                    "concentration": conc,
                    "lift": lift,
                })

    combo_results.sort(key=lambda x: -x["lift"])

    for c in combo_results[:8]:
        r1, r2 = c["r1"], c["r2"]
        print(f"\n  {r1['desc']} {r1['dir']} {r1['thresh']:.2f}")
        print(f"  AND {r2['desc']} {r2['dir']} {r2['thresh']:.2f}")
        print(f"  → 过滤后{c['n_filtered']}只, 3+板{c['n_high']}只, "
              f"浓度{c['concentration']:.1%}, 提升{c['lift']:.1f}x")

    # ── 三重组合 ──
    print(f"\n{'─'*70}")
    print(f"  三重组合过滤")
    print(f"{'─'*70}")

    triple_results = []
    for i in range(len(top_single)):
        for j in range(i + 1, len(top_single)):
            for k in range(j + 1, len(top_single)):
                r1, r2, r3 = top_single[i], top_single[j], top_single[k]

                filtered = []
                for f in all_feats:
                    v1 = f.get(r1["feature"])
                    v2 = f.get(r2["feature"])
                    v3 = f.get(r3["feature"])
                    if v1 is None or v2 is None or v3 is None:
                        continue

                    p1 = (v1 >= r1["thresh"]) if r1["dir"] == ">=" else (v1 <= r1["thresh"])
                    p2 = (v2 >= r2["thresh"]) if r2["dir"] == ">=" else (v2 <= r2["thresh"])
                    p3 = (v3 >= r3["thresh"]) if r3["dir"] == ">=" else (v3 <= r3["thresh"])

                    if p1 and p2 and p3:
                        filtered.append(f)

                if len(filtered) >= 3:
                    n_high = sum(1 for f in filtered if f["streak"] >= 3)
                    conc = n_high / len(filtered)
                    lift = conc / baseline if baseline > 0 else 0
                    triple_results.append({
                        "rules": [r1, r2, r3],
                        "n_filtered": len(filtered),
                        "n_high": n_high,
                        "concentration": conc,
                        "lift": lift,
                    })

    triple_results.sort(key=lambda x: -x["lift"])

    for c in triple_results[:5]:
        rules = c["rules"]
        desc = " AND ".join(f"{r['desc']}{r['dir']}{r['thresh']:.1f}" for r in rules)
        print(f"\n  {desc}")
        print(f"  → 过滤后{c['n_filtered']}只, 3+板{c['n_high']}只, "
              f"浓度{c['concentration']:.1%}, 提升{c['lift']:.1f}x")

    return best_filters, combo_results, triple_results


def main():
    parser = argparse.ArgumentParser(description="横向过滤分析")
    parser.add_argument("--csv", default="analysis_output/dragon_ohlcv.csv")
    parser.add_argument("--pre-daily", action="store_true", help="D0前逐日分析")
    parser.add_argument("--per-level", action="store_true", help="逐板横向共性分析")
    parser.add_argument("--all", action="store_true", help="运行全部分析")

    args = parser.parse_args()

    # 原有分析
    analyze_day_groups(args.csv)

    # 新增分析
    if args.pre_daily or args.per_level or args.all:
        groups = load_groups(args.csv)
        if args.pre_daily or args.all:
            analyze_pre_daily(groups)
        if args.per_level or args.all:
            analyze_per_level(groups)


if __name__ == "__main__":
    main()
