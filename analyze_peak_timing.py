#!/usr/bin/env python3
"""
真假反弹的 D0~D20 走势形态分析
真反弹: D1~D5 横盘缓涨(~10%), D10~D30 主升浪
假反弹: D1~D5 快速拉升后见顶回落
"""
import json, time, sys, statistics, math
sys.path.insert(0, "/root/.openclaw/workspace")
from kline_cache import fetch_kline

with open("test_rsi_indicator_result.json", encoding="utf-8") as f:
    all_trades = json.load(f)

mh = [d for d in all_trades if d['exit_reason'] == 'max_hold']
codes = sorted(set(d['code'] for d in mh))

kline_map = {}
for i, code in enumerate(codes):
    bars = fetch_kline(code, 400)
    if bars and len(bars) > 80:
        kline_map[code] = bars
    if (i + 1) % 100 == 0:
        print(f"  K线: {i+1}/{len(codes)}")
    time.sleep(0.1)
print(f"K线: {len(kline_map)}只\n")

records = []
for t in mh:
    code = t['code']
    if code not in kline_map:
        continue
    bars = kline_map[code]
    sig_idx = None
    for j, b in enumerate(bars):
        if b['time'] == t['signal_date']:
            sig_idx = j
            break
    if sig_idx is None or sig_idx < 60 or sig_idx + 25 >= len(bars):
        continue

    entry_price = bars[sig_idx]['close']
    if entry_price <= 0:
        continue

    # 逐日数据
    closes = []
    highs = []
    lows = []
    vols = []
    for d in range(1, 21):
        idx = sig_idx + d
        if idx >= len(bars):
            break
        closes.append(bars[idx]['close'])
        highs.append(bars[idx]['high'])
        lows.append(bars[idx]['low'])
        vols.append(bars[idx]['volume'])

    if len(closes) < 20:
        continue

    # 峰值天数
    peak = entry_price
    peak_day = 0
    for i, c in enumerate(closes):
        if c > peak:
            peak = c
            peak_day = i + 1

    # 各阶段收益
    d5_ret = (closes[4] / entry_price - 1) * 100
    d10_ret = (closes[9] / entry_price - 1) * 100
    d15_ret = (closes[14] / entry_price - 1) * 100
    d20_ret = (closes[19] / entry_price - 1) * 100

    # D1~D5 各日收益
    d1_ret = (closes[0] / entry_price - 1) * 100

    # D1~D5 走势斜率 (用线性回归)
    x = list(range(5))
    y = [(closes[i] / entry_price - 1) * 100 for i in range(5)]
    n = 5
    sx = sum(x)
    sy = sum(y)
    sxy = sum(x[i]*y[i] for i in range(n))
    sx2 = sum(xi*xi for xi in x)
    slope_5d = (n * sxy - sx * sy) / (n * sx2 - sx * sx) if (n * sx2 - sx * sx) != 0 else 0

    # D1~D5 波动率 (日收益标准差)
    daily_rets_5d = [(closes[i] / (closes[i-1] if i > 0 else entry_price) - 1) * 100 for i in range(5)]
    vol_5d = statistics.stdev(daily_rets_5d) if len(daily_rets_5d) > 1 else 0

    # D1~D5 振幅均值
    range_5d = statistics.mean([(highs[i] - lows[i]) / closes[i] * 100 for i in range(5) if closes[i] > 0])

    # D1~D5 最大单日涨幅
    max_daily_gain = max(daily_rets_5d)

    # D1~D5 最大单日跌幅
    max_daily_loss = min(daily_rets_5d)

    # D1~D5 上涨天数
    up_days_5d = sum(1 for r in daily_rets_5d if r > 0)

    # D1~D5 成交量趋势
    vol_slope = 0
    if len(vols) >= 5:
        vx = list(range(5))
        vy = vols[:5]
        vsx = sum(vx)
        vsy = sum(vy)
        vsxy = sum(vx[i]*vy[i] for i in range(5))
        vsx2 = sum(xi*xi for xi in vx)
        vol_slope = (5 * vsxy - vsx * vsy) / (5 * vsx2 - vsx * vsx) if (5 * vsx2 - vsx * vsx) != 0 else 0

    # D6~D10 走势斜率
    x2 = list(range(5, 10))
    y2 = [(closes[i] / entry_price - 1) * 100 for i in range(5, 10)]
    sx2_2 = sum(x2)
    sy2_2 = sum(y2)
    sxy2 = sum(x2[i]*y2[i] for i in range(5))
    sx22 = sum(xi*xi for xi in x2)
    slope_6_10 = (5 * sxy2 - sx2_2 * sy2_2) / (5 * sx22 - sx2_2 * sx2_2) if (5 * sx22 - sx2_2 * sx2_2) != 0 else 0

    # D11~D20 走势斜率
    x3 = list(range(10, 20))
    y3 = [(closes[i] / entry_price - 1) * 100 for i in range(10, 20)]
    sx3 = sum(x3)
    sy3 = sum(y3)
    sxy3 = sum(x3[i]*y3[i] for i in range(10))
    sx32 = sum(xi*xi for xi in x3)
    slope_11_20 = (10 * sxy3 - sx3 * sy3) / (10 * sx32 - sx3 * sx3) if (10 * sx32 - sx3 * sx3) != 0 else 0

    rec = {
        'code': code,
        'board': t['board'],
        'signal_date': t['signal_date'],
        'is_win': 1 if t['return_pct'] > 0 else 0,
        'return_pct': t['return_pct'],
        'peak_return_pct': t['peak_return_pct'],
        'peak_day': peak_day,
        'signal_rsi': t['signal_rsi'],
        'signal_vol_ratio': t['signal_vol_ratio'],

        # D1~D5 走势特征
        'd5_return': round(d5_ret, 2),
        'd1_return': round(d1_ret, 2),
        'slope_5d': round(slope_5d, 3),        # 趋势斜率 (正=上涨)
        'vol_5d': round(vol_5d, 2),             # 波动率 (大=震荡)
        'range_5d': round(range_5d, 2),         # 振幅
        'max_daily_gain': round(max_daily_gain, 2),
        'max_daily_loss': round(max_daily_loss, 2),
        'up_days_5d': up_days_5d,
        'vol_slope_5d': round(vol_slope, 1),    # 成交量趋势

        # 后续走势
        'd10_return': round(d10_ret, 2),
        'd15_return': round(d15_ret, 2),
        'd20_return': round(d20_ret, 2),
        'slope_6_10': round(slope_6_10, 3),
        'slope_11_20': round(slope_11_20, 3),
    }
    records.append(rec)

