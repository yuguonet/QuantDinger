#!/usr/bin/env python3
"""
日线因子扫描 — 找出对"次日涨幅"有预测力的因子

思路:
  不预设策略, 而是对每个历史日, 测试各种日线因子与次日涨幅的关系.
  找出哪些因子组合下, 次日上涨概率显著高于50%.

因子列表 (每个独立测试):
  1. 趋势类: 5/10/20日涨幅, MA多头排列, MA5角度
  2. 动量类: RSI14, KDJ K值, 3日连涨
  3. 量能类: 量比(vs5日均量), OBV趋势
  4. 形态类: 当日涨幅, 上影线比例, 下影线比例
  5. 位置类: 距5日新高%, 距20日新高%

输出:
  每个因子的"次日胜率"和"次日均收益"
  按区分度(胜率-50%)排序, 找出最强因子

用法:
  python daily_factor_scan.py --days 120
  python daily_factor_scan.py --days 120 --top 20
"""
from __future__ import annotations
import argparse, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False); break
    except Exception: pass
_load_env()

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None: return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def is_limit_up(close, prev_close, bt):
    t = 0.098 if bt == "main" else 0.198
    return prev_close > 0 and (close / prev_close - 1) >= t * 0.98

def get_all_codes():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def load_st_codes():
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db(); pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol FROM stock_basic_info WHERE status='active' AND name ILIKE '%%ST%%'")
            return {row[0] for row in cur.fetchall()}
    except Exception: return set()

