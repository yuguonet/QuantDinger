"""
周期信号 + 技术面联合过滤

流程:
  1. 周期信号: 安静天数 > 中位间隔 × k
  2. 技术面过滤: 多头排列 + MA5角 + RSI + KDJ
  3. 回测: 联合信号触发后1-2天内是否上龙虎榜

运行:
  python dragon_cycle_tech.py --scan        # 扫描当前候选
  python dragon_cycle_tech.py --backtest    # 回测
  python dragon_cycle_tech.py --sweep       # 参数扫描
"""
from __future__ import annotations
import json, argparse, sys, os, time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import numpy as np

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

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache


# ================================================================
# 数据加载
# ================================================================

def load_dragon_data(years: int = 2) -> List[Dict]:
    pool = _get_pool()
    cutoff = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, stock_code, stock_name, "
            "buy_amount, sell_amount, net_amount, change_percent "
            "FROM cnd_dragon_tiger_list WHERE trade_date >= %s ORDER BY trade_date",
            (cutoff,)
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
    records = [dict(zip(columns, row)) for row in rows]
    print(f"龙虎榜: {len(records)}条", file=sys.stderr)
    return records


def load_kline(code: str, days: int = 300) -> List[Dict]:
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = [{"time": str(r["time"])[:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


# ================================================================
# 技术指标
# ================================================================

def calc_ma5_angle(closes: List[float], period: int = 5, days: int = 3) -> Optional[float]:
    if len(closes) < period + days:
        return None
    ma = [sum(closes[i-period+1:i+1]) / period for i in range(period - 1, len(closes))]
    if len(ma) < days:
        return None
    recent = ma[-days:]
    n = days
    sum_x = n * (n - 1) / 2
    sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0
    return slope / recent[-1] * 100 if recent[-1] else None


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0


def calc_kdj_k(closes: List[float], highs: List[float], lows: List[float], period: int = 9) -> Optional[float]:
    if len(closes) < period:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hn = max(highs[i-period+1:i+1])
        ln = min(lows[i-period+1:i+1])
        c = closes[i]
        rsvs.append((c - ln) / (hn - ln) * 100 if hn != ln else 50)
    k_val, d_val = 50.0, 50.0
    for rsv in rsvs:
        k_val = 2/3 * k_val + 1/3 * rsv
        d_val = 2/3 * d_val + 1/3 * k_val
    return k_val


def check_tech(bars: List[Dict], date: str, conditions: Dict) -> bool:
    """检查某天的技术面条件"""
    idx = None
    for i, b in enumerate(bars):
        if b['time'] == date:
            idx = i
            break
    if idx is None or idx < 20:
        return False

    closes = [bars[i]['close'] for i in range(idx - 19, idx + 1)]
    highs = [bars[i]['high'] for i in range(idx - 19, idx + 1)]
    lows = [bars[i]['low'] for i in range(idx - 19, idx + 1)]
    volumes = [bars[i]['volume'] for i in range(idx - 19, idx + 1)]

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes) / 20

    if conditions.get('ma_bull') and not (ma5 > ma10 > ma20):
        return False

    if 'min_ma5_angle' in conditions:
        angle = calc_ma5_angle(closes, 5, 3)
        if angle is None or angle < conditions['min_ma5_angle']:
            return False

    if 'min_rsi14' in conditions:
        rsi = calc_rsi(closes, 14)
        if rsi is None or rsi < conditions['min_rsi14']:
            return False

    if 'min_kdj_k' in conditions:
        kdj = calc_kdj_k(closes, highs, lows, 9)
        if kdj is None or kdj < conditions['min_kdj_k']:
            return False

    # D-1涨幅 (信号日当天涨幅)
    if 'min_d1_chg' in conditions:
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        if prev_close <= 0:
            return False
        d1_chg = (bars[idx]['close'] / prev_close - 1) * 100
        if d1_chg < conditions['min_d1_chg']:
            return False

    # D-1量比 (信号日成交量 / 5日均量)
    if 'min_d1_vol_ratio' in conditions:
        vol5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
        if vol5 <= 0:
            return False
        d1_vol_ratio = bars[idx]['volume'] / vol5
        if d1_vol_ratio < conditions['min_d1_vol_ratio']:
            return False

    # 连涨天数
    if 'min_up_streak' in conditions:
        streak = 0
        for i in range(idx, max(idx - 10, 0), -1):
            if bars[i]['close'] > bars[i-1]['close']:
                streak += 1
            else:
                break
        if streak < conditions['min_up_streak']:
            return False

    return True


# ================================================================
# 周期预计算
# ================================================================

def build_stock_history(records):
    by_code = defaultdict(list)
    for r in records:
        by_code[r['stock_code']].append(r)
    for code in by_code:
        by_code[code].sort(key=lambda x: x['trade_date'])
    return dict(by_code)


def calc_intervals(dates):
    return [(datetime.strptime(dates[i], '%Y-%m-%d') - datetime.strptime(dates[i-1], '%Y-%m-%d')).days
            for i in range(1, len(dates))]


def precompute_cycle_signals(records, lookback_days=180):
    """预计算周期信号: code -> [(date, ratio)]"""
    by_code = build_stock_history(records)
    all_dates = sorted(set(r['trade_date'] for r in records))
    code_date_map = {code: [r['trade_date'] for r in recs] for code, recs in by_code.items()}

    code_signals = {}
    for code, dates in code_date_map.items():
        if len(dates) < 2:
            continue
        signals = []
        for date in all_dates:
            dt = datetime.strptime(date, '%Y-%m-%d')
            lb_cut = (dt - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
            prior = [d for d in dates if d < date and d >= lb_cut]
            if len(prior) < 2:
                continue
            last = prior[-1]
            days_since = (dt - datetime.strptime(last, '%Y-%m-%d')).days
            gaps = calc_intervals(prior)
            if not gaps:
                continue
            median_gap = float(np.median(gaps))
            if median_gap <= 0:
                continue
            signals.append((date, days_since / median_gap))
        if signals:
            code_signals[code] = {d: r for d, r in signals}

    return all_dates, code_signals, code_date_map


# ================================================================
# 联合回测
# ================================================================

def backtest_combined(
    all_dates, code_signals, code_date_map, records,
    cycle_k=1.5, cycle_min_count=3, forward_days=2,
    tech_conds=None, lookback_days=90,
    show_detail=False, kline_cache=None,
    no_cycle=False,
):
    """周期+技术面联合回测

    no_cycle=True: 跳过周期过滤, 对全市场股票做技术面过滤
    """
    if tech_conds is None:
        tech_conds = {}

    date_to_codes = defaultdict(set)
    for code, dates in code_date_map.items():
        for d in dates:
            date_to_codes[d].add(code)

    # 全市场股票列表
    all_codes = set(code_date_map.keys())

    total_signals = 0
    total_hits = 0
    daily_stats = []
    kline_loads = 0

    for di, date in enumerate(all_dates):
        if no_cycle:
            # 无周期过滤: 对全市场股票做技术面过滤
            cycle_codes = list(all_codes)
        else:
            # 周期信号
            eligible = {c for c, d in code_date_map.items() if len(d) >= cycle_min_count}
            cycle_codes = []
            for code in eligible:
                if code not in code_signals:
                    continue
                ratio = code_signals[code].get(date)
                if ratio is not None and ratio > cycle_k and ratio < cycle_k * 4:
                    cycle_codes.append(code)

        if not cycle_codes:
            continue

        # 技术面过滤
        if tech_conds:
            filtered = []
            for code in cycle_codes:
                if kline_cache is not None:
                    if code not in kline_cache:
                        kline_cache[code] = load_kline(code)
                        kline_loads += 1
                    bars = kline_cache[code]
                else:
                    bars = load_kline(code)
                    kline_loads += 1

                if bars and check_tech(bars, date, tech_conds):
                    filtered.append(code)
            signal_codes = filtered
        else:
            signal_codes = cycle_codes

        if not signal_codes:
            continue

        # forward命中
        hit_codes = set()
        for fwd in range(1, forward_days + 1):
            if di + fwd < len(all_dates):
                future = all_dates[di + fwd]
                future_set = date_to_codes.get(future, set())
                for code in signal_codes:
                    if code in future_set:
                        hit_codes.add(code)

        total_signals += len(signal_codes)
        total_hits += len(hit_codes)
        hr = len(hit_codes) / len(signal_codes) * 100

        daily_stats.append({
            'date': date,
            'cycle_pool': len(cycle_codes),
            'tech_pool': len(signal_codes),
            'hits': len(hit_codes),
            'hit_rate': hr,
        })

        if show_detail and signal_codes:
            print(f"  {date}: 周期{len(cycle_codes):>3d} → 技术{len(signal_codes):>3d} → "
                  f"命中{len(hit_codes):>2d} ({hr:>5.1f}%)", file=sys.stderr)

    return {
        'total_days': len(daily_stats),
        'total_signals': total_signals,
        'total_hits': total_hits,
        'avg_daily_signals': total_signals / len(daily_stats) if daily_stats else 0,
        'hit_rate': total_hits / total_signals * 100 if total_signals else 0,
        'kline_loads': kline_loads,
        'daily_stats': daily_stats,
    }


def print_result(result, label=""):
    print(f"\n{'='*60}")
    if label:
        print(f"  {label}")
    print(f"  统计天数: {result['total_days']}")
    print(f"  总信号: {result['total_signals']}")
    print(f"  总命中: {result['total_hits']}")
    print(f"  日均池: {result['avg_daily_signals']:.1f}")
    print(f"  命中率: {result['hit_rate']:.2f}%")
    print(f"{'='*60}")


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="周期+技术面联合过滤")
    parser.add_argument("--scan", action="store_true", help="扫描当前候选")
    parser.add_argument("--backtest", action="store_true", help="回测")
    parser.add_argument("--sweep", action="store_true", help="参数扫描")
    parser.add_argument("--momentum-only", action="store_true", help="只跑纯D-1动量扫描(跳过周期+技术面)")
    parser.add_argument("--years", type=int, default=2, help="数据年数")
    # 周期参数
    parser.add_argument("--cycle-k", type=float, default=1.5, help="安静倍数")
    parser.add_argument("--cycle-count", type=int, default=10, help="最少上榜次数")
    parser.add_argument("--lookback", type=int, default=90, help="间隔回看天数")
    parser.add_argument("--forward", type=int, default=3, help="检查窗口天数")
    # 技术面参数
    parser.add_argument("--ma-bull", action="store_true", help="要求多头排列")
    parser.add_argument("--ma5-angle", type=float, default=0, help="MA5角阈值")
    parser.add_argument("--rsi", type=float, default=0, help="RSI14阈值")
    parser.add_argument("--kdj", type=float, default=0, help="KDJ K阈值")
    parser.add_argument("--d1-chg", type=float, default=0, help="D-1涨幅阈值")
    parser.add_argument("--d1-vol", type=float, default=0, help="D-1量比阈值")
    parser.add_argument("--up-streak", type=int, default=0, help="连涨天数阈值")
    parser.add_argument("--detail", action="store_true", help="输出明细")
    args = parser.parse_args()

    print("加载龙虎榜...", file=sys.stderr)
    records = load_dragon_data(args.years)

    if args.scan:
        # 扫描当前候选
        print("预计算周期信号...", file=sys.stderr)
        all_dates, code_signals, code_date_map = precompute_cycle_signals(records, args.lookback)
        latest = all_dates[-1]
        latest_dt = datetime.strptime(latest, '%Y-%m-%d')

        eligible = {c for c, d in code_date_map.items() if len(d) >= args.cycle_count}
        tech_conds = {}
        if args.ma_bull: tech_conds['ma_bull'] = True
        if args.ma5_angle > 0: tech_conds['min_ma5_angle'] = args.ma5_angle
        if args.rsi > 0: tech_conds['min_rsi14'] = args.rsi
        if args.kdj > 0: tech_conds['min_kdj_k'] = args.kdj

        candidates = []
        for code in eligible:
            if code not in code_signals:
                continue
            ratio = code_signals[code].get(latest)
            if ratio is None or ratio <= args.cycle_k or ratio >= args.cycle_k * 4:
                continue

            # 技术面
            if tech_conds:
                bars = load_kline(code)
                if not bars or not check_tech(bars, latest, tech_conds):
                    continue

            dates = code_date_map[code]
            gaps = calc_intervals(dates)
            median_gap = float(np.median(gaps)) if gaps else 0
            last_date = dates[-1]
            days_since = (latest_dt - datetime.strptime(last_date, '%Y-%m-%d')).days

            candidates.append({
                'code': code,
                'count': len(dates),
                'last_date': last_date,
                'days_since': days_since,
                'median_gap': median_gap,
                'ratio': ratio,
            })

        candidates.sort(key=lambda x: -x['ratio'])
        print(f"\n当前候选 ({latest}): {len(candidates)}只")
        print(f"  条件: cycle_k={args.cycle_k}, count>={args.cycle_count}, tech={tech_conds}")
        print(f"\n  {'代码':>8} {'上榜':>4} {'安静天':>6} {'中位间隔':>6} {'倍数':>5}")
        for c in candidates[:30]:
            print(f"  {c['code']:>8} {c['count']:>4}次 {c['days_since']:>5}天 {c['median_gap']:>5.0f}天 {c['ratio']:>4.1f}x")
        return

    if args.sweep:
        print("预计算周期信号...", file=sys.stderr)
        t0 = time.time()
        all_dates, code_signals, code_date_map = precompute_cycle_signals(records, args.lookback)
        print(f"  耗时: {time.time()-t0:.1f}秒", file=sys.stderr)

        # === 模式1: 周期+技术面 (已有) ===
        tech_combos_cycle = [
            ('无技术面', {}),
            ('多头', {'ma_bull': True}),
            ('多头+角>0.3', {'ma_bull': True, 'min_ma5_angle': 0.3}),
            ('多头+角>0.5', {'ma_bull': True, 'min_ma5_angle': 0.5}),
            ('多头+角>0.7', {'ma_bull': True, 'min_ma5_angle': 0.7}),
            ('多头+角>1.0', {'ma_bull': True, 'min_ma5_angle': 1.0}),
            ('多头+RSI>55', {'ma_bull': True, 'min_rsi14': 55}),
            ('多头+RSI>60', {'ma_bull': True, 'min_rsi14': 60}),
            ('多头+KDJ>60', {'ma_bull': True, 'min_kdj_k': 60}),
            ('多头+KDJ>70', {'ma_bull': True, 'min_kdj_k': 70}),
            ('多头+角>0.5+RSI>60', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60}),
            ('多头+角>0.5+KDJ>70', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_kdj_k': 70}),
            ('多头+角>0.3+RSI>60+KDJ>70', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70}),
            ('多头+角>0.3+RSI>60+KDJ>70+D1涨>3%', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_chg': 3}),
            ('多头+角>0.3+RSI>60+KDJ>70+D1涨>5%', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_chg': 5}),
            ('多头+角>0.3+RSI>60+KDJ>70+D1涨>8%', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_chg': 8}),
            ('多头+角>0.3+RSI>60+KDJ>70+D1涨>10%', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_chg': 10}),
            ('多头+角>0.5+RSI>60+D1涨>3%', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60, 'min_d1_chg': 3}),
            ('多头+角>0.5+RSI>60+D1涨>5%', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60, 'min_d1_chg': 5}),
            ('多头+角>0.5+RSI>60+D1涨>8%', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60, 'min_d1_chg': 8}),
            ('多头+角>1.0+D1涨>3%', {'ma_bull': True, 'min_ma5_angle': 1.0, 'min_d1_chg': 3}),
            ('多头+角>1.0+D1涨>5%', {'ma_bull': True, 'min_ma5_angle': 1.0, 'min_d1_chg': 5}),
            ('多头+角>1.0+D1涨>8%', {'ma_bull': True, 'min_ma5_angle': 1.0, 'min_d1_chg': 8}),
            ('多头+角>0.3+RSI>60+KDJ>70+量比>1.3', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_vol_ratio': 1.3}),
            ('多头+角>0.3+RSI>60+KDJ>70+量比>1.5', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_vol_ratio': 1.5}),
            ('多头+角>0.3+RSI>60+KDJ>70+D1涨>3%+量比>1.3', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_d1_chg': 3, 'min_d1_vol_ratio': 1.3}),
            ('多头+角>0.5+RSI>60+D1涨>3%+量比>1.3', {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60, 'min_d1_chg': 3, 'min_d1_vol_ratio': 1.3}),
            ('多头+角>0.3+RSI>60+KDJ>70+连涨>=2', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_up_streak': 2}),
            ('多头+角>0.3+RSI>60+KDJ>70+连涨>=3', {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70, 'min_up_streak': 3}),
        ]

        cycle_k = args.cycle_k
        cycle_count = args.cycle_count
        forward = args.forward

        if not args.momentum_only:
            print(f"\n{'='*70}")
            print(f"  模式A: 周期+技术面 (k={cycle_k}, count>={cycle_count}, forward={forward})")
            print(f"{'='*70}")
        if not args.momentum_only:
            print(f"  {'技术面':45s} {'周期池':>6} {'技术池':>6} {'命中':>5} {'命中率':>7}")
            print(f"  {'-'*70}")

            for tech_name, tech_conds in tech_combos_cycle:
                kline_cache = {} if tech_conds else None
                r = backtest_combined(
                    all_dates, code_signals, code_date_map, records,
                    cycle_k=cycle_k, cycle_min_count=cycle_count,
                    forward_days=forward, tech_conds=tech_conds,
                    kline_cache=kline_cache,
                )
                avg_cycle = sum(d['cycle_pool'] for d in r['daily_stats']) / max(len(r['daily_stats']), 1)
                avg_tech = r['avg_daily_signals']
                print(f"  {tech_name:45s} {avg_cycle:>6.1f} {avg_tech:>6.1f} {r['total_hits']:>5d} {r['hit_rate']:>6.2f}%")

        # === 模式2: 纯D-1动量 (无周期依赖) ===
        print(f"\n{'='*70}")
        print(f"  模式B: 纯D-1动量 (无周期, 全市场扫描)")
        print(f"{'='*70}")
        print(f"  ⚠️ 全市场扫描, 需要加载K线, 较慢...", file=sys.stderr)

        momentum_combos = [
            ('D1涨>3%', {'min_d1_chg': 3}),
            ('D1涨>5%', {'min_d1_chg': 5}),
            ('D1涨>8%', {'min_d1_chg': 8}),
            ('D1涨>10%', {'min_d1_chg': 10}),
            ('D1涨>12%', {'min_d1_chg': 12}),
            ('D1涨>3%+量比>1.3', {'min_d1_chg': 3, 'min_d1_vol_ratio': 1.3}),
            ('D1涨>5%+量比>1.3', {'min_d1_chg': 5, 'min_d1_vol_ratio': 1.3}),
            ('D1涨>5%+量比>1.5', {'min_d1_chg': 5, 'min_d1_vol_ratio': 1.5}),
            ('D1涨>3%+多头', {'min_d1_chg': 3, 'ma_bull': True}),
            ('D1涨>5%+多头', {'min_d1_chg': 5, 'ma_bull': True}),
            ('D1涨>3%+多头+角>0.3', {'min_d1_chg': 3, 'ma_bull': True, 'min_ma5_angle': 0.3}),
            ('D1涨>5%+多头+角>0.3', {'min_d1_chg': 5, 'ma_bull': True, 'min_ma5_angle': 0.3}),
            ('D1涨>3%+多头+角>0.5', {'min_d1_chg': 3, 'ma_bull': True, 'min_ma5_angle': 0.5}),
            ('D1涨>5%+多头+角>0.5', {'min_d1_chg': 5, 'ma_bull': True, 'min_ma5_angle': 0.5}),
            ('D1涨>3%+多头+RSI>60', {'min_d1_chg': 3, 'ma_bull': True, 'min_rsi14': 60}),
            ('D1涨>5%+多头+RSI>60', {'min_d1_chg': 5, 'ma_bull': True, 'min_rsi14': 60}),
            ('D1涨>3%+多头+角>0.5+RSI>60', {'min_d1_chg': 3, 'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60}),
            ('D1涨>5%+多头+角>0.5+RSI>60', {'min_d1_chg': 5, 'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60}),
            ('D1涨>8%+多头+角>0.5', {'min_d1_chg': 8, 'ma_bull': True, 'min_ma5_angle': 0.5}),
            ('D1涨>8%+多头+RSI>60', {'min_d1_chg': 8, 'ma_bull': True, 'min_rsi14': 60}),
            ('D1涨>8%+多头+角>0.5+RSI>60', {'min_d1_chg': 8, 'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60}),
            ('D1涨>5%+连涨>=2', {'min_d1_chg': 5, 'min_up_streak': 2}),
            ('D1涨>5%+连涨>=3', {'min_d1_chg': 5, 'min_up_streak': 3}),
            ('D1涨>5%+多头+连涨>=2', {'min_d1_chg': 5, 'ma_bull': True, 'min_up_streak': 2}),
        ]

        print(f"  {'技术面':45s} {'池大小':>6} {'命中':>5} {'命中率':>7}")
        print(f"  {'-'*70}")

        kline_cache = {}
        for tech_name, tech_conds in momentum_combos:
            r = backtest_combined(
                all_dates, code_signals, code_date_map, records,
                forward_days=forward, tech_conds=tech_conds,
                no_cycle=True, kline_cache=kline_cache,
            )
            print(f"  {tech_name:45s} {r['avg_daily_signals']:>6.1f} {r['total_hits']:>5d} {r['hit_rate']:>6.2f}%")

        return

    if args.backtest:
        print("预计算周期信号...", file=sys.stderr)
        t0 = time.time()
        all_dates, code_signals, code_date_map = precompute_cycle_signals(records, args.lookback)
        print(f"  耗时: {time.time()-t0:.1f}秒", file=sys.stderr)

        tech_conds = {}
        if args.ma_bull: tech_conds['ma_bull'] = True
        if args.ma5_angle > 0: tech_conds['min_ma5_angle'] = args.ma5_angle
        if args.rsi > 0: tech_conds['min_rsi14'] = args.rsi
        if args.kdj > 0: tech_conds['min_kdj_k'] = args.kdj
        if args.d1_chg > 0: tech_conds['min_d1_chg'] = args.d1_chg
        if args.d1_vol > 0: tech_conds['min_d1_vol_ratio'] = args.d1_vol
        if args.up_streak > 0: tech_conds['min_up_streak'] = args.up_streak

        kline_cache = {} if tech_conds else None
        r = backtest_combined(
            all_dates, code_signals, code_date_map, records,
            cycle_k=args.cycle_k, cycle_min_count=args.cycle_count,
            forward_days=args.forward, tech_conds=tech_conds,
            show_detail=args.detail, kline_cache=kline_cache,
        )
        label = f"cycle_k={args.cycle_k}, count>={args.cycle_count}, tech={tech_conds}"
        print_result(r, label)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
