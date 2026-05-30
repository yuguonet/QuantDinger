#!/usr/bin/env python3
"""
全周期 · 全接口 Provider 覆盖测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
串行测试所有 Provider 的所有接口，生成详细报表。

用法:
  python test_full_coverage.py                    # 默认测 3 只股
  python test_full_coverage.py --codes 600519,000001,300750
  python test_full_coverage.py --sources tencent,sina
  python test_full_coverage.py --timeframes 15m,1D

测试内容:
  1. fetch_kline        — 各周期 × 各源 × 多只股票
  2. fetch_market_kline — 各周期 × 各源（全市场批量）
  3. fetch_ticker       — 单只实时行情
  4. fetch_batch_quotes — 批量实时行情

数据一致性检查:
  - 返回格式是否统一（time/open/high/low/close/volume）
  - 数据是否为空/重复/乱序
  - 前复权是否生效（价格合理性）
"""

import sys
import os
import time
from datetime import datetime

# 确保能找到 app 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data_sources.provider import get_providers, autodiscover, NotSupportedResult

# ================================================================
# 测试配置 — 按需修改
# ================================================================

# 默认测试股票（选几只典型的）
DEFAULT_CODES = ["600519", "000001", "300750"]

# 要测的周期
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "1D"]

# fetch_kline 每次拉多少条
KLINE_COUNT = 30

# fetch_market_kline 的 start_date（用最近的日期避免数据量太大）
MARKET_START = "2026-05-08"


# ================================================================
# 工具函数
# ================================================================

def ts():
    """当前时间戳字符串"""
    return datetime.now().strftime("%H:%M:%S")


def elapsed_str(seconds):
    """耗时格式化"""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    return f"{seconds:.2f}s"


def is_not_supported(result):
    """判断返回值是否为 NotSupported"""
    return isinstance(result, NotSupportedResult)


def is_valid_bar(bar):
    """判断单条K线是否格式正确"""
    if not isinstance(bar, dict):
        return False
    required = {"time", "open", "high", "low", "close", "volume"}
    return required.issubset(bar.keys())


def validate_kline_data(bars, source_name, tf, code):
    """
    验证K线数据的统一性和质量。

    检查项:
      - 非空列表
      - 每条 bar 格式统一（time/open/high/low/close/volume）
      - 时间戳是整数（Unix秒）
      - OHLC 逻辑正确（high >= low）
      - 无重复时间戳
      - 时间升序排列
      - 前复权价格合理性（> 0）

    返回:
      (is_valid, issues_list)
    """
    issues = []

    if not bars:
        return True, []  # 空不算错（可能是该源不支持）

    if not isinstance(bars, list):
        return False, ["返回值不是 list"]

    for i, bar in enumerate(bars):
        # 格式检查
        if not is_valid_bar(bar):
            issues.append(f"第{i}条格式错误: {list(bar.keys()) if isinstance(bar, dict) else type(bar)}")
            continue

        # 时间戳类型
        t = bar["time"]
        if not isinstance(t, (int, float)):
            issues.append(f"第{i}条 time 不是数字: {type(t).__name__}={t}")

        # OHLC 逻辑
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if h > 0 and l > 0 and h < l:
            issues.append(f"第{i}条 high({h}) < low({l})")

        # 价格合理性（前复权后应 > 0）
        if c <= 0 and c != 0:
            issues.append(f"第{i}条 close({c}) <= 0")

    # 重复时间戳
    times = [bar.get("time") for bar in bars if isinstance(bar, dict)]
    if len(times) != len(set(times)):
        issues.append(f"有重复时间戳 ({len(times)}条, {len(set(times))}个唯一)")

    # 时间排序
    if times and times != sorted(times):
        issues.append("时间未升序排列")

    is_valid = len(issues) == 0
    return is_valid, issues


# ================================================================
# 测试结果收集
# ================================================================

class TestResult:
    """单次测试的结果记录"""

    def __init__(self, source, interface, timeframe, code, status, count, elapsed, issues=None):
        self.source = source          # 数据源名称
        self.interface = interface    # 接口名称
        self.timeframe = timeframe    # 周期
        self.code = code              # 股票代码（fetch_market_kline 用 "MARKET"）
        self.status = status          # "OK" / "EMPTY" / "NOT_SUPPORTED" / "ERROR" / "INVALID"
        self.count = count            # 返回数据条数
        self.elapsed = elapsed        # 耗时（秒）
        self.issues = issues or []    # 数据质量问题

    def __repr__(self):
        return f"<{self.source} {self.interface} {self.timeframe} {self.code}={self.status}>"


