#!/usr/bin/env python3
"""
全市场验证：过滤规则在连板 vs 单板上的假阳性率

用 --min-streak=1 导出的全市场数据，验证：
1. 过滤规则对单板股（噪声）的通过率
2. 过滤规则对连板股（信号）的通过率
3. 真实胜率、盈亏比、期望值

用法:
    python validate_full_market.py --csv analysis_output/dragon_ohlcv.csv
    python validate_full_market.py --csv analysis_output/dragon_ohlcv.csv --detail
"""
from __future__ import annotations
import csv
import os
import argparse
from collections import defaultdict
from datetime import datetime


def load_groups(csv_path):
    """按 (code, first_limit_date) 分组"""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    groups = defaultdict(list)
    for r in rows:
        groups[(r["code"], r["run_first_limit_date"])].append(r)
    for k in groups:
        groups[k].sort(key=lambda x: x["time"])
    return groups


def classify_streak(groups):
    """将每组按实际连板数分类"""
    result = {}
    for key, group in groups.items():
        streak = int(group[0]["run_n_limit_ups"])
        result[key] = streak
    return result


def extract_features(group):
    """提取第一板当天特征（T日收盘前可观测）"""
    n = len(group)
    board = group[0].get("board", "")
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

    # 前5天均量
    vol_window = [float(group[j]["volume"]) for j in range(max(0, fl_pos - 5), fl_pos)]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else fl_vol

    features = {
        "code": group[0]["code"],
        "streak": streak,
        "board": board,
        "first_limit_date": fld,
        "fl_pos": fl_pos,
        # T日特征
        "fl_gap_pct": (fl_open / prev_close - 1) * 100,
        "fl_return_pct": (fl_close / prev_close - 1) * 100,
        "fl_seal_pct": (fl_close - fl_low) / fl_close * 100 if fl_close > 0 else 0,
        "fl_vol_ratio": fl_vol / avg_vol if avg_vol > 0 else 0,
        "fl_amplitude_pct": (fl_high - fl_low) / prev_close * 100,
        "fl_body_ratio": abs(fl_close - fl_open) / (fl_high - fl_low) if (fl_high - fl_low) > 0 else 0,
    }

    # 前5天涨幅
    if fl_pos >= 5:
        prev5_close = float(group[fl_pos - 5]["close"])
        features["prev5_return_pct"] = (prev_close / prev5_close - 1) * 100
    else:
        features["prev5_return_pct"] = None

    # T+1 开盘价（买入价）
    b1_pos = fl_pos + 1
    if b1_pos < n:
        features["b1_open"] = float(group[b1_pos]["open"])
        features["b1_pos"] = b1_pos
    else:
        features["b1_open"] = None
        features["b1_pos"] = None

    return features


def simulate_trade(group, entry_pos, entry_price, tp_pct=15, trail_pct=8,
                   trail_activate_pct=5, max_hold=20, stop_loss=10):
    """逐日推进出场，不看未来数据"""
    n = len(group)
    close = [float(r["close"]) for r in group]
    high = [float(r["high"]) for r in group]
    dates = [r["time"] for r in group]

    peak = entry_price
    hold = 0

    for pos in range(entry_pos + 1, n):
        hold += 1
        c = close[pos]
        h = high[pos]
        peak = max(peak, h)

        ret = (c / entry_price - 1) * 100
        peak_ret = (peak / entry_price - 1) * 100

        # 止损
        if ret <= -stop_loss:
            return {"exit_date": dates[pos], "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": f"止损{stop_loss}%"}
        # 止盈
        if ret >= tp_pct:
            return {"exit_date": dates[pos], "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": f"止盈{tp_pct}%"}
        # 追踪止损
        if peak_ret >= trail_activate_pct:
            dd = (c / peak - 1) * 100
            if dd <= -trail_pct:
                return {"exit_date": dates[pos], "pnl_pct": round(ret, 2),
                        "hold_days": hold, "exit_reason": f"追踪止损"}

        # 最大持仓
        if hold >= max_hold:
            return {"exit_date": dates[pos], "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": f"持仓{max_hold}天"}

    return {"exit_date": dates[-1], "pnl_pct": round((close[-1] / entry_price - 1) * 100, 2),
            "hold_days": hold, "exit_reason": "数据结束"}


