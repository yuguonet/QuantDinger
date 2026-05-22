#!/usr/bin/env python3
"""
过滤规则优化：降低单板假阳性率

思路：
1. 先用基础规则（涨幅≥20% + 封板≤2.8%）筛出候选
2. 在候选中找能区分单板/连板的附加特征
3. 用时间序列交叉验证防过拟合
4. 输出最优规则组合
"""
from __future__ import annotations
import csv
import os
import argparse
from collections import defaultdict
from itertools import combinations


def load_and_extract(csv_path):
    """加载数据并提取全部特征"""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    groups = defaultdict(list)
    for r in rows:
        groups[(r["code"], r["run_first_limit_date"])].append(r)
    for k in groups:
        groups[k].sort(key=lambda x: x["time"])

    feats = []
    group_map = {}
    for key, g in groups.items():
        streak = int(g[0]["run_n_limit_ups"])
        board = g[0].get("board", "")
        fld = g[0]["run_first_limit_date"]
        fl_pos = None
        for i, r in enumerate(g):
            if r["time"] == fld:
                fl_pos = i
                break
        if fl_pos is None or fl_pos < 2:
            continue

        fl = g[fl_pos]
        prev = g[fl_pos - 1]
        prev2 = g[fl_pos - 2]
        fl_o = float(fl["open"])
        fl_c = float(fl["close"])
        fl_h = float(fl["high"])
        fl_l = float(fl["low"])
        fl_v = float(fl["volume"])
        pc = float(prev["close"])
        pv = float(prev["volume"])
        pc2 = float(prev2["close"])

        vol_w = [float(g[j]["volume"]) for j in range(max(0, fl_pos - 5), fl_pos)]
        avg_v = sum(vol_w) / len(vol_w) if vol_w else fl_v

        # 前5天
        p5r = None
        p5_volatility = None
        p5_avg_body = None
        if fl_pos >= 5:
            p5c = float(g[fl_pos - 5]["close"])
            p5r = (pc / p5c - 1) * 100
            # 前5天波动率
            rets = []
            bodies = []
            for j in range(fl_pos - 4, fl_pos):
                c_j = float(g[j]["close"])
                c_prev = float(g[j - 1]["close"])
                o_j = float(g[j]["open"])
                rets.append((c_j / c_prev - 1))
                bodies.append(abs(c_j - o_j) / c_prev * 100)
            p5_volatility = (sum(r ** 2 for r in rets) / len(rets)) ** 0.5 * 100
            p5_avg_body = sum(bodies) / len(bodies)

        # 前5天量变化
        vol_trend = None
        if len(vol_w) >= 3:
            vol_trend = (vol_w[-1] - vol_w[0]) / vol_w[0] * 100 if vol_w[0] > 0 else 0

        # T-1日特征
        prev_ret = (pc / float(prev["open"]) - 1) * 100
        prev_amp = (float(prev["high"]) - float(prev["low"])) / pc2 * 100
        prev_body = abs(pc - float(prev["open"])) / (float(prev["high"]) - float(prev["low"])) if (float(prev["high"]) - float(prev["low"])) > 0 else 0

        # T-2日特征
        prev2_ret = (pc2 / float(prev2["open"]) - 1) * 100

        # T+1
        b1_open = float(g[fl_pos + 1]["open"]) if fl_pos + 1 < len(g) else None
        b1_gap = (b1_open / fl_c - 1) * 100 if b1_open and fl_c > 0 else None

        f = {
            "code": g[0]["code"],
            "streak": streak,
            "board": board,
            "first_limit_date": fld,
            "year": fld[:4],
            # === T日特征 ===
            "fl_return_pct": (fl_c / pc - 1) * 100,
            "fl_gap_pct": (fl_o / pc - 1) * 100,
            "fl_seal_pct": (fl_c - fl_l) / fl_c * 100 if fl_c > 0 else 0,
            "fl_vol_ratio": fl_v / avg_v if avg_v > 0 else 0,
            "fl_amplitude_pct": (fl_h - fl_l) / pc * 100,
            "fl_body_pct": abs(fl_c - fl_o) / pc * 100,
            "fl_body_ratio": abs(fl_c - fl_o) / (fl_h - fl_l) if (fl_h - fl_l) > 0 else 0,
            "fl_upper_shadow_pct": (fl_h - fl_c) / pc * 100,
            "fl_lower_shadow_pct": (fl_o - fl_l) / pc * 100,
            "fl_is_yizi": int(abs(fl_o / pc - 1 - (0.198 if board in ("创业板", "科创板") else 0.098)) < 0.005),
            # === T-1日特征 ===
            "prev_return_pct": prev_ret,
            "prev_amplitude_pct": prev_amp,
            "prev_body_ratio": prev_body,
            "prev_vol_ratio": pv / avg_v if avg_v > 0 else 0,
            # === T-2日特征 ===
            "prev2_return_pct": prev2_ret,
            # === 前5天 ===
            "prev5_return_pct": p5r,
            "prev5_volatility": p5_volatility,
            "prev5_avg_body_pct": p5_avg_body,
            "vol_trend": vol_trend,
            # === 板块 ===
            "is_cy": int(board == "创业板"),
            "is_kc": int(board == "科创板"),
            "is_20pct": int(board in ("创业板", "科创板")),
        }

        feats.append(f)
        group_map[(f["code"], f["first_limit_date"])] = g

    return feats, group_map


