#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tools/snapshot_quality.py — 快照拼接通道质量诊断工具 (P3.7, 2026-09-21)

用法:
  python -m app.market_cn.auto.tools.snapshot_quality [--codes 000012,000506] [--days 10]

原理:
  - ground truth = kline_1m 盘后回填数据 (完整, 无丢分钟)
  - candidate    = hybrid_series / minute_live_full 拼接结果
  - 对齐时间戳后逐字段 (open/high/low/last/vol) 比对, 输出偏差统计

⚠️ 注意:
  - 只对比近端窗口 (kline_1m 有数据的交易日)
  - 当日快照可能丢分钟 (realtime_snapshot 每 60s/拍); kline_1m 无当日数据 → 自动跳过
  - 对齐方式: timestamp (秒级) 或接近的时间戳 (容差 30s)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── 路径 (同 core/_paths.py 约定) ──────────────────────────────────────────
# tools/ → auto/ → market_cn/ → app/ → backend_api_python/
# 必须把 **含 app/ 的父目录** (即 backend_api_python/) 放进 sys.path,
# `import app` 才会被识别为 Python 包。
_backend = str(Path(__file__).resolve().parents[4])
sys.path.insert(0, _backend)
os.environ["PYTHONPATH"] = _backend + os.pathsep + os.environ.get("PYTHONPATH", "")

from app.market_cn.auto.core.features.minute_composite import hybrid_series


def _align_rows(gold: list, cand: list, atol: int = 30) -> list:
    """按 timestamp 对齐两列, 返回 [(gold_row, cand_row)]; 无匹配→ (g, None) 或 (None, c)。"""
    idx = {int(r["timestamp"]): i for i, r in enumerate(cand)}
    out = []
    for g in gold:
        ts = int(g["timestamp"])
        if ts in idx:
            out.append((g, cand[idx[ts]]))
        else:
            # 找 ±atol 内的最近
            best, best_d = None, atol + 1
            for cts, ci in idx.items():
                d = abs(cts - ts)
                if d < best_d:
                    best, best_d = ci, d
            out.append((g, cand[best] if best is not None else None))
    # cand 中无 gold 匹配的 → (None, c)
    gold_ts = {int(r["timestamp"]) for r in gold}
    for c in cand:
        if int(c["timestamp"]) not in gold_ts:
            out.append((None, c))
    return out


def _diff(gold_row, cand_row, fields=("open", "high", "low", "last", "vol")):
    if cand_row is None:
        return {f: None for f in fields}
    d = {}
    for f in fields:
        gv = float(gold_row.get(f, 0) or 0)
        cv = float(cand_row.get(f, 0) or 0)
        if gv == 0:
            d[f] = None
        else:
            d[f] = round(cv - gv, 6)
            d[f"{f}_pct"] = round((cv - gv) / gv * 100, 4)
    return d


def _stats(vals):
    """返回均值/标准差/最大值/最小值; vals 中 None 被忽略。"""
    clean = [v for v in vals if v is not None]
    if not clean:
        return {"n": 0, "mean": None, "std": None, "max": None, "min": None}
    n = len(clean)
    m = sum(clean) / n
    std = (sum((v - m) ** 2 for v in clean) / n) ** 0.5
    return {"n": n, "mean": round(m, 6), "std": round(std, 6),
            "max": round(max(clean), 6), "min": round(min(clean), 6)}


def _fmt(v, pct=None):
    if v is None:
        return "N/A"
    s = f"{v:.6f}"
    if pct is not None:
        s += f" ({pct:+.4f}%)"
    return s


