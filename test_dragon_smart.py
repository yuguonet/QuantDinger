#!/usr/bin/env python3
"""
连板猎手 — 智能路径版

核心逻辑:
  D0涨停后，根据D1表现自动选择路径:
  
  路径A: 龙回头
    D1回调(收盘<D0收盘 或 开盘<阈值) → D2开盘低吸
    特征: 缩量调整、不破位 → 最佳预埋点
  
  路径B: 直接拉伸
    D1继续涨(开盘>=阈值 且 收盘>=D0收盘) → D1开盘追入
    特征: 消息/政策驱动、连续拉升

  自动判断: D1盘后看数据，决定走哪条路

用法:
  python3 test_dragon_smart.py
  python3 test_dragon_smart.py --all-trades
  python3 test_dragon_smart.py --compare  # 对比智能 vs 纯V1
"""
from __future__ import annotations
import json, time, argparse
from collections import defaultdict
from datetime import datetime
from typing import List, Dict
import requests

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

def _code_to_sina(code):
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith(("6", "5")): return f"sh{c}"
    elif c.startswith(("0", "3", "2")): return f"sz{c}"
    elif c.startswith("68"): return f"sh{c}"
    return ""

def fetch_kline(code, count=300):
    tc = _code_to_sina(code)
    if not tc: return []
    try:
        resp = _SESSION.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{tc},day,,,{count},qfq"},
            headers={"Referer": "https://gu.qq.com/"}, timeout=10,
        )
        data = resp.json()
        if not isinstance(data, dict) or int(data.get("code", 0)) != 0: return []
        root = (data.get("data") or {}).get(tc)
        if not isinstance(root, dict): return []
        rows = root.get("qfqday") or root.get("day") or []
        bars = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6: continue
            try:
                bars.append({
                    "time": str(r[0])[:10], "open": float(r[1]),
                    "high": float(r[3]), "low": float(r[4]),
                    "close": float(r[2]), "volume": float(r[5]) * 100,
                })
            except: continue
        bars.sort(key=lambda x: x["time"])
        return bars
    except:
        return []

def get_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

# ================================================================
# V2预埋信号检测
# ================================================================

def detect_signals(bars, idx):
    signals = []
    if idx >= 5:
        avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5
        if avg5 > 0:
            avg3 = sum(b['volume'] for b in bars[idx-3:idx]) / 3
            vr = bars[idx]['volume'] / avg5
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            if vr >= 1.5 and chg >= 1.0 and avg3 < avg5 * 0.9:
                signals.append('volume_breakout')
    if idx >= 4:
        yc = 0
        for j in range(idx-2, idx+1):
            prev_c = bars[j-1]['close']
            if prev_c <= 0: continue
            chg = (bars[j]['close'] / prev_c - 1) * 100
            if 0.3 <= chg <= 3.0 and bars[j]['close'] > bars[j]['open']:
                yc += 1
        if yc >= 3:
            avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5 if idx >= 5 else 0
            if avg5 > 0 and bars[idx]['volume'] >= avg5 * 0.8:
                c3 = bars[idx-3]['close']
                if c3 > 0 and (bars[idx]['close'] / c3 - 1) * 100 <= 8:
                    signals.append('small_yang_stack')
    if idx >= 5:
        avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5
        if avg5 > 0:
            vr = bars[idx]['volume'] / avg5
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            c5 = bars[idx-5]['close']
            prev5chg = (prev_c / c5 - 1) * 100 if c5 > 0 else 0
            if vr >= 2.0 and chg >= 2.0 and abs(prev5chg) <= 5:
                signals.append('bottom_volume')
    if idx >= 5:
        lb = min(10, idx)
        max_c = max(b['close'] for b in bars[idx-lb:idx])
        if bars[idx]['close'] > max_c * 1.005:
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            if chg <= 5:
                signals.append('break_high')
    return signals