def simulate_trade(group, entry_pos, entry_price, tp=15, trail=8, activate=5, max_hold=20, stop=10):
    n = len(group)
    close = [float(r["close"]) for r in group]
    high = [float(r["high"]) for r in group]
    peak = entry_price
    hold = 0
    for pos in range(entry_pos + 1, n):
        hold += 1
        c, h = close[pos], high[pos]
        peak = max(peak, h)
        ret = (c / entry_price - 1) * 100
        peak_ret = (peak / entry_price - 1) * 100
        if ret <= -stop:
            return ret, hold, "止损"
        if ret >= tp:
            return ret, hold, "止盈"
        if peak_ret >= activate and (c / peak - 1) * 100 <= -trail:
            return ret, hold, "追踪"
        if hold >= max_hold:
            return ret, hold, "超时"
    return (close[-1] / entry_price - 1) * 100, hold, "结束"


def run_backtest(feats, group_map, filters, tp=15, trail=8, activate=5, max_hold=20, stop=10):
    """对通过过滤的股票回测"""
    passed = []
    for f in feats:
        ok = True
        for rule in filters:
            val = f.get(rule["feat"])
            if val is None:
                ok = False
                break
            if rule["dir"] == ">=" and val < rule["thresh"]:
                ok = False
                break
            if rule["dir"] == "<=" and val > rule["thresh"]:
                ok = False
                break
        if ok:
            passed.append(f)

    trades = []
    for feat in passed:
        gkey = (feat["code"], feat["first_limit_date"])
        g = group_map.get(gkey)
        if not g:
            continue
        fld = feat["first_limit_date"]
        fl_pos = None
        for i, r in enumerate(g):
            if r["time"] == fld:
                fl_pos = i
                break
        if fl_pos is None or fl_pos + 1 >= len(g):
            continue
        ep = float(g[fl_pos + 1]["open"])
        if ep <= 0:
            continue
        pnl, hold, reason = simulate_trade(g, fl_pos + 1, ep, tp, trail, activate, max_hold, stop)
        trades.append({"pnl": pnl, "hold": hold, "reason": reason, "streak": feat["streak"]})

    if not trades:
        return None

    pnl = [t["pnl"] for t in trades]
    n = len(pnl)
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    n_single = sum(1 for t in trades if t["streak"] == 1)
    n_multi = sum(1 for t in trades if t["streak"] >= 2)

    return {
        "n": n,
        "n_single": n_single,
        "n_multi": n_multi,
        "win_rate": len(wins) / n * 100,
        "avg_pnl": sum(pnl) / n,
        "avg_win": avg_win,
        "avg_loss": -avg_loss,
        "pf": avg_win / avg_loss if avg_loss > 0 else float("inf"),
        "ev": len(wins) / n * avg_win - len(losses) / n * avg_loss if n > 0 else 0,
        "precision": n_multi / n * 100 if n > 0 else 0,
        "max_loss": min(pnl),
    }


def time_cv(feats, group_map, filters, n_folds=4, tp=15, trail=8, activate=5, max_hold=20, stop=10):
    """按年份做时间序列交叉验证"""
    years = sorted(set(f["year"] for f in feats))
    if len(years) < 2:
        return None

    fold_results = []
    for i in range(1, len(years)):
        train_years = set(years[:i])
        test_year = years[i]

        # 训练集上不做什么，只是确认规则在训练期有意义
        train_feats = [f for f in feats if f["year"] in train_years]
        test_feats = [f for f in feats if f["year"] == test_year]

        test_result = run_backtest(test_feats, group_map, filters, tp, trail, activate, max_hold, stop)
        if test_result:
            test_result["year"] = test_year
            fold_results.append(test_result)

    return fold_results


