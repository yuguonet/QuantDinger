#!/usr/bin/env python3
"""
A股全市场15分钟K线下载 — 通过 Coordinator 并发获取（纯测速，不落盘）

直接调用 coordinate_market_kline，由 Coordinator 自动调度多源并发下载。

用法:
  python test_market_kline.py                      # 全市场 15m
  python test_market_kline.py --limit 100          # 只测100只
  python test_market_kline.py --codes 600519,000001 # 指定代码
  python test_market_kline.py --timeframe 1D       # 日线模式
  python test_market_kline.py --timeout 600        # 全局超时600秒
  python test_market_kline.py --count 200          # 每只200条
  python test_market_kline.py --adj ""             # 不复权
"""

import argparse
import os
import sys
import time
from datetime import datetime

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 加载 .env（与 run.py 同逻辑）
try:
    from dotenv import load_dotenv
    _here = os.path.dirname(os.path.abspath(__file__))
    _backend = os.path.join(_here, "..")
    load_dotenv(os.path.join(_backend, ".env"), override=False)
    load_dotenv(os.path.join(_backend, "..", ".env"), override=False)
except ImportError:
    pass


def main():
    p = argparse.ArgumentParser(description="A股全市场K线下载 — Coordinator 并发模式 (纯测速)")
    p.add_argument("--limit", type=int, default=0, help="限制股票数量 (0=全部)")
    p.add_argument("--codes", type=str, default="", help="指定代码,逗号分隔")
    p.add_argument("--timeframe", type=str, default="15m", help="K线周期 (15m/1D)")
    p.add_argument("--count", type=int, default=200, help="每只股票数据条数")
    p.add_argument("--adj", type=str, default="qfq", help="复权方式 (qfq/hfq/)")
    p.add_argument("--timeout", type=float, default=300, help="全局超时(秒)")
    p.add_argument("--start-date", type=str, default="", help="起始日期 YYYY-MM-DD")
    from datetime import date, timedelta
    p.add_argument("--end-date", type=str, default=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="结束日期 YYYY-MM-DD (默认昨天，确保走 fetch_market_kline 路径)")
    p.add_argument("--preferred", type=str, default="", help="首选源 (如 tencent)")
    args = p.parse_args()

    print("=" * 65)
    print(f"  A股全市场K线 — Coordinator 并发模式 | {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  周期: {args.timeframe} | 每只: {args.count}条 | 复权: {args.adj or '不复权'}")
    print(f"  超时: {args.timeout}s | 模式: 纯测速(不落盘)")
    print(f"  end_date: {args.end_date} → 确保走 fetch_market_kline 路径")
    if args.start_date:
        print(f"  日期范围: {args.start_date} ~ {args.end_date or '今天'}")
    print("=" * 65)

    # ── 导入 & 初始化 ──
    t_import = time.time()
    from app.data_sources.provider import autodiscover
    from app.data_sources.coordinator import get_coordinator
    from app.data_sources.circuit_breaker import get_realtime_circuit_breaker

    autodiscover()
    coord = get_coordinator()
    cb = get_realtime_circuit_breaker()
    t_import = time.time() - t_import

    # ── 获取股票列表 ──
    if args.codes:
        all_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        print("\n  📋 获取股票列表...")
        from app.utils.basicinfo_db import get_stock_basic_db
        all_codes = get_stock_basic_db().market_all_codes(status="active")
        if not all_codes:
            print("  ❌ 获取股票列表失败")
            return
        if args.limit > 0:
            all_codes = all_codes[: args.limit]

    total = len(all_codes)
    print(f"  📊 目标: {total} 只股票")
    print(f"  🚀 开始下载...\n")

    # ── 调用 Coordinator ──
    t0 = time.time()

    if args.codes or args.limit > 0:
        result = coord.coordinate_market_kline(
            cb=cb,
            market="CNStock",
            timeframe=args.timeframe,
            count=args.count if not args.start_date else None,
            adj=args.adj,
            timeout=args.timeout,
            preferred_source=args.preferred,
            start_date=args.start_date,
            end_date=args.end_date,
            symbols=all_codes,
        )
    else:
        result = coord.coordinate_market_kline(
            cb=cb,
            market="CNStock",
            timeframe=args.timeframe,
            count=args.count if not args.start_date else None,
            adj=args.adj,
            timeout=args.timeout,
            preferred_source=args.preferred,
            start_date=args.start_date,
            end_date=args.end_date,
        )

    elapsed = time.time() - t0

    if not result:
        print("  ❌ 未获取到任何数据")
        return

    # ── 统计 ──
    fetched = len(result)
    total_bars = sum(len(bars) for bars in result.values())
    avg_bars = total_bars / fetched if fetched else 0
    speed = fetched / elapsed if elapsed > 0 else 0
    hit_rate = fetched / total * 100 if total > 0 else 0
    bars_per_sec = total_bars / elapsed if elapsed > 0 else 0

    # 代码列表差异（漏掉的）
    fetched_set = set(result.keys())
    requested_set = set(all_codes)
    missed = requested_set - fetched_set

    print(f"\n  {'─' * 55}")
    print(f"  ✅ 完成 | 耗时 {elapsed:.1f}s (初始化 {t_import:.1f}s)")
    print(f"  📊 请求 {total} 只 → 获取 {fetched} 只 | 命中率 {hit_rate:.1f}%")
    print(f"  📈 共 {total_bars} 条K线 | 平均 {avg_bars:.0f}条/只")
    print(f"  ⚡ 速度 {speed:.1f}只/秒 | {bars_per_sec:.0f}条K线/秒")
    if missed and len(missed) <= 20:
        print(f"  ⚠️  漏掉 {len(missed)} 只: {', '.join(sorted(missed))}")
    elif missed:
        print(f"  ⚠️  漏掉 {len(missed)} 只")
    print(f"  {'─' * 55}\n")


if __name__ == "__main__":
    main()
