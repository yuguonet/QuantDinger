#!/usr/bin/env python3
"""
经典 W 底（双底）突破策略 - 独立回测

═══════════════════════════════════════════════════════════════════
  W 底形态
═══════════════════════════════════════════════════════════════════

               颈线 (neckline)
          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
         /\\              突破↑
        /  \\            /    \\
       /    \\    W     /      \\  → 加速上涨
      /      \\        /        \\
     /        \\      /          \\
    第一底    第二底
    (最低点)  (回踩不破第一底)

  ① 第一底: lookback 内最低点
  ② 颈线: 从第一底反弹到局部高点，涨幅 peak_min_pct~peak_max_pct%
  ③ 第二底: 回踩洗盘，低点 >= 第一底（核心条件！）
  ④ 突破颈线: 放量突破
  ⑤ 加速确认: 突破后继续上涨 N 天
  ⑥ 买入: 加速确认后次日开盘买

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  python test_box_breakout.py
  python test_box_breakout.py --accel-days 1
  python test_box_breakout.py --w-bottom-tolerance 3.0
  python test_box_breakout.py --all-trades
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict

# ================================================================
# 路径初始化
# ================================================================
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

# ================================================================
# DB 数据加载
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def fetch_kline_db(code, days=300):
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        from app.data_sources.provider.adjustment import unadj_to_qfq
        bars = []
        for row in data:
            bars.append({
                'time': str(row.get('time', ''))[:10],
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'close': float(row.get('close', 0)),
                'volume': float(row.get('volume', 0)),
            })
        bars = unadj_to_qfq(bars, code)
        return bars[-days:] if len(bars) > days else bars
    except Exception:
        return []

def fetch_kline(code, days=300):
    from kline_cache import fetch_kline as _fetch_kline
    return _fetch_kline(code, days)

def get_board_name(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith('300'):
        return '创业板'
    elif code.startswith('60'):
        return '沪主板'
    elif code.startswith('00') or code.startswith('001') or code.startswith('002'):
        return '深主板'
    return '未知'

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, stop_loss_pct=5.0,
                 trailing_pct=5.0, max_hold_days=15,
                 take_profit_pct=15.0, trailing_activate_pct=5.0):
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = "data_end"
    max_d = len(bars) - entry_idx - 1

    if max_d <= 0:
        return None

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # 止盈：用收盘价判断
        gain_from_entry = (b['close'] / entry_price - 1) * 100
        if gain_from_entry >= take_profit_pct:
            exit_p = b['close']
            exit_d = d
            exit_reason = "take_profit"
            break

        # 止损：用盘中最低价判断，触及止损线以止损价出场
        # 这样能避免跳空低开导致止损失效
        stop_price = entry_price * (1 - stop_loss_pct / 100)
        if b['low'] <= stop_price:
            # 跳空低开：以开盘价出场（实际无法以止损价成交）
            if b['open'] < stop_price:
                exit_p = b['open']
            else:
                exit_p = stop_price
            exit_d = d
            exit_reason = "stop_loss"
            break

        # 跟踪止损：用收盘价判断
        peak_gain = (peak / entry_price - 1) * 100
        if peak_gain >= trailing_activate_pct:
            trail_price = peak * (1 - trailing_pct / 100)
            if b['low'] <= trail_price:
                if b['open'] < trail_price:
                    exit_p = b['open']
                else:
                    exit_p = trail_price
                exit_d = d
                exit_reason = "trail_stop"
                break

        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "max_hold"
            break

        exit_p = b['close']
        exit_d = d
        exit_reason = "data_end"

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

# ================================================================
# W 底策略
# ================================================================
def strategy_w_bottom(bars, code,
                      lookback=60, peak_min_pct=5.0, peak_max_pct=20.0,
                      peak_within_days=45, min_pullback_days=3,
                      pullback_min_pct=2.0, pullback_max_pct=8.0,
                      vol_shrink_ratio=0.7,
                      vol_expand_min=1.0, vol_expand_max=3.0,
                      w_bottom_tolerance=2.0,
                      stop_loss_pct=5.0, trailing_pct=5.0,
                      trailing_activate_pct=5.0, take_profit_pct=15.0,
                      max_hold_days=15, top_per_day=2,
                      accel_days=2, accel_min_pct=1.5,
                      wave1_min=3, wave1_max=15,
                      wave2_min=3, wave2_max=15,
                      wave3_min=1, wave3_max=10,
                      wave_ratio_max=1.0,
                      require_ma60=True):
    """
    经典 W 底（双底）突破 + 加速确认策略

    入场条件:
      ① 第一底: lookback 内最低点
      ② 颈线: 第一底反弹后的局部高点，涨幅 peak_min_pct~peak_max_pct%
      ③ 第二底: 回踩不破第一底（W底核心！容差 w_bottom_tolerance%）
      ④ 突破颈线: 放量突破，收阳线
      ⑤ 加速确认: 突破后 accel_days 天继续上涨，累计 >= accel_min_pct%
      ⑥ 买入: 加速确认后次日开盘买

    出场条件:
      ① 止盈: take_profit_pct%
      ② 固定止损: stop_loss_pct%
      ③ 跟踪止损: trailing_activate_pct% 后回撤 trailing_pct%
      ④ 持仓上限: max_hold_days 天
    """
    if len(bars) < lookback + accel_days + 10:
        return []

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    candidates = []

    for i in range(lookback + accel_days, len(bars)):
        breakout_idx = i - accel_days
        if breakout_idx < lookback:
            continue

        # ══════════════════════════════════════════════════════════
        # ① 找第一底 + 颈线
        # ══════════════════════════════════════════════════════════
        w_start = max(0, breakout_idx - lookback)
        if breakout_idx - w_start < 10:
            continue

        # 第一底：区间最低点
        first_trough_idx = w_start
        first_trough_low = lows[w_start]
        for j in range(w_start, breakout_idx - 3):
            if lows[j] < first_trough_low:
                first_trough_low = lows[j]
                first_trough_idx = j

        if first_trough_low <= 0:
            continue

        # 颈线：第一底之后到 breakout_idx 之间的局部高点
        neckline_high = 0
        neckline_idx = first_trough_idx
        for j in range(first_trough_idx + 1, breakout_idx):
            if highs[j] > neckline_high:
                neckline_high = highs[j]
                neckline_idx = j

        if neckline_high <= first_trough_low:
            continue

        # 颈线涨幅检查
        neckline_gain_pct = (neckline_high / first_trough_low - 1) * 100
        if neckline_gain_pct < peak_min_pct or neckline_gain_pct > peak_max_pct:
            continue

        # 颈线距 breakout_idx 不能太远
        if breakout_idx - neckline_idx > peak_within_days:
            continue

        # ══════════════════════════════════════════════════════════
        # 波段周期过滤
        # ══════════════════════════════════════════════════════════
        wave1_days = neckline_idx - first_trough_idx  # 第一波: 第一底→颈线
        if wave1_days < wave1_min or wave1_days > wave1_max:
            continue

        # ══════════════════════════════════════════════════════════
        # ② 第二底：回踩不破第一底（W 底核心条件）
        # ══════════════════════════════════════════════════════════
        second_trough_low = lows[neckline_idx]
        second_trough_idx = neckline_idx
        for j in range(neckline_idx + 1, breakout_idx):
            if lows[j] < second_trough_low:
                second_trough_low = lows[j]
                second_trough_idx = j

        # 关键：第二底不能跌破第一底（允许 w_bottom_tolerance% 容差）
        if second_trough_low < first_trough_low * (1 - w_bottom_tolerance / 100):
            continue

        # 第二底到 breakout 之间要有足够的洗盘天数
        pullback_days = breakout_idx - neckline_idx
        if pullback_days < min_pullback_days:
            continue

        # 回踩周期过滤: 颈线→第二底
        wave2_days = second_trough_idx - neckline_idx
        if wave2_days < wave2_min or wave2_days > wave2_max:
            continue

        # 蓄力周期过滤: 第二底→突破
        wave3_days = breakout_idx - second_trough_idx
        if wave3_days < wave3_min or wave3_days > wave3_max:
            continue

        # 回踩/第一波比例过滤: 回踩应该比第一波短
        if wave1_days > 0 and wave2_days / wave1_days > wave_ratio_max:
            continue

        # 洗盘缩量
        pullback_bars_list = bars[neckline_idx + 1:breakout_idx]
        if len(pullback_bars_list) > 0:
            pullback_avg_vol = sum(b['volume'] for b in pullback_bars_list) / len(pullback_bars_list)
        else:
            pullback_avg_vol = bars[neckline_idx]['volume'] * 0.5

        neckline_vol = bars[neckline_idx]['volume']
        if neckline_vol > 0 and pullback_avg_vol > neckline_vol * vol_shrink_ratio:
            continue

        # ══════════════════════════════════════════════════════════
        # ③ 突破颈线
        # ══════════════════════════════════════════════════════════
        if bars[breakout_idx]['close'] <= neckline_high:
            continue

        # 突破日收阳线
        if breakout_idx >= 1 and bars[breakout_idx]['close'] <= bars[breakout_idx - 1]['close']:
            continue

        # 放量确认
        vol_ratio = bars[breakout_idx]['volume'] / pullback_avg_vol if pullback_avg_vol > 0 else 0
        if vol_ratio < vol_expand_min or vol_ratio > vol_expand_max:
            continue

        # ══════════════════════════════════════════════════════════
        # ④ 加速确认
        # ══════════════════════════════════════════════════════════
        # 突破后不跌破颈线
        accel_low = min(bars[j]['low'] for j in range(breakout_idx, i + 1))
        if accel_low < neckline_high * 0.98:
            continue

        # 累计涨幅
        accel_gain = (bars[i]['close'] / bars[breakout_idx]['close'] - 1) * 100
        if accel_gain < accel_min_pct:
            continue

        # 突破后第1天收盘 > 突破日收盘
        if accel_days >= 1 and i > breakout_idx:
            if bars[breakout_idx + 1]['close'] <= bars[breakout_idx]['close']:
                continue

        # 量能不爆量
        accel_avg_vol = sum(bars[j]['volume'] for j in range(breakout_idx, i + 1)) / (accel_days + 1)
        if pullback_avg_vol > 0 and accel_avg_vol > pullback_avg_vol * vol_expand_max * 1.5:
            continue

        # 突破幅度
        breakout_pct = (bars[breakout_idx]['close'] / neckline_high - 1) * 100

        # ══════════════════════════════════════════════════════════
        # MA60过滤（可选）
        # ══════════════════════════════════════════════════════════
        if require_ma60 and i >= 60:
            ma60 = sum(closes[i-59:i+1]) / 60
            if closes[i] < ma60:
                continue

        # ══════════════════════════════════════════════════════════
        # ⑤ 买入：加速确认后次日开盘买
        # ══════════════════════════════════════════════════════════
        entry_idx = i + 1
        if entry_idx >= len(bars):
            continue
        entry_price = bars[entry_idx]['open']
        if entry_price <= 0:
            continue

        candidates.append({
            'idx': i,
            'signal_date': bars[i]['time'],
            'entry_price': entry_price,
            'entry_idx': entry_idx,
            'entry_date': bars[entry_idx]['time'],
            'first_trough_low': round(first_trough_low, 3),
            'neckline_high': round(neckline_high, 3),
            'neckline_gain_pct': round(neckline_gain_pct, 2),
            'second_trough_low': round(second_trough_low, 3),
            'pullback_days': pullback_days,
            'breakout_close': bars[breakout_idx]['close'],
            'breakout_pct': round(breakout_pct, 2),
            'vol_ratio_vs_pullback': round(vol_ratio, 2),
            'accel_gain': round(accel_gain, 2),
            'accel_days': accel_days,
            'wave1_days': wave1_days,
            'wave2_days': wave2_days,
            'wave3_days': wave3_days,
            'wave_total_days': wave1_days + wave2_days + wave3_days,
            # 兼容旧字段名
            'peak_high': round(neckline_high, 3),
            'peak_gain_pct': round(neckline_gain_pct, 2),
        })

    # 同一天按优先级排序
    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c['signal_date']].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (
            -c['accel_gain'],
            -c['breakout_pct'],
            -c['vol_ratio_vs_pullback'],
            c['pullback_days'],
        ))
        filtered.extend(cands[:top_per_day])

    # 生成交易记录
    trades = []
    for c in filtered:
        result = run_backtest(bars, c['entry_idx'], c['entry_price'],
                              stop_loss_pct, trailing_pct, max_hold_days,
                              take_profit_pct, trailing_activate_pct)
        if not result:
            continue

        trades.append({
            'code': code,
            'board': get_board_name(code),
            'path': 'w_bottom',
            'path_label': 'W底突破(加速版)',
            'signal_date': c['signal_date'],
            'signal_close': closes[c['idx']],
            'first_trough_low': c['first_trough_low'],
            'peak_high': c['neckline_high'],
            'neckline_high': c['neckline_high'],
            'neckline_gain_pct': c['neckline_gain_pct'],
            'second_trough_low': c['second_trough_low'],
            'peak_gain_pct': c['neckline_gain_pct'],
            'pullback_days': c['pullback_days'],
            'breakout_close': c['breakout_close'],
            'breakout_pct': c['breakout_pct'],
            'vol_ratio_vs_pullback': c['vol_ratio_vs_pullback'],
            'accel_gain': c['accel_gain'],
            'accel_days': c['accel_days'],
            'wave1_days': c['wave1_days'],
            'wave2_days': c['wave2_days'],
            'wave3_days': c['wave3_days'],
            'wave_total_days': c['wave_total_days'],
            'entry_date': c['entry_date'],
            'entry_price': round(c['entry_price'], 3),
            'buy_mode': 'accel_confirm_next_open',
            **result,
        })

    return trades

# ================================================================
# 测试股票列表
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

# ================================================================
# 统计输出
# ================================================================
def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号")
        return
    rets = [t['return_pct'] for t in trades if t['return_pct'] is not None]
    if not rets:
        print(f"  {label}: 无收益数据")
        return
    win = [r for r in rets if r > 0]
    wr = len(win) / len(rets) * 100
    avg = sum(rets) / len(rets)
    print(f"  {label}: {len(rets)}笔, 胜率{wr:.1f}%, 均值{avg:+.2f}%")

# ================================================================
# 主流程
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="W底突破策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")

    # W底参数
    parser.add_argument("--lookback", type=int, default=60,
                        help="回看窗口 (默认60)")
    parser.add_argument("--peak-min-pct", type=float, default=5.0,
                        help="颈线最小涨幅%% (默认5.0)")
    parser.add_argument("--peak-max-pct", type=float, default=20.0,
                        help="颈线最大涨幅%% (默认20.0)")
    parser.add_argument("--peak-within-days", type=int, default=45,
                        help="颈线距今最大天数 (默认45)")

    # 洗盘参数
    parser.add_argument("--min-pullback-days", type=int, default=3,
                        help="最小洗盘天数 (默认3)")
    parser.add_argument("--pullback-min-pct", type=float, default=2.0,
                        help="洗盘最小回调幅度%% (默认2.0)")
    parser.add_argument("--pullback-max-pct", type=float, default=8.0,
                        help="洗盘最大回调幅度%% (默认8.0)")
    parser.add_argument("--vol-shrink-ratio", type=float, default=0.7,
                        help="洗盘缩量比例 (默认0.7)")

    # 突破参数
    parser.add_argument("--vol-expand-min", type=float, default=1.0,
                        help="突破放量下限 (默认1.0)")
    parser.add_argument("--vol-expand-max", type=float, default=3.0,
                        help="突破放量上限 (默认3.0)")

    # W底核心参数
    parser.add_argument("--w-bottom-tolerance", type=float, default=2.0,
                        help="第二底跌破第一底的容差%% (默认2.0)")

    # 加速确认
    parser.add_argument("--accel-days", type=int, default=2,
                        help="加速确认天数 (默认2)")
    parser.add_argument("--accel-min-pct", type=float, default=1.5,
                        help="加速期间最小累计涨幅%% (默认1.5)")

    # 波段周期过滤
    parser.add_argument("--wave-filter", action="store_true", default=False,
                        help="启用波段周期过滤 (默认关闭)")
    parser.add_argument("--require-ma60", action="store_true", default=False,
                        help="要求站上60日均线 (默认关闭)")
    parser.add_argument("--wave1-min", type=int, default=3,
                        help="第一波最小天数 (默认3)")
    parser.add_argument("--wave1-max", type=int, default=15,
                        help="第一波最大天数 (默认15)")
    parser.add_argument("--wave2-min", type=int, default=1,
                        help="回踩最小天数 (默认1)")
    parser.add_argument("--wave2-max", type=int, default=10,
                        help="回踩最大天数 (默认10)")
    parser.add_argument("--wave3-min", type=int, default=1,
                        help="蓄力最小天数 (默认1)")
    parser.add_argument("--wave3-max", type=int, default=8,
                        help="蓄力最大天数 (默认8)")
    parser.add_argument("--wave-ratio-max", type=float, default=1.0,
                        help="回踩/第一波最大比例 (默认1.0)")

    # 出场参数
    parser.add_argument("--stop-loss", type=float, default=5.0,
                        help="固定止损%% (默认5.0, 主板自动用8%%)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--trailing-activate", type=float, default=5.0,
                        help="跟踪止损激活门槛%% (默认5.0)")
    parser.add_argument("--take-profit", type=float, default=15.0,
                        help="止盈%% (默认15.0)")
    parser.add_argument("--max-hold", type=int, default=15,
                        help="最大持仓天数 (默认15)")
    parser.add_argument("--board-adaptive", action="store_true", default=True,
                        help="板块自适应参数 (默认开启)")
    parser.add_argument("--no-board-adaptive", action="store_false", dest="board_adaptive",
                        help="禁用板块自适应")

    # 其他
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个 (默认2)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"W底（双底）突破策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 第一底: 前{args.lookback}日内最低点")
    print(f"  ② 颈线: 从第一底反弹 {args.peak_min_pct}%~{args.peak_max_pct}%")
    print(f"  ③ 第二底: 回踩不破第一底（容差{args.w_bottom_tolerance}%）, 洗盘>={args.min_pullback_days}天")
    print(f"  ④ 突破颈线: 温和放量{args.vol_expand_min}x~{args.vol_expand_max}x")
    print(f"  ⑤ 加速确认: 突破后{args.accel_days}天继续涨>={args.accel_min_pct}%")
    if args.require_ma60:
        print(f"  ⑥ MA60过滤: 要求站上60日均线")
    print(f"出场条件:")
    print(f"  ① 止盈: {args.take_profit}%")
    print(f"  ② 止损: {args.stop_loss}%")
    print(f"  ③ 跟踪止损: {args.trailing_pct}%（{args.trailing_activate}%后激活）")
    print(f"  ④ 持仓上限: {args.max_hold}天")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        # 板块自适应参数
        board = get_board_name(code)
        if args.board_adaptive and board in ('沪主板', '深主板'):
            # 主板10%涨跌停: 放宽止损, 更严格加速确认
            _stop_loss = 8.0
            _accel_days = max(args.accel_days, 3)
            _accel_min_pct = max(args.accel_min_pct, 3.0)
        else:
            # 创/科板20%涨跌停: 用默认参数
            _stop_loss = args.stop_loss
            _accel_days = args.accel_days
            _accel_min_pct = args.accel_min_pct

        trades = strategy_w_bottom(
            bars, code,
            lookback=args.lookback,
            peak_min_pct=args.peak_min_pct,
            peak_max_pct=args.peak_max_pct,
            peak_within_days=args.peak_within_days,
            min_pullback_days=args.min_pullback_days,
            pullback_min_pct=args.pullback_min_pct,
            pullback_max_pct=args.pullback_max_pct,
            vol_shrink_ratio=args.vol_shrink_ratio,
            vol_expand_min=args.vol_expand_min,
            vol_expand_max=args.vol_expand_max,
            w_bottom_tolerance=args.w_bottom_tolerance,
            stop_loss_pct=_stop_loss,
            trailing_pct=args.trailing_pct,
            trailing_activate_pct=args.trailing_activate,
            take_profit_pct=args.take_profit,
            max_hold_days=args.max_hold,
            top_per_day=args.top_per_day,
            accel_days=_accel_days,
            accel_min_pct=_accel_min_pct,
            wave1_min=args.wave1_min if args.wave_filter else 1,
            wave1_max=args.wave1_max if args.wave_filter else 100,
            wave2_min=args.wave2_min if args.wave_filter else 1,
            wave2_max=args.wave2_max if args.wave_filter else 100,
            wave3_min=args.wave3_min if args.wave_filter else 1,
            wave3_max=args.wave3_max if args.wave_filter else 100,
            wave_ratio_max=args.wave_ratio_max if args.wave_filter else 10.0,
            require_ma60=args.require_ma60,
        )
        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) "
                  f"{len(trades)}个信号")
            success += 1

    # ---- 汇总统计 ----
    print(f"\n{'=' * 80}")
    print(f"回测完成: {success}/{len(codes)} 只股票有信号, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "全部")

        # 按板块统计
        print(f"\n--- 板块统计 ---")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, board)

        # 按出场原因统计
        print(f"\n--- 出场原因统计 ---")
        from collections import Counter
        for reason, cnt in Counter(t['exit_reason'] for t in all_trades).most_common():
            seg = [t for t in all_trades if t['exit_reason'] == reason]
            print_stats(seg, reason)

        # 按突破幅度分段
        print(f"\n--- 突破幅度分段 ---")
        for lo, hi in [(0, 1), (1, 3), (3, 5), (5, 100)]:
            seg = [t for t in all_trades if lo <= t['breakout_pct'] < hi]
            if seg:
                print_stats(seg, f"突破[{lo},{hi})%")

        # 按洗盘天数分段
        print(f"\n--- 洗盘天数分段 ---")
        for lo, hi in [(3, 8), (8, 12), (12, 20), (20, 100)]:
            seg = [t for t in all_trades if lo <= t['pullback_days'] < hi]
            if seg:
                print_stats(seg, f"洗盘[{lo},{hi})天")

        # 按放量倍数分段
        print(f"\n--- 放量倍数分段(相对洗盘期间) ---")
        for lo, hi in [(1.0, 1.3), (1.3, 1.5), (1.5, 2.0), (2.0, 3.0)]:
            seg = [t for t in all_trades if lo <= t['vol_ratio_vs_pullback'] < hi]
            if seg:
                print_stats(seg, f"放量[{lo},{hi})x")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

    # 保存JSON
    if all_trades:
        out_file = "test_box_breakout_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")