print(f"分析样本: {len(records)}笔\n")

wins = [r for r in records if r['is_win'] == 1]
losses = [r for r in records if r['is_win'] == 0]

# ================================================================
# 峰值天数
# ================================================================
print("=" * 70)
print("1. 峰值天数")
print("=" * 70)
w_peak = [r['peak_day'] for r in wins]
l_peak = [r['peak_day'] for r in losses]
print(f"  真反转: 均值={statistics.mean(w_peak):.1f}天, 中位数={statistics.median(w_peak):.0f}天")
print(f"  假反转: 均值={statistics.mean(l_peak):.1f}天, 中位数={statistics.median(l_peak):.0f}天")

# ================================================================
# D1~D5 走势形态对比
# ================================================================
print(f"\n{'=' * 70}")
print("2. D1~D5 走势形态对比")
print("=" * 70)

def cmp(label, key, fmt=".2f"):
    w = [r[key] for r in wins if r[key] is not None]
    l = [r[key] for r in losses if r[key] is not None]
    if w and l:
        wa, la = statistics.mean(w), statistics.mean(l)
        print(f"  {label:<25} 赢={wa:>+{fmt}}  亏={la:>+{fmt}}  差={wa-la:>+{fmt}}")

cmp("D5总收益", "d5_return")
cmp("D1收益", "d1_return")
cmp("5日趋势斜率", "slope_5d", ".3f")
cmp("5日波动率", "vol_5d")
cmp("5日振幅均值", "range_5d")
cmp("最大单日涨幅", "max_daily_gain")
cmp("最大单日跌幅", "max_daily_loss")
cmp("上涨天数", "up_days_5d", ".1f")
cmp("成交量趋势", "vol_slope_5d", ".1f")

# ================================================================
# 后续走势对比
# ================================================================
print(f"\n{'=' * 70}")
print("3. 后续走势对比")
print("=" * 70)
cmp("D10收益", "d10_return")
cmp("D15收益", "d15_return")
cmp("D20收益", "d20_return")
cmp("D6~D10斜率", "slope_6_10", ".3f")
cmp("D11~D20斜率", "slope_11_20", ".3f")

