#!/usr/bin/env python3
"""
连板策略回测 — 横向过滤 + 机械出场

流程：
    1. 每天扫描所有涨停股
    2. 用过滤规则筛选候选
    3. 次日开盘买入
    4. 机械规则出场
    5. 统计实际盈亏
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


def get_day_limit_ups(csv_path):
    """按日期分组：每天有哪些股票涨停"""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # 按 (code, first_limit_date) 分组，只取第一板当天的行
    day_map = defaultdict(list)
    seen = set()
    for r in rows:
        key = (r["code"], r["run_first_limit_date"])
        if key in seen:
            continue
        seen.add(key)
        fld = r["run_first_limit_date"]
        if fld:
            day_map[fld].append(r)

    return day_map


def extract_first_limit_features(group):
    """提取第一板当天+次日特征"""
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
        return None, None

    fl = group[fl_pos]
    prev = group[fl_pos - 1]
    fl_open = float(fl["open"])
    fl_close = float(fl["close"])
    fl_high = float(fl["high"])
    fl_low = float(fl["low"])
    fl_vol = float(fl["volume"])
    prev_close = float(prev["close"])
    prev_vol = float(prev["volume"])

    vol_window = [float(group[j]["volume"]) for j in range(max(0, fl_pos - 5), fl_pos)]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else fl_vol

    features = {
        "code": group[0]["code"],
        "streak": streak,
        "board": board,
        "first_limit_date": fld,
        "fl_pos": fl_pos,
        "fl_gap_pct": (fl_open / prev_close - 1) * 100,
        "fl_return_pct": (fl_close / prev_close - 1) * 100,
        "fl_seal_pct": (fl_close - fl_low) / fl_close * 100 if fl_close > 0 else 0,
        "fl_vol_ratio": fl_vol / avg_vol if avg_vol > 0 else 0,
        "fl_amplitude_pct": (fl_high - fl_low) / prev_close * 100,
        "fl_body_ratio": abs(fl_close - fl_open) / (fl_high - fl_low) if (fl_high - fl_low) > 0 else 0,
        "fl_is_yizi": int(abs(fl_open / prev_close - 1 - threshold) < 0.005),
    }

    # 前5天涨幅
    if fl_pos >= 5:
        prev5_close = float(group[fl_pos - 5]["close"])
        features["prev5_return_pct"] = (prev_close / prev5_close - 1) * 100
    else:
        features["prev5_return_pct"] = None

    # 前一天量比
    if fl_pos >= 2:
        prev_vol_window = [float(group[j]["volume"]) for j in range(max(0, fl_pos - 6), fl_pos - 1)]
        prev_avg_vol = sum(prev_vol_window) / len(prev_vol_window) if prev_vol_window else prev_vol
        features["prev_vol_ratio"] = prev_vol / prev_avg_vol if prev_avg_vol > 0 else 0

    # T+1 — 买入日开盘价（此时只看到T日数据）
    b1_pos = fl_pos + 1
    if b1_pos < n:
        features["b1_pos"] = b1_pos

    return features, group


def simulate_trade(group, entry_pos, entry_price, tp_pct, trail_pct,
                   trail_activate_pct, max_hold_days, stop_loss_pct):
    """逐日推进出场"""
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

        if ret <= -stop_loss_pct:
            return {"exit_date": dates[pos], "exit_price": c, "hold_days": hold,
                    "pnl_pct": round(ret, 2), "exit_reason": f"止损{stop_loss_pct}%"}

        if ret >= tp_pct:
            return {"exit_date": dates[pos], "exit_price": c, "hold_days": hold,
                    "pnl_pct": round(ret, 2), "exit_reason": f"止盈{tp_pct}%"}

        if peak_ret >= trail_activate_pct:
            dd = (c / peak - 1) * 100
            if dd <= -trail_pct:
                return {"exit_date": dates[pos], "exit_price": c, "hold_days": hold,
                        "pnl_pct": round(ret, 2), "exit_reason": f"追踪(峰{peak_ret:.1f}%→{dd:.1f}%)"}

        if hold >= max_hold_days:
            return {"exit_date": dates[pos], "exit_price": c, "hold_days": hold,
                    "pnl_pct": round(ret, 2), "exit_reason": f"持仓{max_hold_days}天"}

    return {"exit_date": dates[-1], "exit_price": close[-1], "hold_days": hold,
            "pnl_pct": round((close[-1] / entry_price - 1) * 100, 2), "exit_reason": "数据结束"}


def backtest(csv_path, filters, tp_pct=15, trail_pct=8, trail_activate=5,
             max_hold=20, stop_loss=10):
    """回测：横向过滤 + 机械出场"""
    print(f"\n{'='*70}")
    print(f"  连板策略回测（横向过滤）")
    print(f"{'='*70}")

    groups = load_groups(csv_path)
    print(f"   总组数: {len(groups)}")

    # 提取所有组的特征
    all_feats = []
    group_map = {}
    for key, g in groups.items():
        feat, grp = extract_first_limit_features(g)
        if feat:
            all_feats.append(feat)
            group_map[feat["code"]] = g

    # 按日期分组
    day_feats = defaultdict(list)
    for f in all_feats:
        day_feats[f["first_limit_date"]].append(f)

    print(f"   交易日数: {len(day_feats)}")

    # 应用过滤
    print(f"\n  过滤条件:")
    for f in filters:
        print(f"    {f['desc']} {f['dir']} {f['thresh']:.2f}")

    all_trades = []
    filtered_count = 0
    total_limit_up = 0

    for date in sorted(day_feats.keys()):
        candidates = day_feats[date]
        total_limit_up += len(candidates)

        # 应用过滤
        passed = []
        for c in candidates:
            ok = True
            for f in filters:
                val = c.get(f["feature"])
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
                passed.append(c)

        filtered_count += len(passed)

        # 对通过过滤的股票模拟交易
        for c in passed:
            # T日收盘确认信号 → T+1开盘买入
            if "b1_pos" not in c:
                continue

            g = group_map[c["code"]]
            b1_pos = c["b1_pos"]
            entry_price = float(g[b1_pos]["open"])

            if entry_price <= 0:
                continue

            trade = simulate_trade(
                g, b1_pos, entry_price,
                tp_pct, trail_pct, trail_activate, max_hold, stop_loss
            )
            trade["code"] = c["code"]
            trade["streak"] = c["streak"]
            trade["board"] = c["board"]
            trade["first_limit_date"] = c["first_limit_date"]
            trade["b1_amplitude_pct"] = c.get("b1_amplitude_pct", 0)
            trade["b1_vol_ratio"] = c.get("b1_vol_ratio", 0)
            all_trades.append(trade)

    print(f"\n   涨停股总数: {total_limit_up}")
    print(f"   通过过滤: {filtered_count} ({filtered_count/total_limit_up*100:.1f}%)")
    print(f"   实际交易: {len(all_trades)}")

    if not all_trades:
        print("\n❌ 无交易")
        return

    # ── 统计 ──
    summarize(all_trades)

    # ── 保存 ──
    out_path = os.path.join(os.path.dirname(csv_path) or ".", "dragon_backtest_filtered.csv")
    fields = ["code", "board", "streak", "first_limit_date",
              "entry_price", "exit_date", "exit_price", "hold_days",
              "pnl_pct", "exit_reason", "b1_amplitude_pct", "b1_vol_ratio"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in all_trades:
            w.writerow({k: t.get(k, "") for k in fields})
    print(f"\n💾 明细已保存: {out_path}")


def summarize(trades):
    pnl = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    n = len(pnl)
    avg = sum(pnl) / n
    pnl_sorted = sorted(pnl)
    median = pnl_sorted[n // 2]

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pf = avg_win / avg_loss if avg_loss > 0 else float("inf")

    print(f"\n{'─'*60}")
    print(f"  📊 回测结果")
    print(f"{'─'*60}")
    print(f"\n  交易数: {n}  盈利: {len(wins)}  亏损: {len(losses)}")
    print(f"  胜率: {len(wins)/n*100:.1f}%")
    print(f"  平均收益: {avg:+.2f}%  中位数: {median:+.2f}%")
    print(f"  盈利均值: {avg_win:+.2f}%  亏损均值: {-avg_loss:+.2f}%")
    print(f"  盈亏比: {pf:.2f}")
    print(f"  期望值: {avg:+.2f}%/笔")
    print(f"  最大盈利: {max(pnl):+.2f}%  最大亏损: {min(pnl):+.2f}%")

    # 持仓
    hd = [t["hold_days"] for t in trades]
    print(f"\n  持仓天数: 均值={sum(hd)/len(hd):.1f}  中位数={sorted(hd)[len(hd)//2]}")

    # 出场原因
    print(f"\n  出场原因:")
    reason_groups = defaultdict(list)
    for t in trades:
        reason_groups[t["exit_reason"]].append(t["pnl_pct"])
    for reason, pnls in sorted(reason_groups.items(), key=lambda x: -len(x[1])):
        avg_r = sum(pnls) / len(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {reason:35s}  {len(pnls):3d}笔  均值={avg_r:+.2f}%  胜率={wr:.0f}%")

    # 按连板数
    print(f"\n  按连板数:")
    streak_g = defaultdict(list)
    for t in trades:
        streak_g[t["streak"]].append(t["pnl_pct"])
    for s in sorted(streak_g.keys()):
        pnls = streak_g[s]
        avg_s = sum(pnls) / len(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {s}板 ({len(pnls):3d}笔): 胜率={wr:.0f}%  均值={avg_s:+.2f}%")

    # 收益分布
    print(f"\n  收益分布:")
    bins = [(-999, -15), (-15, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 999)]
    labels = ["<-15%", "-15~-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~15%", "15~20%", ">20%"]
    for (lo, hi), label in zip(bins, labels):
        cnt = sum(1 for p in pnl if lo <= p < hi)
        bar = "█" * int(cnt / n * 40)
        print(f"    {label:12s}  {cnt:4d} ({cnt/n*100:5.1f}%)  {bar}")

    # 年度
    print(f"\n  按年度:")
    year_g = defaultdict(list)
    for t in trades:
        year_g[t["first_limit_date"][:4]].append(t["pnl_pct"])
    for year in sorted(year_g.keys()):
        pnls = year_g[year]
        avg_y = sum(pnls) / len(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {year} ({len(pnls):3d}笔): 胜率={wr:.0f}%  均值={avg_y:+.2f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="analysis_output/dragon_ohlcv.csv")
    parser.add_argument("--tp", type=float, default=15)
    parser.add_argument("--trail", type=float, default=8)
    parser.add_argument("--activate", type=float, default=5)
    parser.add_argument("--max-hold", type=int, default=20)
    parser.add_argument("--stop-loss", type=float, default=10)
    parser.add_argument("--mode", default="combo", choices=["single", "combo", "strict"])

    args = parser.parse_args()

    # 定义过滤规则（仅T日特征，T+1开盘买入）
    if args.mode == "single":
        # 单维度：第一板涨幅大
        filters = [
            {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0,
             "desc": "第一板涨幅"},
        ]
    elif args.mode == "combo":
        # 双维度：涨幅大 + 封板死
        filters = [
            {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0,
             "desc": "第一板涨幅"},
            {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8,
             "desc": "封板强度"},
        ]
    elif args.mode == "strict":
        # 三维度
        filters = [
            {"feature": "fl_return_pct", "dir": ">=", "thresh": 20.0,
             "desc": "第一板涨幅"},
            {"feature": "fl_gap_pct", "dir": ">=", "thresh": 12.68,
             "desc": "第一板高开"},
            {"feature": "fl_seal_pct", "dir": "<=", "thresh": 2.8,
             "desc": "封板强度"},
        ]

    backtest(args.csv, filters, args.tp, args.trail, args.activate,
             args.max_hold, args.stop_loss)


if __name__ == "__main__":
    main()