def apply_filter(features_list, filters):
    """应用过滤规则，返回通过的特征列表"""
    passed = []
    for feat in features_list:
        ok = True
        for f in filters:
            val = feat.get(f["feature"])
            if val is None:
                ok = False
                break
            if f["dir"] == ">=" and val < f["thresh"]:
                ok = False
                break
            if f["dir"] == "<=" and val > f["thresh"]:
                ok = False
                break
        if ok:
            passed.append(feat)
    return passed


def calc_metrics(trades, label=""):
    """计算交易指标"""
    if not trades:
        return None
    pnl = [t["pnl_pct"] for t in trades]
    n = len(pnl)
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    avg = sum(pnl) / n
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pf = avg_win / avg_loss if avg_loss > 0 else float("inf")
    wr = len(wins) / n * 100

    return {
        "label": label,
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": wr,
        "avg_pnl": avg,
        "avg_win": avg_win,
        "avg_loss": -avg_loss,
        "profit_factor": pf,
        "max_win": max(pnl),
        "max_loss": min(pnl),
        "median": sorted(pnl)[n // 2],
    }


def print_metrics(m, indent=2):
    """打印指标"""
    sp = " " * indent
    print(f"{sp}交易数: {m['n']}  盈利: {m['wins']}  亏损: {m['losses']}")
    print(f"{sp}胜率: {m['win_rate']:.1f}%")
    print(f"{sp}平均收益: {m['avg_pnl']:+.2f}%  中位数: {m['median']:+.2f}%")
    print(f"{sp}盈利均值: {m['avg_win']:+.2f}%  亏损均值: {m['avg_loss']:+.2f}%")
    print(f"{sp}盈亏比: {m['profit_factor']:.2f}")
    print(f"{sp}最大盈利: {m['max_win']:+.2f}%  最大亏损: {m['max_loss']:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description="全市场验证：过滤规则假阳性率")
    parser.add_argument("--csv", default="analysis_output/dragon_ohlcv.csv")
    parser.add_argument("--tp", type=float, default=15, help="止盈%")
    parser.add_argument("--trail", type=float, default=8, help="追踪止损%")
    parser.add_argument("--activate", type=float, default=5, help="追踪激活%")
    parser.add_argument("--max-hold", type=int, default=20, help="最大持仓天数")
    parser.add_argument("--stop-loss", type=float, default=10, help="止损%")
    parser.add_argument("--detail", action="store_true", help="输出详细分类")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  全市场验证：过滤规则假阳性率分析")
    print(f"  数据: {args.csv}")
    print(f"{'='*70}")

    # ── 加载数据 ──
    groups = load_groups(args.csv)
    print(f"\n  总组数: {len(groups)}")

    # 分类
    streak_count = defaultdict(int)
    for key, group in groups.items():
        streak = int(group[0]["run_n_limit_ups"])
        streak_count[streak] += 1

    print(f"\n  连板分布:")
    for s in sorted(streak_count.keys()):
        print(f"    {s}板: {streak_count[s]} 组")
    n_single = streak_count.get(1, 0)
    n_multi = sum(v for k, v in streak_count.items() if k >= 2)
    n_triple = sum(v for k, v in streak_count.items() if k >= 3)
    print(f"\n  单板: {n_single}  连板(2+): {n_multi}  强连板(3+): {n_triple}")

    # ── 提取特征 ──
    all_features = []
    group_map = {}
    for key, group in groups.items():
        feat = extract_features(group)
        if feat:
            all_features.append(feat)
            group_map[(feat["code"], feat["first_limit_date"])] = group

    print(f"  有效特征: {len(all_features)}")

    # ── 基准浓度 ──
    n_total = len(all_features)
    n_2plus = sum(1 for f in all_features if f["streak"] >= 2)
    n_3plus = sum(1 for f in all_features if f["streak"] >= 3)
    baseline_2 = n_2plus / n_total if n_total > 0 else 0
    baseline_3 = n_3plus / n_total if n_total > 0 else 0
    print(f"\n  基准浓度: 2+板={baseline_2:.1%}  3+板={baseline_3:.1%}")

    # ── 定义过滤规则组合 ──
    filter_sets = [
        {
            "name": "无过滤（基准）",
            "filters": [],
        },
        {
            "name": "第一板涨幅≥20%",
            "filters": [
                {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0},
            ],
        },
        {
            "name": "封板强度≤2.8%",
            "filters": [
                {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8},
            ],
        },
        {
            "name": "组合：涨幅≥20% + 封板≤2.8%",
            "filters": [
                {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0},
                {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8},
            ],
        },
        {
            "name": "严格：涨幅≥20% + 封板≤2.8% + 量比≥2",
            "filters": [
                {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0},
                {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8},
                {"feature": "fl_vol_ratio", "dir": ">=", "thresh": 2.0},
            ],
        },
        {
            "name": "涨幅≥15% + 封板≤3%",
            "filters": [
                {"feature": "fl_return_pct", "dir": ">=", "thresh": 15.0},
                {"feature": "fl_seal_pct", "dir": "<=", "thresh": 3.0},
            ],
        },
        {
            "name": "涨幅≥25%（创业板/科创板20%板）",
            "filters": [
                {"feature": "fl_return_pct", "dir": ">=", "thresh": 25.0},
            ],
        },
    ]

    # ── 对每组过滤规则，分析假阳性 ──
    print(f"\n{'='*70}")
    print(f"  过滤规则对比")
    print(f"{'='*70}")

    results_summary = []

    for fs in filter_sets:
        name = fs["name"]
        filters = fs["filters"]

        passed = apply_filter(all_features, filters) if filters else all_features

        # 分类通过的股票
        passed_single = [f for f in passed if f["streak"] == 1]
        passed_2plus = [f for f in passed if f["streak"] >= 2]
        passed_3plus = [f for f in passed if f["streak"] >= 3]

        n_passed = len(passed)
        n_fp = len(passed_single)  # 假阳性：单板通过了过滤
        n_tp_2 = len(passed_2plus)  # 真阳性：2+板通过
        n_tp_3 = len(passed_3plus)  # 真阳性：3+板通过

        precision_2 = n_tp_2 / n_passed if n_passed > 0 else 0
        precision_3 = n_tp_3 / n_passed if n_passed > 0 else 0
        recall_2 = n_tp_2 / n_2plus if n_2plus > 0 else 0
        recall_3 = n_tp_3 / n_3plus if n_3plus > 0 else 0
        fp_rate = n_fp / n_single if n_single > 0 else 0  # 单板中多少被误放

        print(f"\n  ┌─ {name}")
        print(f"  │  通过: {n_passed} / {n_total} ({n_passed/n_total*100:.1f}%)")
        print(f"  │  假阳性(单板通过): {n_fp}  假阳性率: {fp_rate:.1%}")
        print(f"  │  真阳性(2+板通过): {n_tp_2}  精确率: {precision_2:.1%}  召回率: {recall_2:.1%}")
        print(f"  │  真阳性(3+板通过): {n_tp_3}  精确率: {precision_3:.1%}  召回率: {recall_3:.1%}")
        print(f"  │  浓度提升: 2+板 {precision_2/baseline_2:.1f}x  3+板 {precision_3/baseline_3:.1f}x" if baseline_2 > 0 and baseline_3 > 0 else "")

        # 回测：对通过过滤的股票模拟交易
        trades_2plus = []
        trades_single = []
        trades_all = []

        for feat in passed:
            gkey = (feat["code"], feat["first_limit_date"])
            group = group_map.get(gkey)
            if not group or feat["b1_pos"] is None:
                continue

            entry_price = feat["b1_open"]
            if entry_price <= 0:
                continue

            trade = simulate_trade(
                group, feat["b1_pos"], entry_price,
                args.tp, args.trail, args.activate, args.max_hold, args.stop_loss
            )
            trade["code"] = feat["code"]
            trade["streak"] = feat["streak"]
            trade["board"] = feat["board"]
            trade["first_limit_date"] = feat["first_limit_date"]

            trades_all.append(trade)
            if feat["streak"] >= 2:
                trades_2plus.append(trade)
            else:
                trades_single.append(trade)

        if trades_all:
            m_all = calc_metrics(trades_all, "全部")
            m_2p = calc_metrics(trades_2plus, "2+板") if trades_2plus else None
            m_1 = calc_metrics(trades_single, "单板") if trades_single else None

            print(f"  │")
            print(f"  │  📊 回测结果（止盈{args.tp}%/追踪{args.trail}%/{args.activate}%/止损{args.stop_loss}%）")
            print(f"  │  {'─'*50}")

            if m_1:
                print(f"  │  单板({m_1['n']}笔): 胜率={m_1['win_rate']:.1f}%  "
                      f"均值={m_1['avg_pnl']:+.2f}%  盈亏比={m_1['profit_factor']:.2f}")
            if m_2p:
                print(f"  │  2+板({m_2p['n']}笔): 胜率={m_2p['win_rate']:.1f}%  "
                      f"均值={m_2p['avg_pnl']:+.2f}%  盈亏比={m_2p['profit_factor']:.2f}")
            print(f"  │  合计({m_all['n']}笔): 胜率={m_all['win_rate']:.1f}%  "
                  f"均值={m_all['avg_pnl']:+.2f}%  盈亏比={m_all['profit_factor']:.2f}")

            # 期望值 = 胜率×平均盈利 + (1-胜率)×平均亏损
            ev = m_all["win_rate"]/100 * m_all["avg_win"] + (1 - m_all["win_rate"]/100) * m_all["avg_loss"]
            print(f"  │  期望值: {ev:+.2f}%/笔")

            results_summary.append({
                "name": name,
                "n_passed": n_passed,
                "n_fp": n_fp,
                "fp_rate": fp_rate,
                "precision_2": precision_2,
                "recall_2": recall_2,
                "precision_3": precision_3,
                "lift_2": precision_2 / baseline_2 if baseline_2 > 0 else 0,
                "lift_3": precision_3 / baseline_3 if baseline_3 > 0 else 0,
                "m_all": m_all,
                "m_2p": m_2p,
                "m_1": m_1,
                "ev": ev,
            })

        print(f"  └{'─'*60}")

    # ── 汇总对比 ──
    print(f"\n{'='*70}")
    print(f"  📊 汇总对比")
    print(f"{'='*70}")
    print(f"\n  {'规则':<35s} {'通过':>5s} {'FP':>5s} {'FP率':>6s} {'精确2+':>7s} {'提升':>5s} {'胜率':>6s} {'均值':>8s} {'期望':>8s}")
    print(f"  {'─'*90}")
    for r in results_summary:
        m = r["m_all"]
        print(f"  {r['name']:<35s} {r['n_passed']:>5d} {r['n_fp']:>5d} {r['fp_rate']:>5.1%} "
              f"{r['precision_2']:>6.1%} {r['lift_2']:>4.1f}x {m['win_rate']:>5.1f}% "
              f"{m['avg_pnl']:>+7.2f}% {r['ev']:>+7.2f}%")

    # ── 按板块分析最优规则 ──
    print(f"\n{'='*70}")
    print(f"  📊 最优规则按板块分类")
    print(f"{'='*70}")

    best_filters = [
        {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0},
        {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8},
    ]
    passed = apply_filter(all_features, best_filters)

    board_trades = defaultdict(list)
    for feat in passed:
        gkey = (feat["code"], feat["first_limit_date"])
        group = group_map.get(gkey)
        if not group or feat["b1_pos"] is None:
            continue
        entry_price = feat["b1_open"]
        if entry_price <= 0:
            continue
        trade = simulate_trade(
            group, feat["b1_pos"], entry_price,
            args.tp, args.trail, args.activate, args.max_hold, args.stop_loss
        )
        trade["streak"] = feat["streak"]
        board_trades[feat["board"]].append(trade)

    for board in sorted(board_trades.keys()):
        trades = board_trades[board]
        m = calc_metrics(trades, board)
        if m:
            print(f"\n  {board} ({m['n']}笔):")
            print_metrics(m, indent=4)

    # ── 按连板数分析 ──
    print(f"\n{'='*70}")
    print(f"  📊 最优规则按实际连板数分类")
    print(f"{'='*70}")

    streak_trades = defaultdict(list)
    for feat in passed:
        gkey = (feat["code"], feat["first_limit_date"])
        group = group_map.get(gkey)
        if not group or feat["b1_pos"] is None:
            continue
        entry_price = feat["b1_open"]
        if entry_price <= 0:
            continue
        trade = simulate_trade(
            group, feat["b1_pos"], entry_price,
            args.tp, args.trail, args.activate, args.max_hold, args.stop_loss
        )
        streak_trades[feat["streak"]].append(trade)

    for s in sorted(streak_trades.keys()):
        trades = streak_trades[s]
        m = calc_metrics(trades, f"{s}板")
        if m:
            print(f"\n  {s}板 ({m['n']}笔):")
            print_metrics(m, indent=4)

    # ── 详细明细 ──
    if args.detail:
        print(f"\n{'='*70}")
        print(f"  📋 交易明细（最优规则）")
        print(f"{'='*70}")

        detail_trades = []
        for feat in passed:
            gkey = (feat["code"], feat["first_limit_date"])
            group = group_map.get(gkey)
            if not group or feat["b1_pos"] is None:
                continue
            entry_price = feat["b1_open"]
            if entry_price <= 0:
                continue
            trade = simulate_trade(
                group, feat["b1_pos"], entry_price,
                args.tp, args.trail, args.activate, args.max_hold, args.stop_loss
            )
            trade["code"] = feat["code"]
            trade["streak"] = feat["streak"]
            trade["board"] = feat["board"]
            trade["first_limit_date"] = feat["first_limit_date"]
            trade["fl_return_pct"] = feat["fl_return_pct"]
            trade["fl_seal_pct"] = feat["fl_seal_pct"]
            detail_trades.append(trade)

        # 按收益排序
        detail_trades.sort(key=lambda x: x["pnl_pct"])

        out_path = os.path.join(os.path.dirname(args.csv) or ".", "dragon_validation_detail.csv")
        fields = ["code", "board", "streak", "first_limit_date",
                  "fl_return_pct", "fl_seal_pct",
                  "pnl_pct", "hold_days", "exit_reason"]
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for t in detail_trades:
                w.writerow({k: t.get(k, "") for k in fields})
        print(f"\n  💾 明细已保存: {out_path}")

    # ── 核心结论 ──
    print(f"\n{'='*70}")
    print(f"  💡 核心结论")
    print(f"{'='*70}")

    if results_summary:
        # 找到期望值最高的有实际意义的规则
        meaningful = [r for r in results_summary if r["n_passed"] >= 20]
        if meaningful:
            best = max(meaningful, key=lambda x: x["ev"])
            print(f"\n  最优规则: {best['name']}")
            print(f"  - 假阳性率: {best['fp_rate']:.1%}")
            print(f"  - 精确率(2+板): {best['precision_2']:.1%}")
            print(f"  - 浓度提升: {best['lift_2']:.1f}x")
            print(f"  - 胜率: {best['m_all']['win_rate']:.1f}%")
            print(f"  - 期望值: {best['ev']:+.2f}%/笔")

    print()


if __name__ == "__main__":
    main()