def fetch_kline(code, days=200):
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data: return []
        bars = [{"time": str(r["time"])[:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception: return []

# ================================================================
# 因子计算
# ================================================================

def calc_factors(bars, idx):
    """计算某一天的所有因子, 返回 {name: value}"""
    if idx < 30 or idx >= len(bars) - 1:
        return None  # 需要30天历史 + 次日数据

    b = bars[idx]
    prev = bars[idx - 1]
    closes = [bars[i]['close'] for i in range(max(0, idx-59), idx+1)]
    highs = [bars[i]['high'] for i in range(max(0, idx-59), idx+1)]
    lows = [bars[i]['low'] for i in range(max(0, idx-59), idx+1)]
    vols = [bars[i]['volume'] for i in range(max(0, idx-59), idx+1)]

    # 次日涨幅 (目标变量)
    next_bar = bars[idx + 1]
    next_ret = (next_bar['close'] / b['close'] - 1) * 100
    next_up = 1 if next_ret > 0 else 0

    f = {}
    f['next_ret'] = round(next_ret, 2)
    f['next_up'] = next_up

    # --- 趋势类 ---
    # N日涨幅
    for n in [3, 5, 10, 20]:
        if idx >= n and bars[idx-n]['close'] > 0:
            f[f'ret_{n}d'] = round((b['close'] / bars[idx-n]['close'] - 1) * 100, 2)

    # MA排列
    if len(closes) >= 20:
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes) / 20
        f['ma_bull'] = 1 if ma5 > ma10 > ma20 else 0
        f['ma_bear'] = 1 if ma5 < ma10 < ma20 else 0
        # MA5角度 (3日)
        if len(closes) >= 8:
            ma5_prev = sum(closes[-8:-3]) / 5
            angle = (ma5 - ma5_prev) / ma5_prev * 100 / 3 if ma5_prev > 0 else 0
            f['ma5_angle'] = round(angle, 3)

    # --- 动量类 ---
    # RSI
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
        ag = sum(gains[:14])/14; al = sum(losses[:14])/14
        for i in range(14, len(gains)):
            ag = (ag*13 + gains[i])/14; al = (al*13 + losses[i])/14
        rsi = 100 - 100/(1+ag/al) if al > 0 else 100
        f['rsi14'] = round(rsi, 1)
        # RSI区间
        if rsi < 30: f['rsi_oversold'] = 1
        elif rsi > 70: f['rsi_overbought'] = 1
        elif 50 <= rsi <= 60: f['rsi_neutral_high'] = 1

    # KDJ K
    if len(closes) >= 9:
        rsvs = []
        for i in range(8, len(closes)):
            hn = max(highs[i-8:i+1]); ln = min(lows[i-8:i+1])
            rsvs.append((closes[i]-ln)/(hn-ln)*100 if hn!=ln else 50)
        k = 50
        for r in rsvs: k = 2/3*k + 1/3*r
        f['kdj_k'] = round(k, 1)

    # 连涨天数
    streak = 0
    for i in range(idx, max(idx-10, 0), -1):
        if bars[i]['close'] > bars[i-1]['close']: streak += 1
        else: break
    f['up_streak'] = streak

    # --- 量能类 ---
    # 量比
    if idx >= 5:
        avg5 = sum(bars[i]['volume'] for i in range(idx-5, idx)) / 5
        f['vol_ratio'] = round(b['volume'] / avg5, 2) if avg5 > 0 else 1.0

    # OBV趋势 (5日)
    if idx >= 6:
        obv = 0; obvs = [0]
        for i in range(idx-5, idx+1):
            if i > 0:
                if bars[i]['close'] > bars[i-1]['close']: obv += bars[i]['volume']
                elif bars[i]['close'] < bars[i-1]['close']: obv -= bars[i]['volume']
            obvs.append(obv)
        f['obv_trend'] = 1 if obvs[-1] > obvs[0] else (-1 if obvs[-1] < obvs[0] else 0)

    # --- 形态类 ---
    # 当日涨幅
    f['change'] = round((b['close'] / prev['close'] - 1) * 100, 2) if prev['close'] > 0 else 0
    # 是否涨停
    bt = get_board_type('')
    f['limit_up'] = 1 if is_limit_up(b['close'], prev['close'], bt) else 0
    # 上影线比例
    bar_range = b['high'] - b['low']
    if bar_range > 0:
        f['upper_shadow'] = round((b['high'] - max(b['open'], b['close'])) / bar_range * 100, 1)
        f['lower_shadow'] = round((min(b['open'], b['close']) - b['low']) / bar_range * 100, 1)
        f['body_pct'] = round(abs(b['close'] - b['open']) / bar_range * 100, 1)

    # --- 位置类 ---
    high5 = max(bars[i]['high'] for i in range(max(0, idx-4), idx+1))
    high20 = max(bars[i]['high'] for i in range(max(0, idx-19), idx+1))
    f['dist_high5'] = round((b['close'] / high5 - 1) * 100, 2)
    f['dist_high20'] = round((b['close'] / high20 - 1) * 100, 2)
    f['near_high5'] = 1 if b['close'] >= high5 * 0.98 else 0

    return f

# ================================================================
# 因子扫描
# ================================================================

def scan_factors(all_bars: Dict[str, List], st_codes: set,
                 test_days: int = 120):
    """对全市场每个交易日, 计算因子 → 统计次日胜率"""
    # 收集所有因子值和次日结果
    factor_data = defaultdict(list)  # factor_name -> [(value, next_ret, next_up), ...]

    # 提取交易日
    all_dates = set()
    for bars in all_bars.values():
        for b in bars:
            all_dates.add(str(b['time'])[:10])
    trade_dates = sorted(all_dates)
    test_dates = trade_dates[-test_days:]

    for di, date in enumerate(test_dates):
        if (di + 1) % 20 == 0:
            print(f"\r  扫描: {di+1}/{len(test_dates)}...", end="", flush=True)

        for code, bars in all_bars.items():
            if code in st_codes: continue
            idx = None
            for i, b in enumerate(bars):
                if str(b['time'])[:10] == date:
                    idx = i; break
            if idx is None: continue

            f = calc_factors(bars, idx)
            if f is None: continue

            next_ret = f.pop('next_ret')
            next_up = f.pop('next_up')

            for name, val in f.items():
                if val is not None:
                    factor_data[name].append((val, next_ret, next_up))

    print(f"\r  扫描完成: {len(factor_data)}个因子")

    # 统计每个因子的预测力
    results = []
    for name, data in factor_data.items():
        if len(data) < 100:
            continue

        vals = [d[0] for d in data]
        rets = [d[1] for d in data]
        ups = [d[2] for d in data]

        avg_ret = sum(rets) / len(rets)
        win_rate = sum(ups) / len(ups) * 100

        # 找最佳阈值 (简单: 按中位数分两组)
        median_val = sorted(vals)[len(vals)//2]
        high = [(v, r, u) for v, r, u in data if v >= median_val]
        low = [(v, r, u) for v, r, u in data if v < median_val]

        if len(high) < 50 or len(low) < 50:
            continue

        high_wr = sum(d[2] for d in high) / len(high) * 100
        low_wr = sum(d[2] for d in low) / len(low) * 100
        high_ret = sum(d[1] for d in high) / len(high)
        low_ret = sum(d[1] for d in low) / len(low)

        # 区分度 = 高组胜率 - 低组胜率
        diff = high_wr - low_wr

        results.append({
            'factor': name,
            'samples': len(data),
            'baseline_wr': round(win_rate, 1),
            'baseline_ret': round(avg_ret, 2),
            'median': round(median_val, 3),
            'high_wr': round(high_wr, 1),
            'low_wr': round(low_wr, 1),
            'high_ret': round(high_ret, 2),
            'low_ret': round(low_ret, 2),
            'diff_wr': round(diff, 1),
            'diff_ret': round(high_ret - low_ret, 2),
        })

    # 按区分度排序
    results.sort(key=lambda x: -abs(x['diff_wr']))
    return results

# ================================================================
# 因子组合扫描 (两两组合)
# ================================================================

def scan_factor_combos(all_bars, st_codes, top_factors, test_days=120):
    """对 top N 因子做两两组合, 找最佳组合"""
    all_dates = set()
    for bars in all_bars.values():
        for b in bars:
            all_dates.add(str(b['time'])[:10])
    trade_dates = sorted(all_dates)
    test_dates = trade_dates[-test_days:]

    # 先收集所有因子数据
    daily_factors = []  # [(code, date, {factor: value}, next_ret, next_up)]

    for di, date in enumerate(test_dates):
        if (di + 1) % 50 == 0:
            print(f"\r  组合扫描: {di+1}/{len(test_dates)}...", end="", flush=True)
        for code, bars in all_bars.items():
            if code in st_codes: continue
            idx = None
            for i, b in enumerate(bars):
                if str(b['time'])[:10] == date:
                    idx = i; break
            if idx is None: continue
            f = calc_factors(bars, idx)
            if f is None: continue
            next_ret = f.pop('next_ret')
            next_up = f.pop('next_up')
            # 只保留 top 因子
            subset = {k: v for k, v in f.items() if k in top_factors and v is not None}
            if subset:
                daily_factors.append((code, date, subset, next_ret, next_up))

    print(f"\r  组合扫描完成: {len(daily_factors)}条记录")

    # 两两组合
    factor_names = list(top_factors)
    combos = []
    for i in range(len(factor_names)):
        for j in range(i+1, len(factor_names)):
            f1, f2 = factor_names[i], factor_names[j]
            # 按两个因子的中位数分4组
            vals1 = [d[2].get(f1) for d in daily_factors if f1 in d[2]]
            vals2 = [d[2].get(f2) for d in daily_factors if f2 in d[2]]
            if len(vals1) < 100 or len(vals2) < 100:
                continue
            med1 = sorted(vals1)[len(vals1)//2]
            med2 = sorted(vals2)[len(vals2)//2]

            # 两个因子都高于中位数的组
            both_high = [d for d in daily_factors
                        if d[2].get(f1) is not None and d[2].get(f2) is not None
                        and d[2][f1] >= med1 and d[2][f2] >= med2]
            if len(both_high) < 30:
                continue

            wr = sum(d[4] for d in both_high) / len(both_high) * 100
            avg = sum(d[3] for d in both_high) / len(both_high)

            combos.append({
                'combo': f"{f1} + {f2}",
                'samples': len(both_high),
                'win_rate': round(wr, 1),
                'avg_ret': round(avg, 2),
                'f1_med': round(med1, 3),
                'f2_med': round(med2, 3),
            })

    combos.sort(key=lambda x: -x['win_rate'])
    return combos

# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="日线因子扫描")
    parser.add_argument("--days", type=int, default=120, help="测试天数")
    parser.add_argument("--top", type=int, default=15, help="显示 top N 因子")
    parser.add_argument("--combo", action="store_true", help="扫描因子两两组合")
    args = parser.parse_args()

    print("=" * 70)
    print("  🔬 日线因子扫描")
    print("  目标: 找出对次日涨幅有预测力的因子")
    print("=" * 70)

    # 加载数据
    print(f"\n  [1/2] 加载全市场日线...")
    all_codes = get_all_codes()
    st_codes = load_st_codes()
    codes = [c for c in all_codes if c not in st_codes]
    print(f"        {len(codes)}只")

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(args.days * 3))).strftime("%Y-%m-%d")
    all_bars = {}
    for i, code in enumerate(codes):
        if (i + 1) % 500 == 0:
            print(f"\r  加载: {i+1}/{len(codes)}...", end="", flush=True)
        bars = fetch_kline(code, args.days * 2)
        if bars and len(bars) >= 30:
            all_bars[code] = bars
    print(f"\r  日线: {len(all_bars)}只")

    # 扫描
    print(f"\n  [2/2] 扫描因子...")
    results = scan_factors(all_bars, st_codes, args.days)

    # 输出
    print(f"\n{'='*90}")
    print(f"  📊 因子预测力排名 (按区分度)")
    print(f"{'='*90}")
    print(f"  {'因子':<18} {'样本':>7} {'基线胜率':>8} {'高组胜率':>8} {'低组胜率':>8} {'区分度':>7} {'高组均收':>8} {'低组均收':>8}")
    print(f"  {'-'*90}")
    for r in results[:args.top]:
        diff_emoji = '✅' if abs(r['diff_wr']) > 5 else ''
        print(f"  {r['factor']:<18} {r['samples']:>7} {r['baseline_wr']:>7.1f}% "
              f"{r['high_wr']:>7.1f}% {r['low_wr']:>7.1f}% {r['diff_wr']:>+6.1f}% "
              f"{r['high_ret']:>+7.2f}% {r['low_ret']:>+7.2f}% {diff_emoji}")

    # 因子组合
    if args.combo:
        top_names = [r['factor'] for r in results[:10]]
        print(f"\n  扫描 Top 10 因子的两两组合...")
        combos = scan_factor_combos(all_bars, st_codes, set(top_names), args.days)
        print(f"\n{'='*70}")
        print(f"  📊 最佳因子组合 (双因子都高于中位数)")
        print(f"{'='*70}")
        print(f"  {'组合':<30} {'样本':>7} {'胜率':>7} {'均收益':>8}")
        print(f"  {'-'*55}")
        for c in combos[:20]:
            emoji = '🔥' if c['win_rate'] >= 55 else ('✅' if c['win_rate'] >= 50 else '')
            print(f"  {c['combo']:<30} {c['samples']:>7} {c['win_rate']:>6.1f}% {c['avg_ret']:>+7.2f}% {emoji}")

if __name__ == "__main__":
    main()
