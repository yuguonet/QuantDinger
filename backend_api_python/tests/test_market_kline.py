#!/usr/bin/env python3
"""
A股全市场15分钟K线下载 — 通过 Coordinator 并发获取

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

OUTPUT_DIR = "data/kline_data"


def save_results(result: dict, out_dir: str, timeframe: str):
    """将 coordinate_market_kline 的结果保存为 CSV"""
    subdir = os.path.join(out_dir, timeframe)
    os.makedirs(subdir, exist_ok=True)
    header = "time,open,high,low,close,volume\n"

    saved = 0
    for code, bars in result.items():
        if not bars:
            continue
        fp = os.path.join(subdir, f"{code}.csv")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(header)
            for r in bars:
                t = r.get("time", "")
                if isinstance(t, (int, float)) and t > 1000000000:
                    t = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
                f.write(
                    f"{t},{r.get('open', 0)},{r.get('high', 0)},"
                    f"{r.get('low', 0)},{r.get('close', 0)},{r.get('volume', 0)}\n"
                )
        saved += 1

    return saved


def main():
    p = argparse.ArgumentParser(description="A股全市场K线下载 — Coordinator 并发模式")
    p.add_argument("--limit", type=int, default=0, help="限制股票数量 (0=全部)")
    p.add_argument("--codes", type=str, default="", help="指定代码,逗号分隔")
    p.add_argument("--timeframe", type=str, default="15m", help="K线周期 (15m/1D)")
    p.add_argument("--count", type=int, default=200, help="每只股票数据条数")
    p.add_argument("--adj", type=str, default="qfq", help="复权方式 (qfq/hfq/)")
    p.add_argument("--timeout", type=float, default=300, help="全局超时(秒)")
    p.add_argument("--start-date", type=str, default="", help="起始日期 YYYY-MM-DD")
    p.add_argument("--end-date", type=str, default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--preferred", type=str, default="", help="首选源 (如 tencent)")
    args = p.parse_args()

    print("=" * 65)
    print(f"  A股全市场K线 — Coordinator 并发模式 | {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  周期: {args.timeframe} | 每只: {args.count}条 | 复权: {args.adj or '不复权'}")
    print(f"  超时: {args.timeout}s | 输出: {os.path.abspath(OUTPUT_DIR)}/{args.timeframe}/")
    if args.start_date:
        print(f"  日期范围: {args.start_date} ~ {args.end_date or '今天'}")
    print("=" * 65)

    # ── 导入 & 初始化 ──
    from app.data_sources.provider import autodiscover, _fetch_all_cn_codes
    from app.data_sources.coordinator import get_coordinator
    from app.data_sources.circuit_breaker import get_realtime_circuit_breaker

    autodiscover()

    coord = get_coordinator()
    cb = get_realtime_circuit_breaker()

    # ── 获取股票列表（仅用于 limit/codes 过滤 & 显示） ──
    if args.codes:
        all_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        print("\n  📋 获取股票列表...")
        all_codes = _fetch_all_cn_codes()
        if not all_codes:
            print("  ❌ 获取股票列表失败")
            return
        if args.limit > 0:
            all_codes = all_codes[: args.limit]

    total = len(all_codes)
    print(f"  🚀 开始下载...\n")

    # ── 调用 Coordinator ──
    t0 = time.time()

    # 如果指定了 limit 或 codes，需要临时替换 _fetch_all_cn_codes 的返回
    # 通过环境变量传递给 coordinator（它内部会调 _fetch_all_cn_codes）
    # 这里直接用 coordinate_market_kline 但先手动注入股票列表
    if args.codes or args.limit > 0:
        # 走自定义路径：直接构造任务
        result = _run_with_custom_codes(
            coord, cb, all_codes,
            timeframe=args.timeframe,
            count=args.count if not args.start_date else None,
            adj=args.adj,
            timeout=args.timeout,
            start_date=args.start_date,
            end_date=args.end_date,
            preferred_source=args.preferred,
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

    # ── 保存 ──
    print(f"\n  💾 保存 {len(result)} 只股票数据...")

    # ── 统计 ──
    total_bars = sum(len(bars) for bars in result.values())
    avg_bars = total_bars / len(result) if result else 0
    speed = len(result) / elapsed if elapsed > 0 else 0

    print(f"\n  {'─' * 55}")
    print(f"  ✅ 完成 | 耗时 {elapsed:.1f}s")
    print(f"  📊 获取 {len(result)} 只 | 保存 {saved} 只 | 共 {total_bars} 条K线")
    print(f"  ⚡ 速度 {speed:.1f}只/秒 | 平均 {avg_bars:.0f}条/只")
    print(f"  📁 {os.path.abspath(OUTPUT_DIR)}/{args.timeframe}/")
    print(f"  {'─' * 55}\n")


def _run_with_custom_codes(
    coord, cb, codes, *,
    timeframe, count, adj, timeout, start_date, end_date, preferred_source,
):
    """
    对指定股票列表运行 coordinate_market_kline 的逻辑。
    因为 coordinate_market_kline 内部调 _fetch_all_cn_codes()，
    这里用 monkey-patch 注入自定义列表。
    """
    import app.data_sources.provider as provider_mod

    # monkey-patch: 让 _fetch_all_cn_codes 返回我们指定的列表
    original_fetch = provider_mod._fetch_all_cn_codes
    provider_mod._fetch_all_cn_codes = lambda: codes

    try:
        result = coord.coordinate_market_kline(
            cb=cb,
            market="CNStock",
            timeframe=timeframe,
            count=count,
            adj=adj,
            timeout=timeout,
            preferred_source=preferred_source,
            start_date=start_date,
            end_date=end_date,
        )
    finally:
        provider_mod._fetch_all_cn_codes = original_fetch

    return result


if __name__ == "__main__":
    main()