def diagnose_code(code: str, days: int = 10, end_date: str = None) -> dict:
    """单股诊断. 返回结构化报告 dict.

    比较 kline_1m (ground truth) vs hybrid_series (candidate) 逐分钟偏差.
    ⚠️ 需要 DB 连接 (kline_1m / hybrid_series 均依赖 hub 层).
    """
    from datetime import datetime, timedelta
    from app.market_cn.auto.core.features.minute_composite import (
        hybrid_series, plan_tiers)
    from app.market_cn.auto.core.data.hub import minute_1m

    # 本地生成最近 N 个交易日 (跳过周六/日, 不依赖 DB)
    end_str = str(end_date or datetime.now().strftime("%Y-%m-%d"))[:10]
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    dates = []
    d = end_dt
    while len(dates) < days:
        if d.weekday() < 5:          # Mon-Fri
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    dates.reverse()
    report = {"code": code, "dates": dates, "slots": [], "summary": {}}

    for d in dates:
        gold_rows = minute_1m(code, start=d, end=d, as_of=d)
        if not gold_rows:
            continue
        cand_rows = hybrid_series(code, start_date=d, end_date=d)
        if not cand_rows:
            report["slots"].append({"date": d, "status": "no_cand"})
            continue

        aligned = _align_rows(gold_rows, cand_rows)
        diffs = [_diff(g, c) for g, c in aligned if g is not None]
        if not diffs:
            report["slots"].append({"date": d, "status": "no_gold"})
            continue

        fields = ("open", "high", "low", "last", "vol")
        slot_stat = {"date": d, "n_gold": len(gold_rows), "n_cand": len(cand_rows),
                     "fields": {}}
        warn = 0
        for f in fields:
            vals = [dd.get(f) for dd in diffs]
            pct_key = f"{f}_pct"
            pcts = [dd.get(pct_key) for dd in diffs]
            s = _stats(vals)
            ps = _stats(pcts)
            # 判断: pct > 0.1% → warning; > 1% → error
            err_pct = max(abs(v) for v in pcts if v is not None) if pcts else 0
            status = "ok" if err_pct < 0.1 else ("warn" if err_pct < 1.0 else "error")
            slot_stat["fields"][f] = {"stats": s, "pct_stats": ps, "status": status,
                                       "max_pct": round(err_pct, 4)}
            if status != "ok":
                warn += 1
        slot_stat["status"] = "ok" if warn == 0 else ("warn" if warn <= 2 else "error")
        report["slots"].append(slot_stat)

    # 汇总
    total = len(report["slots"])
    ok_n = sum(1 for s in report["slots"] if s.get("status") == "ok")
    warn_n = sum(1 for s in report["slots"] if s.get("status") == "warn")
    err_n = sum(1 for s in report["slots"] if s.get("status") == "error")
    no_cand = sum(1 for s in report["slots"] if s.get("status") == "no_cand")
    report["summary"] = {"total_slots": total,
                         "ok": ok_n, "warn": warn_n, "error": err_n,
                         "no_cand": no_cand,
                         "quality_score": round(ok_n / total * 100, 1) if total else 0}
    return report


def _print_report(r: dict, verbose: bool = False):
    print(f"\n{'='*60}")
    print(f"  Code: {r['code']}   Days: {len(r['dates'])}")
    print(f"{'='*60}")
    s = r["summary"]
    print(f"  槽位质量: {s['quality_score']}%  (ok={s['ok']} warn={s['warn']} "
          f"error={s['error']} no_cand={s['no_cand']})")
    for slot in r["slots"]:
        d = slot["date"]
        st = slot.get("status", "?")
        if st == "no_cand":
            print(f"  [{d}] no_cand")
            continue
        if st == "no_gold":
            print(f"  [{d}] no_gold")
            continue
        markers = {"ok": "✓", "warn": "⚠", "error": "✗"}
        print(f"  [{d}] {markers.get(st,'?')} {st:5s}  n_g={slot['n_gold']} n_c={slot['n_cand']}")
        if verbose:
            for f, v in slot["fields"].items():
                sp = v["pct_stats"]
                st_f = v["status"]
                print(f"      {f:5s}: mean={_fmt(sp['mean'])} "
                      f"max_err={_fmt(sp['max'])} status={st_f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip())
    ap.add_argument("--codes", default="000012,000506",
                   help="逗号分隔股票代码 (default: 000012,000506)")
    ap.add_argument("--days", type=int, default=10,
                   help="诊断窗口天数 (default: 10)")
    ap.add_argument("--end", dest="end_date", default=None,
                   help="截止日期 YYYY-MM-DD (default: 今天)")
    ap.add_argument("--json", action="store_true",
                   help="输出 JSON 格式 (machine-readable)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    reports = []
    t0 = time.time()
    for code in codes:
        print(f"  诊断 {code} ...", end="", flush=True)
        r = diagnose_code(code, days=args.days, end_date=args.end_date)
        reports.append(r)
        print(f" done (score={r['summary']['quality_score']}%)")

    elapsed = round(time.time() - t0, 1)
    if args.json:
        out = {"reports": reports, "elapsed_s": elapsed}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            _print_report(r, verbose=args.verbose)
        print(f"\n  总耗时: {elapsed}s")


if __name__ == "__main__":
    main()