def optimize(feats, group_map, tp=15, trail=8, activate=5, max_hold=20, stop=10):
    """搜索最优过滤规则组合"""

    # 基础规则（必须）
    base_filters = [
        {"feat": "fl_return_pct", "dir": ">=", "thresh": 20.0},
        {"feat": "fl_seal_pct", "dir": "<=", "thresh": 2.8},
    ]

    # 候选附加规则
    candidates = [
        # 量相关
        {"feat": "fl_vol_ratio", "dir": "<=", "thresh": 1.5, "desc": "量比≤1.5"},
        {"feat": "fl_vol_ratio", "dir": "<=", "thresh": 1.0, "desc": "量比≤1.0"},
        {"feat": "fl_vol_ratio", "dir": "<=", "thresh": 0.8, "desc": "量比≤0.8"},
        {"feat": "vol_trend", "dir": "<=", "thresh": 50.0, "desc": "量趋势≤50%"},
        {"feat": "vol_trend", "dir": "<=", "thresh": 100.0, "desc": "量趋势≤100%"},
        {"feat": "vol_trend", "dir": "<=", "thresh": 200.0, "desc": "量趋势≤200%"},
        # 波动相关
        {"feat": "prev5_volatility", "dir": "<=", "thresh": 5.0, "desc": "前5天波动≤5%"},
        {"feat": "prev5_volatility", "dir": "<=", "thresh": 8.0, "desc": "前5天波动≤8%"},
        {"feat": "prev5_volatility", "dir": "<=", "thresh": 10.0, "desc": "前5天波动≤10%"},
        {"feat": "prev5_volatility", "dir": "<=", "thresh": 12.0, "desc": "前5天波动≤12%"},
        # 振幅相关
        {"feat": "fl_amplitude_pct", "dir": ">=", "thresh": 3.0, "desc": "振幅≥3%"},
        {"feat": "fl_amplitude_pct", "dir": ">=", "thresh": 5.0, "desc": "振幅≥5%"},
        {"feat": "fl_amplitude_pct", "dir": ">=", "thresh": 4.0, "desc": "振幅≥4%"},
        # 上影线
        {"feat": "fl_upper_shadow_pct", "dir": ">=", "thresh": 1.0, "desc": "上影≥1%"},
        {"feat": "fl_upper_shadow_pct", "dir": ">=", "thresh": 2.0, "desc": "上影≥2%"},
        {"feat": "fl_upper_shadow_pct", "dir": ">=", "thresh": 3.0, "desc": "上影≥3%"},
        # 实体
        {"feat": "fl_body_pct", "dir": ">=", "thresh": 1.0, "desc": "实体≥1%"},
        {"feat": "fl_body_pct", "dir": ">=", "thresh": 2.0, "desc": "实体≥2%"},
        # 前5天涨幅
        {"feat": "prev5_return_pct", "dir": ">=", "thresh": -10.0, "desc": "前5天≥-10%"},
        {"feat": "prev5_return_pct", "dir": ">=", "thresh": -5.0, "desc": "前5天≥-5%"},
        {"feat": "prev5_return_pct", "dir": ">=", "thresh": 0.0, "desc": "前5天≥0%"},
        # T-1日
        {"feat": "prev_return_pct", "dir": ">=", "thresh": -3.0, "desc": "T-1≥-3%"},
        {"feat": "prev_return_pct", "dir": ">=", "thresh": 0.0, "desc": "T-1≥0%"},
        # 是否一字板
        {"feat": "fl_is_yizi", "dir": "==", "thresh": 0, "desc": "非一字板"},
        # 涨幅更高
        {"feat": "fl_return_pct", "dir": ">=", "thresh": 25.0, "desc": "涨幅≥25%"},
        {"feat": "fl_return_pct", "dir": ">=", "thresh": 30.0, "desc": "涨幅≥30%"},
    ]

    # 基准
    base_result = run_backtest(feats, group_map, base_filters, tp, trail, activate, max_hold, stop)
    print(f"\n{'='*70}")
    print(f"  过滤规则优化搜索")
    print(f"{'='*70}")
    print(f"\n  基准（涨幅≥20% + 封板≤2.8%）:")
    if base_result:
        print(f"    {base_result['n']}笔 | 单板{base_result['n_single']} 连板{base_result['n_multi']} | "
              f"精确率{base_result['precision']:.1f}% | 胜率{base_result['win_rate']:.1f}% | "
              f"均值{base_result['avg_pnl']:+.2f}% | 期望{base_result['ev']:+.2f}%")

    # ── 单规则搜索 ──
    print(f"\n  ── 单规则附加效果 ──")
    print(f"  {'规则':<25s} {'笔数':>4s} {'单板':>4s} {'连板':>4s} {'精确':>6s} {'胜率':>6s} {'均值':>8s} {'期望':>8s} {'PF':>5s}")
    print(f"  {'─'*75}")

    single_results = []
    for cand in candidates:
        filters = base_filters + [cand]
        r = run_backtest(feats, group_map, filters, tp, trail, activate, max_hold, stop)
        if r and r["n"] >= 10:
            single_results.append((cand, r))
            print(f"  + {cand['desc']:<23s} {r['n']:>4d} {r['n_single']:>4d} {r['n_multi']:>4d} "
                  f"{r['precision']:>5.1f}% {r['win_rate']:>5.1f}% {r['avg_pnl']:>+7.2f}% {r['ev']:>+7.2f}% {r['pf']:>5.2f}")

    # 按 EV 排序
    single_results.sort(key=lambda x: -x[1]["ev"])
    print(f"\n  🏆 Top 5 单规则:")
    for cand, r in single_results[:5]:
        print(f"    +{cand['desc']}: {r['n']}笔 精确{r['precision']:.1f}% 胜率{r['win_rate']:.1f}% 均值{r['avg_pnl']:+.2f}% EV{r['ev']:+.2f}%")

    # ── 双规则搜索 ──
    print(f"\n  ── 双规则组合搜索 ──")
    double_results = []
    # 只搜有提升的候选
    top_candidates = [c for c, r in single_results[:10]]
    for i in range(len(top_candidates)):
        for j in range(i + 1, len(top_candidates)):
            c1, c2 = top_candidates[i], top_candidates[j]
            # 跳过同特征不同阈值
            if c1["feat"] == c2["feat"]:
                continue
            filters = base_filters + [c1, c2]
            r = run_backtest(feats, group_map, filters, tp, trail, activate, max_hold, stop)
            if r and r["n"] >= 8:
                double_results.append(([c1, c2], r))

    double_results.sort(key=lambda x: -x[1]["ev"])
    print(f"\n  🏆 Top 10 双规则:")
    print(f"  {'规则组合':<45s} {'笔数':>4s} {'单板':>4s} {'精确':>6s} {'胜率':>6s} {'均值':>8s} {'EV':>8s}")
    print(f"  {'─'*80}")
    for combo, r in double_results[:10]:
        desc = f"{combo[0]['desc']} + {combo[1]['desc']}"
        print(f"  {desc:<45s} {r['n']:>4d} {r['n_single']:>4d} {r['precision']:>5.1f}% "
              f"{r['win_rate']:>5.1f}% {r['avg_pnl']:>+7.2f}% {r['ev']:>+7.2f}%")

    # ── 三规则搜索 ──
    print(f"\n  ── 三规则组合搜索 ──")
    triple_results = []
    top_double_combos = [c for c, r in double_results[:15]]
    for combo in top_double_combos:
        for cand in top_candidates:
            if cand["feat"] in [c["feat"] for c in combo]:
                continue
            filters = base_filters + combo + [cand]
            r = run_backtest(feats, group_map, filters, tp, trail, activate, max_hold, stop)
            if r and r["n"] >= 5:
                triple_results.append((combo + [cand], r))

    triple_results.sort(key=lambda x: -x[1]["ev"])
    print(f"\n  🏆 Top 10 三规则:")
    print(f"  {'规则组合':<55s} {'笔数':>4s} {'单板':>4s} {'精确':>6s} {'胜率':>6s} {'均值':>8s} {'EV':>8s}")
    print(f"  {'─'*90}")
    for combo, r in triple_results[:10]:
        desc = " + ".join(c["desc"] for c in combo)
        print(f"  {desc:<55s} {r['n']:>4d} {r['n_single']:>4d} {r['precision']:>5.1f}% "
              f"{r['win_rate']:>5.1f}% {r['avg_pnl']:>+7.2f}% {r['ev']:>+7.2f}%")

    # ── 最优规则时间序列验证 ──
    print(f"\n{'='*70}")
    print(f"  📊 最优规则时间序列交叉验证")
    print(f"{'='*70}")

    # 取 top 3 规则做 CV
    all_candidates = single_results[:5] + [(combo, r) for combo, r in double_results[:5]] + [(combo, r) for combo, r in triple_results[:5]]

    seen_descs = set()
    top_for_cv = []
    for item, r in all_candidates:
        if isinstance(item, list):
            desc = " + ".join(c["desc"] for c in item)
            filters = base_filters + item
        else:
            desc = item["desc"]
            filters = base_filters + [item]
        if desc not in seen_descs:
            seen_descs.add(desc)
            top_for_cv.append((desc, filters, r))

    for desc, filters, full_r in top_for_cv[:8]:
        cv_results = time_cv(feats, group_map, filters, tp=tp, trail=trail, activate=activate, max_hold=max_hold, stop=stop)
        if not cv_results:
            continue
        print(f"\n  规则: {desc}")
        print(f"  {'年份':>6s} {'笔数':>4s} {'单板':>4s} {'精确':>6s} {'胜率':>6s} {'均值':>8s} {'EV':>8s}")
        for cr in cv_results:
            print(f"  {cr['year']:>6s} {cr['n']:>4d} {cr['n_single']:>4d} {cr['precision']:>5.1f}% "
                  f"{cr['win_rate']:>5.1f}% {cr['avg_pnl']:>+7.2f}% {cr['ev']:>+7.2f}%")
        # 汇总
        all_n = sum(cr["n"] for cr in cv_results)
        all_single = sum(cr["n_single"] for cr in cv_results)
        all_pnl = []
        for cr in cv_results:
            # 需要重新算
            pass
        avg_precision = sum(cr["precision"] * cr["n"] for cr in cv_results) / all_n if all_n > 0 else 0
        avg_ev = sum(cr["ev"] * cr["n"] for cr in cv_results) / all_n if all_n > 0 else 0
        print(f"  {'汇总':>6s} {all_n:>4d} {all_single:>4d} {avg_precision:>5.1f}% {'─':>6s} {'─':>8s} {avg_ev:>+7.2f}%")

    # ── 输出最终推荐 ──
    print(f"\n{'='*70}")
    print(f"  🎯 最终推荐规则")
    print(f"{'='*70}")

    # 综合考虑：精确率>80%、笔数>=10、EV最高
    best = None
    for desc, filters, r in top_for_cv:
        if r["precision"] >= 80 and r["n"] >= 10 and r["ev"] > 0:
            if best is None or r["ev"] > best[2]["ev"]:
                best = (desc, filters, r)

    if best:
        desc, filters, r = best
        print(f"\n  推荐: {desc}")
        print(f"  规则详情:")
        for f in filters:
            print(f"    {f['feat']} {f['dir']} {f['thresh']}")
        print(f"\n  全样本表现:")
        print(f"    交易数: {r['n']} (单板{r['n_single']} 连板{r['n_multi']})")
        print(f"    精确率: {r['precision']:.1f}%")
        print(f"    胜率: {r['win_rate']:.1f}%")
        print(f"    平均收益: {r['avg_pnl']:+.2f}%")
        print(f"    期望值: {r['ev']:+.2f}%/笔")
        print(f"    盈亏比: {r['pf']:.2f}")
        print(f"    最大亏损: {r['max_loss']:+.2f}%")
    else:
        print(f"\n  ⚠️ 未找到精确率>80%且笔数>=10的规则")
        # 退而求其次
        for desc, filters, r in top_for_cv:
            if r["precision"] >= 75 and r["n"] >= 8:
                print(f"\n  备选: {desc}")
                print(f"    {r['n']}笔 精确{r['precision']:.1f}% 胜率{r['win_rate']:.1f}% 均值{r['avg_pnl']:+.2f}% EV{r['ev']:+.2f}%")
                break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="analysis_output/dragon_ohlcv.csv")
    parser.add_argument("--tp", type=float, default=15)
    parser.add_argument("--trail", type=float, default=8)
    parser.add_argument("--activate", type=float, default=5)
    parser.add_argument("--max-hold", type=int, default=20)
    parser.add_argument("--stop", type=float, default=10)
    args = parser.parse_args()

    feats, group_map = load_and_extract(args.csv)
    print(f"加载 {len(feats)} 组数据")

    optimize(feats, group_map, args.tp, args.trail, args.activate, args.max_hold, args.stop)


if __name__ == "__main__":
    main()
