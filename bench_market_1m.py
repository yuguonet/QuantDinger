#!/usr/bin/env python3
"""
bench_market_1m.py — 全市场批量行情快照 → 1m OHLCV 基准测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

使用项目自身基础设施:
  - basicinfo_db.market_all_codes() 获取全市场股票代码
  - coordinator.coordinate_batch_quotes() 批量拉取行情快照
  - 多轮测试验证稳定性 + 模拟DB写入

用法:
  cd backend_api_python
  python ../bench_market_1m.py [--rounds 3] [--pause 2] [--limit 0] [--no-write]
"""

import json, os, sys, time, threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ═══════════════ 路径设置 (抄 test_dragon.py) ═══════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_backend_root = os.path.join(SCRIPT_DIR, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, ".env"), os.path.join(SCRIPT_DIR, ".env")]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

_load_env()

from app.data_sources.coordinator import get_coordinator

TZ = timezone(timedelta(hours=8))


# ═══════════════ 获取全市场代码 (抄 test_dragon.py) ═══════════════
def load_all_codes():
    """从 basicinfo_db 获取全市场活跃股票代码 (纯6位数字)"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    return db.market_all_codes(status="active")


# ═══════════════ 快照 → 1m OHLCV bar ═══════════════
def snapshot_to_1m_bars(quotes, ts_str):
    """批量行情快照 → 1m OHLCV bar 列表"""
    bars = []
    for q in quotes:
        sym = q.get("symbol", "")
        last = q.get("last") or q.get("close") or q.get("price") or 0
        if last <= 0:
            continue
        bars.append({
            "symbol": sym,
            "time": ts_str,
            "open": q.get("open") or last,
            "high": q.get("high") or last,
            "low": q.get("low") or last,
            "close": last,
            "volume": q.get("volume") or 0,
        })
    return bars


# ═══════════════ DB写入模拟 ═══════════════
def bench_db_simulation(bars, batch_size=5000):
    """模拟 execute_values 批量写入的序列化开销"""
    t0 = time.time()
    template = 'INSERT INTO "kline_1m_2026" (symbol, time, open, high, low, close, volume) VALUES '
    sql_parts = []
    for i in range(0, len(bars), batch_size):
        batch = bars[i:i + batch_size]
        values = ",".join(
            f"('{b['symbol']}','{b['time']}',{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']})"
            for b in batch
        )
        sql_parts.append(template + values)
    serialize_time = time.time() - t0
    total_bytes = sum(len(s.encode()) for s in sql_parts)
    return {
        "records": len(bars),
        "batches": len(sql_parts),
        "serialize_s": round(serialize_time, 4),
        "sql_mb": round(total_bytes / 1024 / 1024, 2),
    }


# ═══════════════ 单轮测试 ═══════════════
def run_one_round(codes, timeout=60, skip_write=False):
    """单轮全量测试: 拉取 → 转换 → (可选)模拟写入"""
    coord = get_coordinator()
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:00")

    # ── 拉取 ──
    t0 = time.time()
    quotes = coord.coordinate_batch_quotes(
        symbols=codes, market="CNStock", timeout=timeout,
    )
    fetch_s = time.time() - t0

    # ── 转换 ──
    t1 = time.time()
    bars = snapshot_to_1m_bars(quotes, ts)
    convert_s = time.time() - t1

    # ── DB模拟 (可跳过) ──
    db_sim = None
    if not skip_write and bars:
        db_sim = bench_db_simulation(bars)

    return {
        "stocks": len(quotes),
        "total_codes": len(codes),
        "fetch_s": round(fetch_s, 3),
        "convert_s": round(convert_s, 4),
        "bars_count": len(bars),
        "db_sim": db_sim,
    }


# ═══════════════ 主流程 ═══════════════
def main():
    import argparse
    p = argparse.ArgumentParser(description="全市场1分钟K线基准测试 (coordinator)")
    p.add_argument("--rounds", type=int, default=3, help="测试轮数")
    p.add_argument("--pause", type=float, default=2, help="轮间间隔(秒)")
    p.add_argument("--limit", type=int, default=0, help="限制股票数(0=全市场)")
    p.add_argument("--timeout", type=int, default=60, help="每轮超时(秒)")
    p.add_argument("--no-write", action="store_true", help="跳过DB写入模拟，只测拉取+转换")
    args = p.parse_args()

    now = datetime.now(TZ)
    mode = "拉取+转换" if args.no_write else "拉取+转换+DB模拟"
    print("=" * 65)
    print(f"  🚀 全市场1分钟K线 基准测试 | {now:%Y-%m-%d %H:%M:%S}")
    print(f"  接口: coordinator.coordinate_batch_quotes()")
    print(f"  测试: {args.rounds}轮, 间隔 {args.pause}s, 超时 {args.timeout}s | 模式: {mode}")
    print("=" * 65)

    # ── 获取股票列表 ──
    print(f"\n  📋 获取全市场代码 (basicinfo_db)...")
    t0 = time.time()
    codes = load_all_codes()
    list_time = time.time() - t0
    print(f"  ✅ {len(codes)} 只 | {list_time:.2f}s")

    if args.limit > 0:
        codes = codes[:args.limit]
        print(f"  ⚙️  限制为前 {args.limit} 只")

    # ── 预热: prepare() 初始化各源cookie/连接 ──
    print(f"\n  🔥 预热数据源 (prepare)...")
    coord = get_coordinator()
    t0 = time.time()
    prep = coord.prepare(market="CNStock")
    prep_time = time.time() - t0
    for name, ok in prep.items():
        status = "✅" if ok else "❌"
        print(f"    {status} {name}")
    print(f"  耗时 {prep_time:.2f}s")

    # ── 多轮测试 ──
    print(f"\n  📡 开始测试 ({len(codes)} 只 × {args.rounds}轮)")
    results = []
    for i in range(args.rounds):
        print(f"\n  {'─' * 55}")
        print(f"  第 {i+1}/{args.rounds} 轮")
        r = run_one_round(codes, timeout=args.timeout, skip_write=args.no_write)
        results.append(r)

        if r["stocks"] > 0:
            speed = r["stocks"] / r["fetch_s"] if r["fetch_s"] > 0 else 0
            print(f"    拉取: {r['stocks']}/{r['total_codes']} 只 | {r['fetch_s']:.3f}s | {speed:.0f} 只/秒")
            print(f"    转换: {r['bars_count']} bar | {r['convert_s']:.4f}s")
            if r["db_sim"]:
                print(f"    DB模拟: {r['db_sim']['batches']}批 | {r['db_sim']['sql_mb']}MB | {r['db_sim']['serialize_s']}s")
        else:
            print(f"    ❌ 拉取失败")

        if i < args.rounds - 1:
            time.sleep(args.pause)

    # ── 汇总 ──
    valid = [r for r in results if r["stocks"] > 0]
    if valid:
        avg_fetch = sum(r["fetch_s"] for r in valid) / len(valid)
        avg_stocks = sum(r["stocks"] for r in valid) / len(valid)
        avg_speed = avg_stocks / avg_fetch if avg_fetch > 0 else 0
        avg_convert = sum(r["convert_s"] for r in valid) / len(valid)
        avg_bars = sum(r["bars_count"] for r in valid) / len(valid)
        est_db = avg_bars / 50000
        est_total = avg_fetch + avg_convert + est_db

        print(f"\n  {'=' * 60}")
        print(f"  📈 汇总 ({len(valid)}/{len(results)} 轮有效):")
        print(f"    平均拉取: {avg_stocks:.0f} 只 / {avg_fetch:.3f}s ({avg_speed:.0f} 只/秒)")
        print(f"    平均转换: {avg_convert:.4f}s ({avg_bars:.0f} bar)")
        if not args.no_write:
            print(f"    预估DB写入: ~{est_db:.3f}s (按5万条/秒)")
        print(f"    预估全链路: ~{est_total:.3f}s")
        print()
        if est_total <= 5:
            print(f"    ✅ 每分钟调度完全可行！余量 {60 - est_total:.1f}s")
        elif est_total <= 30:
            print(f"    ✅ 每分钟调度可行，余量 {60 - est_total:.1f}s")
        elif est_total <= 60:
            print(f"    ⚠️  勉强1分钟，余量 {60 - est_total:.1f}s")
        else:
            print(f"    ❌ 超过1分钟，需优化")

    # ── 保存报告 ──
    report = {"timestamp": now.isoformat(), "total_codes": len(codes), "rounds": results}
    report_path = os.path.join(SCRIPT_DIR, "bench_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  📄 报告: {report_path}")


if __name__ == "__main__":
    main()