def has_preload_signal(bars, d0_idx, lookback=10):
    start = max(0, d0_idx - lookback)
    for i in range(start, d0_idx):
        sigs = detect_signals(bars, i)
        if sigs:
            return True, bars[i]['time'], sigs[0]
    return False, None, None

# ================================================================
# 智能路径策略
# ================================================================

def find_first_limits(bars, threshold):
    result = []
    for i in range(2, len(bars)):
        prev_c = bars[i-1]['close']
        if prev_c <= 0: continue
        ret = (bars[i]['close'] / prev_c - 1)
        if ret < threshold * 0.98: continue
        prev2_c = bars[i-2]['close']
        if prev2_c > 0 and (bars[i-1]['close'] / prev2_c - 1) >= threshold * 0.98:
            continue
        result.append(i)
    return result

def classify_d1(fl_close, d1_open, d1_close, d1_high, d1_low, d1_vol, fl_vol, board_type):
    """
    根据D1表现判断路径:
    返回: 'pullback'(龙回头) 或 'momentum'(直接拉伸)
    
    龙回头条件:
    1. D1收阴(回调)
    2. D1缩量(量比<1.2)
    3. D1不破D0收盘(最低价>=D0收盘*0.97)
    4. D1跌幅有限(>-8%)
    """
    d1_gap = (d1_open / fl_close - 1) * 100
    d1_change = (d1_close / fl_close - 1) * 100
    d1_vol_ratio = d1_vol / fl_vol if fl_vol > 0 else 0
    d1_low_vs_d0 = (d1_low / fl_close - 1) * 100
    
    # 龙回头: D1回调 + 缩量 + 不破位 + 跌幅有限
    if (d1_change < 0 and                    # D1收阴
        d1_vol_ratio < 1.2 and                # 缩量
        d1_low_vs_d0 > -3.0 and               # 不破D0收盘(容忍-3%)
        d1_change > -8.0):                    # 跌幅有限
        return 'pullback'
    else:
        return 'momentum'

