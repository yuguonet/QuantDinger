"""
龙虎榜 D0 预警系统 (双层策略)

高确信: 周期 + 多头+角>0.3+RSI>60+KDJ>80+D1涨>10%   → 命中率~57%
标准:   周期 + 多头+角>0.3+RSI>60+KDJ>80+D1涨>3%+连涨>=2 → 命中率~51%

逻辑:
  1. 维护龙虎榜常客池 (历史上榜>=10次)
  2. 追踪每只的上榜间隔中位数
  3. 当某只安静天数 > 中位间隔 × 1.0 → 周期信号
  4. 信号日当天需满足: 多头排列 + MA5角>0.3% + RSI14>60 + KDJ K>70 + 涨幅>10%
  5. 全部满足 → 输出预警, 预计1-3天内上龙虎榜

运行:
  python dragon_d0_alert.py --scan           # 扫描当前预警
  python dragon_d0_alert.py --scan --detail  # 扫描并输出K线详情
  python dragon_d0_alert.py --backtest       # 回测验证

依赖: QuantDinger数据库 (龙虎榜 + K线)
"""
from __future__ import annotations
import json, argparse, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import numpy as np

# ================================================================
# 环境
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
# 策略参数
# ================================================================
CYCLE_K = 1.0           # 安静倍数阈值
CYCLE_MIN_COUNT = 10    # 最少上榜次数
FORWARD_DAYS = 3        # 预警后检查天数
LOOKBACK_DAYS = 90      # 间隔计算回看天数

TECH_STD = {
    'ma_bull': True,         # 多头排列 MA5>MA10>MA20
    'min_ma5_angle': 0.3,    # MA5斜率 > 0.3%/天
    'min_rsi14': 60,         # RSI14 > 60
    'min_kdj_k': 80,         # KDJ K > 80
    'min_d1_chg': 3,         # 信号日涨幅 > 3%
    'min_up_streak': 2,      # 连涨 >= 2天
}

TECH_HIGH = {
    'ma_bull': True,
    'min_ma5_angle': 0.3,
    'min_rsi14': 60,
    'min_kdj_k': 80,
    'min_d1_chg': 10,        # 信号日涨幅 > 10% (涨停级)
}


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
    return [dict(zip(columns, row)) for row in rows]


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

def calc_ma5_angle(closes, period=5, days=3):
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


def calc_rsi(closes, period=14):
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


def calc_kdj_k(closes, highs, lows, period=9):
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


def check_tech(bars, date, conditions):
    """检查某天是否满足全部技术条件"""
    idx = None
    for i, b in enumerate(bars):
        if b['time'] == date:
            idx = i
            break
    if idx is None or idx < 20:
        return False, {}

    closes = [bars[i]['close'] for i in range(idx - 19, idx + 1)]
    highs = [bars[i]['high'] for i in range(idx - 19, idx + 1)]
    lows = [bars[i]['low'] for i in range(idx - 19, idx + 1)]
    volumes = [bars[i]['volume'] for i in range(idx - 19, idx + 1)]

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes) / 20
    ma_bull = ma5 > ma10 > ma20

    angle = calc_ma5_angle(closes, 5, 3)
    rsi = calc_rsi(closes, 14)
    kdj = calc_kdj_k(closes, highs, lows, 9)

    prev_close = bars[idx - 1]['close'] if idx > 0 else 0
    d1_chg = (bars[idx]['close'] / prev_close - 1) * 100 if prev_close > 0 else 0

    vol5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
    d1_vol = bars[idx]['volume'] / vol5 if vol5 > 0 else 1

    # 连涨天数
    streak = 0
    for i in range(idx, max(idx - 10, 0), -1):
        if bars[i]['close'] > bars[i-1]['close']:
            streak += 1
        else:
            break

    details = {
        'ma5': round(ma5, 2), 'ma10': round(ma10, 2), 'ma20': round(ma20, 2),
        'ma_bull': ma_bull,
        'angle': round(angle, 2) if angle else None,
        'rsi14': round(rsi, 1) if rsi else None,
        'kdj_k': round(kdj, 1) if kdj else None,
        'd1_chg': round(d1_chg, 2),
        'd1_vol': round(d1_vol, 2),
        'up_streak': streak,
    }

    # 检查条件
    if conditions.get('ma_bull') and not ma_bull:
        return False, details
    if 'min_ma5_angle' in conditions and (angle is None or angle < conditions['min_ma5_angle']):
        return False, details
    if 'min_rsi14' in conditions and (rsi is None or rsi < conditions['min_rsi14']):
        return False, details
    if 'min_kdj_k' in conditions and (kdj is None or kdj < conditions['min_kdj_k']):
        return False, details
    if 'min_d1_chg' in conditions and d1_chg < conditions['min_d1_chg']:
        return False, details
    if 'min_d1_vol_ratio' in conditions and d1_vol < conditions['min_d1_vol_ratio']:
        return False, details
    if 'min_up_streak' in conditions and streak < conditions['min_up_streak']:
        return False, details

    return True, details


