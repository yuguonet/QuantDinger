#!/usr/bin/env python3
"""
龙回头 - 独立策略

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
from kline_cache import fetch_kline

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

def run_backtest(bars, entry_idx, entry_price, hold_days=20, stop_loss=-10.0, trailing_stop=-8.0, board_type="main"):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    d1_limit_up = False

    for d in range(1, hold_days + 1):
        idx = entry_idx + d
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']

        # D1判断: 是否涨停
        if d == 1:
            d1_ret = (b['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

        # D1没涨停 → D+1快速离场: 高开>2%直接抛, 否则D1最高价-1%
        if d == 2 and not d1_limit_up:
            d1_bar = bars[entry_idx + 1]
            d1_high = d1_bar['high']
            d1_close = d1_bar['close']
            d1_change = (b['open'] / d1_close - 1) * 100 if d1_close > 0 else 0
            # 止损优先
            if b['low'] <= entry_price * (1 + stop_loss / 100):
                exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
            # 高开>2%直接抛
            if d1_change > 2.0:
                exit_p = b['open']; exit_d = d; break
            # D1最高价-1%追踪离场
            exit_trigger = d1_high * 0.99
            if b['low'] <= exit_trigger:
                exit_p = exit_trigger; exit_d = d; break
            exit_p = b['close']; exit_d = d; break

        # D1涨停 → 原有逻辑: 持仓20天+止损+追踪止损
        if d > 1 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break
        if b['low'] <= entry_price * (1 + stop_loss / 100):
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break
        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
        'd1_limit_up': d1_limit_up,
    }

def strategy_dragon_callback(bars, code, min_pullback_days=3, max_pullback_days=11,
                             max_last_chg=3.0,
                             hold_days=20, stop_loss=-5.0, trailing_stop=-5.0):
    """
    龙回头v3:
    D-N涨停 → 回调3-11天 → 末期十字星/小阳+量<0.8x(弱转强信号) → D+1买入
    """
    board_type = get_board_type(code)
    threshold = 0.098 if board_type == "main" else 0.198

    limit_ups = find_limit_ups(bars, board_type)
    trades = []
    used_ranges = []

    for lu_idx in limit_ups:
        lu_close = bars[lu_idx]['close']
        lu_vol = bars[lu_idx]['volume']

        # 找回调期: close < lu_close 的最后一天
        pullback_end = None
        for j in range(lu_idx + 1, min(lu_idx + 20, len(bars))):
            if bars[j]['close'] < lu_close:
                pullback_end = j
            elif j >= lu_idx + min_pullback_days:
                break
            else:
                break

        if pullback_end is None:
            continue

        pullback_days = pullback_end - lu_idx
        if pullback_days < min_pullback_days or pullback_days > max_pullback_days:
            continue

        # 弱转强信号: 最后一天十字星/小阳 + 量比<阈值
        last_pb = bars[pullback_end]
        last_pb_prev = bars[pullback_end - 1] if pullback_end > 0 else bars[lu_idx]
        last_pb_prev_c = last_pb_prev['close']
        if last_pb_prev_c <= 0: continue
        last_chg = (last_pb['close'] / last_pb_prev_c - 1) * 100
        last_vol_r = last_pb['volume'] / lu_vol if lu_vol > 0 else 0

        # 排除大阴(跌>3%)或放量(>2x)
        if last_chg < -3.0 or last_vol_r > 2.0:
            continue

        # 十字星: |涨跌|<1.5%, 或小阳: 涨<max_last_chg%
        is_signal = abs(last_chg) < 1.5 or (0 < last_chg < max_last_chg)
        if not is_signal:
            continue

        # 检查是否已被使用
        skip = False
        for (s, e) in used_ranges:
            if abs(pullback_end - s) <= 2 or abs(pullback_end - e) <= 2:
                skip = True; break
        if skip: continue

        # D+1买入
        if pullback_end + 1 >= len(bars): continue
        d1 = bars[pullback_end + 1]
        entry_price = d1['open']
        if entry_price <= 0: continue

        # D+1收阴排除
        d1_change = (d1['close'] / last_pb['close'] - 1) * 100
        if d1_change < 0:
            continue

        used_ranges.append((lu_idx, pullback_end))

        result = run_backtest(bars, pullback_end + 1, entry_price, hold_days, stop_loss, trailing_stop, board_type)
        if not result: continue

        trades.append({
            'code': code, 'board': get_board_name(code),
            'path': 'dragon_callback',
            'path_label': '龙回头',
            'lu_date': bars[lu_idx]['time'],
            'pullback_days': pullback_days,
            'signal_date': last_pb['time'],
            'signal_chg': round(last_chg, 2),
            'signal_vol_r': round(last_vol_r, 2),
            'entry_date': d1['time'],
            'entry_price': round(entry_price, 3),
            'd1_change': round(d1_change, 2),
            **result,
        })

    return trades

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
        # 前10天不是涨停（排除连板中间板，只取第一板）
        skip = False
        for k in range(1, 11):
            if i - k < 1: break
            prev_k_c = bars[i-k-1]['close']
            if prev_k_c > 0 and (bars[i-k]['close'] / prev_k_c - 1) >= threshold * 0.98:
                skip = True; break
        if skip: continue

        fl = bars[i]
        fl_close = fl['close']
        fl_high = fl['high']
        fl_vol = fl['volume']
        fl_prev_close = bars[i-1]['close']
        fl_prev_vol = bars[i-1]['volume']
        ref = bars[i-2]['close'] if i >= 2 else fl_prev_close

        if ref <= 0: continue

        vol_ratio = fl_vol / fl_prev_vol if fl_prev_vol > 0 else 0
        upper_shadow = (fl_high - fl_close) / ref * 100
        if upper_shadow >= max_upper_shadow: continue

        # 跳空>5% + 量比<1.5x
        gap_pct = (fl['open'] / fl_prev_close - 1) * 100 if fl_prev_close > 0 else 0
        if gap_pct <= 5.0 or vol_ratio >= 1.5:
            continue

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

        # D1开盘涨幅过滤 (板块分离) - 低开不买
        d1_gap = (entry_price / fl_close - 1) * 100
        min_d1_gap = -2.0 if board_type == "main" else -5.0
        if d1_gap < min_d1_gap:
            continue

        d1_change = (d1['close'] / fl_close - 1) * 100
        if d1_change < 0: continue  # D1收阴排除

        bt = run_backtest(bars, i + 1, entry_price, hold_days, stop_loss, trailing_stop, board_type)
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
    "000066","000402","000553","000586","000601","000637","000720","000753","000767","000783",
    "000925","000950","001208","001259","001316","002010","002011","002012","002013","002014",
    "002015","002016","002017","002018","002019","002020","002021","002022","002023","002024",
    "002025","002026","002027","002028","002029","002030","002031","002032","002033","002034",
    "002035","002036","002037","002038","002039","002040","002041","002042","002043","002044",
    "002045","002046","002047","002048","002049","002050","002055","002056","002063","002065",
    "002074","002077","002079","002081","002084","002088","002092","002093","002095","002097",
    "002100","002104","002106","002111","002115","002119","002120","002125","002127","002130",
    "002131","002137","002139","002141","002146","002149","002150","002152","002153","002156",
    "002158","002160","002163","002165","002169","002170","002172","002175","002177","002180",
    "002183","002185","002188","002190","002191","002194","002196","002198","002200","002202",
    "002208","002209","002211","002214","002218","002222","002227","002230","002232","002234",
    "002236","002238","002240","002242","002244","002248","002249","002252","002253","002255",
    "002258","002261","002263","002266","002268","002270","002272","002274","002276","002278",
    "002280","002297","002366","002464","002468","002498","002510","002512","002535","002552",
    "002560","002580","002640","002805","002858","002918","002989","300001","300002","300003",
    "300004","300005","300006","300007","300008","300009","300010","300011","300012","300013",
    "300014","300015","300016","300017","300018","300019","300020","300021","300022","300023",
    "300024","300025","300026","300027","300028","300029","300030","300031","300032","300033",
    "300034","300035","300036","300037","300038","300039","300059","300106","300124","300152",
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
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--pullback", type=int, default=3, help="最少回调天数")
    parser.add_argument("--max-pullback", type=int, default=11, help="最多回调天数")
    parser.add_argument("--max-last-chg", type=float, default=3.0, help="末期小阳最大涨幅%")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES

    print(f"{'=' * 80}")
    print(f"龙回头v3 + V1对比")
    print(f"{'=' * 80}")
    print(f"龙回头: 回调{args.pullback}-{args.max_pullback}天 | 末期十字星/小阳<{args.max_last_chg}% → D+1买")
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
                                       max_pullback_days=args.max_pullback,
                                       max_last_chg=args.max_last_chg)
        dc_trades.extend(dc)

        v1 = strategy_v1(bars, code, use_preload_filter=False)
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
            # 末期量比分段
            print(f"\n  末期量比(vs涨停日):")
            for lo, hi, label in [(0,0.3,"<0.3x"), (0.3,0.5,"0.3-0.5x"), (0.5,0.8,"0.5-0.8x")]:
                seg = [t for t in dc_trades if lo <= t['signal_vol_r'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")

            # 回调天数分布
            print(f"\n  回调天数分布:")
            for lo, hi, label in [(3,5,"3-4天"), (5,8,"5-7天"), (8,12,"8-11天")]:
                seg = [t for t in dc_trades if lo <= t['pullback_days'] < hi]
                if seg:
                    print_stats(seg, f"    {label}")

            # TOP5
            print(f"\n  🏆 龙回头TOP5:")
            for t in sorted(dc_trades, key=lambda x: -x['peak_return_pct'])[:5]:
                print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

            if len(dc_trades) > 5:
                print(f"\n  💀 龙回头BOTTOM5:")
                for t in sorted(dc_trades, key=lambda x: x['return_pct'])[:5]:
                    print(f"    {t['code']} 涨停{t['lu_date']} 回调{t['pullback_days']}天 → {t['signal_date']}信号{t['signal_chg']:+.1f}% 量{t['signal_vol_r']:.2f}x → {t['entry_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

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
                  f"{t['signal_date']}信号{t['signal_chg']:>+5.1f}% 量{t['signal_vol_r']:.2f}x → "
                  f"{t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:>+6.2f}% 峰值{t['peak_return_pct']:>+6.2f}%")

    # 导出
    all_out = dc_trades + v1_trades
    if all_out:
        with open("test_dragon_callback_result.json", "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        print(f"\n💾 test_dragon_callback_result.json")

if __name__ == "__main__":
    main()