def strategy_smart(bars, code, use_v2_filter=True, lookback=10,
                   min_vol_ratio=2.0, max_upper_shadow=0.5,
                   hold_days=20, stop_loss=-10.0, trailing_stop=-8.0):
    """
    智能路径策略:
    - 有V2预埋信号的涨停才考虑
    - D1表现决定走龙回头还是直接拉伸
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    
    first_limits = find_first_limits(bars, threshold)
    trades = []
    
    for fl_idx in first_limits:
        if fl_idx + 2 >= len(bars): continue
        
        fl = bars[fl_idx]
        fl_prev = bars[fl_idx - 1]
        fl_prev2 = bars[fl_idx - 2]
        fl_close = fl['close']
        fl_high = fl['high']
        fl_low = fl['low']
        fl_vol = fl['volume']
        fl_prev_close = fl_prev['close']
        fl_prev_vol = fl_prev['volume']
        fl_prev2_close = fl_prev2['close']
        
        if fl_prev_close <= 0 or fl_close <= 0 or fl_prev2_close <= 0:
            continue
        
        # V2过滤: 有预埋信号才考虑
        has_preload, preload_date, preload_type = has_preload_signal(bars, fl_idx, lookback)
        if use_v2_filter and not has_preload:
            continue
        
        # V1筛选
        bar_range = (fl_high - fl_low) / fl_prev2_close * 100
        if bar_range < 0.2: continue
        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        if vol_ratio < min_vol_ratio: continue
        upper_shadow = (fl_high - fl_close) / fl_prev2_close * 100
        if upper_shadow >= max_upper_shadow: continue
        
        # D1分析
        d1 = bars[fl_idx + 1]
        d1_open = d1['open']
        d1_close = d1['close']
        d1_high = d1['high']
        d1_low = d1['low']
        d1_vol = d1['volume']
        
        if d1_open <= 0: continue
        
        # 路径判断
        path = classify_d1(fl_close, d1_open, d1_close, d1_high, d1_low, d1_vol, fl_vol, board_type)
        
        # 根据路径选择入场点
        if path == 'pullback':
            # 龙回头: D2开盘买入(低吸)
            d2 = bars[fl_idx + 2]
            entry_price = d2['open']
            entry_idx = fl_idx + 2
            entry_date = d2['time']
        else:
            # 直接拉伸: D1开盘买入(追涨)
            entry_price = d1_open
            entry_idx = fl_idx + 1
            entry_date = d1['time']
        
        if entry_price <= 0: continue
        
        # 持仓回测
        peak = entry_price
        exit_p = entry_price
        exit_d = 0
        for d in range(1, hold_days + 1):
            idx = entry_idx + d
            if idx >= len(bars): break
            b = bars[idx]
            if b['high'] > peak: peak = b['high']
            if b['low'] <= peak * (1 + trailing_stop / 100) and d > 1:
                exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
            if b['low'] <= entry_price * (1 + stop_loss / 100):
                exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
            exit_p = b['close']; exit_d = d
        
        trades.append({
            'code': code, 'board': get_board_name(code),
            'path': path,
            'path_label': '龙回头' if path == 'pullback' else '直接拉伸',
            'preload_date': preload_date,
            'preload_type': preload_type,
            'd0_date': fl['time'],
            'd1_date': d1['time'],
            'entry_date': entry_date,
            'entry_price': round(entry_price, 3),
            'exit_price': round(exit_p, 3),
            'exit_day': exit_d,
            'return_pct': round((exit_p / entry_price - 1) * 100, 2),
            'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
            'd0_vol_ratio': round(vol_ratio, 2),
            'd1_change': round((d1_close / fl_close - 1) * 100, 2),
            'd1_gap': round((d1_open / fl_close - 1) * 100, 2),
            'd1_vol_ratio': round(d1_vol / fl_vol if fl_vol > 0 else 0, 2),
            'd1_amplitude': round((d1_high - d1_low) / fl_close * 100, 2),
            'd1_low_vs_d0': round((d1_low / fl_close - 1) * 100, 2),
        })
    
    return trades

# 纯V1策略(对比用)
def strategy_v1_pure(bars, code, min_vol_ratio=2.0, max_upper_shadow=0.5,
                     hold_days=20, stop_loss=-10.0, trailing_stop=-8.0):
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    first_limits = find_first_limits(bars, threshold)
    trades = []
    for fl_idx in first_limits:
        if fl_idx + 1 >= len(bars): continue
        fl = bars[fl_idx]; fl_prev = bars[fl_idx-1]; fl_prev2 = bars[fl_idx-2]
        fl_close = fl['close']; fl_high = fl['high']; fl_low = fl['low']; fl_vol = fl['volume']
        fl_prev_close = fl_prev['close']; fl_prev_vol = fl_prev['volume']; fl_prev2_close = fl_prev2['close']
        if fl_prev_close <= 0 or fl_close <= 0 or fl_prev2_close <= 0: continue
        bar_range = (fl_high - fl_low) / fl_prev2_close * 100
        if bar_range < 0.2: continue
        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        if vol_ratio < min_vol_ratio: continue
        upper_shadow = (fl_high - fl_close) / fl_prev2_close * 100
        if upper_shadow >= max_upper_shadow: continue
        d1 = bars[fl_idx+1]; entry_price = d1['open']
        if entry_price <= 0: continue
        peak = entry_price; exit_p = entry_price; exit_d = 0
        for d in range(1, hold_days+1):
            idx = fl_idx+1+d
            if idx >= len(bars): break
            b = bars[idx]
            if b['high'] > peak: peak = b['high']
            if b['low'] <= peak*(1+trailing_stop/100) and d>1: exit_p=peak*(1+trailing_stop/100); exit_d=d; break
            if b['low'] <= entry_price*(1+stop_loss/100): exit_p=entry_price*(1+stop_loss/100); exit_d=d; break
            exit_p=b['close']; exit_d=d
        trades.append({
            'code': code, 'board': get_board_name(code), 'path': 'v1_pure',
            'path_label': '纯V1', 'd0_date': fl['time'],
            'entry_date': d1['time'], 'entry_price': round(entry_price,3),
            'exit_price': round(exit_p,3), 'exit_day': exit_d,
            'return_pct': round((exit_p/entry_price-1)*100,2),
            'peak_return_pct': round((peak/entry_price-1)*100,2),
            'd0_vol_ratio': round(vol_ratio,2),
        })
    return trades

# ================================================================
# 测试列表
# ================================================================

TEST_CODES = [
    "601991", "002918", "600530", "600172", "001259", "600396",
    "000601", "000767", "000925", "002208", "002498", "002560",
    "603007", "603052", "603070", "603203", "002081", "600379",
    "603399", "000402", "002805", "603937", "000066", "000553",
    "000783", "001316", "002149", "002158", "002464", "002640",
    "002989", "603045", "002580", "002297", "002858", "002033",
    "002468", "600683", "605388", "600488", "000586", "000720",
    "000950", "002552", "605006", "002475", "000858", "600276",
    "603259", "000002", "002535", "002512", "600726", "002190",
    "002095", "000958", "300746", "600198", "600633", "600770",
    "600855", "300152", "300871", "002366", "001208", "603956",
    "300289", "300106", "000637", "603656", "601177", "600881",
    "002510", "000753", "600460", "603859", "603366", "605208",
    "300385", "300515", "300986", "301077", "301071", "301150",
    "301109", "301102", "301059", "301023", "301086", "301101",
]

def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号"); return
    wr = sum(1 for t in trades if t['return_pct'] > 0) / len(trades) * 100
    avg = sum(t['return_pct'] for t in trades) / len(trades)
    peak = sum(t['peak_return_pct'] for t in trades) / len(trades)
    ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
    ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
    if ws and ls:
        pl = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls))
    elif ws:
        pl = 999.0
    else:
        pl = 0.0
    print(f"  {label}: {len(trades):>4}笔 胜率{wr:>5.1f}% 均收益{avg:>+6.2f}% 均峰值{peak:>+6.2f}% 盈亏比{pl:.2f}")

def main():
    parser = argparse.ArgumentParser(description="智能路径: 龙回头低吸 vs 直接拉伸追涨")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--no-filter", action="store_true", help="不用V2过滤")
    parser.add_argument("--all-trades", action="store_true")
    args = parser.parse_args()
    
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_v2 = not args.no_filter
    
    print(f"{'=' * 80}")
    print(f"智能路径策略: 龙回头低吸 vs 直接拉伸追涨")
    print(f"{'=' * 80}")
    print(f"股票: {len(codes)}只 | V2过滤: {'开' if use_v2 else '关'}")
    
    smart_trades = []
    v1_trades = []
    success = 0
    
    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)})", end=" ", flush=True)
        bars = fetch_kline(code, args.days)
        if not bars:
            print("❌"); continue
        print(f"✓{len(bars)}根", end=" ", flush=True)
        
        trades = strategy_smart(bars, code, use_v2_filter=use_v2)
        smart_trades.extend(trades)
        
        if args.compare:
            v1 = strategy_v1_pure(bars, code)
            v1_trades.extend(v1)
        
        print(f"→ {len(trades)}笔")
        success += 1
        time.sleep(0.3)
    
    # ===== 输出 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只")
    print(f"{'=' * 80}")
    
    if smart_trades:
        print(f"\n📊 智能路径总览:")
        print_stats(smart_trades, "智能路径")
        
        # 按路径分组
        pullback = [t for t in smart_trades if t['path'] == 'pullback']
        momentum = [t for t in smart_trades if t['path'] == 'momentum']
        
        print(f"\n📊 路径对比:")
        print_stats(pullback, "龙回头(D2低吸)")
        print_stats(momentum, "直接拉伸(D1追涨)")
        
        # 龙回头的预埋信号
        if pullback:
            print(f"\n  龙回头预埋信号:")
            by_sig = defaultdict(list)
            for t in pullback:
                if t.get('preload_type'):
                    by_sig[t['preload_type']].append(t)
            for sig, ts in sorted(by_sig.items(), key=lambda x: -len(x[1])):
                print_stats(ts, f"    {sig}")
        
        # D1涨跌分布
        print(f"\n  龙回头D1表现:")
        d1_neg = [t for t in pullback if t.get('d1_change', 0) < 0]
        d1_pos = [t for t in pullback if t.get('d1_change', 0) >= 0]
        if d1_neg:
            print_stats(d1_neg, f"    D1收阴({len(d1_neg)}笔)")
        if d1_pos:
            print_stats(d1_pos, f"    D1收阳({len(d1_pos)}笔)")
        
        # TOP收益
        print(f"\n  🏆 龙回头TOP5:")
        for t in sorted(pullback, key=lambda x: -x['peak_return_pct'])[:5]:
            print(f"    {t['code']} {t['d0_date']} D1{t['d1_change']:+.1f}% → D2买入{t['entry_price']:.2f} 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
        
        print(f"\n  🏆 直接拉伸TOP5:")
        for t in sorted(momentum, key=lambda x: -x['peak_return_pct'])[:5]:
            print(f"    {t['code']} {t['d0_date']} D1开盘{t['entry_price']:.2f} 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
    
    # 对比
    if args.compare and v1_trades:
        print(f"\n{'=' * 80}")
        print(f"对比: 智能路径 vs 纯V1")
        print(f"{'=' * 80}")
        print_stats(smart_trades, "智能路径")
        print_stats(v1_trades, "纯V1(D1直接买)")
        
        s_wr = sum(1 for t in smart_trades if t['return_pct'] > 0) / len(smart_trades) * 100 if smart_trades else 0
        v_wr = sum(1 for t in v1_trades if t['return_pct'] > 0) / len(v1_trades) * 100 if v1_trades else 0
        s_avg = sum(t['return_pct'] for t in smart_trades) / len(smart_trades) if smart_trades else 0
        v_avg = sum(t['return_pct'] for t in v1_trades) / len(v1_trades) if v1_trades else 0
        print(f"\n  提升:")
        print(f"    胜率: {v_wr:.1f}% → {s_wr:.1f}% ({s_wr-v_wr:+.1f}%)")
        print(f"    均收益: {v_avg:+.2f}% → {s_avg:+.2f}% ({s_avg-v_avg:+.2f}%)")
    
    # 交易明细
    if args.all_trades and smart_trades:
        print(f"\n📋 交易明细:")
        print(f"{'路径':<6} {'代码':<8} {'板块':<4} {'预埋':<20} {'D0日':<11} {'D1涨跌':>6} {'买入日':<11} {'入场':>7} {'出场':>7} {'天':>2} {'收益':>7} {'峰值':>7}")
        print("-" * 110)
        for t in sorted(smart_trades, key=lambda x: x['entry_date']):
            path = "龙回头" if t['path'] == 'pullback' else "拉伸"
            print(f"{path:<6} {t['code']:<8} {t['board']:<4} {t.get('preload_type',''):<20} "
                  f"{t['d0_date']:<11} {t.get('d1_change',0):>+5.1f}% {t['entry_date']:<11} "
                  f"{t['entry_price']:>7.2f} {t['exit_price']:>7.2f} {t['exit_day']:>2} "
                  f"{t['return_pct']:>+6.2f}% {t['peak_return_pct']:>+6.2f}%")
    
    # 导出
    if smart_trades:
        with open("test_dragon_smart_result.json", "w", encoding="utf-8") as f:
            json.dump(smart_trades, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_smart_result.json")

if __name__ == "__main__":
    main()
