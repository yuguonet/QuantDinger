#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量A股K线拉取 & 双源比对 (纯HTTP)
源1: 新浪财经 (日K + 15min)
源2: 腾讯财经 (日K, 前复权)
自动取最近交易日数据比对
"""

import requests
import json
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

TARGET_DATE = None  # None=自动取最近交易日
OUTPUT_DIR = "./a_share_data"
MAX_WORKERS = 30
SLEEP = 0.015

os.makedirs(OUTPUT_DIR, exist_ok=True)
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
})


# ===================================================================
#  股票代码 (直接生成)
# ===================================================================

def gen_codes():
    codes = []
    for i in range(600000, 605999): codes.append(f"sh{i}")
    for i in range(688000, 689999): codes.append(f"sh{i}")
    for i in range(1, 4000): codes.append(f"sz{i:06d}")
    for i in range(300000, 301999): codes.append(f"sz{i}")
    print(f"[代码] 候选: {len(codes)}", flush=True)
    return codes


# ===================================================================
#  新浪 K线
# ===================================================================

def sina_kline(sym, scale=240, datalen=10):
    """
    新浪 K线 HTTP 接口
    scale: 240=日K, 15=15min, 60=60min, 5=5min
    """
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sym, "scale": scale, "ma": "no", "datalen": datalen}
    r = s.get(url, params=params, timeout=8)
    if r.status_code != 200:
        return None
    data = json.loads(r.text)
    if not data:
        return None
    res = {"date": [], "open": [], "close": [], "high": [], "low": [], "vol": []}
    for row in data:
        res["date"].append(row["day"][:10] if scale < 240 else row["day"])
        res["open"].append(row["open"])
        res["close"].append(row["close"])
        res["high"].append(row["high"])
        res["low"].append(row["low"])
        res["vol"].append(row["volume"])
    return res


def sina_15min_raw(sym, datalen=100):
    """15min返回完整datetime用于比对"""
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sym, "scale": 15, "ma": "no", "datalen": datalen}
    r = s.get(url, params=params, timeout=8)
    if r.status_code != 200:
        return None
    data = json.loads(r.text)
    if not data:
        return None
    res = {"datetime": [], "open": [], "close": [], "high": [], "low": [], "vol": []}
    for row in data:
        res["datetime"].append(row["day"])
        res["open"].append(row["open"])
        res["close"].append(row["close"])
        res["high"].append(row["high"])
        res["low"].append(row["low"])
        res["vol"].append(row["volume"])
    return res


# ===================================================================
#  腾讯 日K (前复权)
# ===================================================================

def qq_daily(sym, end_date=None):
    """
    腾讯日K 前复权
    param格式: symbol,day,startDate,endDate,count,adjust
    """
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    today = datetime.now().strftime("%Y-%m-%d")
    ed = end_date or today
    start = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    params = {"param": f"{sym},day,{start},{ed},30,qfq"}
    r = s.get(url, params=params, timeout=8)
    if r.status_code != 200:
        return None
    data = r.json()
    stock = data.get("data", {}).get(sym, {})
    klines = stock.get("qfqday") or stock.get("day") or []
    if not klines:
        return None
    res = {"date": [], "open": [], "close": [], "high": [], "low": [], "vol": []}
    for row in klines:
        res["date"].append(row[0])
        res["open"].append(row[1])
        res["close"].append(row[2])
        res["high"].append(row[3])
        res["low"].append(row[4])
        res["vol"].append(row[5] if len(row) > 5 else "0")
    return res


# ===================================================================
#  批量并发
# ===================================================================

def batch(codes, fn, label):
    print(f"\n[{label}] 启动 ({len(codes)}只, {MAX_WORKERS}并发)", flush=True)
    results = {}
    ok = fail = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(fn, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                d = f.result()
                if d and len(list(d.values())[0]) > 0:
                    results[c] = d
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            total = ok + fail
            if total % 500 == 0:
                print(f"  [{label}] {total}/{len(codes)} ok={ok} {time.time()-t0:.0f}s", flush=True)

    print(f"[{label}] ok:{ok} fail:{fail} {time.time()-t0:.0f}s", flush=True)
    return results


# ===================================================================
#  比对
# ===================================================================

def to_map(data, key="date"):
    n = len(list(data.values())[0])
    m = {}
    for i in range(n):
        k = data[key][i]
        try:
            m[k] = {
                "open": float(data["open"][i]),
                "close": float(data["close"][i]),
                "high": float(data["high"][i]),
                "low": float(data["low"][i]),
            }
        except:
            pass
    return m


def compare(src1, src2, codes, label, src1_name="新浪", src2_name="腾讯", key="date", target_date=None):
    print(f"\n{'='*60}", flush=True)
    print(f"  {label}: {src1_name} vs {src2_name}", flush=True)
    if target_date:
        print(f"  目标日期: {target_date}", flush=True)
    print(f"{'='*60}", flush=True)

    match = diff = only1 = only2 = 0
    diffs = []

    for code in codes:
        d1, d2 = src1.get(code), src2.get(code)
        if not d1 and not d2:
            continue
        if not d1:
            only2 += 1
            continue
        if not d2:
            only1 += 1
            continue

        m1, m2 = to_map(d1, key), to_map(d2, key)

        if target_date:
            if target_date in m1 and target_date in m2:
                for field in ["open", "close", "high", "low"]:
                    v1, v2 = m1[target_date][field], m2[target_date][field]
                    if v1 == 0 or v2 == 0:
                        continue
                    pct = abs(v1 - v2) / max(v1, v2) * 100
                    if pct > 0.1:
                        diff += 1
                        diffs.append({"code": code, "date": target_date, "field": field,
                                      src1_name: v1, src2_name: v2, "pct": round(pct, 3)})
                        break
                else:
                    match += 1
        else:
            common = set(m1) & set(m2)
            if not common:
                continue
            has_diff = False
            for k in common:
                for field in ["close"]:
                    v1, v2 = m1[k][field], m2[k][field]
                    if v1 == 0 or v2 == 0:
                        continue
                    pct = abs(v1 - v2) / max(v1, v2) * 100
                    if pct > 0.1:
                        has_diff = True
                        diffs.append({"code": code, "date": k, "field": field,
                                      src1_name: v1, src2_name: v2, "pct": round(pct, 3)})
            if has_diff:
                diff += 1
            else:
                match += 1

    both = match + diff
    print(f"{src1_name}:{len(src1)} | {src2_name}:{len(src2)} | 比对:{both}", flush=True)
    if both:
        print(f"  ✅ 一致: {match} ({match/both*100:.1f}%)", flush=True)
        print(f"  ❌ 差异: {diff} ({diff/both*100:.1f}%)", flush=True)
    print(f"仅{src1_name}:{only1} | 仅{src2_name}:{only2}", flush=True)

    if diffs:
        path = os.path.join(OUTPUT_DIR, f"{label.replace(' ','_')}_diff.csv")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(f"code,date,field,{src1_name},{src2_name},pct_diff\n")
            for d in diffs:
                f.write(f"{d['code']},{d['date']},{d['field']},{d[src1_name]},{d[src2_name]},{d['pct']}\n")
        print(f"  差异明细: {path} ({len(diffs)}条)", flush=True)
        for d in diffs[:10]:
            print(f"    {d['code']} {d['date']} {d['field']}: Δ={d['pct']}%", flush=True)
    return diffs


def save_csv(data, name):
    if not data:
        return
    rows = []
    for code, d in data.items():
        n = len(list(d.values())[0])
        for i in range(n):
            rows.append({k: v[i] for k, v in d.items()})
    if not rows:
        return
    path = os.path.join(OUTPUT_DIR, f"{name}.csv")
    h = list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(",".join(h) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in h) + "\n")
    mb = os.path.getsize(path) / 1024 / 1024
    print(f"[保存] {path} {len(rows)}行 {mb:.1f}MB", flush=True)


# ===================================================================
#  MAIN
# ===================================================================

def main():
    print("全量A股K线 | 新浪+腾讯 双源比对", flush=True)
    codes = gen_codes()

    # ===== 日K =====
    print(f"\n{'='*40} 日K {'='*40}", flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(batch, codes, lambda c: sina_kline(c, 240, 10), "新浪-日K")
        f2 = pool.submit(batch, codes, lambda c: qq_daily(c), "腾讯-日K")
        sina_d = f1.result()
        qq_d = f2.result()

    save_csv(sina_d, "sina_daily")
    save_csv(qq_d, "qq_daily")

    # 找到两边都有的最新日期
    sample_code = next((c for c in codes if c in sina_d and c in qq_d), None)
    if sample_code:
        sina_latest = max(sina_d[sample_code]["date"])
        qq_latest = max(qq_d[sample_code]["date"])
        target = min(sina_latest, qq_latest)
        print(f"\n[日期] 新浪最新:{sina_latest} 腾讯最新:{qq_latest} 比对目标:{target}", flush=True)
    else:
        target = None
        print("\n[警告] 没有找到两个源都有的股票", flush=True)

    compare(sina_d, qq_d, codes, "日K比对", target_date=target)

    # ===== 15min =====
    print(f"\n{'='*40} 15min {'='*40}", flush=True)
    sina_m = batch(codes, lambda c: sina_15min_raw(c, 100), "新浪-15min")
    save_csv(sina_m, "sina_15min")

    # 15min自校验
    print(f"\n[15min自校验] datalen=20 vs datalen=100", flush=True)
    sina_m2 = batch(codes[:500], lambda c: sina_15min_raw(c, 20), "新浪-15min短")
    sm = sd = 0
    for code in codes[:500]:
        d1, d2 = sina_m.get(code), sina_m2.get(code)
        if not d1 or not d2:
            continue
        m1, m2 = to_map(d1, "datetime"), to_map(d2, "datetime")
        for k in set(m1) & set(m2):
            if abs(m1[k]["close"] - m2[k]["close"]) > 0.02:
                sd += 1
            else:
                sm += 1
    print(f"  ✅一致:{sm} ❌差异:{sd}", flush=True)

    print(f"\n完成! {os.path.abspath(OUTPUT_DIR)}", flush=True)
    for f in sorted(os.listdir(OUTPUT_DIR)):
        fp = os.path.join(OUTPUT_DIR, f)
        print(f"  {f:40s} {os.path.getsize(fp)/1024/1024:>6.1f}MB", flush=True)


if __name__ == "__main__":
    main()
