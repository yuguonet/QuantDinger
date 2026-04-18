#!/usr/bin/env python3
"""
A股数据源集成测试 — 验证腾讯/新浪/东方财富数据源
用法: python3 test_cn_datasources.py [股票代码]
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.data_sources.tencent import (
    normalize_cn_code,
    fetch_quote,
    parse_quote_to_ticker,
    fetch_kline,
    tencent_kline_rows_to_dicts,
)
from app.data_sources.sina import fetch_sina_quote, fetch_sina_kline, sina_kline_to_ticker
from app.data_sources.eastmoney import fetch_eastmoney_quote, fetch_eastmoney_kline, eastmoney_kline_to_ticker
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker


def banner(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def test_tencent(code: str):
    banner("📡 腾讯财经数据源")
    tencent_code = normalize_cn_code(code)
    print(f"  标准化: {code} → {tencent_code}")

    t0 = time.time()
    parts = fetch_quote(tencent_code)
    elapsed = time.time() - t0
    if parts:
        t = parse_quote_to_ticker(parts)
        print(f"  ✅ 实时行情 ({elapsed:.2f}s): {t.get('name','')} "
              f"价格={t.get('last',0)} 涨跌={t.get('changePercent',0)}%")
    else:
        print(f"  ❌ 实时行情失败 ({elapsed:.2f}s)")

    t0 = time.time()
    rows = fetch_kline(tencent_code, period="day", count=5, adj="qfq")
    bars = tencent_kline_rows_to_dicts(rows) if rows else []
    elapsed = time.time() - t0
    if bars:
        print(f"  ✅ 日K线 ({elapsed:.2f}s): {len(bars)} 条, 最新: C={bars[-1]['close']}")
    else:
        print(f"  ❌ 日K线失败 ({elapsed:.2f}s)")


def test_eastmoney(code: str):
    banner("📊 东方财富数据源")

    t0 = time.time()
    quote = fetch_eastmoney_quote(code)
    elapsed = time.time() - t0
    if quote:
        print(f"  ✅ 实时行情 ({elapsed:.2f}s): {quote.get('name','')} "
              f"价格={quote.get('last',0)} 涨跌={quote.get('changePercent',0)}%")
    else:
        print(f"  ❌ 实时行情失败 ({elapsed:.2f}s)")

    t0 = time.time()
    bars = fetch_eastmoney_kline(code, period="1D", count=5, adj="qfq")
    elapsed = time.time() - t0
    if bars:
        print(f"  ✅ 日K线 ({elapsed:.2f}s): {len(bars)} 条, 最新: C={bars[-1]['close']}")
    else:
        print(f"  ❌ 日K线失败 ({elapsed:.2f}s)")


def test_sina(code: str):
    banner("📰 新浪财经数据源")

    t0 = time.time()
    quote = fetch_sina_quote(code)
    elapsed = time.time() - t0
    if quote:
        print(f"  ✅ 实时行情 ({elapsed:.2f}s): {quote.get('name','')} "
              f"价格={quote.get('last',0)} 涨跌={quote.get('changePercent',0)}%")
    else:
        print(f"  ❌ 实时行情失败 ({elapsed:.2f}s)")

    t0 = time.time()
    bars = fetch_sina_kline(code, count=5)
    elapsed = time.time() - t0
    if bars:
        print(f"  ✅ 日K线 ({elapsed:.2f}s): {len(bars)} 条, 最新: C={bars[-1]['close']}")
    else:
        print(f"  ❌ 日K线失败 ({elapsed:.2f}s)")


def test_cnstock_datasource(code: str):
    banner("🏭 CNStockDataSource 集成测试")

    from app.data_sources.cn_stock import CNStockDataSource
    ds = CNStockDataSource()

    t0 = time.time()
    ticker = ds.get_ticker(code)
    elapsed = time.time() - t0
    print(f"  get_ticker ({elapsed:.2f}s): last={ticker.get('last',0)}, "
          f"change={ticker.get('changePercent',0)}%")

    t0 = time.time()
    kline = ds.get_kline(code, "1D", 5)
    elapsed = time.time() - t0
    print(f"  get_kline  1D ({elapsed:.2f}s): {len(kline)} bars")
    if kline:
        last = kline[-1]
        print(f"    Latest: O={last['open']} H={last['high']} "
              f"L={last['low']} C={last['close']}")


def test_circuit_breaker():
    banner("🔌 熔断器状态")
    cb = get_realtime_circuit_breaker()
    status = cb.get_status()
    if status:
        for source, info in status.items():
            print(f"  {source}: {info['state']} (failures={info['failures']})")
    else:
        print("  无熔断记录（全部正常）")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    stock_code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(f"\n🧪 A股数据源测试 — {stock_code}")

    test_tencent(stock_code)
    test_eastmoney(stock_code)
    test_sina(stock_code)
    test_circuit_breaker()
    test_cnstock_datasource(stock_code)

    print("\n✅ 测试完成\n")
