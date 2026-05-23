#!/usr/bin/env python3
"""
实盘数据测试脚本 — 从新浪/腾讯拉前复权K线，跑 dragon V1 回测

用法:
  # 测试几只近期连板股
  python test_dragon_live.py

  # 指定股票
  python test_dragon_live.py --codes 600519,000001,300750

  # 拉更长数据
  python test_dragon_live.py --days 500
"""
from __future__ import annotations
import os, sys, json, time, re
import argparse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

# ================================================================
# 零依赖数据拉取 (纯 requests, 无需数据库/pytdx)
# ================================================================

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
})

def _code_to_sina(code: str) -> str:
    """股票代码 → 新浪格式 (sh600519 / sz000001)"""
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith(("6", "5")):
        return f"sh{c}"
    elif c.startswith(("0", "3", "2")):
        return f"sz{c}"
    elif c.startswith("68"):
        return f"sh{c}"
    return ""

def _code_to_tencent(code: str) -> str:
    """股票代码 → 腾讯格式 (sh600519 / sz000001)"""
    return _code_to_sina(code)  # 格式一致

def fetch_sina_kline(code: str, count: int = 300, timeout: int = 10) -> List[Dict]:
    """从新浪拉日线K线 (不复权)"""
    sc = _code_to_sina(code)
    if not sc:
        return []
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sc, "scale": 240, "ma": "no", "datalen": min(count, 2000)}
    try:
        resp = _SESSION.get(url, params=params, timeout=timeout,
                            headers={"Referer": "https://finance.sina.com.cn/"})
        data = resp.json()
        if not isinstance(data, list):
            return []
        bars = []
        for item in data:
            try:
                bars.append({
                    "time": str(item["day"])[:10],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "volume": float(item["volume"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        bars.sort(key=lambda x: x["time"])
        return bars
    except Exception as e:
        print(f"  [新浪] {code} 拉取失败: {e}")
        return []

def fetch_tencent_kline(code: str, count: int = 300, adj: str = "qfq", timeout: int = 10) -> List[Dict]:
    """从腾讯拉日线K线 (前复权)"""
    tc = _code_to_tencent(code)
    if not tc:
        return []
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{tc},day,,,{count},{adj}"}
    try:
        resp = _SESSION.get(url, params=params, timeout=timeout,
                            headers={"Referer": "https://gu.qq.com/"})
        data = resp.json()
        if not isinstance(data, dict) or int(data.get("code", 0)) != 0:
            return []
        root = (data.get("data") or {}).get(tc)
        if not isinstance(root, dict):
            return []
        # 腾讯返回 qfqday / day 等 key
        rows = root.get("qfqday") or root.get("day") or []
        if not isinstance(rows, list):
            return []
        bars = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            try:
                # 腾讯 fqkline 返回顺序: [date, open, close, high, low, volume]
                # 注意: 第2列是close, 第3列是high, 第4列是low!
                bars.append({
                    "time": str(r[0])[:10],
                    "open": float(r[1]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "close": float(r[2]),
                    "volume": float(r[5]) * 100,  # 腾讯volume是手, 转股
                })
            except (ValueError, TypeError, IndexError):
                continue
        bars.sort(key=lambda x: x["time"])
        return bars
    except Exception as e:
        print(f"  [腾讯] {code} 拉取失败: {e}")
        return []

def fetch_kline(code: str, count: int = 300, source: str = "auto") -> List[Dict]:
    """拉取K线, 自动选择数据源"""
    if source == "sina":
        return fetch_sina_kline(code, count)
    elif source == "tencent":
        return fetch_tencent_kline(code, count)
    else:  # auto: 腾讯前复权优先
        bars = fetch_tencent_kline(code, count)
        if bars:
            return bars
        return fetch_sina_kline(code, count)


# ================================================================
# V1 策略核心逻辑 (独立版, 无需导入 optimizer)
# ================================================================

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("30") or c.startswith("68"):
        return "gem_star"
    return "main"

def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

BOARD_PARAMS = {
    "main":     {"threshold": 0.098, "min_streak": 2, "max_d1_gap": 2.0},
    "gem_star": {"threshold": 0.198, "min_streak": 2, "max_d1_gap": 5.0},
}

def find_limit_up_days(bars: List[Dict], threshold: float) -> List[int]:
    """找到所有涨停日的索引"""
    limit_idxs = []
    for i in range(1, len(bars)):
        close = bars[i]["close"]
        prev_close = bars[i-1]["close"]
        if prev_close <= 0:
            continue
        ret = (close / prev_close - 1)
        if ret >= threshold * 0.98:  # 浮点容差
            limit_idxs.append(i)
    return limit_idxs

def find_first_limit_ups(bars: List[Dict], threshold: float) -> List[int]:
    """找到所有第一板涨停 (非连板中间)"""
    limit_idxs = find_limit_up_days(bars, threshold)
    first_limits = []
    for idx in limit_idxs:
        # 检查前一日是否也涨停
        if idx >= 1:
            prev_close = bars[idx-1]["close"]
            prev2_close = bars[idx-2]["close"] if idx >= 2 else bars[idx-1]["close"]
            if prev2_close > 0:
                prev_ret = (prev_close / prev2_close - 1)
                if prev_ret >= threshold * 0.98:
                    continue  # 前一日也涨停, 跳过 (不是第一板)
        first_limits.append(idx)
    return first_limits

def analyze_dragon_v1(bars: List[Dict], code: str, min_vol_ratio: float = 2.0,
                      max_upper_shadow: float = 0.5) -> List[Dict]:
    """V1 策略分析: 第一板筛选 → D+1 开盘买入 → 持有5天"""
    board_type = get_board_type(code)
    params = BOARD_PARAMS[board_type]
    threshold = params["threshold"]
    max_d1_gap = params["max_d1_gap"]

    first_limits = find_first_limit_ups(bars, threshold)
    trades = []

    for fl_idx in first_limits:
        if fl_idx < 2 or fl_idx + 1 >= len(bars):
            continue

        fl = bars[fl_idx]
        fl_prev = bars[fl_idx - 1]
        fl_prev2 = bars[fl_idx - 2]

        fl_close = fl["close"]
        fl_high = fl["high"]
        fl_low = fl["low"]
        fl_vol = fl["volume"]
        fl_prev_close = fl_prev["close"]
        fl_prev_vol = fl_prev["volume"]
        fl_prev2_close = fl_prev2["close"]

        if fl_prev_close <= 0 or fl_close <= 0 or fl_prev2_close <= 0:
            continue

        # 1. 排除一字板 (振幅 < 0.2%)
        bar_range = (fl_high - fl_low) / fl_prev2_close * 100
        if bar_range < 0.2:
            continue

        # 2. 量比 > 2x
        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        if vol_ratio < min_vol_ratio:
            continue

        # 3. 上影线 < 0.5%
        upper_shadow = (fl_high - fl_close) / fl_prev2_close * 100
        if upper_shadow >= max_upper_shadow:
            continue

        # D+1 开盘
        d1 = bars[fl_idx + 1]
        d1_open = d1["open"]
        if d1_open <= 0:
            continue

        d1_gap = (d1_open / fl_close - 1) * 100
        if d1_gap > max_d1_gap:
            continue

        # 持仓5天回测
        entry_price = d1_open
        best_return = 0
        exit_price = entry_price
        exit_day = 0
        peak_price = entry_price

        for d in range(1, 6):
            if fl_idx + 1 + d >= len(bars):
                break
            day_bar = bars[fl_idx + 1 + d]
            day_close = day_bar["close"]
            day_high = day_bar["high"]
            day_low = day_bar["low"]

            # 更新峰值
            if day_high > peak_price:
                peak_price = day_high

            # 追踪止损: 从峰值回撤超过6%
            trailing_stop = peak_price * 0.94
            if day_low <= trailing_stop:
                exit_price = trailing_stop
                exit_day = d
                break

            # 止损: 跌破买入价8%
            if day_low <= entry_price * 0.92:
                exit_price = entry_price * 0.92
                exit_day = d
                break

            # 止盈: 涨超15%
            if day_high >= entry_price * 1.15:
                exit_price = entry_price * 1.15
                exit_day = d
                break

            exit_price = day_close
            exit_day = d

        ret_pct = (exit_price / entry_price - 1) * 100

        trades.append({
            "code": code,
            "board": get_board_name(code),
            "first_limit_date": fl["time"],
            "d1_date": d1["time"],
            "entry_price": round(entry_price, 3),
            "exit_price": round(exit_price, 3),
            "exit_day": exit_day,
            "return_pct": round(ret_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "upper_shadow": round(upper_shadow, 4),
            "d1_gap": round(d1_gap, 2),
            "fl_close": round(fl_close, 3),
            "bar_range": round(bar_range, 2),
        })

    return trades


# ================================================================
# 主程序
# ================================================================

# 测试用: 近期有过连板的股票 (随机挑几只)
TEST_CODES = [
    # 沪主板
    "600519", "601398", "600036", "601318", "600276",
    "601166", "600030", "601888", "600900", "603259",
    # 深主板
    "000001", "000002", "000858", "000333", "002475",
    "002594", "000725", "002415", "000063", "002230",
    # 创业板
    "300750", "300059", "300124", "300033", "300274",
    "300122", "300015", "300142", "300347", "300498",
    # 科创板
    "688981", "688012", "688111", "688036", "688599",
]

def main():
    parser = argparse.ArgumentParser(description="Dragon V1 实盘数据测试")
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="拉取天数")
    parser.add_argument("--source", type=str, default="auto", choices=["auto", "sina", "tencent"])
    parser.add_argument("--min-vol-ratio", type=float, default=2.0)
    parser.add_argument("--max-upper-shadow", type=float, default=0.5)
    parser.add_argument("--all-trades", action="store_true", help="显示所有交易明细")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    source_name = {"auto": "腾讯/新浪", "sina": "新浪", "tencent": "腾讯"}[args.source]

    print(f"=" * 70)
    print(f"Dragon V1 实盘数据测试")
    print(f"数据源: {source_name} | 天数: {args.days} | 股票数: {len(codes)}")
    print(f"参数: min_vol_ratio={args.min_vol_ratio}, max_upper_shadow={args.max_upper_shadow}")
    print(f"=" * 70)

    all_trades = []
    success = 0
    failed = 0

    for i, code in enumerate(codes):
        print(f"\n[{i+1}/{len(codes)}] {code} ({get_board_name(code)})", end=" ", flush=True)
        bars = fetch_kline(code, args.days, args.source)
        if not bars:
            print("❌ 无数据")
            failed += 1
            continue

        print(f"✓ {len(bars)}根K线 [{bars[0]['time']} ~ {bars[-1]['time']}]", end=" ")

        trades = analyze_dragon_v1(bars, code, args.min_vol_ratio, args.max_upper_shadow)
        if trades:
            print(f"→ {len(trades)}笔信号")
            all_trades.extend(trades)
        else:
            print("→ 0笔信号")

        success += 1
        time.sleep(0.3)  # 限流

    # ================================================================
    # 汇总
    # ================================================================
    print(f"\n{'=' * 70}")
    print(f"汇总: 成功 {success} 只, 失败 {failed} 只, 共 {len(all_trades)} 笔交易信号")
    print(f"{'=' * 70}")

    if not all_trades:
        print("\n⚠️  无交易信号。可能原因:")
        print("  1. 这些股票近期没有符合条件的第一板涨停")
        print("  2. 可以用 --codes 指定近期有连板的股票")
        print("  3. 可以调低 --min-vol-ratio 或调高 --max-upper-shadow")
        return

    # 按板块统计
    boards = {}
    for t in all_trades:
        b = t["board"]
        if b not in boards:
            boards[b] = {"count": 0, "wins": 0, "returns": []}
        boards[b]["count"] += 1
        if t["return_pct"] > 0:
            boards[b]["wins"] += 1
        boards[b]["returns"].append(t["return_pct"])

    print(f"\n📊 按板块统计:")
    print(f"{'板块':<8} {'笔数':>6} {'胜率':>8} {'均收益':>10} {'盈亏比':>8}")
    print("-" * 45)
    for b, s in sorted(boards.items()):
        wr = s["wins"] / s["count"] * 100 if s["count"] else 0
        avg_ret = sum(s["returns"]) / len(s["returns"])
        wins = [r for r in s["returns"] if r > 0]
        losses = [r for r in s["returns"] if r <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.01
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else 999
        print(f"{b:<8} {s['count']:>6} {wr:>7.1f}% {avg_ret:>+9.2f}% {pl_ratio:>7.2f}")

    # 总计
    total_wr = sum(1 for t in all_trades if t["return_pct"] > 0) / len(all_trades) * 100
    total_avg = sum(t["return_pct"] for t in all_trades) / len(all_trades)
    all_wins = [t["return_pct"] for t in all_trades if t["return_pct"] > 0]
    all_losses = [t["return_pct"] for t in all_trades if t["return_pct"] <= 0]
    avg_w = sum(all_wins) / len(all_wins) if all_wins else 0
    avg_l = abs(sum(all_losses) / len(all_losses)) if all_losses else 0.01
    total_pl = avg_w / avg_l if avg_l > 0 else 999
    print("-" * 45)
    print(f"{'合计':<8} {len(all_trades):>6} {total_wr:>7.1f}% {total_avg:>+9.2f}% {total_pl:>7.2f}")

    # 按连板数统计
    print(f"\n📊 按D0量比分组:")
    vr_groups = {}
    for t in all_trades:
        vr = int(t["vol_ratio"])
        if vr not in vr_groups:
            vr_groups[vr] = []
        vr_groups[vr].append(t["return_pct"])
    for vr in sorted(vr_groups.keys()):
        rets = vr_groups[vr]
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg = sum(rets) / len(rets)
        print(f"  量比>={vr}x: {len(rets)}笔 胜率{wr:.1f}% 均收益{avg:+.2f}%")

    # 交易明细
    if args.all_trades:
        print(f"\n📋 交易明细:")
        print(f"{'代码':<8} {'板块':<6} {'首板日':<12} {'D1日':<12} {'入场':>8} {'出场':>8} {'天数':>4} {'收益':>8} {'量比':>6} {'上影':>6} {'D1缺口':>7}")
        print("-" * 100)
        for t in all_trades:
            print(f"{t['code']:<8} {t['board']:<6} {t['first_limit_date']:<12} {t['d1_date']:<12} "
                  f"{t['entry_price']:>8.3f} {t['exit_price']:>8.3f} {t['exit_day']:>4} "
                  f"{t['return_pct']:>+7.2f}% {t['vol_ratio']:>5.1f}x {t['upper_shadow']:>5.3f}% {t['d1_gap']:>+6.2f}%")

    # 最近的信号
    recent = sorted(all_trades, key=lambda x: x["d1_date"], reverse=True)[:10]
    if recent:
        print(f"\n🔔 最近10笔信号:")
        print(f"{'代码':<8} {'首板日':<12} {'D1买入日':<12} {'入场价':>8} {'收益':>8} {'量比':>6}")
        print("-" * 60)
        for t in recent:
            print(f"{t['code']:<8} {t['first_limit_date']:<12} {t['d1_date']:<12} "
                  f"{t['entry_price']:>8.3f} {t['return_pct']:>+7.2f}% {t['vol_ratio']:>5.1f}x")

    # 导出 JSON
    out_file = "test_dragon_live_result.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, ensure_ascii=False, indent=2)
    print(f"\n💾 结果已导出: {out_file}")


if __name__ == "__main__":
    main()
