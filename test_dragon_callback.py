#!/usr/bin/env python3
"""
龙回头 — 独立策略

逻辑:
  1. D-N: 股票涨停 (龙)
  2. D-N+1 ~ D-1: 回调至少2天, 不破关键位 (蓄力)
  3. D0: 放量涨≥5%, 弱转强确认 (回头)
  4. V1质量检查: 量比>2x, 上影<0.5%, 排除一字板
  5. D1开盘: 买入

与V1互不干扰: V1看首板次日, 龙回头看回调后再起
"""
from __future__ import annotations
import json, time, argparse, math
from collections import defaultdict
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
# 预埋信号 (仅保留有效的)
# ================================================================

def detect_signals(bars, idx):
    signals = []
    if idx >= 5:
        avg5 = sum(b['volume'] for b in bars[idx-5:idx]) / 5
        if avg5 > 0:
            vr = bars[idx]['volume'] / avg5
            prev_c = bars[idx-1]['close']
            chg = (bars[idx]['close'] / prev_c - 1) * 100 if prev_c > 0 else 0
            avg3 = sum(b['volume'] for b in bars[idx-3:idx]) / 3
            if vr >= 1.5 and chg >= 1.0 and avg3 < avg5 * 0.9:
                signals.append('volume_breakout')
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

# ================================================================
# 核心逻辑
# ================================================================

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

def find_limit_ups(bars, board_type):
    """找到所有涨停日"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result

def run_backtest(bars, entry_idx, entry_price, hold_days=20, stop_loss=-10.0, trailing_stop=-8.0):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    for d in range(1, hold_days + 1):
        idx = entry_idx + d
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
        exit_p = b['close']; exit_d = d
    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

def strategy_dragon_callback(bars, code, min_pullback_days=2, min_rebound_pct=5.0,
                             min_vol_ratio=2.0, max_upper_shadow=0.5,
                             hold_days=20, stop_loss=-10.0, trailing_stop=-8.0,
                             use_preload_filter=True):
    """
    龙回头独立策略:
    D-N涨停 → 回调≥2天 → D0放量涨≥5%(弱转强) → V1质量检查 → D1买入
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    
    limit_ups = find_limit_ups(bars, board_type)
    trades = []
    used_ranges = []  # 避免同一段涨停重复触发
    
    for lu_idx in limit_ups:
        # 从涨停日往后找: 回调期 + 弱转强日
        # 回调期: 至少min_pullback_days天, 期间收盘<涨停日收盘
        lu_close = bars[lu_idx]['close']
        lu_high = bars[lu_idx]['high']
        lu_vol = bars[lu_idx]['volume']
        
        # 找回调期结束点
        pullback_end = None
        for j in range(lu_idx + 1, min(lu_idx + 20, len(bars))):
            # 回调期: 收盘低于涨停日收盘, 或至少没创新高
            if bars[j]['close'] < lu_close:
                pullback_end = j
            elif j >= lu_idx + min_pullback_days:
                # 已经回调够天数, 且现在站回来了
                break
            else:
                # 涨停后直接继续涨, 不是龙回头
                break
        
        if pullback_end is None:
            continue
        
        # 从回调结束后找弱转强日: 涨≥5%
        for j in range(pullback_end + 1, min(pullback_end + 10, len(bars))):
            prev_c = bars[j-1]['close']
            if prev_c <= 0: continue
            chg = (bars[j]['close'] / prev_c - 1) * 100
            
            if chg >= min_rebound_pct:
                # 弱转强日! 做V1质量检查
                d0_idx = j
                d0 = bars[d0_idx]
                d0_prev = bars[d0_idx - 1]
                d0_prev2 = bars[d0_idx - 2] if d0_idx >= 2 else None
                
                # 检查是否已被使用
                skip = False
                for (s, e) in used_ranges:
                    if abs(d0_idx - s) <= 2 or abs(d0_idx - e) <= 2:
                        skip = True; break
                if skip: continue
                
                # V1质量检查 (用d0本身的涨停质量)
                d0_close = d0['close']
                d0_high = d0['high']
                d0_low = d0['low']
                d0_vol = d0['volume']
                d0_prev_close = d0_prev['close']
                d0_prev_vol = d0_prev['volume']
                
                if d0_prev_close <= 0: continue
                
                # 量比
                vol_ratio = d0_vol / d0_prev_vol if d0_prev_vol > 0 else 0
                if vol_ratio < min_vol_ratio: continue
                
                # 上影线
                if d0_prev2:
                    ref = d0_prev2['close']
                else:
                    ref = d0_prev_close
                upper_shadow = (d0_high - d0_close) / ref * 100 if ref > 0 else 99
                if upper_shadow >= max_upper_shadow: continue
                
                # 排除一字板
                bar_range = (d0_high - d0_low) / ref * 100 if ref > 0 else 0
                if bar_range < 0.2: continue
                
                # 预埋信号 (在涨停日和回调期间找)
                has_preload = False
                preload_type = None
                preload_date = None
                for k in range(max(0, lu_idx - 5), d0_idx):
                    sigs = detect_signals(bars, k)
                    if sigs:
                        has_preload = True
                        preload_type = sigs[0]
                        preload_date = bars[k]['time']
                        break
                
                if use_preload_filter and not has_preload:
                    continue
                
                # D1买入
                if d0_idx + 1 >= len(bars): continue
                d1 = bars[d0_idx + 1]
                entry_price = d1['open']
                if entry_price <= 0: continue
                
                # D1收阴排除 (龙回头的D1判断)
                d1_change = (d1['close'] / d0_close - 1) * 100
                if d1_change < 0:
                    continue
                
                result = run_backtest(bars, d0_idx + 1, entry_price, hold_days, stop_loss, trailing_stop)
                if not result: continue
                
                used_ranges.append((lu_idx, d0_idx))
                
                trades.append({
                    'code': code, 'board': get_board_name(code),
                    'path': 'dragon_callback',
                    'path_label': '龙回头',
                    'lu_date': bars[lu_idx]['time'],
                    'pullback_days': d0_idx - lu_idx,
                    'd0_date': d0['time'],
                    'd0_change': round(chg, 2),
                    'd1_date': d1['time'],
                    'entry_date': d1['time'],
                    'entry_price': round(entry_price, 3),
                    'vol_ratio': round(vol_ratio, 2),
                    'upper_shadow': round(upper_shadow, 2),
                    'd1_change': round(d1_change, 2),
                    'has_preload': has_preload,
                    'preload_type': preload_type,
                    'preload_date': preload_date,
                    **result,
                })
                break  # 一个涨停日只触发一次
    
    return trades