results: list = []  # 全局结果列表


def record(source, interface, timeframe, code, status, count=0, elapsed=0.0, issues=None):
    """记录一条测试结果"""
    r = TestResult(source, interface, timeframe, code, status, count, elapsed, issues)
    results.append(r)


# ================================================================
# 测试逻辑 — 每个函数对应一个测试场景
# ================================================================

def test_fetch_kline(providers, codes, timeframes):
    """
    测试 fetch_kline 接口。

    对每个 Provider × 每个周期 × 每只股票，调用一次 fetch_kline，
    验证返回数据格式和内容。
    """
    print(f"\n{'='*60}")
    print(f"  TEST 1: fetch_kline")
    print(f"  {len(providers)} 源 × {len(timeframes)} 周期 × {len(codes)} 只股票")
    print(f"{'='*60}")

    for p in providers:
        print(f"\n  📡 {p.name} (priority={p.priority})")
        for tf in timeframes:
            for code in codes:
                # 检查该源是否声明支持此周期
                if tf not in p.capabilities.get("kline_tf", set()):
                    record(p.name, "fetch_kline", tf, code, "NOT_SUPPORTED")
                    continue

                # 调用 fetch_kline
                t0 = time.time()
                try:
                    data = p.fetch_kline(code, tf, KLINE_COUNT)
                except Exception as e:
                    elapsed = time.time() - t0
                    record(p.name, "fetch_kline", tf, code, "ERROR", elapsed=elapsed)
                    print(f"    ❌ {tf} {code} 异常: {e}")
                    continue
                elapsed = time.time() - t0

                # 判断结果
                if is_not_supported(data):
                    record(p.name, "fetch_kline", tf, code, "NOT_SUPPORTED", elapsed=elapsed)
                    print(f"    ⚪ {tf} {code} NotSupported ({elapsed_str(elapsed)})")
                    continue

                if not data:
                    record(p.name, "fetch_kline", tf, code, "EMPTY", elapsed=elapsed)
                    print(f"    ⚪ {tf} {code} 空数据 ({elapsed_str(elapsed)})")
                    continue

                # 数据验证
                valid, issues = validate_kline_data(data, p.name, tf, code)
                count = len(data)
                if valid:
                    record(p.name, "fetch_kline", tf, code, "OK", count=count, elapsed=elapsed)
                    print(f"    ✅ {tf} {code} {count}条 ({elapsed_str(elapsed)})")
                else:
                    record(p.name, "fetch_kline", tf, code, "INVALID", count=count, elapsed=elapsed, issues=issues)
                    print(f"    ⚠️  {tf} {code} {count}条 但有问题: {'; '.join(issues[:3])}")


def test_fetch_market_kline(providers, timeframes):
    """
    测试 fetch_market_kline 接口。

    对每个 Provider × 每个周期，调用一次 fetch_market_kline（全市场批量），
    验证返回数据格式和数量。
    """
    print(f"\n{'='*60}")
    print(f"  TEST 2: fetch_market_kline")
    print(f"  {len(providers)} 源 × {len(timeframes)} 周期")
    print(f"{'='*60}")

    for p in providers:
        print(f"\n  📡 {p.name}")
        for tf in timeframes:
            if tf not in p.capabilities.get("kline_tf", set()):
                record(p.name, "fetch_market_kline", tf, "MARKET", "NOT_SUPPORTED")
                continue

            t0 = time.time()
            try:
                data = p.fetch_market_kline(
                    timeframe=tf,
                    count=KLINE_COUNT,
                    timeout=30,
                    start_date=MARKET_START,
                )
            except Exception as e:
                elapsed = time.time() - t0
                record(p.name, "fetch_market_kline", tf, "MARKET", "ERROR", elapsed=elapsed)
                print(f"    ❌ {tf} 异常: {e}")
                continue
            elapsed = time.time() - t0

            if is_not_supported(data):
                record(p.name, "fetch_market_kline", tf, "MARKET", "NOT_SUPPORTED", elapsed=elapsed)
                print(f"    ⚪ {tf} NotSupported ({elapsed_str(elapsed)})")
                continue

            if not data:
                record(p.name, "fetch_market_kline", tf, "MARKET", "EMPTY", elapsed=elapsed)
                print(f"    ⚪ {tf} 空数据 ({elapsed_str(elapsed)})")
                continue

            # 抽样验证（取前3只）
            stock_count = len(data)
            sample_issues = []
            sample_ok = 0
            for code, bars in list(data.items())[:3]:
                valid, issues = validate_kline_data(bars, p.name, tf, code)
                if valid:
                    sample_ok += 1
                else:
                    sample_issues.extend(issues[:2])

            if sample_issues:
                record(p.name, "fetch_market_kline", tf, "MARKET", "INVALID",
                       count=stock_count, elapsed=elapsed, issues=sample_issues)
                print(f"    ⚠️  {tf} {stock_count}只 但抽样有问题: {'; '.join(sample_issues[:3])}")
            else:
                record(p.name, "fetch_market_kline", tf, "MARKET", "OK",
                       count=stock_count, elapsed=elapsed)
                print(f"    ✅ {tf} {stock_count}只 ({elapsed_str(elapsed)})")