# ================================================================
# 周期计算
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


# ================================================================
# 扫描
# ================================================================

def scan_alerts(records, kline_cache=None, show_detail=False):
    """扫描当前预警信号"""
    by_code = build_stock_history(records)
    all_dates = sorted(set(r['trade_date'] for r in records))
    latest = all_dates[-1]
    latest_dt = datetime.strptime(latest, '%Y-%m-%d')
    lb_cutoff = (latest_dt - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    # 常客池
    eligible = {code for code, recs in by_code.items() if len(recs) >= CYCLE_MIN_COUNT}
    code_dates = {code: [r['trade_date'] for r in recs] for code, recs in by_code.items()}

    candidates = []
    for code in eligible:
        dates = code_dates[code]
        prior = [d for d in dates if d < latest and d >= lb_cutoff]
        if len(prior) < 2:
            continue

        last = prior[-1]
        days_since = (latest_dt - datetime.strptime(last, '%Y-%m-%d')).days
        gaps = calc_intervals(prior)
        if not gaps:
            continue
        median_gap = float(np.median(gaps))
        if median_gap <= 0:
            continue

        ratio = days_since / median_gap
        if ratio <= CYCLE_K or ratio >= CYCLE_K * 4:
            continue

        # 排除信号日当天已在龙虎榜上的股票 (我们要预测的是明天)
        if latest in set(code_dates[code]):
            continue

        # 技术面
        if kline_cache is not None:
            if code not in kline_cache:
                kline_cache[code] = load_kline(code)
            bars = kline_cache[code]
        else:
            bars = load_kline(code)

        if not bars:
            continue

        passed_high, details = check_tech(bars, latest, TECH_HIGH)
        passed_std, details = check_tech(bars, latest, TECH_STD)
        if not passed_high and not passed_std:
            continue
        tier = 'high' if passed_high else 'std'

        name = by_code[code][-1].get('stock_name', '')
        candidates.append({
            'code': code,
            'name': name,
            'tier': tier,
            'count': len(dates),
            'last_dragon': last,
            'days_since': days_since,
            'median_gap': median_gap,
            'ratio': ratio,
            'details': details,
        })

    candidates.sort(key=lambda x: (0 if x['tier']=='high' else 1, -x['ratio']))

    print(f"\n{'='*70}")
    print(f"  龙虎榜 D0 预警 ({latest})")
    print(f"  双层策略: 高确信(D1涨>10%) + 标准(D1涨>3%+连涨>=2)")
    print(f"{'='*70}")

    high_cands = [c for c in candidates if c['tier']=='high']
    std_cands = [c for c in candidates if c['tier']=='std']
    print(f"  高确信: {len(high_cands)}只 | 标准: {len(std_cands)}只")

    if candidates:
        print(f"\n  {'层级':>4} {'代码':>8} {'名称':>8} {'上榜':>4} {'安静':>4} {'中位':>4} {'倍数':>5} {'D1涨':>6} {'RSI':>5} {'KDJ':>5} {'角':>5}")
        print(f"  {'-'*70}")
        for c in candidates:
            d = c['details']
            tier_name = '★高' if c['tier']=='high' else '☆标'
            print(f"  {tier_name:>4} {c['code']:>8} {c['name']:>8} {c['count']:>4}次 {c['days_since']:>3}天 "
                  f"{c['median_gap']:>3.0f}天 {c['ratio']:>4.1f}x "
                  f"{d['d1_chg']:>+5.1f}% {d['rsi14']:>5.1f} {d['kdj_k']:>5.1f} {d['angle']:>5.2f}")

            if show_detail:
                print(f"          MA5={d['ma5']} MA10={d['ma10']} MA20={d['ma20']} "
                      f"多头={'✓' if d['ma_bull'] else '✗'} "
                      f"量比={d['d1_vol']} 连涨={d['up_streak']}天")
    else:
        print(f"\n  今日无预警信号")

    return candidates


# ================================================================
# 交易模拟
# ================================================================

def simulate_trade(bars, buy_date, hold_days=7, stop_loss=-8.0, take_profit=15.0):
    """D+1开盘买入, 持有最多7天, 追踪止损/止盈"""
    buy_idx = None
    for i, b in enumerate(bars):
        if b['time'] == buy_date:
            buy_idx = i
            break
    if buy_idx is None:
        return None

    buy_price = bars[buy_idx]['open']
    if buy_price <= 0:
        return None

    peak = buy_price
    exit_price = buy_price
    exit_day = 0
    exit_reason = '持仓到期'
    daily_returns = []

    for d in range(0, hold_days + 1):
        idx = buy_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']
        ret = (b['close'] / buy_price - 1) * 100
        daily_returns.append(round(ret, 2))

        if b['high'] >= buy_price * (1 + take_profit / 100):
            exit_price = buy_price * (1 + take_profit / 100)
            exit_day = d
            exit_reason = '止盈'
            break
        if d > 0 and b['low'] <= peak * (1 + stop_loss / 100):
            exit_price = peak * (1 + stop_loss / 100)
            exit_day = d
            exit_reason = '追踪止损'
            break
        if b['low'] <= buy_price * (1 + stop_loss / 100):
            exit_price = buy_price * (1 + stop_loss / 100)
            exit_day = d
            exit_reason = '止损'
            break
        exit_price = b['close']
        exit_day = d

    return {
        'buy_price': round(buy_price, 3),
        'exit_price': round(exit_price, 3),
        'return_pct': round((exit_price / buy_price - 1) * 100, 2),
        'peak_pct': round((peak / buy_price - 1) * 100, 2),
        'hold_days': exit_day,
        'exit_reason': exit_reason,
        'daily_returns': daily_returns,
    }


# ================================================================
# 回测
# ================================================================

def classify_signal(code, date, kline_cache):
    """判断信号层级: high / std / None"""
    if code not in kline_cache:
        kline_cache[code] = load_kline(code)
    bars = kline_cache[code]
    if not bars:
        return None, {}
    high_pass, details = check_tech(bars, date, TECH_HIGH)
    if high_pass:
        return 'high', details
    std_pass, details = check_tech(bars, date, TECH_STD)
    if std_pass:
        return 'std', details
    return None, {}


def backtest(records, show_detail=False):
    """回测: 分层统计 (high=涨停级, std=标准)"""
    by_code = build_stock_history(records)
    all_dates = sorted(set(r['trade_date'] for r in records))
    lb_cutoff_dt = datetime.strptime(all_dates[0], '%Y-%m-%d') + timedelta(days=LOOKBACK_DAYS)

    code_dates = {code: [r['trade_date'] for r in recs] for code, recs in by_code.items()}
    date_to_codes = defaultdict(set)
    for code, dates in code_dates.items():
        for d in dates:
            date_to_codes[d].add(code)

    eligible = {c for c, recs in by_code.items() if len(recs) >= CYCLE_MIN_COUNT}
    kline_cache = {}

    stats = {'high': {'signals':0,'hits':0,'trades':[]}, 'std': {'signals':0,'hits':0,'trades':[]}}
    daily_stats = []

    for di, date in enumerate(all_dates):
        dt = datetime.strptime(date, '%Y-%m-%d')
        if dt < lb_cutoff_dt:
            continue

        lb_cut = (dt - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

        # 周期信号
        cycle_codes = []
        for code in eligible:
            dates = code_dates[code]
            prior = [d for d in dates if d < date and d >= lb_cut]
            if len(prior) < 2:
                continue
            gaps = calc_intervals(prior)
            if not gaps:
                continue
            last = prior[-1]
            days_since = (dt - datetime.strptime(last, '%Y-%m-%d')).days
            median_gap = float(np.median(gaps))
            if median_gap <= 0:
                continue
            ratio = days_since / median_gap
            if ratio > CYCLE_K and ratio < CYCLE_K * 4:
                if date not in set(code_dates.get(code, [])):
                    cycle_codes.append(code)

        if not cycle_codes:
            continue

        # 分层分类
        high_codes, std_codes = [], []
        for code in cycle_codes:
            tier, _ = classify_signal(code, date, kline_cache)
            if tier == 'high':
                high_codes.append(code)
            elif tier == 'std':
                std_codes.append(code)

        day_stat = {'date': date, 'high': len(high_codes), 'std': len(std_codes), 'high_hits': 0, 'std_hits': 0}

        for tier, codes in [('high', high_codes), ('std', std_codes)]:
            if not codes:
                continue

            # 命中率
            hit_codes = set()
            for fwd in range(1, FORWARD_DAYS + 1):
                if di + fwd < len(all_dates):
                    future = all_dates[di + fwd]
                    for code in codes:
                        if code in date_to_codes.get(future, set()):
                            hit_codes.add(code)

            stats[tier]['signals'] += len(codes)
            stats[tier]['hits'] += len(hit_codes)
            day_stat[f'{tier}_hits'] = len(hit_codes)

            # 收益回测
            if di + 1 < len(all_dates):
                buy_date = all_dates[di + 1]
                for code in codes:
                    bars = kline_cache.get(code)
                    if not bars:
                        continue
                    result = simulate_trade(bars, buy_date)
                    if result:
                        stats[tier]['trades'].append({
                            'code': code, 'signal_date': date, 'buy_date': buy_date,
                            'tier': tier, 'hit': code in hit_codes, **result
                        })

        daily_stats.append(day_stat)
        if show_detail and (high_codes or std_codes):
            print(f"  {date}: 高确信{len(high_codes)} 命中{day_stat['high_hits']} | "
                  f"标准{len(std_codes)} 命中{day_stat['std_hits']}")

    # === 输出 ===
    print(f"\n{'='*60}")
    print(f"  回测结果 (双层策略)")
    print(f"{'='*60}")
    print(f"  统计天数: {len(daily_stats)}")

    for tier, label in [('high', '高确信(D1涨>10%)'), ('std', '标准(D1涨>3%+连涨>=2)')]:
        s = stats[tier]
        n = s['signals']
        if n == 0:
            print(f"\n  {label}: 无信号")
            continue
        trades = s['trades']
        hit_rate = s['hits'] / n * 100
        avg_pool = n / len(daily_stats) if daily_stats else 0

        print(f"\n  {'='*50}")
        print(f"  {label}")
        print(f"  {'='*50}")
        print(f"  总信号: {n} | 命中: {s['hits']} ({hit_rate:.1f}%) | 日均池: {avg_pool:.1f}")

        if trades:
            nt = len(trades)
            wins = [t for t in trades if t['return_pct'] > 0]
            losses = [t for t in trades if t['return_pct'] <= 0]
            win_rate = len(wins)/nt*100
            avg_ret = sum(t['return_pct'] for t in trades)/nt
            avg_peak = sum(t['peak_pct'] for t in trades)/nt
            avg_win = sum(t['return_pct'] for t in wins)/len(wins) if wins else 0
            avg_loss = sum(t['return_pct'] for t in losses)/len(losses) if losses else 0
            total_ret = sum(t['return_pct'] for t in trades)
            total_days = sum(t['hold_days'] for t in trades)
            rpd = total_ret/total_days if total_days > 0 else 0

            exit_reasons = defaultdict(int)
            for t in trades:
                exit_reasons[t['exit_reason']] += 1

            hit_trades = [t for t in trades if t['hit']]
            miss_trades = [t for t in trades if not t['hit']]

            print(f"  胜率: {win_rate:.1f}% | 盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss else f"  胜率: {win_rate:.1f}%")
            print(f"  均收益: {avg_ret:+.2f}% | 均峰值: {avg_peak:+.2f}%")
            print(f"  总收益: {total_ret:+.2f}% | 日均: {rpd:+.3f}% | 年化: {rpd*250:+.1f}%")

            print(f"  出场:", end="")
            for reason, cnt in sorted(exit_reasons.items(), key=lambda x:-x[1]):
                print(f"  {reason}:{cnt}", end="")
            print()

            if hit_trades and miss_trades:
                h_avg = sum(t['return_pct'] for t in hit_trades)/len(hit_trades)
                m_avg = sum(t['return_pct'] for t in miss_trades)/len(miss_trades)
                print(f"  命中({len(hit_trades)}笔): {h_avg:+.2f}% | 未命中({len(miss_trades)}笔): {m_avg:+.2f}%")

    # 汇总
    all_trades = stats['high']['trades'] + stats['std']['trades']
    if all_trades:
        total_ret = sum(t['return_pct'] for t in all_trades)
        total_days = sum(t['hold_days'] for t in all_trades)
        rpd = total_ret/total_days if total_days > 0 else 0
        print(f"\n  {'='*50}")
        print(f"  合计")
        print(f"  {'='*50}")
        print(f"  总信号: {len(all_trades)} | 总收益: {total_ret:+.2f}% | 年化: {rpd*250:+.1f}%")

    return daily_stats, all_trades


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="龙虎榜D0预警系统")
    parser.add_argument("--scan", action="store_true", help="扫描当前预警")
    parser.add_argument("--backtest", action="store_true", help="回测验证")
    parser.add_argument("--detail", action="store_true", help="输出详细信息")
    parser.add_argument("--years", type=int, default=2, help="数据年数")
    args = parser.parse_args()

    print("加载龙虎榜数据...", file=sys.stderr)
    records = load_dragon_data(args.years)
    print(f"  {len(records)}条记录", file=sys.stderr)

    if args.scan:
        kline_cache = {}
        scan_alerts(records, kline_cache=kline_cache, show_detail=args.detail)
        return

    if args.backtest:
        backtest(records, show_detail=args.detail)
        return

    # 默认: 扫描
    scan_alerts(records, show_detail=args.detail)


if __name__ == "__main__":
    main()
