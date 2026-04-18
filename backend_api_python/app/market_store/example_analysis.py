#!/usr/bin/env python3
"""
example_analysis.py — 演示如何从 MarketStore 读取数据进行分析

前提: market_store.py 的采集器已在后台运行，或已通过 `python market_store.py fetch` 采集过数据。
"""

from market_store import MarketStore
import pandas as pd

store = MarketStore()


def example_1_basic_query():
    """基础查询：最近 24 小时的所有数据"""
    print("=" * 60)
    print("示例1: 最近 24 小时所有数据")
    print("=" * 60)
    df = store.query(hours=24)
    print(f"共 {len(df)} 条记录")
    print(df.head(10))
    print()


def example_2_filter_by_category():
    """按类别筛选"""
    print("=" * 60)
    print("示例2: 只看加密货币")
    print("=" * 60)
    df = store.query(category="crypto", hours=24)
    print(df[["timestamp", "symbol", "price", "change_pct"]].to_string())
    print()


def example_3_filter_by_symbol():
    """按标的筛选"""
    print("=" * 60)
    print("示例3: BTC 历史走势")
    print("=" * 60)
    df = store.query(category="crypto", symbol="BTC")
    print(df[["timestamp", "price", "change_pct"]].to_string())
    print()


def example_4_date_range():
    """日期范围查询"""
    print("=" * 60)
    print("示例4: 指定日期范围")
    print("=" * 60)
    df = store.query(start="2026-04-10", end="2026-04-18", category="indices")
    if df.empty:
        print("(该日期范围内无数据)")
    else:
        # 按标的分组看最新价格
        latest = df.groupby("symbol").last().reset_index()
        print(latest[["symbol", "name", "price", "change_pct"]].to_string())
    print()


def example_5_sentiment_snapshot():
    """情绪指标快照"""
    print("=" * 60)
    print("示例5: 情绪指标 (恐贪/VIX/DXY)")
    print("=" * 60)
    df = store.query(category="sentiment", hours=48)
    if df.empty:
        print("(无情绪数据)")
    else:
        for _, row in df.iterrows():
            extra = row.get("extra", "")
            print(f"  {row['symbol']:12s} | {row['name']:12s} | "
                  f"值={row['price']:>10} | 涨跌={row['change_pct']:>7}% | {extra}")
    print()


def example_6_pivot_table():
    """数据透视：每个标的的最新价格一览"""
    print("=" * 60)
    print("示例6: 全市场最新价格一览")
    print("=" * 60)
    df = store.query(hours=6)
    if df.empty:
        print("(无数据)")
        return

    latest = df.groupby(["category", "symbol"]).last().reset_index()
    for cat in ["indices", "crypto", "forex", "commodities", "sentiment"]:
        sub = latest[latest["category"] == cat]
        if sub.empty:
            continue
        print(f"\n  [{cat.upper()}]")
        for _, r in sub.iterrows():
            arrow = "↑" if r["change_pct"] > 0 else "↓" if r["change_pct"] < 0 else "→"
            print(f"    {r['symbol']:14s} {r['name']:16s} "
                  f"{r['price']:>12}  {arrow} {r['change_pct']:>7.2f}%")
    print()


def example_7_export_csv():
    """导出 CSV"""
    print("=" * 60)
    print("示例7: 导出最近 7 天数据到 CSV")
    print("=" * 60)
    df = store.query(hours=24 * 7)
    csv_path = "market_export.csv"
    df.to_csv(csv_path, index=False)
    print(f"已导出 {len(df)} 行到 {csv_path}")
    print()


def example_8_stats():
    """存储统计"""
    print("=" * 60)
    print("示例8: 存储统计")
    print("=" * 60)
    import json
    print(json.dumps(store.stats(), indent=2, ensure_ascii=False))
    print()


def example_9_market_score():
    """市场综合评分"""
    print("=" * 60)
    print("示例9: 市场综合评分")
    print("=" * 60)
    from market_scorer import MarketScorer

    df = store.query(hours=6)
    if df.empty:
        print("(无数据，先运行 fetch)")
        return

    scorer = MarketScorer(df)

    # 打印完整报告
    scorer.print_report()
    print()

    # 获取各指标 JSON
    cfgi = scorer.cfgi()
    print(f"  恐贪子分: {cfgi['components']}")
    print()

    mhs = scorer.mhs()
    print(f"  健康度子分: {mhs['components']}")
    print()

    # 获取信号列表
    sigs = scorer.signals()
    if sigs:
        print("  信号:")
        for s in sigs:
            print(f"    {s['emoji']} [{s['type']}] {s['message']}")
    print()


if __name__ == "__main__":
    example_1_basic_query()
    example_2_filter_by_category()
    example_3_filter_by_symbol()
    example_4_date_range()
    example_5_sentiment_snapshot()
    example_6_pivot_table()
    example_7_export_csv()
    example_8_stats()
    example_9_market_score()