def test_fetch_ticker(providers, codes):
    """
    测试 fetch_ticker 接口。

    对每个 Provider × 每只股票，调用一次 fetch_ticker，
    验证返回数据格式。
    """
    print(f"\n{'='*60}")
    print(f"  TEST 3: fetch_ticker")
    print(f"  {len(providers)} 源 × {len(codes)} 只股票")
    print(f"{'='*60}")

    for p in providers:
        print(f"\n  📡 {p.name}")
        for code in codes:
            t0 = time.time()
            try:
                data = p.fetch_ticker(code)
            except Exception as e:
                elapsed = time.time() - t0
                record(p.name, "fetch_ticker", "-", code, "ERROR", elapsed=elapsed)
                print(f"    ❌ {code} 异常: {e}")
                continue
            elapsed = time.time() - t0

            if is_not_supported(data):
                record(p.name, "fetch_ticker", "-", code, "NOT_SUPPORTED", elapsed=elapsed)
                print(f"    ⚪ {code} NotSupported ({elapsed_str(elapsed)})")
                continue

            if not data:
                record(p.name, "fetch_ticker", "-", code, "EMPTY", elapsed=elapsed)
                print(f"    ⚪ {code} 空数据 ({elapsed_str(elapsed)})")
                continue

            # 验证字段
            required = {"last", "change", "changePercent", "high", "low", "open", "previousClose"}
            missing = required - set(data.keys())
            if missing:
                record(p.name, "fetch_ticker", "-", code, "INVALID", elapsed=elapsed,
                       issues=[f"缺少字段: {missing}"])
                print(f"    ⚠️  {code} 缺少字段: {missing}")
            elif data.get("last", 0) <= 0:
                record(p.name, "fetch_ticker", "-", code, "INVALID", elapsed=elapsed,
                       issues=[f"last={data.get('last')}"])
                print(f"    ⚠️  {code} last={data.get('last')}")
            else:
                last = data["last"]
                chg = data.get("changePercent", 0)
                record(p.name, "fetch_ticker", "-", code, "OK", elapsed=elapsed)
                print(f"    ✅ {code} last={last} chg={chg}% ({elapsed_str(elapsed)})")


def test_fetch_batch_quotes(providers, codes):
    """
    测试 fetch_batch_quotes 接口。

    对每个 Provider，传入多只股票代码，一次获取批量行情。
    """
    print(f"\n{'='*60}")
    print(f"  TEST 4: fetch_batch_quotes")
    print(f"  {len(providers)} 源 × {len(codes)} 只股票")
    print(f"{'='*60}")

    for p in providers:
        print(f"\n  📡 {p.name}")
        t0 = time.time()
        try:
            data = p.fetch_batch_quotes(codes)
        except Exception as e:
            elapsed = time.time() - t0
            record(p.name, "fetch_batch_quotes", "-", "BATCH", "ERROR", elapsed=elapsed)
            print(f"    ❌ 异常: {e}")
            continue
        elapsed = time.time() - t0

        if is_not_supported(data):
            record(p.name, "fetch_batch_quotes", "-", "BATCH", "NOT_SUPPORTED", elapsed=elapsed)
            print(f"    ⚪ NotSupported ({elapsed_str(elapsed)})")
            continue

        if not data:
            record(p.name, "fetch_batch_quotes", "-", "BATCH", "EMPTY", elapsed=elapsed)
            print(f"    ⚪ 空数据 ({elapsed_str(elapsed)})")
            continue

        # 检查返回了多少只
        got = len(data)
        # 检查每只的格式
        ok_count = 0
        issues = []
        for code, q in data.items():
            if not isinstance(q, dict):
                issues.append(f"{code}: 不是 dict")
                continue
            if q.get("last", 0) <= 0:
                issues.append(f"{code}: last={q.get('last')}")
                continue
            ok_count += 1

        if issues:
            record(p.name, "fetch_batch_quotes", "-", "BATCH", "INVALID",
                   count=got, elapsed=elapsed, issues=issues)
            print(f"    ⚠️  {got}只返回, {ok_count}只有效, 问题: {'; '.join(issues[:3])}")
        else:
            record(p.name, "fetch_batch_quotes", "-", "BATCH", "OK",
                   count=got, elapsed=elapsed)
            print(f"    ✅ {got}只 ({elapsed_str(elapsed)})")


