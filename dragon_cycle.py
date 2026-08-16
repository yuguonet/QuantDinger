"""
龙虎榜活跃股周期追踪回测

思路:
  1. 维护"龙虎榜常客池" (历史>=N次的股票)
  2. 追踪每只的上榜间隔 (中位数/均值)
  3. 信号: 当某只安静天数 > 其历史中位间隔的K倍 → 进入观察池
  4. 回测: 信号触发后1-2天内是否再次上榜 → 命中率

运行:
  python dragon_cycle.py                    # 默认参数回测
  python dragon_cycle.py --min-count 5      # 最少上榜5次
  python dragon_cycle.py --k 1.5            # 安静天>1.5倍中位间隔
  python dragon_cycle.py --detail           # 输出每日明细
  python dragon_cycle.py --scan             # 扫描当前候选
"""
from __future__ import annotations
import json, argparse, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# 环境
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

_load_env()

_pool_cache = None
def _get_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    _pool_cache = get_market_db_manager()._get_pool("CNStock")
    return _pool_cache


def load_dragon_data(years: int = 2) -> List[Dict]:
    """加载龙虎榜数据 (默认近两年)"""
    pool = _get_pool()
    cutoff = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, stock_code, stock_name, "
            "buy_amount, sell_amount, net_amount, change_percent "
            "FROM cnd_dragon_tiger_list "
            "WHERE trade_date >= %s "
            "ORDER BY trade_date",
            (cutoff,)
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
    records = [dict(zip(columns, row)) for row in rows]
    print(f"龙虎榜: {len(records)}条, {years}年", file=sys.stderr)
    return records


def build_stock_history(records: List[Dict]) -> Dict[str, List[Dict]]:
    """按股票聚合, 按日期排序"""
    by_code = defaultdict(list)
    for r in records:
        by_code[r['stock_code']].append(r)
    for code in by_code:
        by_code[code].sort(key=lambda x: x['trade_date'])
    return dict(by_code)


def calc_intervals(dates: List[str]) -> List[int]:
    """计算相邻日期间隔"""
    if len(dates) < 2:
        return []
    gaps = []
    for i in range(1, len(dates)):
        d1 = datetime.strptime(dates[i-1], '%Y-%m-%d')
        d2 = datetime.strptime(dates[i], '%Y-%m-%d')
        gaps.append((d2 - d1).days)
    return gaps


def calc_interval_stats(gaps: List[int]) -> Dict:
    """间隔统计"""
    if not gaps:
        return {}
    import numpy as np
    arr = np.array(gaps)
    return {
        'count': len(arr),
        'mean': float(arr.mean()),
        'median': float(np.median(arr)),
        'std': float(np.std(arr)),
        'p25': float(np.percentile(arr, 25)),
        'p75': float(np.percentile(arr, 75)),
        'min': int(arr.min()),
        'max': int(arr.max()),
    }