# ================================================================
# D1~D5 形态分桶
# ================================================================
print(f"\n{'=' * 70}")
print("4. D1~D5 形态分桶 → 最终收益")
print("=" * 70)

# 按 D5 收益分桶
print(f"\n  --- D5收益分桶 ---")
for lo, hi, label in [(-20, -5, "<-5%"), (-5, -2, "-5%~-2%"), (-2, 0, "-2%~0%"), (0, 5, "0%~5%"), (5, 10, "5%~10%"), (10, 30, ">10%")]:
    bucket = [r for r in records if lo <= r['d5_return'] < hi]
    if not bucket:
        continue
    w = len([r for r in bucket if r['is_win'] == 1])
    avg = statistics.mean([r['return_pct'] for r in bucket])
    avg_peak = statistics.mean([r['peak_day'] for r in bucket])
    print(f"    {label:>10}: {len(bucket):>3}笔, 胜率={w/len(bucket)*100:.1f}%, 均收益={avg:+.2f}%, 峰值天={avg_peak:.1f}")

# 按波动率分桶
print(f"\n  --- D1~D5波动率分桶 ---")
vol_vals = sorted([r['vol_5d'] for r in records])
p33 = vol_vals[len(vol_vals)//3]
p66 = vol_vals[2*len(vol_vals)//3]
for lo, hi, label in [(0, p33, f"低波动(<{p33:.1f})"), (p33, p66, f"中波动({p33:.1f}~{p66:.1f})"), (p66, 100, f"高波动(>{p66:.1f})")]:
    bucket = [r for r in records if lo <= r['vol_5d'] < hi]
    if not bucket:
        continue
    w = len([r for r in bucket if r['is_win'] == 1])
    avg = statistics.mean([r['return_pct'] for r in bucket])
    print(f"    {label}: {len(bucket)}笔, 胜率={w/len(bucket)*100:.1f}%, 均收益={avg:+.2f}%")

# 按趋势斜率分桶
print(f"\n  --- D1~D5趋势斜率分桶 ---")
slope_vals = sorted([r['slope_5d'] for r in records])
p33s = slope_vals[len(slope_vals)//3]
p66s = slope_vals[2*len(slope_vals)//3]
for lo, hi, label in [(-10, p33s, f"下跌趋势(<{p33s:.2f})"), (p33s, p66s, f"横盘({p33s:.2f}~{p66s:.2f})"), (p66s, 10, f"上涨趋势(>{p66s:.2f})")]:
    bucket = [r for r in records if lo <= r['slope_5d'] < hi]
    if not bucket:
        continue
    w = len([r for r in bucket if r['is_win'] == 1])
    avg = statistics.mean([r['return_pct'] for r in bucket])
    avg_peak = statistics.mean([r['peak_day'] for r in bucket])
    print(f"    {label}: {len(bucket)}笔, 胜率={w/len(bucket)*100:.1f}%, 均收益={avg:+.2f}%, 峰值天={avg_peak:.1f}")

# 按最大单日涨幅分桶
print(f"\n  --- D1~D5最大单日涨幅分桶 ---")
for lo, hi, label in [(0, 2, "<2%"), (2, 4, "2%~4%"), (4, 7, "4%~7%"), (7, 30, ">7%")]:
    bucket = [r for r in records if lo <= r['max_daily_gain'] < hi]
    if not bucket:
        continue
    w = len([r for r in bucket if r['is_win'] == 1])
    avg = statistics.mean([r['return_pct'] for r in bucket])
    avg_peak = statistics.mean([r['peak_day'] for r in bucket])
    print(f"    {label:>8}: {len(bucket):>3}笔, 胜率={w/len(bucket)*100:.1f}%, 均收益={avg:+.2f}%, 峰值天={avg_peak:.1f}")

# ================================================================
# 组合过滤: 横盘缓涨 vs 急拉
# ================================================================
print(f"\n{'=' * 70}")
print("5. 组合过滤效果")
print("=" * 70)

filters = [
    ("无过滤", lambda r: True),
    ("D5收益>0", lambda r: r['d5_return'] > 0),
    ("D5收益 0%~10%", lambda r: 0 < r['d5_return'] < 10),
    ("D5收益 2%~10%", lambda r: 2 < r['d5_return'] < 10),
    ("D5收益 3%~10%", lambda r: 3 < r['d5_return'] < 10),
    ("波动率<中位数", lambda r: r['vol_5d'] < p33),
    ("斜率>0 (缓涨)", lambda r: r['slope_5d'] > 0),
    ("斜率 0~0.5 (窄幅缓涨)", lambda r: 0 < r['slope_5d'] < 0.5),
    ("斜率 0.3~0.8 (温和上涨)", lambda r: 0.3 < r['slope_5d'] < 0.8),
    ("最大单日涨<4%", lambda r: r['max_daily_gain'] < 4),
    ("最大单日涨<3%", lambda r: r['max_daily_gain'] < 3),
    ("上涨>=3天+最大单日涨<4%", lambda r: r['up_days_5d'] >= 3 and r['max_daily_gain'] < 4),
    ("D5 2%~10%+低波动", lambda r: 2 < r['d5_return'] < 10 and r['vol_5d'] < p33),
    ("D5 2%~10%+最大单日涨<4%", lambda r: 2 < r['d5_return'] < 10 and r['max_daily_gain'] < 4),
    ("斜率>0+最大单日涨<4%", lambda r: r['slope_5d'] > 0 and r['max_daily_gain'] < 4),
    ("D5 3%~10%+斜率>0+最大单日涨<5%", lambda r: 3 < r['d5_return'] < 10 and r['slope_5d'] > 0 and r['max_daily_gain'] < 5),
    ("量比>=1.4+D5 2%~10%", lambda r: r['signal_vol_ratio'] >= 1.4 and 2 < r['d5_return'] < 10),
    ("量比>=1.4+斜率>0+最大单日涨<4%", lambda r: r['signal_vol_ratio'] >= 1.4 and r['slope_5d'] > 0 and r['max_daily_gain'] < 4),
    ("量比>=1.4+D5 3%~10%+最大单日涨<5%", lambda r: r['signal_vol_ratio'] >= 1.4 and 3 < r['d5_return'] < 10 and r['max_daily_gain'] < 5),
    ("量比>=1.6+D5 2%~10%+最大单日涨<4%", lambda r: r['signal_vol_ratio'] >= 1.6 and 2 < r['d5_return'] < 10 and r['max_daily_gain'] < 4),
]

print(f"\n{'过滤条件':<45} {'笔数':>5} {'胜率':>7} {'均收益':>8} {'峰值天':>6}")
print("-" * 78)
for name, fn in filters:
    subset = [r for r in records if fn(r)]
    if len(subset) < 10:
        continue
    w = len([r for r in subset if r['is_win'] == 1])
    avg = statistics.mean([r['return_pct'] for r in subset])
    avg_peak = statistics.mean([r['peak_day'] for r in subset])
    print(f"  {name:<43} {len(subset):>5} {w/len(subset)*100:>6.1f}% {avg:>+7.2f}% {avg_peak:>5.1f}")

# ================================================================
# 最佳组合详细统计
# ================================================================
print(f"\n{'=' * 70}")
print("6. 最佳组合详情")
print("=" * 70)

best_fn = lambda r: r['signal_vol_ratio'] >= 1.4 and 3 < r['d5_return'] < 10 and r['max_daily_gain'] < 5
best = [r for r in records if best_fn(r)]
if best:
    wins_b = [r for r in best if r['is_win'] == 1]
    losses_b = [r for r in best if r['is_win'] == 0]
    print(f"  笔数: {len(best)}")
    print(f"  胜率: {len(wins_b)/len(best)*100:.1f}%")
    print(f"  均收益: {statistics.mean([r['return_pct'] for r in best]):+.2f}%")
    if wins_b:
        print(f"  赢均: {statistics.mean([r['return_pct'] for r in wins_b]):+.2f}%")
    if losses_b:
        print(f"  亏均: {statistics.mean([r['return_pct'] for r in losses_b]):+.2f}%")
    print(f"  均峰值天: {statistics.mean([r['peak_day'] for r in best]):.1f}")
    print(f"  总收益: {sum(r['return_pct'] for r in best):+.2f}%")

    print(f"\n  峰值天分布:")
    for d in [1,3,5,7,10,15,20]:
        cnt = sum(1 for r in best if r['peak_day'] <= d)
        print(f"    <=D{d}: {cnt}/{len(best)} ({cnt/len(best)*100:.0f}%)")