# ================================================================
# 报表生成
# ================================================================

def print_report():
    """
    打印详细测试报表。

    包含:
      1. 总览统计
      2. 按源汇总
      3. 按接口汇总
      4. 问题清单
      5. 耗时排行
    """
    print(f"\n{'='*75}")
    print(f"  📊 测试报表 | {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*75}")

    total = len(results)
    ok = sum(1 for r in results if r.status == "OK")
    empty = sum(1 for r in results if r.status == "EMPTY")
    not_supported = sum(1 for r in results if r.status == "NOT_SUPPORTED")
    invalid = sum(1 for r in results if r.status == "INVALID")
    error = sum(1 for r in results if r.status == "ERROR")

    # ── 1. 总览 ──
    print(f"\n  ┌─ 总览 ──────────────────────────────────────┐")
    print(f"  │  总测试: {total:>4}                              │")
    print(f"  │  ✅ OK:          {ok:>4}  ({ok/total*100:.1f}%)" if total else "  │  ✅ OK:          0")
    print(f"  │  ⚪ 空数据:      {empty:>4}  ({empty/total*100:.1f}%)" if total else "  │  ⚪ 空数据:      0")
    print(f"  │  ⚪ NotSupport:  {not_supported:>4}  ({not_supported/total*100:.1f}%)" if total else "  │  ⚪ NotSupport:  0")
    print(f"  │  ⚠️  数据问题:    {invalid:>4}  ({invalid/total*100:.1f}%)" if total else "  │  ⚠️  数据问题:    0")
    print(f"  │  ❌ 异常:        {error:>4}  ({error/total*100:.1f}%)" if total else "  │  ❌ 异常:        0")
    print(f"  └──────────────────────────────────────────────┘")

    # ── 2. 按源汇总 ──
    sources = sorted(set(r.source for r in results))
    print(f"\n  ┌─ 按源汇总 ──────────────────────────────────┐")
    print(f"  │  {'源':<14} {'OK':>4} {'EMPTY':>5} {'NS':>4} {'INV':>4} {'ERR':>4} │")
    print(f"  │  {'─'*14} {'─'*4} {'─'*5} {'─'*4} {'─'*4} {'─'*4} │")
    for s in sources:
        sr = [r for r in results if r.source == s]
        s_ok = sum(1 for r in sr if r.status == "OK")
        s_emp = sum(1 for r in sr if r.status == "EMPTY")
        s_ns = sum(1 for r in sr if r.status == "NOT_SUPPORTED")
        s_inv = sum(1 for r in sr if r.status == "INVALID")
        s_err = sum(1 for r in sr if r.status == "ERROR")
        print(f"  │  {s:<14} {s_ok:>4} {s_emp:>5} {s_ns:>4} {s_inv:>4} {s_err:>4} │")
    print(f"  └──────────────────────────────────────────────┘")

    # ── 3. 按接口汇总 ──
    interfaces = ["fetch_kline", "fetch_market_kline", "fetch_ticker", "fetch_batch_quotes"]
    print(f"\n  ┌─ 按接口汇总 ────────────────────────────────┐")
    print(f"  │  {'接口':<22} {'OK':>4} {'TOTAL':>5} {'通过率':>8} │")
    print(f"  │  {'─'*22} {'─'*4} {'─'*5} {'─'*8} │")
    for iface in interfaces:
        ir = [r for r in results if r.interface == iface]
        i_ok = sum(1 for r in ir if r.status == "OK")
        i_total = len(ir)
        rate = f"{i_ok/i_total*100:.0f}%" if i_total else "N/A"
        print(f"  │  {iface:<22} {i_ok:>4} {i_total:>5} {rate:>8} │")
    print(f"  └──────────────────────────────────────────────┘")

    # ── 4. 问题清单 ──
    problems = [r for r in results if r.status in ("INVALID", "ERROR")]
    if problems:
        print(f"\n  ┌─ 问题清单 ({len(problems)}条) ────────────────────────────┐")
        for r in problems:
            icon = "⚠️" if r.status == "INVALID" else "❌"
            print(f"  │  {icon} {r.source:<12} {r.interface:<20} {r.timeframe:<4} {r.code:<8}")
            for issue in r.issues[:2]:
                print(f"  │    → {issue}")
        print(f"  └──────────────────────────────────────────────┘")
    else:
        print(f"\n  ✅ 无问题清单 — 全部通过")

    # ── 5. 耗时排行（仅 OK 的） ──
    ok_results = [r for r in results if r.status == "OK" and r.elapsed > 0]
    if ok_results:
        ok_results.sort(key=lambda r: -r.elapsed)
        print(f"\n  ┌─ 耗时排行 TOP 10 ────────────────────────────┐")
        for r in ok_results[:10]:
            print(f"  │  {elapsed_str(r.elapsed):>8}  {r.source:<12} {r.interface:<20} {r.timeframe:<4} {r.code:<8}")
        print(f"  └──────────────────────────────────────────────┘")

    # ── 6. 周期覆盖矩阵 ──
    kline_results = [r for r in results if r.interface == "fetch_kline" and r.code == DEFAULT_CODES[0]]
    if kline_results:
        tfs = sorted(set(r.timeframe for r in kline_results))
        srcs = sorted(set(r.source for r in kline_results))
        print(f"\n  ┌─ 周期覆盖矩阵 ({DEFAULT_CODES[0]}) ─────────────────────┐")
        header = f"  │  {'源':<14}" + "".join(f" {tf:>5}" for tf in tfs) + " │"
        print(header)
        sep = f"  │  {'─'*14}" + "".join(f" {'─'*5}" for tf in tfs) + " │"
        print(sep)
        for s in srcs:
            row = f"  │  {s:<14}"
            for tf in tfs:
                match = [r for r in kline_results if r.source == s and r.timeframe == tf]
                if not match:
                    row += "     -"
                elif match[0].status == "OK":
                    row += f" {match[0].count:>4}✓"
                elif match[0].status == "NOT_SUPPORTED":
                    row += "    ⚪"
                elif match[0].status == "EMPTY":
                    row += "    ·"
                else:
                    row += "    ✗"
            row += " │"
            print(row)
        print(f"  └──────────────────────────────────────────────┘")

    print(f"\n  ✓=OK  ⚪=NotSupported  ·=Empty  ✗=Error/Invalid  -=未测\n")