def precompute_stock_signals(
    records: List[Dict],
    lookback_days: int = 180,
) -> Tuple[List[str], Dict[str, List[Tuple[str, float]]]]:
    """预计算每只股票在每个交易日的"安静倍数"

    Returns:
        (all_dates, code_signals)
        code_signals[code] = [(date, days_since/median_gap), ...]
        即: 每只股票在哪些天触发了信号, 以及安静倍数
    """
    by_code = build_stock_history(records)
    all_dates = sorted(set(r['trade_date'] for r in records))
    code_date_map = {}
    for code, recs in by_code.items():
        code_date_map[code] = [r['trade_date'] for r in recs]

    code_signals = {}  # code -> [(date, ratio)]

    for code, recs in by_code.items():
        dates = code_date_map[code]
        if len(dates) < 2:
            continue

        # 用滑动窗口算每段的中位间隔
        signals = []
        for di, date in enumerate(all_dates):
            dt = datetime.strptime(date, '%Y-%m-%d')
            lb_cutoff = (dt - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

            # 该股在date之前且在lookback内的记录
            prior_dates = [d for d in dates if d < date and d >= lb_cutoff]
            if len(prior_dates) < 2:
                continue

            # 最近一次上榜
            last = prior_dates[-1]
            days_since = (dt - datetime.strptime(last, '%Y-%m-%d')).days

            # 中位间隔
            gaps = calc_intervals(prior_dates)
            if not gaps:
                continue
            import numpy as np
            median_gap = float(np.median(gaps))
            if median_gap <= 0:
                continue

            ratio = days_since / median_gap
            signals.append((date, ratio))

        if signals:
            code_signals[code] = signals

    return all_dates, code_signals, code_date_map


def backtest_from_precomputed(
    all_dates: List[str],
    code_signals: Dict[str, List[Tuple[str, float]]],
    code_date_map: Dict[str, List[str]],
    k: float,
    forward_days: int,
    min_total_count: int = 3,
    show_detail: bool = False,
) -> Dict:
    """从预计算结果做回测 (快速)"""
    # 建立 date->set(code) 索引
    date_to_codes = defaultdict(set)
    for code, dates in code_date_map.items():
        for d in dates:
            date_to_codes[d].add(code)

    # 建立 code -> (date -> ratio) 索引
    code_date_ratio = {}
    for code, sigs in code_signals.items():
        code_date_ratio[code] = {d: r for d, r in sigs}

    # 常客池 (总上榜次数 >= min_total_count)
    if min_total_count > 0:
        eligible = {c for c, d in code_date_map.items() if len(d) >= min_total_count}
    else:
        eligible = set(code_signals.keys())

    total_signals = 0
    total_hits = 0
    daily_stats = []

    for di, date in enumerate(all_dates):
        # 当天触发信号的股票
        signal_codes = []
        for code in eligible:
            if code not in code_date_ratio:
                continue
            ratio = code_date_ratio[code].get(date)
            if ratio is not None and ratio > k and ratio < k * 4:
                signal_codes.append(code)

        if not signal_codes:
            continue

        # forward_days内是否上榜
        hit_codes = set()
        for fwd in range(1, forward_days + 1):
            if di + fwd < len(all_dates):
                future_date = all_dates[di + fwd]
                future_set = date_to_codes.get(future_date, set())
                for code in signal_codes:
                    if code in future_set:
                        hit_codes.add(code)

        total_signals += len(signal_codes)
        total_hits += len(hit_codes)
        hr = len(hit_codes) / len(signal_codes) * 100

        daily_stats.append({
            'date': date,
            'signals': len(signal_codes),
            'hits': len(hit_codes),
            'hit_rate': hr,
        })

        if show_detail and signal_codes:
            print(f"  {date}: 池{len(signal_codes):>4d} | 命中{len(hit_codes):>3d} | 命中率{hr:>5.1f}%", file=sys.stderr)

    return {
        'total_days': len(daily_stats),
        'total_signals': total_signals,
        'total_hits': total_hits,
        'avg_daily_signals': total_signals / len(daily_stats) if daily_stats else 0,
        'hit_rate': total_hits / total_signals * 100 if total_signals else 0,
        'daily_stats': daily_stats,
    }


def backtest_cycle(
    records: List[Dict],
    min_count: int = 3,
    k: float = 1.5,
    lookback_days: int = 180,
    forward_days: int = 2,
    show_detail: bool = False,
) -> Dict:
    """周期回测 (兼容接口)"""
    all_dates, code_signals, code_date_map = precompute_stock_signals(records, lookback_days)
    return backtest_from_precomputed(
        all_dates, code_signals, code_date_map,
        min_count=min_count, k=k, forward_days=forward_days,
        show_detail=show_detail,
    )


def print_result(result: Dict, min_count: int, k: float, forward_days: int):
    print(f"\n{'='*70}")
    print(f"  周期回测结果")
    print(f"  常客阈值: >= {min_count}次 | 安静倍数: {k}x | 检查窗口: {forward_days}天")
    print(f"{'='*70}")
    print(f"  统计天数: {result['total_days']}")
    print(f"  总信号数: {result['total_signals']}")
    print(f"  总命中数: {result['total_hits']}")
    print(f"  日均信号: {result['avg_daily_signals']:.1f}")
    print(f"  命中率: {result['hit_rate']:.2f}%")

    # 月度
    monthly = defaultdict(lambda: {'signals': 0, 'hits': 0, 'days': 0})
    for d in result['daily_stats']:
        m = d['date'][:7]
        monthly[m]['signals'] += d['signals']
        monthly[m]['hits'] += d['hits']
        monthly[m]['days'] += 1

    print(f"\n  月份       日均信号  命中率    日均命中")
    print(f"  {'-'*50}")
    for m in sorted(monthly.keys()):
        d = monthly[m]
        avg_s = d['signals'] / d['days']
        hr = d['hits'] / d['signals'] * 100 if d['signals'] else 0
        avg_h = d['hits'] / d['days']
        print(f"  {m}  {avg_s:>7.1f}   {hr:>5.1f}%   {avg_h:>6.1f}")


def scan_current(records: List[Dict], min_count: int = 3, k: float = 1.5, lookback_days: int = 180):
    """扫描当前候选"""
    by_code = build_stock_history(records)
    all_dates = sorted(set(r['trade_date'] for r in records))
    latest = all_dates[-1]
    latest_dt = datetime.strptime(latest, '%Y-%m-%d')
    lookback_cutoff = (latest_dt - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

    candidates = []
    for code, recs in by_code.items():
        if len(recs) < min_count:
            continue

        last_date = recs[-1]['trade_date']
        days_since = (latest_dt - datetime.strptime(last_date, '%Y-%m-%d')).days

        recent = [r for r in recs if r['trade_date'] >= lookback_cutoff]
        if len(recent) < 2:
            continue

        gaps = calc_intervals([r['trade_date'] for r in recent])
        if not gaps:
            continue

        stats = calc_interval_stats(gaps)
        threshold = stats['median'] * k

        if days_since > threshold and days_since < threshold * 4:
            candidates.append({
                'code': code,
                'name': recs[-1].get('stock_name', ''),
                'count': len(recs),
                'last_date': last_date,
                'days_since': days_since,
                'median_gap': stats['median'],
                'threshold': threshold,
                'ratio': days_since / stats['median'] if stats['median'] > 0 else 0,
                'avg_net': sum(float(r.get('net_amount', 0) or 0) for r in recent) / len(recent) / 10000,
            })

    candidates.sort(key=lambda x: -x['ratio'])

    print(f"\n{'='*70}")
    print(f"  当前候选 (截至 {latest})")
    print(f"  常客阈值: >= {min_count}次 | 安静倍数: {k}x")
    print(f"  候选数: {len(candidates)}")
    print(f"{'='*70}")

    print(f"\n  {'代码':>8} {'名称':>8} {'上榜':>4} {'安静天':>6} {'中位间隔':>6} {'阈值':>5} {'倍数':>5} {'净买均':>8}")
    print(f"  {'-'*65}")
    for c in candidates[:40]:
        print(f"  {c['code']:>8} {c['name']:>8} {c['count']:>4}次 {c['days_since']:>5}天 "
              f"{c['median_gap']:>5.0f}天 {c['threshold']:>4.0f}天 {c['ratio']:>4.1f}x {c['avg_net']:>+7.0f}万")


def main():
    parser = argparse.ArgumentParser(description="龙虎榜活跃股周期追踪")
    parser.add_argument("--min-count", type=int, default=3, help="最少上榜次数")
    parser.add_argument("--k", type=float, default=1.5, help="安静天数=中位间隔*k")
    parser.add_argument("--lookback", type=int, default=180, help="间隔计算回看天数")
    parser.add_argument("--forward", type=int, default=2, help="信号后检查天数")
    parser.add_argument("--years", type=int, default=2, help="加载几年数据")
    parser.add_argument("--detail", action="store_true", help="输出每日明细")
    parser.add_argument("--scan", action="store_true", help="扫描当前候选")
    parser.add_argument("--sweep", action="store_true", help="参数扫描")
    args = parser.parse_args()

    print("加载数据...", file=sys.stderr)
    records = load_dragon_data(args.years)

    if not records:
        print("❌ 无数据")
        return

    if args.scan:
        scan_current(records, args.min_count, args.k, args.lookback)
        return

    if args.sweep:
        print(f"\n{'='*80}")
        print(f"  参数扫描")
        print(f"{'='*80}")
        # 每个lookback只预计算一次
        print(f"\n  {'min_count':>5} {'k':>5} {'forward':>5} {'lookback':>5} {'日均信号':>8} {'命中率':>7} {'总命中':>6}")
        print(f"  {'-'*55}")
        for lb in [90, 180, 365]:
            print(f"  # 预计算 lookback={lb}...", file=sys.stderr)
            all_dates, code_signals, code_date_map = precompute_stock_signals(records, lb)
            for mc in [2, 3, 5, 7, 10]:
                for k in [1.0, 1.2, 1.5, 2.0, 2.5, 3.0]:
                    for fwd in [1, 2, 3]:
                        r = backtest_from_precomputed(
                            all_dates, code_signals, code_date_map,
                            k=k, forward_days=fwd,
                            min_total_count=mc,
                        )
                        if r['total_signals'] > 0:
                            print(f"  {mc:>5} {k:>5.1f} {fwd:>5} {lb:>5} "
                                  f"{r['avg_daily_signals']:>7.1f} "
                                  f"{r['hit_rate']:>6.2f}% "
                                  f"{r['total_hits']:>5d}")
        return

    # 默认回测
    result = backtest_cycle(records, args.min_count, args.k, args.lookback, args.forward, args.detail)
    print_result(result, args.min_count, args.k, args.forward)

    # 保存
    outfile = "dragon_cycle_result.json"
    export = {k: v for k, v in result.items() if k != 'daily_stats'}
    export['params'] = {'min_count': args.min_count, 'k': args.k, 'lookback': args.lookback, 'forward': args.forward}
    export['monthly'] = {}
    for d in result['daily_stats']:
        m = d['date'][:7]
        if m not in export['monthly']:
            export['monthly'][m] = {'signals': 0, 'hits': 0, 'days': 0}
        export['monthly'][m]['signals'] += d['signals']
        export['monthly'][m]['hits'] += d['hits']
        export['monthly'][m]['days'] += 1
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n💾 {outfile}")


if __name__ == "__main__":
    main()