# 同时跑V1作为对比
def strategy_v1(bars, code, min_vol_ratio=2.0, max_upper_shadow=0.5,
                hold_days=20, stop_loss=-10.0, trailing_stop=-8.0,
                use_preload_filter=True):
    """V1基线策略 (带D1收阴排除)"""
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198
    
    result = []
    for i in range(2, len(bars)):
        prev_c = bars[i-1]['close']
        if prev_c <= 0: continue
        ret = (bars[i]['close'] / prev_c - 1)
        if ret < threshold * 0.98: continue
        prev2_c = bars[i-2]['close']
        if prev2_c > 0 and (bars[i-1]['close'] / prev2_c - 1) >= threshold * 0.98:
            continue
        
        fl = bars[i]
        fl_close = fl['close']
        fl_high = fl['high']
        fl_vol = fl['volume']
        fl_prev_close = bars[i-1]['close']
        fl_prev_vol = bars[i-1]['volume']
        ref = bars[i-2]['close'] if i >= 2 else fl_prev_close
        
        if ref <= 0: continue
        
        bar_range = (fl_high - fl['low']) / ref * 100
        if bar_range < 0.2: continue
        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        if vol_ratio < min_vol_ratio: continue
        upper_shadow = (fl_high - fl_close) / ref * 100
        if upper_shadow >= max_upper_shadow: continue
        
        has_preload = False
        preload_type = None
        for k in range(max(0, i - 10), i):
            sigs = detect_signals(bars, k)
            if sigs:
                has_preload = True
                preload_type = sigs[0]
                break
        
        if use_preload_filter and not has_preload:
            continue
        
        if i + 1 >= len(bars): continue
        d1 = bars[i + 1]
        entry_price = d1['open']
        if entry_price <= 0: continue
        
        d1_change = (d1['close'] / fl_close - 1) * 100
        if d1_change < 0: continue  # D1收阴排除
        
        bt = run_backtest(bars, i + 1, entry_price, hold_days, stop_loss, trailing_stop)
        if not bt: continue
        
        result.append({
            'code': code, 'board': get_board_name(code),
            'path': 'v1', 'path_label': 'V1',
            'd0_date': fl['time'],
            'entry_date': d1['time'],
            'entry_price': round(entry_price, 3),
            'vol_ratio': round(vol_ratio, 2),
            'd1_change': round(d1_change, 2),
            'has_preload': has_preload,
            'preload_type': preload_type,
            **bt,
        })
    
    return result

# ================================================================
# 测试列表 (去蓝筹)
# ================================================================

