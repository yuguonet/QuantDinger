#!/usr/bin/env python3
"""
V3断板买入策略: 在连板股断板位置买入

逻辑:
  1. 检测连板段: 连续涨停≥2板
  2. 断板日: 涨停中断的第一天(非涨停)
  3. 买入: 断板日收盘 or 断板次日开盘
  4. 卖出: 追踪止损/止盈/峰值信号

分析不同断板位置(第几板后断)的效果
"""
from __future__ import annotations
import time
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

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

CODES = [
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

# ================================================================
# 回测引擎
# ================================================================

def run_backtest(bars, entry_idx, entry_price, hold_days=20, stop_loss=-8.0,
                 trailing_stop=-6.0, take_profit=15.0, board_type="main"):
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

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0

        # 止损
        if ret <= stop_loss:
            exit_p = entry_price * (1 + stop_loss / 100); exit_d = d; break

        # 追踪止损 (盈利时)
        if ret_from_high <= trailing_stop and ret > 0:
            exit_p = peak * (1 + trailing_stop / 100); exit_d = d; break

        # 止盈
        if ret >= take_profit:
            exit_p = entry_price * (1 + take_profit / 100); exit_d = d; break

        # 峰值信号: RSI高+上影线大 (简化: 涨幅>10%后出现大上影)
        if ret > 10:
            bar_range = b['high'] - b['low']
            upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
            if upper > 40 and b['close'] < b['high'] * 0.98:
                exit_p = b['close']; exit_d = d; break

        exit_p = b['close']; exit_d = d

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


# ===== 板块参数分离 =====
BOARD_PARAMS = {
    "main": {  # 沪主板 + 深主板
        "stop_loss": -8.0,
        "trailing_stop": -6.0,
        "take_profit": 15.0,
        "hold_days": 20,
        "vol_max": 1.2,        # 断板期量比上限 (缩量洗盘)
        "drawdown_max": -10,   # 断板期最大回撤%
    },
    "gem_star": {  # 创业板 + 科创板
        "stop_loss": -10.0,    # 20cm波动大，止损放宽
        "trailing_stop": -8.0, # 追踪止损放宽
        "take_profit": 20.0,   # 止盈提高
        "hold_days": 15,       # 持仓缩短，20cm波动快
        "vol_max": 1.5,        # 20cm放量更常见，放宽
        "drawdown_max": -15,   # 20cm回撤空间更大
    },
}


def strategy_break_buy(bars, code, min_streak=2, max_break_gap=5,
                        buy_mode="break_close",
                        override_params=None):
    """
    断板买入策略 (板块参数自动分离)

    入场规则:
      1. 连板≥2 → 断板
      2. 断板期不破涨停日开盘价
      3. 断板期缩量 (量比<板块阈值)
      4. 断板期回撤<板块阈值

    buy_mode:
      "break_close" — 断板日收盘价买入
      "next_open"   — 断板次日开盘价买入
      "break_low"   — 断板日最低价买入(理想化低吸)

    override_params: 可覆盖板块默认参数
    """
    bt = get_board_type(code)
    threshold = 0.098 if bt == "main" else 0.198

    # 合并参数
    params = dict(BOARD_PARAMS[bt])
    if override_params:
        params.update(override_params)

    stop_loss = params["stop_loss"]
    trailing_stop = params["trailing_stop"]
    take_profit = params["take_profit"]
    hold_days = params["hold_days"]
    vol_max = params["vol_max"]
    drawdown_max = params["drawdown_max"]

    trades = []
    used = set()

    i = 1
    while i < len(bars) - 1:
        # 找连板起点: 当天涨停 + 前10天非涨停（第一板）
        if not is_limit_up(bars[i]['close'], bars[i-1]['close'], bt):
            i += 1; continue
        # 前10天不能有涨停，确保是真正的第一板
        is_first = True
        for k in range(1, min(11, i + 1)):
            if is_limit_up(bars[i-k]['close'], bars[i-k-1]['close'], bt) if (i-k-1) >= 0 else False:
                is_first = False; break
        if not is_first:
            i += 1; continue

        # 数连板
        streak_start = i
        streak_end = i
        while streak_end < len(bars) - 1:
            if is_limit_up(bars[streak_end+1]['close'], bars[streak_end]['close'], bt):
                streak_end += 1
            else:
                break
        streak_len = streak_end - streak_start + 1

        if streak_len < min_streak:
            i = streak_end + 1; continue

        # 找断板位置: streak_end+1 是第一个非涨停日
        break_idx = streak_end + 1
        if break_idx >= len(bars):
            i = streak_end + 1; continue

        # 检查断板后的连续非涨停天数
        break_days = 0
        for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
            if not is_limit_up(bars[j]['close'], bars[j-1]['close'], bt):
                break_days += 1
            else:
                break

        # ===== 入场过滤 (必须全部通过) =====
        limit_bar = bars[streak_end]  # 涨停日(最后一板)
        limit_open = float(limit_bar['open'])
        limit_close = float(limit_bar['close'])
        limit_vol = float(limit_bar['volume'])

        break_bars = bars[break_idx:break_idx + break_days]
        if not break_bars:
            i = streak_end + 1; continue

        # 1. 断板期不破涨停日开盘价
        break_low = min(float(b['low']) for b in break_bars)
        if break_low < limit_open:
            i = streak_end + 1; continue

        # 2. 断板期缩量 (量比<板块阈值)
        break_vol_sum = sum(float(b['volume']) for b in break_bars)
        break_vol_avg = break_vol_sum / len(break_bars) if break_bars else 0
        if limit_vol > 0 and break_vol_avg / limit_vol >= vol_max:
            i = streak_end + 1; continue

        # 3. 断板期最大回撤<板块阈值 (vs涨停日收盘)
        break_drawdown = (break_low / limit_close - 1) * 100 if limit_close > 0 else 0
        if break_drawdown < drawdown_max:
            i = streak_end + 1; continue

        # 防重复
        key = (bars[streak_start]['time'], bars[break_idx]['time'])
        if key in used:
            i = streak_end + 1; continue
        used.add(key)

        # 买入
        if buy_mode == "break_close":
            entry_price = bars[break_idx]['close']
            entry_idx = break_idx
        elif buy_mode == "next_open":
            if break_idx + 1 >= len(bars):
                i = streak_end + 1; continue
            entry_price = bars[break_idx + 1]['open']
            entry_idx = break_idx + 1
        elif buy_mode == "break_low":
            entry_price = bars[break_idx]['low']
            entry_idx = break_idx
        else:
            i = streak_end + 1; continue

        if entry_price <= 0:
            i = streak_end + 1; continue

        # 回测
        result = run_backtest(bars, entry_idx, entry_price,
                              hold_days, stop_loss, trailing_stop, take_profit, bt)
        if not result:
            i = streak_end + 1; continue

        # 断板日特征
        break_bar = bars[break_idx]
        prev_bar = bars[break_idx - 1]
        break_chg = (break_bar['close'] / prev_bar['close'] - 1) * 100
        break_vol_r = break_bar['volume'] / prev_bar['volume'] if prev_bar['volume'] > 0 else 0
        break_gap = (break_bar['open'] / prev_bar['close'] - 1) * 100

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'board_type': bt,
            'streak_len': streak_len,
            'streak_start': bars[streak_start]['time'],
            'streak_end': bars[streak_end]['time'],
            'break_date': bars[break_idx]['time'],
            'break_chg': round(break_chg, 2),
            'break_gap': round(break_gap, 2),
            'break_vol_r': round(break_vol_r, 2),
            'next_limit_gap': 0,  # 不再要求
            'entry_date': bars[entry_idx]['time'],
            'entry_price': round(entry_price, 3),
            'buy_mode': buy_mode,
            **result,
        })

        i = streak_end + 1

    return trades


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
    print(f"{'=' * 80}")
    print(f"V3断板买入策略回测 (板块参数分离)")
    print(f"{'=' * 80}")
    print(f"入场: 连板≥2断板 + 不破涨停开盘 + 缩量 + 回撤限制")
    for bt_name, p in BOARD_PARAMS.items():
        label = "主板" if bt_name == "main" else "创/科"
        print(f"  {label}: 止损{p['stop_loss']}% 追踪{p['trailing_stop']}% 止盈{p['take_profit']}% 持仓{p['hold_days']}天 量比<{p['vol_max']}x 回撤>{p['drawdown_max']}%")
    print(f"股票: {len(CODES)}只\n")

    all_by_mode = {}
    for mode in ["break_close", "next_open", "break_low"]:
        label = {"break_close": "断板日收盘买", "next_open": "断板次日开盘买", "break_low": "断板日最低价买(理想)"}[mode]
        print(f"\n{'─' * 80}")
        print(f"模式: {label}")
        print(f"{'─' * 80}")

        all_trades = []
        success = 0
        for idx, code in enumerate(CODES):
            print(f"[{idx+1}/{len(CODES)}] {code}", end=" ", flush=True)
            bars = fetch_kline(code, 300)
            if not bars:
                print("❌"); continue
            trades = strategy_break_buy(bars, code, buy_mode=mode, min_streak=2,
                                         max_break_gap=5)
            all_trades.extend(trades)
            print(f"✓ → {len(trades)}笔")
            success += 1
            time.sleep(0.2)

        all_by_mode[mode] = all_trades

        print(f"\n{'=' * 80}")
        print(f"结果: {success}只, {len(all_trades)}笔交易")
        print(f"{'=' * 80}")

        if not all_trades:
            print("无信号"); continue

        print_stats(all_trades, "总览")

        # 按连板数分
        print(f"\n  按连板数:")
        for sl in sorted(set(t['streak_len'] for t in all_trades)):
            seg = [t for t in all_trades if t['streak_len'] == sl]
            print_stats(seg, f"    {sl}板后断")

        # 按板块
        print(f"\n  按板块:")
        for board in ["沪主板", "深主板", "创业板", "科创板"]:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, f"    {board}")

        # 按断板日涨跌幅
        print(f"\n  按断板日涨跌幅:")
        for lo, hi, label in [(-10,-3,"大跌<-3%"), (-3,0,"小跌"), (0,3,"小涨"), (3,7,"中涨"), (7,12,"大涨")]:
            seg = [t for t in all_trades if lo <= t['break_chg'] < hi]
            if seg:
                print_stats(seg, f"    {label}")

        # TOP5 / BOTTOM5
        if len(all_trades) >= 5:
            print(f"\n  🏆 TOP5:")
            for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:5]:
                print(f"    {t['code']} {t['streak_len']}板断 → {t['break_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")
            print(f"\n  💀 BOTTOM5:")
            for t in sorted(all_trades, key=lambda x: x['return_pct'])[:5]:
                print(f"    {t['code']} {t['streak_len']}板断 → {t['break_date']}买 收益{t['return_pct']:+.1f}% 峰值{t['peak_return_pct']:+.1f}%")

    # ===== 三种模式对比 =====
    print(f"\n{'=' * 80}")
    print(f"📊 三种买入模式对比")
    print(f"{'=' * 80}")
    print(f"{'模式':>16} {'笔数':>6} {'胜率':>7} {'均收益':>8} {'均峰值':>8} {'盈亏比':>7}")
    print("-" * 60)
    for mode, label in [("break_close","断板日收盘"), ("next_open","断板次日开盘"), ("break_low","断板日最低(理想)")]:
        trades = all_by_mode.get(mode, [])
        if not trades: continue
        wr = sum(1 for t in trades if t['return_pct'] > 0) / len(trades) * 100
        avg = sum(t['return_pct'] for t in trades) / len(trades)
        peak = sum(t['peak_return_pct'] for t in trades) / len(trades)
        ws = [t['return_pct'] for t in trades if t['return_pct'] > 0]
        ls = [t['return_pct'] for t in trades if t['return_pct'] <= 0]
        pl = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls)) if ws and ls else (999 if ws else 0)
        print(f"{label:>16} {len(trades):>6} {wr:>6.1f}% {avg:>+7.2f}% {peak:>+7.2f}% {pl:>7.2f}")


if __name__ == "__main__":
    main()