# ================================================================
# 主入口
# ================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="全周期全接口 Provider 覆盖测试")
    parser.add_argument("--codes", type=str, default="",
                        help="股票代码,逗号分隔 (默认: {})".format(",".join(DEFAULT_CODES)))
    parser.add_argument("--sources", type=str, default="",
                        help="只测指定源,逗号分隔 (默认: 全部)")
    parser.add_argument("--timeframes", type=str, default="",
                        help="只测指定周期,逗号分隔 (默认: {})".format(",".join(ALL_TIMEFRAMES)))
    args = parser.parse_args()

    # 解析参数
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else DEFAULT_CODES
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()] if args.timeframes else ALL_TIMEFRAMES

    # 自动发现 Provider
    autodiscover()

    # 获取所有 kline 类型的 Provider
    all_providers = get_providers(capability="kline")

    if args.sources:
        names = [s.strip() for s in args.sources.split(",")]
        providers = [p for p in all_providers if p.name in names]
    else:
        providers = all_providers

    if not providers:
        print("  ❌ 无可用 Provider")
        return

    # ── 开始测试 ──
    print(f"\n  🚀 全周期全接口测试 | {ts()}")
    print(f"  股票: {', '.join(codes)}")
    print(f"  周期: {', '.join(timeframes)}")
    print(f"  源:   {', '.join(p.name for p in providers)}")
    print(f"  count={KLINE_COUNT} start_date={MARKET_START}")

    t_total = time.time()

    # 串行执行所有测试
    test_fetch_kline(providers, codes, timeframes)
    test_fetch_market_kline(providers, timeframes)
    test_fetch_ticker(providers, codes)
    test_fetch_batch_quotes(providers, codes)

    total_elapsed = time.time() - t_total

    # 打印报表
    print_report()

    print(f"  ⏱ 总耗时: {elapsed_str(total_elapsed)}")
    print()


if __name__ == "__main__":
    main()