TEST_CODES = [
    "002918","600530","600172","001259","000601","000767","000925","002208","002498","002560",
    "603007","603052","603070","603203","002081","600379","603399","000402","002805","603937",
    "000066","000553","000783","001316","002149","002158","002464","002640","002989","603045",
    "002580","002297","002858","002033","002468","600683","605388","000586","000720","000950",
    "002552","605006","002535","002512","002190","002095","300746","600633","600770","300152",
    "300871","002366","001208","603956","300289","300106","000637","603656","601177","600881",
    "002510","000753","600460","603859","603366","605208","300385","300515","300986","301077",
    "301071","301150","301109","301102","301059","301023","301086","301101",
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
    parser = argparse.ArgumentParser(description="龙回头独立策略 + V1对比")
    parser.add_argument("--codes", default="")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--no-preload", action="store_true", help="不用V2预埋过滤")
    parser.add_argument("--no-d1-filter", action="store_true", help="不用D1收阴排除")
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=2, help="最少回调天数")
    parser.add_argument("--rebound", type=float, default=5.0, help="弱转强最低涨幅%")
    args = parser.parse_args()
    
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_preload = not args.no_preload
    
    print(f"{'=' * 80}")
    print(f"龙回头独立策略")
    print(f"{'=' * 80}")
    print(f"配置: 回调≥{args.pullback}天 | 弱转强≥{args.rebound}% | V2预埋: {'开' if use_preload else '关'}")
    print(f"股票: {len(codes)}只")
    
    dc_trades = []
    v1_trades = []
    success = 0
    
    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)})", end=" ", flush=True)
        bars = fetch_kline(code, args.days)
        if not bars:
            print("❌"); continue
        print(f"✓{len(bars)}根", end=" ", flush=True)
        
        dc = strategy_dragon_callback(bars, code,
                                       min_pullback_days=args.pullback,
                                       min_rebound_pct=args.rebound,
                                       use_preload_filter=use_preload)
        dc_trades.extend(dc)
        
        v1 = strategy_v1(bars, code, use_preload_filter=use_preload)
        v1_trades.extend(v1)
        
        print(f"→ 龙回头{len(dc)}笔 V1{len(v1)}笔")
        success += 1
        time.sleep(0.3)
    
    # ===== 输出 =====
    print(f"\n{'=' * 80}")
    print(f"结果: {success}只")
    print(f"{'=' * 80}")
    
    if dc_trades or v1_trades:
        print(f"\n📊 总览:")
        print_stats(dc_trades, "龙回头")
        print_stats(v1_trades, "V1(对比)")
        
        # 龙回头信号分析
        if dc_trades:
            print(f"\n  龙回头预埋信号:")
            by_sig = defaultdict(list)
            for t in dc_trades:
                sig = t.get('preload_type', 'none')
                by_sig[sig].append(t)
            for sig, ts in sorted(by_sig.items(), key=lambda x: -len(x[1])):
                print_stats(ts, f"    {sig}")
            
            # 回调天数分布
            print(f"\n  回调天数分布:")
            for lo, hi, label in [(2,4,"2-3天"), (4,7,"4-6天"), (7,14,"7-13天"), (14,20,"14-20天")]:
                seg = [t for t in dc_trades if lo <= t['pullback_days'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            
            # 弱转强涨幅分布
            print(f"\n  弱转强涨幅分布:")
            for lo, hi, label in [(5,8,"5-8%"), (8,10,"8-10%"), (10,20,"10-20%")]:
                seg = [t for t in dc_trades if lo <= t['d0_change'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")
            
            # TOP5
            print(f"\n  🏆 龙回头TOP5:")
            for t in sorted(dc_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['d0_date']}弱转强{t['d0_change']:+.1f}% → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
            
            print(f"\n  💀 龙回头BOTTOM5:")
            for t in sorted(dc_trades, key=lambda x: x['return_pct'])[:5]:
                print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['d0_date']}弱转强{t['d0_change']:+.1f}% → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
        
        # 合并统计
        all_trades = dc_trades + v1_trades
        if len(dc_trades) > 0 and len(v1_trades) > 0:
            print(f"\n📊 合并(龙回头+V1):")
            print_stats(all_trades, "合并")
    
    # 交易明细
    if args.all_trades and dc_trades:
        print(f"\n📋 龙回头交易明细:")
        for t in sorted(dc_trades, key=lambda x: x['entry_date']):
            print(f"  {t['code']:<8} {t['board']:<6} 涨停{t['lu_date']} → 回调{t['pullback_days']}天 → "
                  f"{t['d0_date']}弱转强{t['d0_change']:>+5.1f}% → {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}%")
    
    # 导出
    all_out = dc_trades + v1_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json")

if __name__ == "__main__":
    main()
