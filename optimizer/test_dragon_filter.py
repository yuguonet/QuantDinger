#!/usr/bin/env python3
"""
连板猎手 v2 测试框架

1. 随机数据生成器：各种场景OHLCV
2. 逐日推进引擎：不用未来数据
3. 正确性验证
4. 真实数据比对
"""
from __future__ import annotations
import os, sys, csv, random, math
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any

_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def std_dev(vals):
    if len(vals) < 2: return 0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


# ================================================================
# 数据生成器
# ================================================================

def _next_date(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def gen_ohlcv(n_days, start_price, daily_vol, limit_pct, seed=None):
    """无涨停随机OHLCV"""
    if seed is not None: random.seed(seed)
    rows = []
    price = start_price
    date = datetime(2026, 1, 5)
    for _ in range(n_days):
        date = _next_date(date)
        prev = price
        ret = max(-limit_pct * 0.9, min(limit_pct * 0.9, random.gauss(0, daily_vol)))
        c = round(prev * (1 + ret), 2)
        o = round(prev * (1 + random.gauss(0, daily_vol * 0.2)), 2)
        spread = abs(ret) + daily_vol * 0.3
        h = round(max(o, c) * (1 + abs(random.gauss(0, spread * 0.3))), 2)
        l = round(min(o, c) * (1 - abs(random.gauss(0, spread * 0.3))), 2)
        h = max(h, o, c); l = min(l, o, c); l = max(l, 0.01)
        v = int(math.exp(random.gauss(15, 0.5)))
        rows.append({"date": date.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
    return rows


def gen_limit_up(n_days, start_price, limit_pct, streak, start_day,
                 seal="tight", after_crash=False, seed=None):
    """含连板的OHLCV"""
    if seed is not None: random.seed(seed)
    rows = []
    price = start_price
    date = datetime(2026, 1, 5)
    td = 0
    limit_end = start_day + streak

    for _ in range(n_days + streak + 15):
        date = _next_date(date)
        prev = price

        if start_day <= td < limit_end:
            # 涨停日
            c = round(prev * (1 + limit_pct), 2)
            if seal == "yizi":
                o = c; h = c; l = c
            elif seal == "tight":
                o = round(prev * (1 + random.uniform(0.02, 0.06)), 2)
                h = round(c * (1 + random.uniform(0, 0.003)), 2)
                l = round(min(o, c) * (1 - random.uniform(0, 0.008)), 2)
            else:  # loose
                o = round(prev * (1 + random.uniform(0.03, 0.08)), 2)
                h = round(c * (1 + random.uniform(0.02, 0.05)), 2)
                l = round(min(o, c) * (1 - random.uniform(0.01, 0.03)), 2)
            v = int(math.exp(random.gauss(16, 0.5)))

        elif after_crash and td == limit_end:
            c = round(prev * (1 - limit_pct), 2)
            o = round(prev * (1 - random.uniform(0.02, 0.05)), 2)
            h = round(prev * 0.97, 2)
            l = c
            v = int(math.exp(random.gauss(16.5, 0.5)))

        elif after_crash and limit_end < td < limit_end + 3:
            c = round(prev * (1 - limit_pct), 2)
            o = c; h = round(prev * 0.98, 2); l = c
            v = int(math.exp(random.gauss(14, 0.3)))

        else:
            ret = max(-limit_pct * 0.9, min(limit_pct * 0.9, random.gauss(0, 0.02)))
            c = round(prev * (1 + ret), 2)
            o = round(prev * (1 + random.gauss(0, 0.005)), 2)
            sp = abs(ret) + 0.01
            h = round(max(o, c) * (1 + abs(random.gauss(0, sp * 0.3))), 2)
            l = round(min(o, c) * (1 - abs(random.gauss(0, sp * 0.3))), 2)
            v = int(math.exp(random.gauss(15, 0.5)))

        h = max(h, o, c); l = min(l, o, c); l = max(l, 0.01)
        rows.append({"date": date.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
        td += 1
        if td >= n_days + streak + 8: break

    return rows[:n_days + streak + 5]


# ================================================================
# 策略逻辑
# ================================================================

BOARD_PARAMS = {
    "10pct": {"min_return": 9.8, "max_seal": 5.5, "min_upper": 0.0, "max_upper": 8.0, "max_volatility": 8.0},
    "20pct": {"min_return": 19.8, "max_seal": 2.8, "min_upper": 2.0, "max_upper": 8.0, "max_volatility": 10.0},
}

def lim_thresh(code):
    return 0.198 if code[:3] in ("300", "301", "688") else 0.098

def is_20pct(code):
    return code[:3] in ("300", "301", "688")

def board_name(code):
    c = code[:3]
    if c.startswith("68"): return "科创板"
    if c.startswith("30"): return "创业板"
    if c.startswith("6"): return "沪主板"
    if c.startswith(("0", "2")): return "深主板"
    return "未知"


def extract_feat(code, rows, i):
    """提取第i天特征（只用i及之前数据）"""
    if i < 6: return None
    close = [r["close"] for r in rows]
    prev_c = close[i - 1]
    if prev_c <= 0: return None

    threshold = lim_thresh(code)
    fl_c, fl_o, fl_h, fl_l = rows[i]["close"], rows[i]["open"], rows[i]["high"], rows[i]["low"]

    # 一字板
    lup = prev_c * (1 + threshold)
    gap = abs(fl_o - lup) / lup
    amp = (fl_h - fl_l) / prev_c
    if gap < 0.01 and amp < 0.01: return None

    # 非第一板
    if i >= 2 and close[i-1] / close[i-2] - 1 >= threshold * 0.98:
        return None

    ret = (fl_c / prev_c - 1) * 100
    seal = (fl_c - fl_l) / fl_c * 100 if fl_c > 0 else 999
    upper = (fl_h - fl_c) / prev_c * 100

    rets = [close[j] / close[j-1] - 1 for j in range(max(1, i-5), i) if close[j-1] > 0]
    vol = std_dev(rets) * 100 if len(rets) >= 3 else 999

    return {"code": code, "board": board_name(code), "is_20": is_20pct(code),
            "date": rows[i]["date"], "idx": i,
            "ret": ret, "seal": seal, "upper": upper, "vol": vol}


def passes(f):
    p = BOARD_PARAMS["20pct"] if f["is_20"] else BOARD_PARAMS["10pct"]
    return (f["ret"] >= p["min_return"] and f["seal"] <= p["max_seal"]
            and p["min_upper"] <= f["upper"] <= p["max_upper"]
            and f["vol"] <= p["max_volatility"])


def sim_trade(rows, buy_idx, limit_pct):
    """逐日推进出场"""
    n = len(rows)
    if buy_idx >= n: return None
    entry = rows[buy_idx]["open"]
    if entry <= 0: return None

    peak = entry; hold = 0
    for pos in range(buy_idx + 1, n):
        hold += 1
        c = rows[pos]["close"]
        h = rows[pos]["high"]
        prev_c = rows[pos - 1]["close"]
        if h > peak: peak = h
        ret = (c / entry - 1) * 100

        if ret <= -9.99:
            return {"exit": pos, "pnl": ret, "hold": hold, "reason": "止损10%"}

        if prev_c > 0:
            lp = prev_c * (1 + limit_pct)
            is_limit = (lp - c) / lp < 0.02
        else:
            is_limit = False

        if not is_limit:
            if ret >= 15:
                return {"exit": pos, "pnl": ret, "hold": hold, "reason": "止盈15%"}
            pk_ret = (peak / entry - 1) * 100
            if pk_ret >= 5 and (peak - c) / entry * 100 >= 8:
                return {"exit": pos, "pnl": ret, "hold": hold, "reason": "追踪止损"}
            return {"exit": pos, "pnl": ret, "hold": hold, "reason": "开板卖出"}

        if hold >= 20:
            return {"exit": pos, "pnl": ret, "hold": hold, "reason": "持仓20天"}

    ret = (rows[-1]["close"] / entry - 1) * 100
    return {"exit": n-1, "pnl": ret, "hold": hold, "reason": "数据结束"}


def backtest(code, rows, limit_pct):
    """逐日推进回测"""
    trades = []
    close = [r["close"] for r in rows]
    n = len(rows)
    i = 6
    while i < n - 1:
        # 涨停检测（和extract_feat一致用0.98）
        if i < 1 or close[i] / close[i-1] - 1 < limit_pct * 0.98:
            i += 1; continue

        feat = extract_feat(code, rows, i)
        if feat is None or not passes(feat):
            i += 1; continue

        buy_idx = i + 1
        result = sim_trade(rows, buy_idx, limit_pct)
        if result:
            trades.append({
                "code": code, "board": feat["board"],
                "signal": feat["date"], "buy": rows[buy_idx]["date"],
                "exit": rows[result["exit"]]["date"],
                "entry": round(result["entry"] if "entry" in result else rows[buy_idx]["open"], 2),
                "exit_p": round(rows[result["exit"]]["close"], 2),
                "pnl": round(result["pnl"], 2), "hold": result["hold"],
                "reason": result["reason"],
                "seal": round(feat["seal"], 1), "upper": round(feat["upper"], 1), "vol": round(feat["vol"], 1),
            })
            i = result["exit"] + 1
        else:
            i += 1
    return trades


# ================================================================
# 测试用例
# ================================================================

def test_random():
    """纯随机不应有信号"""
    print("=" * 60)
    print("  测试1: 纯随机（无涨停）→ 0信号")
    print("=" * 60)
    total = 0
    for s in range(20):
        for code, lp in [("000001", 0.10), ("300001", 0.20)]:
            rows = gen_ohlcv(60, 10 + s, 0.02, lp, seed=s * 10)
            total += len(backtest(code, rows, lp))
    print(f"  20组×2只=40只 信号: {total}")
    ok = total == 0
    print(f"  {'✅' if ok else '❌'} PASS")
    return ok


def test_tight_seal():
    """封板紧应识别"""
    print("\n" + "=" * 60)
    print("  测试2: 封板紧连板 → 识别首板")
    print("=" * 60)
    ok = True
    for streak in [1, 2, 3]:
        rows = gen_limit_up(30, 10.0, 0.10, streak, 10, seal="tight", seed=42 + streak)
        trades = backtest("000001", rows, 0.10)
        has = len(trades) > 0
        print(f"  {streak}连板: {len(trades)}笔 {'✅' if has else '❌'}")
        for t in trades:
            print(f"    {t['signal']}→{t['exit']} PnL={t['pnl']:+.1f}% {t['reason']} seal={t['seal']}%")
        if not has: ok = False
    return ok


def test_yizi():
    """一字板排除"""
    print("\n" + "=" * 60)
    print("  测试3: 一字板 → 排除")
    print("=" * 60)
    rows = gen_limit_up(30, 10.0, 0.10, 3, 10, seal="yizi", seed=99)
    trades = backtest("000001", rows, 0.10)
    ok = len(trades) == 0
    print(f"  一字板3连板: {len(trades)}笔 {'✅' if ok else '❌'}")
    return ok


def test_loose_seal():
    """封板松应过滤"""
    print("\n" + "=" * 60)
    print("  测试4: 封板松 → 过滤")
    print("=" * 60)
    # 多跑几次看是否有漏网
    passed = 0
    for s in range(10):
        rows = gen_limit_up(30, 10.0, 0.10, 1, 10, seal="loose", seed=70 + s)
        trades = backtest("000001", rows, 0.10)
        for t in trades:
            if t["seal"] <= 5.5:
                passed += 1
    print(f"  10组封板松: {passed}个通过过滤")
    ok = passed <= 2  # 允许少量随机波动
    print(f"  {'✅' if ok else '❌'} PASS (应<=2)")
    return ok


def test_board_branch():
    """双分支"""
    print("\n" + "=" * 60)
    print("  测试5: 双分支 主板10%/创科20%")
    print("=" * 60)
    ok = True
    for code, lp, name in [("000001", 0.10, "主板"), ("300001", 0.20, "创业板"), ("688001", 0.20, "科创板")]:
        rows = gen_limit_up(30, 10.0, lp, 2, 10, seal="tight", seed=50)
        trades = backtest(code, rows, lp)
        has = len(trades) > 0
        print(f"  {name}({code}): {len(trades)}笔 {'✅' if has else '❌'}")
        if not has: ok = False
    return ok


def test_after_crash():
    """连板后跌停"""
    print("\n" + "=" * 60)
    print("  测试6: 连板后跌停 → 出场")
    print("=" * 60)
    rows = gen_limit_up(30, 10.0, 0.10, 3, 10, seal="tight", after_crash=True, seed=60)
    trades = backtest("000001", rows, 0.10)
    print(f"  3连板后跌停: {len(trades)}笔")
    for t in trades:
        print(f"    {t['signal']} PnL={t['pnl']:+.1f}% {t['reason']}")
    ok = len(trades) > 0
    if ok:
        has_exit = any("止损" in t["reason"] or "开板" in t["reason"] for t in trades)
        ok = ok and has_exit
    print(f"  {'✅' if ok else '❌'} PASS")
    return ok


def test_multi():
    """综合"""
    print("\n" + "=" * 60)
    print("  测试7: 综合多场景")
    print("=" * 60)
    cases = [
        ("000001", 0.10, "主板2连板", {"n_days": 40, "start_price": 10, "limit_pct": 0.10, "streak": 2, "start_day": 12, "seal": "tight", "seed": 200}),
        ("000002", 0.10, "主板4连板+跌停", {"n_days": 40, "start_price": 15, "limit_pct": 0.10, "streak": 4, "start_day": 8, "seal": "tight", "after_crash": True, "seed": 201}),
        ("300001", 0.20, "创业板1连板", {"n_days": 40, "start_price": 30, "limit_pct": 0.20, "streak": 1, "start_day": 15, "seal": "tight", "seed": 202}),
        ("300002", 0.20, "创业板3连板+跌停", {"n_days": 40, "start_price": 50, "limit_pct": 0.20, "streak": 3, "start_day": 10, "seal": "tight", "after_crash": True, "seed": 203}),
        ("688001", 0.20, "科创板2连板", {"n_days": 40, "start_price": 100, "limit_pct": 0.20, "streak": 2, "start_day": 18, "seal": "tight", "seed": 204}),
        ("000999", 0.10, "纯随机主板", {"n_days": 60, "start_price": 8, "limit_pct": 0.10, "streak": 0, "start_day": 999, "seed": 205}),
        ("300999", 0.20, "纯随机创业板", {"n_days": 60, "start_price": 25, "limit_pct": 0.20, "streak": 0, "start_day": 999, "seed": 206}),
    ]
    all_ok = True
    for code, lp, name, kw in cases:
        rows = gen_limit_up(**kw)
        trades = backtest(code, rows, lp)
        if "纯随机" in name:
            ok = len(trades) == 0
            print(f"  {name}: {len(trades)}笔 {'✅' if ok else '❌'}")
        else:
            ok = len(trades) > 0
            print(f"  {name}: {len(trades)}笔 {'✅' if ok else '❌'}")
            for t in trades:
                print(f"    {t['signal']} PnL={t['pnl']:+.1f}% {t['reason']}")
        if not ok: all_ok = False
    return all_ok


def analyze_real():
    """真实数据分析"""
    print("\n" + "=" * 60)
    print("  真实数据分析")
    print("=" * 60)
    csv_path = os.path.join(_project_root, "analysis_output", "dragon_ohlcv.csv")
    if not os.path.isfile(csv_path):
        print("  ⚠️ 文件不存在"); return

    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): rows.append(r)

    by_code = defaultdict(list)
    for r in rows: by_code[r["code"]].append(r)
    for c in by_code: by_code[c].sort(key=lambda x: x["time"])

    runs = defaultdict(list)
    for r in rows: runs[(r["code"], r["run_first_limit_date"])].append(r)

    first = []
    seen = set()
    for (code, fd), rl in runs.items():
        for r in rl:
            if r["time"] == fd and (code, fd) not in seen:
                seen.add((code, fd)); first.append(r); break

    feats = []
    for r in first:
        code = r["code"]; cl = by_code[code]; idx = None
        for i, cr in enumerate(cl):
            if cr["time"] == r["time"]: idx = i; break
        if idx is None or idx < 6: continue
        pc = float(cl[idx-1]["close"])
        if pc <= 0: continue
        fc, fo, fh, fl = float(r["close"]), float(r["open"]), float(r["high"]), float(r["low"])
        th = lim_thresh(code)
        lup = pc * (1 + th)
        if abs(fo - lup) / lup < 0.01 and (fh - fl) / pc < 0.01: continue
        ret = (fc / pc - 1) * 100
        seal = (fc - fl) / fc * 100 if fc > 0 else 999
        upper = (fh - fc) / pc * 100
        rets = [float(cl[j]["close"]) / float(cl[j-1]["close"]) - 1
                for j in range(max(1, idx-5), idx) if float(cl[j-1]["close"]) > 0]
        vol = std_dev(rets) * 100 if len(rets) >= 3 else 999
        feats.append({"code": code, "board": r["board"], "is_20": is_20pct(code),
                       "date": r["time"], "ret": ret, "seal": seal, "upper": upper, "vol": vol})

    print(f"  非一字板首板: {len(feats)}")
    passed = [f for f in feats if passes(f)]
    print(f"  双分支通过: {len(passed)}")
    pb = defaultdict(int)
    for f in passed: pb[f["board"]] += 1
    for b, c in sorted(pb.items(), key=lambda x: -x[1]):
        print(f"    {b}: {c}")

    for label, filt in [("主板", lambda f: not f["is_20"]), ("创/科", lambda f: f["is_20"])]:
        sub = [f for f in passed if filt(f)]
        if not sub: continue
        print(f"\n  {label}信号特征({len(sub)}笔):")
        for k, l in [("seal", "封板%"), ("upper", "上影%"), ("vol", "波动%")]:
            vals = sorted(f[k] for f in sub)
            print(f"    {l}: P25={vals[len(vals)//4]:.1f} P50={vals[len(vals)//2]:.1f} P75={vals[len(vals)*3//4]:.1f}")


# ================================================================
# 主函数
# ================================================================

def main():
    print("\n" + "=" * 70)
    print("  连板猎手 v2 测试框架")
    print("=" * 70)

    results = {}
    results["随机数据"] = test_random()
    results["封板紧连板"] = test_tight_seal()
    results["一字板排除"] = test_yizi()
    results["封板松过滤"] = test_loose_seal()
    results["双分支"] = test_board_branch()
    results["连板后跌停"] = test_after_crash()
    results["综合测试"] = test_multi()

    analyze_real()

    print("\n" + "=" * 70)
    print("  测试汇总")
    print("=" * 70)
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    p = sum(1 for v in results.values() if v)
    print(f"\n  通过: {p}/{len(results)}")


if __name__ == "__main__":
    main()
