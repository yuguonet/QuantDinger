# -*- coding: utf-8 -*-
"""策略: 尾盘(14:56)买入非涨停股 → 押"明天开盘上涨"
V2 评分系统 (2026-09-08)

精掐规则 (默认):
  score>=8 + pre5_norm<=-10 + norm_amp>=10 + tail_ret -2.8~-0.5%
  回测: +60.17% 累计, 夏普8.117, 回撤-3.40%, 日胜率72.4%, 9.5笔/天, 29活跃日/58天

评分因子 (归一化: 创/科板*0.5, 主板不变):
  day_gain   当日大跌幅度    权重最高(4分), 核心因子
  tail_ret   尾盘回落幅度    14:20~14:40均价 → 14:55收盘, 20分钟均价更稳定
  pos_range  日内位置(不归一化) 低位入场安全边际大
  amplitude  振幅(组合放大器)  单独无区分度, 与tail_ret组合有效
  pre5_gain  近5日涨幅(归一化) 超卖信号, 作为score>=8后的二次过滤

不使用的因子(V2验证无效):
  dist_limit   近板=追高, 回测全亏
  touched_today/lu_recent  近板型, 无独立alpha
  vw_frac/breadth/vol_ratio  区分度弱或lift<1x

卖出策略:
  SMART: A系规则(近板<=3.5%+炸板+人气)→移动止盈2%; 其余→开盘卖

数据: kline_1m_YYYY (3个月窗口)
"""
import sys
import argparse
import json
from datetime import datetime
from multiprocessing import Pool
from collections import defaultdict

sys.path.insert(0, r'D:\quantdinger')
sys.path.insert(0, r'D:\quantdinger\backend_api_python')

BUY_BAR = 235
TAIL_START = 199  # 14:20
TAIL_END = 219    # 14:40
MIN_BARS = 230
MAX_DAY_GAP = 7

_writer = None
START = END = None


def _w():
    global _writer
    if _writer is None:
        from backtest_realtime_monitor import _get_writer
        _writer = _get_writer()
    return _writer


def fetch_1m(code, start_date, end_date):
    from app.data_sources.provider.adjustment import unadj_to_qfq
    try:
        data = _w().query('CNStock', code, '1m',
                          start_time=start_date, end_time=end_date, limit=0)
        if not data:
            return []
        bars = [{'time': str(r['time']), 'open': float(r['open']), 'high': float(r['high']),
                 'low': float(r['low']), 'close': float(r['close']), 'volume': float(r['volume'])}
                for r in data]
        return unadj_to_qfq(bars, code)
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def group_days(bars):
    days = []
    cur, cur_bars = None, None
    for b in bars:
        d = b['time'][:10]
        if d != cur:
            if cur is not None and cur_bars and len(cur_bars) >= MIN_BARS:
                days.append((cur, cur_bars))
            cur = d
            cur_bars = [b]
        else:
            cur_bars.append(b)
    if cur is not None and cur_bars and len(cur_bars) >= MIN_BARS:
        days.append((cur, cur_bars))
    return days


def day_limit_pct(board_type):
    return 0.20 if board_type == 'gem_star' else 0.10


def analyze_stock(code, bars, board_type):
    days = group_days(bars)
    if len(days) < 8:
        return []
    limit_pct = day_limit_pct(board_type)
    rows = []
    for k in range(6, len(days) - 1):
        date, day = days[k]
        if len(day) <= BUY_BAR:
            continue
        next_date, next_day = days[k + 1]
        try:
            gd = (datetime.strptime(next_date, '%Y-%m-%d') - datetime.strptime(date, '%Y-%m-%d')).days
        except ValueError:
            continue
        if gd < 1 or gd > MAX_DAY_GAP or not next_day:
            continue

        prev_close = float(days[k - 1][1][-1]['close'])
        if prev_close <= 0:
            continue
        limit_price_today = round(prev_close * (1 + limit_pct), 2)
        buy_price = float(day[BUY_BAR]['open'])
        if buy_price <= 0:
            continue
        # 当日14:56仍封板 → 买不进, 排除
        if buy_price >= limit_price_today * 0.998:
            continue

        seg = day[:BUY_BAR]
        cum_pv = cum_v = 0.0
        above = up_bars = 0
        prev_c = None
        day_high = day_low = None
        vol_today = 0.0
        touched = False
        morning_close = None
        for m, bb in enumerate(seg):
            o, c, h, l, v = (float(bb['open']), float(bb['close']), float(bb['high']),
                             float(bb['low']), float(bb['volume']))
            tp = (h + l + c) / 3.0
            cum_pv += tp * v
            cum_v += v
            if cum_v > 0 and c > cum_pv / cum_v:
                above += 1
            if prev_c is not None and c > prev_c:
                up_bars += 1
            prev_c = c
            day_high = h if day_high is None else max(day_high, h)
            day_low = l if day_low is None else min(day_low, l)
            vol_today += v
            if h >= limit_price_today * 0.998:
                touched = True
            if m == 119:
                morning_close = c
        if cum_v <= 0 or not day_high or day_high <= day_low:
            continue
        vw_frac = above / len(seg)
        vwap = cum_pv / cum_v if cum_v > 0 else buy_price
        vwap_dist = (buy_price - vwap) / vwap * 100  # 正=价格在VWAP上方, 负=下方
        # 尾盘回落: 14:55收盘 vs 14:20~14:40均价 (20分钟均价更稳定)
        tail_bars = [float(day[i]['close']) for i in range(TAIL_START, min(TAIL_END + 1, len(day)))]
        tail_avg = sum(tail_bars) / len(tail_bars) if tail_bars else float(seg[-1]['close'])
        tail_ret = (float(seg[-1]['close']) / tail_avg - 1) * 100
        day_gain = (buy_price / prev_close - 1) * 100
        dist_limit = (limit_price_today - buy_price) / buy_price * 100
        amplitude = (day_high - day_low) / prev_close * 100
        pos_range = (buy_price - day_low) / (day_high - day_low)
        breadth = up_bars / len(seg)
        vol_ratio = vol_today / (sum(float(x['volume']) for x in days[k - 1][1]) or 1)
        morning_ret = (morning_close / float(day[0]['open']) - 1) * 100 if morning_close else 0.0
        aft_ret = (buy_price / morning_close - 1) * 100 if morning_close else 0.0
        lu_recent = 0
        for dd in range(1, 6):
            cl = float(days[k - dd][1][-1]['close'])
            pc = float(days[k - dd - 1][1][-1]['close'])
            if pc > 0 and cl >= pc * (1 + limit_pct) * 0.998:
                lu_recent += 1
        pre5_gain = (buy_price / float(days[k - 5][1][-1]['close']) - 1) * 100

        # ===== 结果 (明日) =====
        day_close = float(day[-1]['close'])
        limit_price_next = round(day_close * (1 + limit_pct), 2)
        next_open = float(next_day[0]['open'])
        next_high = max(float(x['high']) for x in next_day)
        next_close = float(next_day[-1]['close'])
        next_low = min(float(x['low']) for x in next_day)
        next_touch = next_high >= limit_price_next * 0.998
        next_max_gain = (next_high / day_close - 1) * 100
        next_gap = (next_open / day_close - 1) * 100

        # ===== 多卖出策略模拟 (基于明日分钟线) =====
        # 策略命名: {触板处理}_{不触板处理}
        # 触板处理: lim = 涨停价卖
        # 不触板处理: open/close/stop_X%/trail_X%/morning

        def _ret(px):
            return (px / buy_price - 1) * 100

        # S1-raw: 触板→涨停价, 否则→开盘价 (原始基准, 仅对比用)
        ret_raw_open = _ret(limit_price_next) if next_touch else _ret(next_open)

        # S2: 触板→涨停价, 否则→收盘价
        ret_close = _ret(limit_price_next) if next_touch else _ret(next_close)

        # S3~S5: 触板→涨停价, 否则→止损X% (盘中低点触及止损价→按止损价成交; 未触发→收盘价)
        def _stop_loss(stop_pct):
            if next_touch:
                return _ret(limit_price_next)
            stop_price = buy_price * (1 + stop_pct)
            for bar in next_day:
                if float(bar['low']) <= stop_price:
                    return _ret(stop_price)
            return _ret(next_close)

        ret_stop2 = _stop_loss(-0.02)
        ret_stop3 = _stop_loss(-0.03)
        ret_stop5 = _stop_loss(-0.05)

        # S6: 触板→涨停价, 否则→移动止盈2%
        # 盘中最高涨幅超0.5%后回落2%触发卖出, 未触发则按收盘卖
        if next_touch:
            ret_trail2 = _ret(limit_price_next)
        else:
            highest = buy_price
            sold = False
            for bar in next_day:
                h = float(bar['high'])
                if h > highest:
                    highest = h
                trail_price = highest * (1 - 0.02)
                if float(bar['low']) <= trail_price and highest > buy_price * 1.005:
                    ret_trail2 = _ret(trail_price)
                    sold = True
                    break
            if not sold:
                ret_trail2 = _ret(next_close)

        # 默认策略(=SMART): 命中A系规则(近板+炸板+人气)→移动止盈2%; 否则→开盘卖
        # A系规则: 0 < dist_limit <= 3.5 and touched_today and lu_recent >= 1
        hit_a_rule = (0 < dist_limit <= 3.5 and touched and lu_recent >= 1)
        ret_open = ret_trail2 if hit_a_rule else ret_raw_open

        # SMART2: A系→移动止盈2%, 入口过滤(amp>=5+尾盘回落)→移动止盈2%, 其余→开盘卖
        # 入口过滤: amplitude>=5 且 tail_ret<=-0.3 (score2-6段最强过滤, 31%覆盖, 夏普2.0)
        hit_entry_filter = (amplitude >= 5 and tail_ret <= -0.3)
        ret_smart2 = ret_trail2 if (hit_a_rule or hit_entry_filter) else ret_raw_open

        # S7: 触板→涨停价, 否则→移动止盈3%
        def _trail_stop(trail_pct):
            if next_touch:
                return _ret(limit_price_next)
            highest = buy_price
            for bar in next_day:
                h = float(bar['high'])
                if h > highest:
                    highest = h
                trail_price = highest * (1 - trail_pct)
                if float(bar['low']) <= trail_price:
                    return _ret(trail_price)
            return _ret(next_close)

        ret_trail3 = _trail_stop(0.03)

        # S8: 触板→涨停价, 否则→上午收盘(11:30)卖
        morning_sell_idx = 119
        if next_touch:
            ret_morning = _ret(limit_price_next)
        elif len(next_day) > morning_sell_idx:
            ret_morning = _ret(float(next_day[morning_sell_idx]['close']))
        else:
            ret_morning = _ret(next_close)

        # S9: 触板→涨停价, 否则→开盘价, 兜底止损-3%
        if next_touch:
            ret_open_sl3 = _ret(limit_price_next)
        else:
            stop_price = buy_price * 0.97
            hit_stop = False
            for bar in next_day:
                if float(bar['low']) <= stop_price:
                    ret_open_sl3 = _ret(stop_price)
                    hit_stop = True
                    break
            if not hit_stop:
                ret_open_sl3 = _ret(next_open)

        # S10: 触板→涨停价, 否则→收盘价, 兜底止损-3%
        if next_touch:
            ret_close_sl3 = _ret(limit_price_next)
        else:
            stop_price = buy_price * 0.97
            hit_stop = False
            for bar in next_day:
                if float(bar['low']) <= stop_price:
                    ret_close_sl3 = _ret(stop_price)
                    hit_stop = True
                    break
            if not hit_stop:
                ret_close_sl3 = _ret(next_close)

        # S11: 触板→涨停价, 否则→移动止盈2%+止损-3% 双保险
        if next_touch:
            ret_trail2_sl3 = _ret(limit_price_next)
        else:
            stop_price = buy_price * 0.97
            highest = buy_price
            sold = False
            for bar in next_day:
                h, l = float(bar['high']), float(bar['low'])
                if h > highest:
                    highest = h
                # 止损优先
                if l <= stop_price:
                    ret_trail2_sl3 = _ret(stop_price)
                    sold = True
                    break
                # 移动止盈
                trail_price = highest * 0.98
                if l <= trail_price and highest > buy_price * 1.005:
                    ret_trail2_sl3 = _ret(trail_price)
                    sold = True
                    break
            if not sold:
                ret_trail2_sl3 = _ret(next_close)

        # 明日最低价收益
        next_min_ret = _ret(next_low)
        next_close_ret = _ret(next_close)

        rows.append({
            'code': code, 'board': board_type, 'date': date,
            'day_gain': round(day_gain, 2), 'dist_limit': round(dist_limit, 2),
            'touched_today': touched, 'vw_frac': round(vw_frac, 4),
            'vwap_dist': round(vwap_dist, 3),
            'tail_ret': round(tail_ret, 3), 'amplitude': round(amplitude, 2),
            'pos_range': round(pos_range, 3), 'breadth': round(breadth, 3),
            'vol_ratio': round(vol_ratio, 3), 'morning_ret': round(morning_ret, 2),
            'aft_ret': round(aft_ret, 2), 'lu_recent': lu_recent,
            'pre5_gain': round(pre5_gain, 2),
            'day_close': day_close, 'next_open': next_open,
            'next_touch': next_touch, 'next_max_gain': round(next_max_gain, 2),
            'next_gap': round(next_gap, 2),
            # 多卖出策略收益 (ret_open已=SMART: A系→移动止盈2%, 其余→开盘卖)
            'ret_open': round(ret_open, 3),              # 默认SMART: A系→移动止盈2%, 其余→开盘卖
            'ret_smart2': round(ret_smart2, 3),          # SMART2: A系+入口过滤→移动止盈2%, 其余→开盘卖
            'ret_raw_open': round(ret_raw_open, 3),      # 纯基准: 触板→涨停,否则→开盘卖
            'ret_close': round(ret_close, 3),            # S2: 触板→涨停,否则→收盘
            'ret_stop2': round(ret_stop2, 3),            # S3: 触板→涨停,否则→止损-2%
            'ret_stop3': round(ret_stop3, 3),            # S4: 触板→涨停,否则→止损-3%
            'ret_stop5': round(ret_stop5, 3),            # S5: 触板→涨停,否则→止损-5%
            'ret_trail2': round(ret_trail2, 3),          # S6: 触板→涨停,否则→移动止盈2%
            'ret_trail3': round(ret_trail3, 3),          # S7: 触板→涨停,否则→移动止盈3%
            'ret_morning': round(ret_morning, 3),        # S8: 触板→涨停,否则→上午收盘卖
            'ret_open_sl3': round(ret_open_sl3, 3),      # S9: 触板→涨停,否则→开盘+止损-3%
            'ret_close_sl3': round(ret_close_sl3, 3),    # S10: 触板→涨停,否则→收盘+止损-3%
            'ret_trail2_sl3': round(ret_trail2_sl3, 3),  # S11: 触板→涨停,否则→移动止盈2%+止损-3%
            'next_min_ret': round(next_min_ret, 3), 'next_close_ret': round(next_close_ret, 3),
        })
    return rows


def worker(code):
    try:
        from test_bb_indicator import get_board_type
        bars = fetch_1m(code, START, END)
        if not bars:
            return []
        return analyze_stock(code, bars, get_board_type(code))
    except Exception:
        return []


def init_worker(start, end):
    global START, END
    START, END = start, end


def calc_score(t):
    """V2评分系统: 归一化版, 创/科板因子*0.5映射到主板10%基准

    归一化: 创/科板涨跌幅限制20%, 主板10%
      所有百分比因子(day_gain/tail_ret/amplitude/pre5_gain)统一到10%基准
      pos_range已0~1不需归一化
    精掐: score>=8 + p5<=-10 + norm_amp>=10 + tail_ret(-2.8~-0.5%)
      N=275  日均9.5  活跃日29/58  胜率72.4%  累计+60.17%  夏普8.117  回撤-3.40%

    不使用的因子(V1遗留, V2验证无效):
      dist_limit: 近板=追高, 回测全亏
      touched_today/lu_recent: 近板型, 无独立alpha
      vw_frac/breadth/vol_ratio: 区分度弱或lift<1x
    """
    score = 0.0

    # 归一化系数: 创/科板(gem_star)*0.5, 主板不变
    nf = 0.5 if t.get('board') == 'gem_star' else 1.0

    # === 核心因子1: 当日大跌幅度(归一化) ===
    dg = t['day_gain'] * nf
    if dg <= -8:
        score += 4.0
    elif dg <= -5:
        score += 3.0
    elif dg <= -2:
        score += 1.2
    elif dg <= 0:
        score += 0.5

    # === 核心因子2: 尾盘回落/卖压枯竭(归一化) ===
    tr = t['tail_ret'] * nf
    if tr <= -2:
        score += 3.0
    elif tr <= -1:
        score += 2.5
    elif tr <= -0.3:
        score += 1.5

    # === 核心因子3: 日内低位入场 (不需归一化, 已0~1) ===
    pr = t['pos_range']
    if pr <= 0.2:
        score += 2.0
    elif pr <= 0.4:
        score += 1.0

    # === 组合放大器: 振幅+尾盘回落(归一化) ===
    amp_n = t['amplitude'] * nf
    if amp_n >= 5 and tr <= -0.3:
        score += 1.0

    # === 辅助因子: 近5日超卖(归一化) ===
    p5_n = t['pre5_gain'] * nf
    if p5_n <= -10:
        score += 0.3
    elif p5_n <= -5:
        score += 0.1

    return round(score, 2)


def rule_stats(R, fn, name='', ret_key='ret_open'):
    """计算单个规则的详细统计: 胜率、盈亏比、覆盖、期望收益"""
    sub = [t for t in R if fn(t)]
    if not sub:
        return None
    n = len(sub)
    wins = [t for t in sub if t[ret_key] > 0]
    losses = [t for t in sub if t[ret_key] <= 0]
    n_touch = sum(1 for t in sub if t['next_touch'])
    p_touch = n_touch / n * 100
    avg_ret = sum(t[ret_key] for t in sub) / n
    avg_win = sum(t[ret_key] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t[ret_key] for t in losses) / len(losses) if losses else 0
    win_rate = len(wins) / n * 100
    # 盈亏比 = avg_win / |avg_loss|
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    # 期望收益 = 胜率 * 平均盈利 + (1-胜率) * 平均亏损
    expectancy = (len(wins) / n * avg_win) + (len(losses) / n * avg_loss)
    # 夏普比 (简化: 均值/标准差)
    rets = [t[ret_key] for t in sub]
    mean_r = sum(rets) / n
    var_r = sum((r - mean_r) ** 2 for r in rets) / n
    std_r = var_r ** 0.5
    sharpe = mean_r / std_r if std_r > 0 else 0
    # 最大单笔亏损
    max_loss = min(t[ret_key] for t in sub)
    # 最大单笔盈利
    max_win = max(t[ret_key] for t in sub)
    # 明日最低收益均值 (持仓风险)
    avg_min = sum(t['next_min_ret'] for t in sub) / n

    return {
        'name': name, 'n': n, 'p_touch': p_touch, 'avg_ret': avg_ret,
        'win_rate': win_rate, 'avg_win': avg_win, 'avg_loss': avg_loss,
        'pl_ratio': pl_ratio, 'expectancy': expectancy, 'sharpe': sharpe,
        'max_loss': max_loss, 'max_win': max_win, 'avg_min': avg_min,
        'cover': len(sub) / len(R) * 100,
    }


def print_rule_stat(s, base_touch):
    """打印单个规则的详细统计"""
    if s is None:
        return
    print(f"  {s['name']:<32} N={s['n']:>6} 覆盖{s['cover']:>5.2f}%")
    print(f"    触板率={s['p_touch']:>5.2f}%({s['p_touch']/base_touch:.2f}x)  "
          f"胜率={s['win_rate']:>5.2f}%  盈亏比={s['pl_ratio']:>5.2f}")
    print(f"    均收={s['avg_ret']:>+6.3f}%  期望={s['expectancy']:>+6.3f}%  "
          f"夏普={s['sharpe']:>5.3f}")
    print(f"    盈均={s['avg_win']:>+6.3f}%  亏均={s['avg_loss']:>+6.3f}%  "
          f"最大盈={s['max_win']:>+6.2f}%  最大亏={s['max_loss']:>+6.2f}%")
    print(f"    明日最低均={s['avg_min']:>+6.3f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', type=int, default=0)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--start', default='2026-06-05')
    ap.add_argument('--end', default='2026-09-05')
    ap.add_argument('--out', default=r'D:\quantdinger\_limitnext_full.json')
    ap.add_argument('--load', action='store_true', help='直接加载已有数据文件, 跳过数据采集')
    args = ap.parse_args()
    START, END = args.start, args.end

    from test_bb_indicator import get_all_codes_basicinfo
    codes = get_all_codes_basicinfo(filter_st=True)
    if args.sample and args.sample < len(codes):
        step = len(codes) // args.sample
        codes = codes[::step][:args.sample]
    print('股票数:', len(codes), flush=True)

    if args.load:
        print(f'加载已有数据: {args.out}', flush=True)
        with open(args.out, 'r', encoding='utf-8') as f:
            all_rows = json.load(f)
        print(f'已加载 {len(all_rows)} 条记录', flush=True)
        # 字段兼容: 旧数据 ret_open=基准, ret_smart=SMART; 新代码期望 ret_open=SMART, ret_raw_open=基准
        if 'ret_raw_open' not in all_rows[0] and 'ret_smart' in all_rows[0]:
            for t in all_rows:
                t['ret_raw_open'] = t.pop('ret_open')  # 旧ret_open(基准) → ret_raw_open
                t['ret_open'] = t.pop('ret_smart')      # 旧ret_smart(SMART) → ret_open
            print('  字段兼容: ret_smart→ret_open, 旧ret_open→ret_raw_open', flush=True)
        # 计算ret_smart2 (旧数据没有该字段, 从已有字段推算)
        if 'ret_smart2' not in all_rows[0]:
            for t in all_rows:
                hit_a = (0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['lu_recent'] >= 1)
                hit_ef = (t['amplitude'] >= 5 and t['tail_ret'] <= -0.3)
                t['ret_smart2'] = t['ret_trail2'] if (hit_a or hit_ef) else t['ret_raw_open']
            print('  已计算 ret_smart2 (A系+入口过滤→S6, 其余→开盘卖)', flush=True)
        # vwap_dist 旧数据没有, 设为None (因子分析中会跳过)
        if 'vwap_dist' not in all_rows[0]:
            for t in all_rows:
                t['vwap_dist'] = None
    else:
        all_rows = []
        with Pool(args.workers, initializer=init_worker, initargs=(START, END)) as pool:
            for i, rows in enumerate(pool.imap_unordered(worker, codes, chunksize=8)):
                all_rows.extend(rows)
                if (i + 1) % 250 == 0:
                    print(f'  ...{i+1}/{len(codes)} 样本 {len(all_rows)}', flush=True)
        print('总样本:', len(all_rows), flush=True)
        json.dump(all_rows, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False)
        print('已保存:', args.out, flush=True)

    # 计算多因子评分
    for t in all_rows:
        t['score'] = calc_score(t)

    # ================= 明日涨停/大涨 归因 =================
    R = all_rows
    print('\n========== 结果倒推: 明日涨停/大涨 ==========')
    base_touch = sum(1 for t in R if t['next_touch']) / len(R) * 100
    base_winrate = sum(1 for t in R if t['ret_open'] > 0) / len(R) * 100
    base_avg = sum(t['ret_open'] for t in R) / len(R)
    print(f'基率: P(明日触板)={base_touch:.2f}%  智能胜率={base_winrate:.2f}%  智能均收={base_avg:+.3f}%')
    for x in (3, 5, 7, 9):
        p = sum(1 for t in R if t['next_max_gain'] >= x) / len(R) * 100
        print(f'  P(明日最大涨幅>={x}%)={p:.2f}%')

    def lift(title, key_fn, bins, labels, target='next_touch'):
        print(f'\n--- {title} ---')
        print(f'{"分桶":<22}{"N":>8}{"P(触板)":>9}{"lift":>6}{"P(大涨9%)":>10}{"lift":>6}')
        for (lo, hi), tag in zip(bins, labels):
            sub = [t for t in R if lo <= key_fn(t) < hi]
            if not sub:
                continue
            p = sum(1 for t in sub if t[target]) / len(sub) * 100
            p9 = sum(1 for t in sub if t['next_max_gain'] >= 9) / len(sub) * 100
            print(f'{tag:<22}{len(sub):>8}{p:>8.2f}%{p/base_touch:>5.2f}x{p9:>9.2f}%{p9/2.0:>5.2f}x'
                  if base_touch > 0 else f'{tag:<22}{len(sub):>8}')

    lift('当日涨幅 (14:56)', lambda t: t['day_gain'],
         [(-99, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 8), (8, 9.5), (9.5, 99)],
         ['<-5', '-5~-2', '-2~0', '0~2', '2~5', '5~8', '8~9.5(近板)', '9.5+(贴板)'])
    lift('距涨停剩余空间 dist_limit%', lambda t: t['dist_limit'],
         [(0, 1), (1, 2), (2, 3.5), (3.5, 5), (5, 8), (8, 99)],
         ['<1(贴板)', '1~2', '2~3.5', '3.5~5', '5~8', '>=8'])
    lift('盘中炸板 touched_today', lambda t: 1 if t['touched_today'] else 0,
         [(0, 1), (1, 2)], ['未触板', '炸过板'])
    lift('近5日涨停次数 lu_recent', lambda t: t['lu_recent'],
         [(0, 1), (1, 2), (2, 3), (3, 9)], ['0次', '1次', '2次', '>=3次'])
    lift('近5日涨幅 pre5_gain%', lambda t: t['pre5_gain'],
         [(-99, -10), (-10, 0), (0, 10), (10, 20), (20, 99)],
         ['<-10', '-10~0', '0~10', '10~20', '>=20'])
    lift('量比 vol_ratio', lambda t: t['vol_ratio'],
         [(0, 0.7), (0.7, 1), (1, 1.5), (1.5, 2.5), (2.5, 4), (4, 99)],
         ['<0.7', '0.7~1', '1~1.5', '1.5~2.5', '2.5~4', '>=4'])
    lift('VWAP上方占比', lambda t: t['vw_frac'],
         [(0, .5), (.5, .8), (.8, .9), (.9, .95), (.95, .98), (.98, 1.01)],
         ['<0.5', '0.5~0.8', '0.8~0.9', '0.9~0.95', '0.95~0.98', '>=0.98'])
    # vwap_dist: 仅新采集数据有此字段
    if R and R[0].get('vwap_dist') is not None:
        lift('VWAP距离 vwap_dist%', lambda t: t['vwap_dist'],
             [(-99, -5), (-5, -2), (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 5), (5, 99)],
             ['<-5(远低于)', '-5~-2', '-2~-1', '-1~0(略低)', '0~1(略高)', '1~2', '2~5', '>=5(远高于)'])
    lift('尾盘15分钟 tail_ret%', lambda t: t['tail_ret'],
         [(-99, -2), (-2, -1), (-1, -0.3), (-0.3, 0), (0, 0.5), (0.5, 99)],
         ['<-2', '-2~-1', '-1~-0.3', '-0.3~0', '0~0.5', '>=0.5'])
    lift('日内位置 pos_range', lambda t: t['pos_range'],
         [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)],
         ['0~0.2(近低点)', '0.2~0.4', '0.4~0.6', '0.6~0.8', '0.8~1(近高点)'])
    lift('振幅 amplitude%', lambda t: t['amplitude'],
         [(0, 5), (5, 8), (8, 12), (12, 99)], ['<5', '5~8', '8~12', '>=12'])

    # ================= 因子冗余分析 =================
    print('\n========== 因子相关性 (Pearson, 全样本) ==========')
    import math
    factor_keys = ['dist_limit', 'vol_ratio', 'amplitude', 'tail_ret', 'pos_range',
                   'day_gain', 'pre5_gain', 'vw_frac', 'breadth']
    # 计算均值
    n_r = len(R)
    means = {k: sum(t[k] for t in R) / n_r for k in factor_keys}
    # 计算标准差
    stds = {}
    for k in factor_keys:
        var = sum((t[k] - means[k]) ** 2 for t in R) / n_r
        stds[k] = math.sqrt(var) if var > 0 else 1
    # 计算相关系数矩阵
    def _corr(k1, k2):
        m1, m2 = means[k1], means[k2]
        s1, s2 = stds[k1], stds[k2]
        cov = sum((t[k1] - m1) * (t[k2] - m2) for t in R) / n_r
        return cov / (s1 * s2) if s1 > 0 and s2 > 0 else 0
    # 打印相关性矩阵 (只显示|corr|>0.15的配对)
    print(f'  {"因子对":<40}{"相关系数":>8}{"冗余度":>8}')
    pairs = []
    for i, k1 in enumerate(factor_keys):
        for k2 in factor_keys[i + 1:]:
            c = _corr(k1, k2)
            if abs(c) > 0.15:
                pairs.append((k1, k2, c))
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    for k1, k2, c in pairs:
        tag = '高' if abs(c) > 0.5 else ('中' if abs(c) > 0.3 else '低')
        print(f'  {k1 + " x " + k2:<40}{c:>+.4f}  {tag}')

    # ================= 因子在score2-6子集的区分度 =================
    print('\n========== 因子区分度 (score2-6子集: 赢家vs输家) ==========')
    s26 = [t for t in R if 2 <= t['score'] < 6]
    s26_w = [t for t in s26 if t['ret_open'] > 0]
    s26_l = [t for t in s26 if t['ret_open'] <= 0]
    print(f'  赢家: {len(s26_w)}笔  输家: {len(s26_l)}笔')
    print(f'  {"因子":<20}{"赢家均值":>10}{"输家均值":>10}{"差值":>10}{"区分度":>8}')
    for k in factor_keys:
        if k == 'vw_frac':
            continue  # skip vw_frac
        mw = sum(t[k] for t in s26_w) / len(s26_w) if s26_w else 0
        ml = sum(t[k] for t in s26_l) / len(s26_l) if s26_l else 0
        diff = mw - ml
        # 区分度 = |差值| / 标准差
        all_vals = [t[k] for t in s26]
        mean_all = sum(all_vals) / len(all_vals)
        std_all = math.sqrt(sum((v - mean_all) ** 2 for v in all_vals) / len(all_vals))
        d = abs(diff) / std_all if std_all > 0 else 0
        tag = '强' if d > 0.3 else ('中' if d > 0.15 else '弱')
        print(f'  {k:<20}{mw:>+10.3f}{ml:>+10.3f}{diff:>+10.3f}  {tag}({d:.3f})')

    # ================= 多因子评分分析 (实测校准版) =================
    print('\n========== 多因子评分分布 (实测校准) ==========')
    score_bins = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 13), (13, 99)]
    score_labels = ['0~2(弱)', '2~4', '4~6', '6~8', '8~10', '10~13(强)', '>=13(超强)']
    print(f'{"分数段":<18}{"N":>8}{"P(触板)":>9}{"lift":>6}{"智能胜率":>8}{"盈亏比":>8}{"智能均收":>9}{"期望":>9}{"夏普":>7}')
    for (lo, hi), tag in zip(score_bins, score_labels):
        sub = [t for t in R if lo <= t['score'] < hi]
        if not sub:
            continue
        n = len(sub)
        p = sum(1 for t in sub if t['next_touch']) / n * 100
        wins = [t for t in sub if t['ret_open'] > 0]
        losses = [t for t in sub if t['ret_open'] <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t['ret_open'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['ret_open'] for t in losses) / len(losses) if losses else 0
        pl = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        avg_r = sum(t['ret_open'] for t in sub) / n
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t['ret_open'] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        print(f'{tag:<18}{n:>8}{p:>8.2f}%{p/base_touch:>5.2f}x{wr:>7.2f}%{pl:>8.2f}{avg_r:>+8.3f}%{exp:>+8.3f}%{sh:>7.3f}')

    # ================= 评分2~6 子集因子归因 =================
    print('\n========== 评分2~6 子集因子归因 (40,958笔, 目标: 找入口过滤条件) ==========')
    s26 = [t for t in R if 2 <= t['score'] < 6]
    n_s26 = len(s26)
    base_wr_s26 = sum(1 for t in s26 if t['ret_open'] > 0) / n_s26 * 100
    base_exp_s26 = sum(t['ret_open'] for t in s26) / n_s26
    base_touch_s26 = sum(1 for t in s26 if t['next_touch']) / n_s26 * 100
    print(f'基线: N={n_s26}  触板率={base_touch_s26:.2f}%  胜率={base_wr_s26:.2f}%  期望={base_exp_s26:+.3f}%')

    def _s26_lift(title, key_fn, bins, labels):
        """score2-6子集内的因子lift分析"""
        print(f'\n--- {title} ---')
        print(f'{"分桶":<22}{"N":>8}{"触板%":>8}{"lift":>6}{"胜率%":>8}{"期望%":>9}{"vs基线":>8}')
        for (lo, hi), tag in zip(bins, labels):
            sub = [t for t in s26 if lo <= key_fn(t) < hi]
            if not sub or len(sub) < 50:
                continue
            n = len(sub)
            p = sum(1 for t in sub if t['next_touch']) / n * 100
            wr = sum(1 for t in sub if t['ret_open'] > 0) / n * 100
            exp = sum(t['ret_open'] for t in sub) / n
            print(f'{tag:<22}{n:>8}{p:>7.2f}%{p/base_touch_s26:>5.2f}x{wr:>7.2f}%{exp:>+8.3f}%{exp-base_exp_s26:>+7.3f}%')

    _s26_lift('评分2~6: dist_limit', lambda t: t['dist_limit'],
              [(0, 1), (1, 2), (2, 3.5), (3.5, 5), (5, 8), (8, 99)],
              ['<1(贴板)', '1~2', '2~3.5', '3.5~5', '5~8', '>=8'])
    _s26_lift('评分2~6: touched_today', lambda t: 1 if t['touched_today'] else 0,
              [(0, 1), (1, 2)], ['未触板', '炸过板'])
    _s26_lift('评分2~6: lu_recent', lambda t: t['lu_recent'],
              [(0, 1), (1, 2), (2, 9)], ['0次', '1次', '>=2次'])
    _s26_lift('评分2~6: vol_ratio', lambda t: t['vol_ratio'],
              [(0, 1), (1, 1.5), (1.5, 2.5), (2.5, 4), (4, 99)],
              ['<1', '1~1.5', '1.5~2.5', '2.5~4', '>=4'])
    _s26_lift('评分2~6: tail_ret', lambda t: t['tail_ret'],
              [(-99, -2), (-2, -1), (-1, -0.3), (-0.3, 0.3), (0.3, 99)],
              ['<-2', '-2~-1', '-1~-0.3', '-0.3~0.3', '>=0.3'])
    _s26_lift('评分2~6: amplitude', lambda t: t['amplitude'],
              [(0, 5), (5, 8), (8, 12), (12, 99)],
              ['<5', '5~8', '8~12', '>=12'])
    _s26_lift('评分2~6: pos_range', lambda t: t['pos_range'],
              [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)],
              ['0~0.2(近低)', '0.2~0.4', '0.4~0.6', '0.6~0.8', '0.8~1(近高)'])
    _s26_lift('评分2~6: day_gain', lambda t: t['day_gain'],
              [(-99, -5), (-5, 0), (0, 5), (5, 9.5), (9.5, 99)],
              ['<-5(大跌)', '-5~0', '0~5', '5~9.5', '9.5+(贴板)'])
    _s26_lift('评分2~6: pre5_gain', lambda t: t['pre5_gain'],
              [(-99, -10), (-10, 0), (0, 10), (10, 20), (20, 99)],
              ['<-10', '-10~0', '0~10', '10~20', '>=20'])
    _s26_lift('评分2~6: breadth', lambda t: t['breadth'],
              [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)],
              ['<0.3', '0.3~0.5', '0.5~0.7', '>=0.7'])

    # ================= 评分2~6 组合过滤规则 =================
    print('\n========== 评分2~6 组合过滤规则 (SMART策略) ==========')
    s26_rules = [
        ('baseline (无过滤)', lambda t: True),
        # 单因子过滤
        ('dist_limit <= 3.5', lambda t: 0 < t['dist_limit'] <= 3.5),
        ('dist_limit <= 5', lambda t: 0 < t['dist_limit'] <= 5),
        ('touched_today', lambda t: t['touched_today']),
        ('lu_recent >= 1', lambda t: t['lu_recent'] >= 1),
        ('vol_ratio >= 1.5', lambda t: t['vol_ratio'] >= 1.5),
        ('vol_ratio >= 2', lambda t: t['vol_ratio'] >= 2),
        ('tail_ret <= -0.3', lambda t: t['tail_ret'] <= -0.3),
        ('tail_ret <= -1', lambda t: t['tail_ret'] <= -1),
        ('amplitude >= 8', lambda t: t['amplitude'] >= 8),
        ('amplitude >= 5', lambda t: t['amplitude'] >= 5),
        ('pos_range <= 0.4', lambda t: t['pos_range'] <= 0.4),
        ('day_gain >= 5', lambda t: t['day_gain'] >= 5),
        ('day_gain <= -5', lambda t: t['day_gain'] <= -5),
        ('pre5_gain >= 10', lambda t: t['pre5_gain'] >= 10),
        # 双因子组合
        ('dl<=5 + amp>=5', lambda t: 0 < t['dist_limit'] <= 5 and t['amplitude'] >= 5),
        ('dl<=5 + vol>=1.5', lambda t: 0 < t['dist_limit'] <= 5 and t['vol_ratio'] >= 1.5),
        ('dl<=5 + lu>=1', lambda t: 0 < t['dist_limit'] <= 5 and t['lu_recent'] >= 1),
        ('dl<=3.5 + amp>=5', lambda t: 0 < t['dist_limit'] <= 3.5 and t['amplitude'] >= 5),
        ('dl<=3.5 + vol>=1.5', lambda t: 0 < t['dist_limit'] <= 3.5 and t['vol_ratio'] >= 1.5),
        ('dl<=3.5 + tail<=-0.3', lambda t: 0 < t['dist_limit'] <= 3.5 and t['tail_ret'] <= -0.3),
        ('amp>=5 + vol>=1.5', lambda t: t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5),
        ('amp>=5 + lu>=1', lambda t: t['amplitude'] >= 5 and t['lu_recent'] >= 1),
        ('amp>=5 + tail<=-0.3', lambda t: t['amplitude'] >= 5 and t['tail_ret'] <= -0.3),
        ('vol>=1.5 + lu>=1', lambda t: t['vol_ratio'] >= 1.5 and t['lu_recent'] >= 1),
        ('vol>=1.5 + tail<=-0.3', lambda t: t['vol_ratio'] >= 1.5 and t['tail_ret'] <= -0.3),
        ('dg>=5 + amp>=5', lambda t: t['day_gain'] >= 5 and t['amplitude'] >= 5),
        ('dg<=-5 + lu>=1', lambda t: t['day_gain'] <= -5 and t['lu_recent'] >= 1),
        ('pre5>=10 + amp>=5', lambda t: t['pre5_gain'] >= 10 and t['amplitude'] >= 5),
        ('pre5>=10 + vol>=1.5', lambda t: t['pre5_gain'] >= 10 and t['vol_ratio'] >= 1.5),
        # 三因子组合
        ('dl<=5 + amp>=5 + vol>=1.5', lambda t: 0 < t['dist_limit'] <= 5 and t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5),
        ('dl<=5 + amp>=5 + lu>=1', lambda t: 0 < t['dist_limit'] <= 5 and t['amplitude'] >= 5 and t['lu_recent'] >= 1),
        ('dl<=3.5 + amp>=8 + vol>=1.5', lambda t: 0 < t['dist_limit'] <= 3.5 and t['amplitude'] >= 8 and t['vol_ratio'] >= 1.5),
        ('dl<=5 + vol>=2 + lu>=1', lambda t: 0 < t['dist_limit'] <= 5 and t['vol_ratio'] >= 2 and t['lu_recent'] >= 1),
        ('amp>=5 + vol>=1.5 + lu>=1', lambda t: t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5 and t['lu_recent'] >= 1),
        ('amp>=5 + vol>=1.5 + tail<=-0.3', lambda t: t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5 and t['tail_ret'] <= -0.3),
        ('dl<=5 + amp>=5 + tail<=-0.3', lambda t: 0 < t['dist_limit'] <= 5 and t['amplitude'] >= 5 and t['tail_ret'] <= -0.3),
        ('dg>=5 + amp>=5 + vol>=1.5', lambda t: t['day_gain'] >= 5 and t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5),
        ('dg<=-5 + amp>=5 + lu>=1', lambda t: t['day_gain'] <= -5 and t['amplitude'] >= 5 and t['lu_recent'] >= 1),
        ('pre5>=10 + amp>=5 + vol>=1.5', lambda t: t['pre5_gain'] >= 10 and t['amplitude'] >= 5 and t['vol_ratio'] >= 1.5),
    ]

    print(f'\n{"规则":<36}{"N":>8}{"覆盖%":>8}{"触板%":>8}{"lift":>6}{"胜率%":>8}{"期望%":>9}{"夏普":>7}{"均收%":>9}')
    print('-' * 115)
    s26_results = []
    for name, fn in s26_rules:
        sub = [t for t in s26 if fn(t)]
        if len(sub) < 50:
            continue
        n = len(sub)
        p = sum(1 for t in sub if t['next_touch']) / n * 100
        wr = sum(1 for t in sub if t['ret_open'] > 0) / n * 100
        avg_r = sum(t['ret_open'] for t in sub) / n
        wins = [t for t in sub if t['ret_open'] > 0]
        losses = [t for t in sub if t['ret_open'] <= 0]
        avg_w = sum(t['ret_open'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['ret_open'] for t in losses) / len(losses) if losses else 0
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t['ret_open'] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        s26_results.append({'name': name, 'n': n, 'cover': n/n_s26*100, 'p_touch': p,
                            'lift': p/base_touch_s26 if base_touch_s26 > 0 else 0,
                            'win_rate': wr, 'expectancy': exp, 'sharpe': sh, 'avg_ret': avg_r})
        print(f'  {name:<34}{n:>8}{n/n_s26*100:>7.1f}%{p:>7.2f}%{p/base_touch_s26:>5.2f}x'
              f'{wr:>7.2f}%{exp:>+8.3f}%{sh:>6.3f}{avg_r:>+8.3f}%')

    # 按期望收益排名 (正期望)
    print('\n--- 评分2~6 组合规则排名 (按期望收益, 正期望, N>=50) ---')
    by_exp_s26 = sorted([s for s in s26_results if s['expectancy'] > 0],
                        key=lambda x: x['expectancy'], reverse=True)[:20]
    for i, s in enumerate(by_exp_s26):
        print(f'  {i+1:>2}. {s["name"]:<34} 期望={s["expectancy"]:>+6.3f}%  '
              f'胜率={s["win_rate"]:>5.1f}%  夏普={s["sharpe"]:>5.3f}  '
              f'N={s["n"]:>5}  覆盖={s["cover"]:>4.1f}%')
    print('\n===== 规则对比: 原版 vs 增强版 =====')
    print('=' * 120)

    # 原版规则
    rules_old = [
        ('[原]R1 近板(距板<=2%)', lambda t: 0 < t['dist_limit'] <= 2),
        ('[原]R2 近板 & 炸过板', lambda t: 0 < t['dist_limit'] <= 2 and t['touched_today']),
        ('[原]R3 近5日涨停>=2 & 近板', lambda t: t['lu_recent'] >= 2 and 0 < t['dist_limit'] <= 2),
        ('[原]R4 涨幅5%+ & 量比>=2.5', lambda t: t['day_gain'] >= 5 and t['vol_ratio'] >= 2.5),
        ('[原]R5 涨幅5%+ & breadth>=0.6', lambda t: t['day_gain'] >= 5 and t['breadth'] >= 0.6),
        ('[原]R6 人气(>=1) & 涨幅2%+', lambda t: t['lu_recent'] >= 1 and t['day_gain'] >= 2),
        ('[原]R7 人气 & 近板', lambda t: t['lu_recent'] >= 1 and 0 < t['dist_limit'] <= 3.5),
        ('[原]R8 涨幅5%+ & 尾盘回落<=0', lambda t: t['day_gain'] >= 5 and t['tail_ret'] <= 0),
    ]

    # 增强版规则 (基于实测lift数据优化, 修正VWAP/尾盘/位置方向)
    rules_new = [
        # --- 核心规则: 贴板+炸板+人气 三重确认 (lift叠加) ---
        ('[新]A1 贴板<=2%+炸板+人气', lambda t: (
            0 < t['dist_limit'] <= 2 and t['touched_today'] and t['lu_recent'] >= 1)),
        ('[新]A2 贴板<=2%+炸板+人气>=2', lambda t: (
            0 < t['dist_limit'] <= 2 and t['touched_today'] and t['lu_recent'] >= 2)),
        ('[新]A3 贴板<=1%+炸板+人气', lambda t: (
            0 < t['dist_limit'] <= 1 and t['touched_today'] and t['lu_recent'] >= 1)),
        ('[新]A4 贴板<=3.5%+炸板+人气', lambda t: (
            0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['lu_recent'] >= 1)),

        # --- 人气+放量组合 (利用人气的高lift) ---
        ('[新]B1 人气>=2+放量>=2.5', lambda t: (
            t['lu_recent'] >= 2 and t['vol_ratio'] >= 2.5)),
        ('[新]B2 人气>=2+涨幅5%+', lambda t: (
            t['lu_recent'] >= 2 and 5 <= t['day_gain'] < 9.5)),
        ('[新]B3 人气>=1+涨幅5%+量比>=1.5', lambda t: (
            t['lu_recent'] >= 1 and t['day_gain'] >= 5 and t['vol_ratio'] >= 1.5)),
        ('[新]B4 人气>=2+近板<=3.5%', lambda t: (
            t['lu_recent'] >= 2 and 0 < t['dist_limit'] <= 3.5)),

        # --- 修正方向规则 (利用尾盘回落+低位的正向lift) ---
        ('[新]C1 近板<=2%+炸板+尾盘回落', lambda t: (
            0 < t['dist_limit'] <= 2 and t['touched_today'] and t['tail_ret'] <= -0.3)),
        ('[新]C2 近板<=2%+人气+低位', lambda t: (
            0 < t['dist_limit'] <= 2 and t['lu_recent'] >= 1 and t['pos_range'] <= 0.4)),
        ('[新]C3 近板<=3.5%+炸板+放量', lambda t: (
            0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['vol_ratio'] >= 1.5)),
        ('[新]C4 炸板+人气+放量', lambda t: (
            t['touched_today'] and t['lu_recent'] >= 1 and t['vol_ratio'] >= 2)),

        # --- 大跌反弹规则 (lift: <-5→2.13x) ---
        ('[新]D1 大跌>5%+炸板', lambda t: (
            t['day_gain'] <= -5 and t['touched_today'])),
        ('[新]D2 大跌>5%+人气+放量', lambda t: (
            t['day_gain'] <= -5 and t['lu_recent'] >= 1 and t['vol_ratio'] >= 1.5)),

        # --- 宽松覆盖规则 (兼顾覆盖率) ---
        ('[新]E1 近板<=3.5%+炸板', lambda t: (
            0 < t['dist_limit'] <= 3.5 and t['touched_today'])),
        ('[新]E2 近板<=3.5%+人气>=1', lambda t: (
            0 < t['dist_limit'] <= 3.5 and t['lu_recent'] >= 1)),
        ('[新]E3 近板<=2%+放量>=2', lambda t: (
            0 < t['dist_limit'] <= 2 and t['vol_ratio'] >= 2)),

        # --- 评分阈值规则 (综合打分, 校准后) ---
        ('[新]S1 评分>=10', lambda t: t['score'] >= 10),
        ('[新]S2 评分>=12', lambda t: t['score'] >= 12),
        ('[新]S3 评分>=14', lambda t: t['score'] >= 14),
        ('[新]S4 评分>=10+近板<=3.5', lambda t: t['score'] >= 10 and 0 < t['dist_limit'] <= 3.5),
        ('[新]S5 评分>=8+炸板', lambda t: t['score'] >= 8 and t['touched_today']),
        ('[新]S6 评分>=8+人气>=1', lambda t: t['score'] >= 8 and t['lu_recent'] >= 1),
    ]

    all_rules = rules_old + rules_new

    print(f'\n{"规则":<36}{"N":>7}{"覆盖%":>7}{"触板%":>7}{"触板lift":>8}'
          f'{"胜率%":>7}{"盈亏比":>7}{"均收%":>8}{"期望%":>8}{"夏普":>7}{"最大亏%":>8}')
    print('  (默认ret_open: A系规则→移动止盈2%, 其余→开盘卖)')
    print('-' * 130)

    results_all = []
    for name, fn in all_rules:
        s = rule_stats(R, fn, name, ret_key='ret_open')
        if s is None:
            print(f'  {name:<34} 无样本')
            continue
        results_all.append(s)
        print(f'  {s["name"]:<34}{s["n"]:>7}{s["cover"]:>6.2f}%'
              f'{s["p_touch"]:>6.2f}%{s["p_touch"]/base_touch:>7.2f}x'
              f'{s["win_rate"]:>6.2f}%{s["pl_ratio"]:>7.2f}'
              f'{s["avg_ret"]:>+7.3f}%{s["expectancy"]:>+7.3f}%'
              f'{s["sharpe"]:>7.3f}{s["max_loss"]:>+7.2f}%')

    # ================= 卖出策略对比 =================
    print('\n========== 卖出策略对比 (默认=SMART: A系→移动止盈2%, 其余→开盘卖) ==========')
    sell_strategies = [
        ('DEFAULT SMART(默认)',                'ret_open'),
        ('SMART2(A系+入口过滤→S6)',           'ret_smart2'),
        ('S0  纯基准:触板→涨停,否则→开盘',    'ret_raw_open'),
        ('S6  触板→涨停,否则→移动止盈2%',     'ret_trail2'),
        ('S2  触板→涨停,否则→收盘',           'ret_close'),
        ('S3  触板→涨停,否则→止损-2%',        'ret_stop2'),
        ('S4  触板→涨停,否则→止损-3%',        'ret_stop3'),
        ('S7  触板→涨停,否则→移动止盈3%',     'ret_trail3'),
        ('S11 触板→涨停,否则→移动止盈2%+止损-3%', 'ret_trail2_sl3'),
    ]
    print(f'\n{"策略":<42}{"N":>8}{"胜率%":>8}{"盈亏比":>8}{"均收%":>9}{"期望%":>9}{"夏普":>7}{"最大亏%":>9}{"亏均%":>8}')
    print('-' * 115)
    for sname, rkey in sell_strategies:
        n = len(R)
        wins = [t for t in R if t[rkey] > 0]
        losses = [t for t in R if t[rkey] <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t[rkey] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t[rkey] for t in losses) / len(losses) if losses else 0
        pl = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        avg_r = sum(t[rkey] for t in R) / n
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t[rkey] for t in R]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        mx_loss = min(rets)
        print(f'  {sname:<40}{n:>8}{wr:>7.2f}%{pl:>8.2f}{avg_r:>+8.3f}%{exp:>+8.3f}%{sh:>7.3f}{mx_loss:>+8.2f}%{avg_l:>+7.3f}%')

    # 卖出策略 x 最佳规则 A1 交叉对比
    print('\n--- 卖出策略 x 规则A1(贴板<=2%+炸板+人气) 交叉对比 ---')
    a1_fn = lambda t: 0 < t['dist_limit'] <= 2 and t['touched_today'] and t['lu_recent'] >= 1
    print(f'{"策略":<42}{"N":>7}{"胜率%":>8}{"盈亏比":>8}{"均收%":>9}{"期望%":>9}{"夏普":>7}')
    print('-' * 90)
    for sname, rkey in sell_strategies:
        sub = [t for t in R if a1_fn(t)]
        n = len(sub)
        if n < 10:
            continue
        wins = [t for t in sub if t[rkey] > 0]
        losses = [t for t in sub if t[rkey] <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t[rkey] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t[rkey] for t in losses) / len(losses) if losses else 0
        pl = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        avg_r = sum(t[rkey] for t in sub) / n
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t[rkey] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        print(f'  {sname:<40}{n:>7}{wr:>7.2f}%{pl:>8.2f}{avg_r:>+8.3f}%{exp:>+8.3f}%{sh:>7.3f}')

    # 卖出策略 x 规则D2(大跌>5%+人气+放量) 交叉对比
    print('\n--- 卖出策略 x 规则D2(大跌>5%+人气+放量) 交叉对比 ---')
    d2_fn = lambda t: t['day_gain'] <= -5 and t['lu_recent'] >= 1 and t['vol_ratio'] >= 1.5
    print(f'{"策略":<42}{"N":>7}{"胜率%":>8}{"盈亏比":>8}{"均收%":>9}{"期望%":>9}{"夏普":>7}')
    print('-' * 90)
    for sname, rkey in sell_strategies:
        sub = [t for t in R if d2_fn(t)]
        n = len(sub)
        if n < 10:
            continue
        wins = [t for t in sub if t[rkey] > 0]
        losses = [t for t in sub if t[rkey] <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t[rkey] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t[rkey] for t in losses) / len(losses) if losses else 0
        pl = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        avg_r = sum(t[rkey] for t in sub) / n
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t[rkey] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        print(f'  {sname:<40}{n:>7}{wr:>7.2f}%{pl:>8.2f}{avg_r:>+8.3f}%{exp:>+8.3f}%{sh:>7.3f}')

    # 卖出策略 x 评分2~6区间 (上轮发现的最优分数段)
    print('\n--- 卖出策略 x 评分2~6区间 (最优分数段) ---')
    score_fn = lambda t: 2 <= t['score'] < 6
    print(f'{"策略":<42}{"N":>8}{"胜率%":>8}{"盈亏比":>8}{"均收%":>9}{"期望%":>9}{"夏普":>7}')
    print('-' * 95)
    for sname, rkey in sell_strategies:
        sub = [t for t in R if score_fn(t)]
        n = len(sub)
        if n < 10:
            continue
        wins = [t for t in sub if t[rkey] > 0]
        losses = [t for t in sub if t[rkey] <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t[rkey] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t[rkey] for t in losses) / len(losses) if losses else 0
        pl = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        avg_r = sum(t[rkey] for t in sub) / n
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t[rkey] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        print(f'  {sname:<40}{n:>8}{wr:>7.2f}%{pl:>8.2f}{avg_r:>+8.3f}%{exp:>+8.3f}%{sh:>7.3f}')

    # ================= 最优规则排名 =================
    print('\n===== 最优规则排名 =====')

    # 按期望收益排名 (正期望)
    print('\n--- 按期望收益排名 (Top 15, 要求N>=30) ---')
    by_exp = sorted([s for s in results_all if s['n'] >= 30 and s['expectancy'] > 0],
                    key=lambda x: x['expectancy'], reverse=True)[:15]
    for i, s in enumerate(by_exp):
        print(f'  {i+1:>2}. {s["name"]:<34} 期望={s["expectancy"]:>+6.3f}%  '
              f'胜率={s["win_rate"]:>5.1f}%  盈亏比={s["pl_ratio"]:>5.2f}  '
              f'N={s["n"]:>5}  覆盖={s["cover"]:>4.2f}%')

    # 按夏普比排名
    print('\n--- 按夏普比排名 (Top 15, 要求N>=30) ---')
    by_sharpe = sorted([s for s in results_all if s['n'] >= 30 and s['sharpe'] > 0],
                       key=lambda x: x['sharpe'], reverse=True)[:15]
    for i, s in enumerate(by_sharpe):
        print(f'  {i+1:>2}. {s["name"]:<34} 夏普={s["sharpe"]:>6.3f}  '
              f'均收={s["avg_ret"]:>+6.3f}%  胜率={s["win_rate"]:>5.1f}%  '
              f'N={s["n"]:>5}  覆盖={s["cover"]:>4.2f}%')

    # 按盈亏比排名 (胜率>50%)
    print('\n--- 按盈亏比排名 (Top 15, 要求N>=30 & 胜率>50%) ---')
    by_pl = sorted([s for s in results_all if s['n'] >= 30 and s['win_rate'] > 50],
                   key=lambda x: x['pl_ratio'], reverse=True)[:15]
    for i, s in enumerate(by_pl):
        print(f'  {i+1:>2}. {s["name"]:<34} 盈亏比={s["pl_ratio"]:>6.2f}  '
              f'胜率={s["win_rate"]:>5.1f}%  期望={s["expectancy"]:>+6.3f}%  '
              f'N={s["n"]:>5}')

    # ================= 高分段详细归因 =================
    print('\n========== 高分段(>=13)样本详细归因 ==========')
    high_score = [t for t in R if t['score'] >= 13]
    if high_score:
        n_hs = len(high_score)
        p_hs = sum(1 for t in high_score if t['next_touch']) / n_hs * 100
        avg_hs = sum(t['ret_open'] for t in high_score) / n_hs
        wins_hs = [t for t in high_score if t['ret_open'] > 0]
        losses_hs = [t for t in high_score if t['ret_open'] <= 0]
        wr_hs = len(wins_hs) / n_hs * 100
        avg_w = sum(t['ret_open'] for t in wins_hs) / len(wins_hs) if wins_hs else 0
        avg_l = sum(t['ret_open'] for t in losses_hs) / len(losses_hs) if losses_hs else 0
        pl_hs = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
        exp_hs = (len(wins_hs) / n_hs * avg_w) + (len(losses_hs) / n_hs * avg_l)
        print(f'  高分(>=11): N={n_hs}  触板率={p_hs:.2f}%({p_hs/base_touch:.2f}x)  '
              f'胜率={wr_hs:.2f}%  盈亏比={pl_hs:.2f}')
        print(f'    均收={avg_hs:+.3f}%  期望={exp_hs:+.3f}%  '
              f'盈均={avg_w:+.3f}%  亏均={avg_l:+.3f}%')

        # 高分段中各因子分布
        print('\n  高分段因子分布:')
        print(f'    炸板: {sum(1 for t in high_score if t["touched_today"])}/{n_hs} = '
              f'{sum(1 for t in high_score if t["touched_today"])/n_hs*100:.1f}%')
        print(f'    人气>=1: {sum(1 for t in high_score if t["lu_recent"]>=1)}/{n_hs} = '
              f'{sum(1 for t in high_score if t["lu_recent"]>=1)/n_hs*100:.1f}%')
        print(f'    量比>=2: {sum(1 for t in high_score if t["vol_ratio"]>=2)}/{n_hs} = '
              f'{sum(1 for t in high_score if t["vol_ratio"]>=2)/n_hs*100:.1f}%')
        print(f'    VWAP>=0.9: {sum(1 for t in high_score if t["vw_frac"]>=0.9)}/{n_hs} = '
              f'{sum(1 for t in high_score if t["vw_frac"]>=0.9)/n_hs*100:.1f}%')
        print(f'    尾盘>=0: {sum(1 for t in high_score if t["tail_ret"]>=0)}/{n_hs} = '
              f'{sum(1 for t in high_score if t["tail_ret"]>=0)/n_hs*100:.1f}%')

    # ================= 回测模拟: 累计收益 & 最大回撤 =================
    print('\n========== 回测模拟 (等权每日平均, SMART策略 vs 纯基准) ==========')
    from collections import defaultdict

    # 按日期分组, 每日等权平均收益
    daily_smart = defaultdict(list)
    daily_smart2 = defaultdict(list)     # SMART2: 全样本SMART
    daily_smart2_s26 = defaultdict(list) # score2-6入口过滤+SMART卖: 仅score2-6且通过入口过滤的用SMART, 其余用SMART
    daily_raw = defaultdict(list)
    daily_touch = defaultdict(list)
    daily_score26 = defaultdict(list)  # 评分2~6区间 SMART
    daily_score26_s6 = defaultdict(list)  # 评分2~6区间 纯S6移动止盈2%
    daily_a_rule = defaultdict(list)   # A系规则
    daily_a_rule_s6 = defaultdict(list)  # A系规则 纯S6移动止盈2%
    daily_s26_dg5 = defaultdict(list)    # score2-6 + day_gain<=-5 (SMART)
    daily_s26_dg5_s6 = defaultdict(list) # score2-6 + day_gain<=-5 (S6)
    daily_s26_tail1 = defaultdict(list)  # score2-6 + tail_ret<=-1 (SMART)
    daily_s26_pos4 = defaultdict(list)   # score2-6 + pos_range<=0.4 (SMART)
    daily_s26_at = defaultdict(list)     # score2-6 + amp>=5 + tail<=-0.3 (SMART)
    daily_s4_plus = defaultdict(list)    # score>=4 (SMART)
    daily_s6_plus = defaultdict(list)    # score>=6 (SMART)
    daily_s8_plus = defaultdict(list)    # score>=8 (SMART)
    daily_s10_plus = defaultdict(list)   # score>=10 (SMART)
    daily_s8_p5 = defaultdict(list)      # score>=8 + 归一化p5<=-10 (SMART)
    daily_s8_p5_amp10 = defaultdict(list) # score>=8 + p5 + 归一化amp>=10 + tail_ret(-2.8~-0.5%) (掐尖)
    for t in R:
        d = t['date']
        daily_smart[d].append(t['ret_open'])      # SMART (默认)
        daily_smart2[d].append(t['ret_smart2'])    # SMART2 (全样本)
        daily_raw[d].append(t['ret_raw_open'])     # 纯基准
        daily_touch[d].append(t['next_touch'])
        # SMART2仅score2-6段: score2-6通过入口过滤→SMART卖, score2-6未通过→不开仓, 其余→SMART
        is_s26 = (2 <= t['score'] < 6)
        is_entry_pass = (t['amplitude'] >= 5 and t['tail_ret'] <= -0.3)
        if is_s26 and is_entry_pass:
            daily_smart2_s26[d].append(t['ret_open'])  # 入口过滤通过, 用SMART卖出
        elif not is_s26:
            daily_smart2_s26[d].append(t['ret_open'])  # 非score2-6, 正常SMART
        # score2-6未通过入口过滤 → 不开仓 (不加入daily)
        if is_s26:
            daily_score26[d].append(t['ret_open'])
            daily_score26_s6[d].append(t['ret_trail2'])
            if t['day_gain'] <= -5:
                daily_s26_dg5[d].append(t['ret_open'])
                daily_s26_dg5_s6[d].append(t['ret_trail2'])
            if t['tail_ret'] <= -1:
                daily_s26_tail1[d].append(t['ret_open'])
            if t['pos_range'] <= 0.4:
                daily_s26_pos4[d].append(t['ret_open'])
            if t['amplitude'] >= 5 and t['tail_ret'] <= -0.3:
                daily_s26_at[d].append(t['ret_open'])
        if 0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['lu_recent'] >= 1:
            daily_a_rule[d].append(t['ret_open'])
            daily_a_rule_s6[d].append(t['ret_trail2'])
        # 分数阈值回测 (宁缺毋滥, 高胜率高溢价)
        sc = t['score']
        if sc >= 4:
            daily_s4_plus[d].append(t['ret_open'])
        if sc >= 6:
            daily_s6_plus[d].append(t['ret_open'])
        if sc >= 8:
            daily_s8_plus[d].append(t['ret_open'])
        if sc >= 10:
            daily_s10_plus[d].append(t['ret_open'])
        # score>=8 + 超卖掐尖 (归一化p5)
        nf_p5 = 0.5 if t.get('board') == 'gem_star' else 1.0
        if sc >= 8 and t['pre5_gain'] * nf_p5 <= -10:
            daily_s8_p5[d].append(t['ret_open'])
            if t['amplitude'] * nf_p5 >= 10 and -2.8 <= t['tail_ret'] <= -0.5:
                daily_s8_p5_amp10[d].append(t['ret_open'])

    dates_sorted = sorted(daily_smart.keys())

    def _calc_curve(daily_dict, dates):
        """计算累计收益曲线和最大回撤"""
        curve = [1.0]
        daily_rets = []
        active_dates = []
        for d in dates:
            rets = daily_dict.get(d, [])
            if not rets:
                continue  # 无交易日跳过(不改变曲线)
            avg_r = sum(rets) / len(rets) / 100  # 转为小数
            daily_rets.append(avg_r)
            active_dates.append(d)
            curve.append(curve[-1] * (1 + avg_r))
        # 最大回撤
        peak = curve[0]
        max_dd = 0
        max_dd_start = max_dd_end = active_dates[0] if active_dates else ''
        dd_start = active_dates[0] if active_dates else ''
        for i, v in enumerate(curve[1:]):
            if v > peak:
                peak = v
                dd_start = active_dates[i] if i < len(active_dates) else ''
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd
                max_dd_start = dd_start
                max_dd_end = active_dates[i] if i < len(active_dates) else ''
        # 日度统计
        n_days = len(daily_rets)
        win_days = sum(1 for r in daily_rets if r > 0)
        total_ret = curve[-1] - 1
        # 年化 (按交易日)
        ann_ret = (1 + total_ret) ** (252 / max(n_days, 1)) - 1 if total_ret > -1 else -1
        # 日度夏普
        import math
        mean_r = sum(daily_rets) / n_days if n_days else 0
        std_r = (sum((r - mean_r) ** 2 for r in daily_rets) / n_days) ** 0.5 if n_days else 1
        daily_sharpe = mean_r / std_r if std_r > 0 else 0
        ann_sharpe = daily_sharpe * (252 ** 0.5)
        return {
            'curve': curve, 'dates': dates, 'total_ret': total_ret,
            'ann_ret': ann_ret, 'max_dd': max_dd, 'max_dd_start': max_dd_start,
            'max_dd_end': max_dd_end, 'n_days': n_days, 'win_days': win_days,
            'win_day_rate': win_days / n_days * 100 if n_days else 0,
            'ann_sharpe': ann_sharpe,
        }

    def _print_curve_stats(s, label):
        print(f'\n  [{label}]')
        print(f'    回测天数: {s["n_days"]}天  交易日胜率: {s["win_day_rate"]:.1f}%')
        print(f'    累计收益: {s["total_ret"]*100:>+.2f}%  年化收益: {s["ann_ret"]*100:>+.2f}%')
        print(f'    最大回撤: {s["max_dd"]*100:>.2f}%  ({s["max_dd_start"]} ~ {s["max_dd_end"]})')
        print(f'    年化夏普: {s["ann_sharpe"]:>.3f}')
        # 收益曲线关键节点
        curve = s['curve']
        n = len(curve) - 1
        for pct in (25, 50, 75, 100):
            idx = int(n * pct / 100)
            if idx <= n:
                print(f'    {pct:>3}%进度: 累计 {(curve[idx]-1)*100:>+.2f}%')

    print(f'\n--- 全样本回测 ---')
    s_smart = _calc_curve(daily_smart, dates_sorted)
    _print_curve_stats(s_smart, 'SMART策略(默认)')

    s_smart2 = _calc_curve(daily_smart2, dates_sorted)
    _print_curve_stats(s_smart2, 'SMART2策略(全样本)')

    s_smart2_s26 = _calc_curve(daily_smart2_s26, dates_sorted)
    _print_curve_stats(s_smart2_s26, 'SMART2仅score2-6段')

    s_raw = _calc_curve(daily_raw, dates_sorted)
    _print_curve_stats(s_raw, '纯基准(开盘卖)')

    # 评分2~6区间回测
    print(f'\n--- 评分2~6区间回测 ---')
    s_s26 = _calc_curve(daily_score26, dates_sorted)
    _print_curve_stats(s_s26, 'SMART策略 评分2~6')

    # A系规则回测
    print(f'\n--- A系规则(近板+炸板+人气)回测 ---')
    s_a = _calc_curve(daily_a_rule, dates_sorted)
    _print_curve_stats(s_a, 'SMART策略 A系规则')

    # S6纯移动止盈2% + 评分2~6区间回测
    print(f'\n--- S6纯移动止盈2% + 评分2~6区间回测 ---')
    s_s26_s6 = _calc_curve(daily_score26_s6, dates_sorted)
    _print_curve_stats(s_s26_s6, 'S6移动止盈2% 评分2~6')

    # S6纯移动止盈2% + A系规则回测
    print(f'\n--- S6纯移动止盈2% + A系规则回测 ---')
    s_a_s6 = _calc_curve(daily_a_rule_s6, dates_sorted)
    _print_curve_stats(s_a_s6, 'S6移动止盈2% A系规则')

    # ========== 分数阈值回测 (宁缺毋滥) ==========
    print(f'\n========== 分数阈值回测 (宁缺毋滥, 高胜率高溢价) ==========')
    s_s4 = _calc_curve(daily_s4_plus, dates_sorted)
    _print_curve_stats(s_s4, 'score>=4 (SMART)')
    s_s6 = _calc_curve(daily_s6_plus, dates_sorted)
    _print_curve_stats(s_s6, 'score>=6 (SMART)')
    s_s8 = _calc_curve(daily_s8_plus, dates_sorted)
    _print_curve_stats(s_s8, 'score>=8 (SMART)')
    s_s10 = _calc_curve(daily_s10_plus, dates_sorted)
    _print_curve_stats(s_s10, 'score>=10 (SMART)')

    # score>=8 + 超卖掐尖回测
    s_s8_p5 = _calc_curve(daily_s8_p5, dates_sorted)
    _print_curve_stats(s_s8_p5, 'score>=8 + p5<=-10 (掐尖)')
    s_s8_p5_amp10 = _calc_curve(daily_s8_p5_amp10, dates_sorted)
    _print_curve_stats(s_s8_p5_amp10, 'score>=8 + p5 + norm_amp>=10 (精掐)')

    # 各阈值的交易统计
    for thr, daily_d in [(4, daily_s4_plus), (6, daily_s6_plus), (8, daily_s8_plus), (10, daily_s10_plus), ('8+p5', daily_s8_p5), ('8+p5+amp10', daily_s8_p5_amp10)]:
        total_trades = sum(len(v) for v in daily_d.values())
        active_days = len(daily_d)
        avg_per_day = total_trades / active_days if active_days > 0 else 0
        print(f'  score>={thr}: 总笔数={total_trades}  活跃日={active_days}  均{avg_per_day:.1f}笔/天')

    # ========== score>=8 精炼分析 (60笔/天 → 目标0.5~5笔/天) ==========
    print(f'\n========== score>=8 精炼分析 (60笔/天 → 0.5~5笔/天, 宁缺毋滥) ==========')
    s8 = [t for t in R if t['score'] >= 8]
    n_s8 = len(s8)
    s8_w = [t for t in s8 if t['ret_open'] > 0]
    s8_l = [t for t in s8 if t['ret_open'] <= 0]
    base_wr_s8 = len(s8_w) / n_s8 * 100
    base_exp_s8 = sum(t['ret_open'] for t in s8) / n_s8
    print(f'基线 score>=8: N={n_s8}  胜率={base_wr_s8:.2f}%  期望={base_exp_s8:+.3f}%')
    print(f'  赢家={len(s8_w)}笔  输家={len(s8_l)}笔')

    # 赢家vs输家的因子差异
    import math
    print(f'\n--- score>=8 赢家vs输家因子差异 ---')
    factor_keys_s8 = ['day_gain', 'tail_ret', 'pos_range', 'amplitude', 'vol_ratio',
                      'dist_limit', 'pre5_gain', 'lu_recent', 'vw_frac', 'breadth']
    print(f'  {"因子":<16}{"赢家均值":>10}{"输家均值":>10}{"差值":>10}{"区分度":>8}{"方向":>6}')
    for k in factor_keys_s8:
        mw = sum(t[k] for t in s8_w) / len(s8_w) if s8_w else 0
        ml = sum(t[k] for t in s8_l) / len(s8_l) if s8_l else 0
        diff = mw - ml
        all_vals = [t[k] for t in s8]
        mean_all = sum(all_vals) / len(all_vals)
        std_all = math.sqrt(sum((v - mean_all) ** 2 for v in all_vals) / len(all_vals))
        d = abs(diff) / std_all if std_all > 0 else 0
        tag = '强' if d > 0.3 else ('中' if d > 0.15 else '弱')
        # 方向: 赢家更小还是更大
        direction = '↓好' if diff < 0 else '↑好'
        if d < 0.1:
            direction = '--'
        print(f'  {k:<16}{mw:>+10.3f}{ml:>+10.3f}{diff:>+10.3f}  {tag}({d:.3f}) {direction}')

    # score>=8 子集内的因子阈值扫描
    print(f'\n--- score>=8 子集内因子阈值扫描 ---')

    def _eval_s8(sub, label=''):
        """评估score>=8子集内一个子分组"""
        if not sub or len(sub) < 10:
            return None
        n = len(sub)
        wins = [t for t in sub if t['ret_open'] > 0]
        losses = [t for t in sub if t['ret_open'] <= 0]
        wr = len(wins) / n
        avg_r = sum(t['ret_open'] for t in sub) / n
        avg_w = sum(t['ret_open'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['ret_open'] for t in losses) / len(losses) if losses else 0
        mx_loss = min(t['ret_open'] for t in sub)
        pd = wr / abs(mx_loss) if mx_loss < 0 else float('inf')
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        rets = [t['ret_open'] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        # 预估每日笔数 (假设58天)
        per_day = n / 58
        return {'n': n, 'wr': wr, 'avg_r': avg_r, 'pd': pd, 'exp': exp, 'sh': sh,
                'mx_loss': mx_loss, 'per_day': per_day, 'label': label}

    s8_tests = [
        # 当日跌幅细分
        ('dg<=-8', lambda t: t['day_gain'] <= -8),
        ('dg -8~-5', lambda t: -8 < t['day_gain'] <= -5),
        ('dg -5~-2', lambda t: -5 < t['day_gain'] <= -2),
        ('dg -2~0', lambda t: -2 < t['day_gain'] <= 0),
        ('dg 0~5', lambda t: 0 < t['day_gain'] <= 5),
        ('dg 5~9.5', lambda t: 5 <= t['day_gain'] < 9.5),
        ('dg>=9.5', lambda t: t['day_gain'] >= 9.5),
        # 尾盘回落细分
        ('tr<=-2', lambda t: t['tail_ret'] <= -2),
        ('tr -2~-1', lambda t: -2 < t['tail_ret'] <= -1),
        ('tr -1~-0.3', lambda t: -1 < t['tail_ret'] <= -0.3),
        ('tr -0.3~0', lambda t: -0.3 < t['tail_ret'] <= 0),
        ('tr>0', lambda t: t['tail_ret'] > 0),
        # 位置区间细分
        ('pr<=0.2', lambda t: t['pos_range'] <= 0.2),
        ('pr 0.2~0.4', lambda t: 0.2 < t['pos_range'] <= 0.4),
        ('pr 0.4~0.6', lambda t: 0.4 < t['pos_range'] <= 0.6),
        ('pr>0.6', lambda t: t['pos_range'] > 0.6),
        # 振幅细分
        ('amp>=12', lambda t: t['amplitude'] >= 12),
        ('amp 8~12', lambda t: 8 <= t['amplitude'] < 12),
        ('amp 5~8', lambda t: 5 <= t['amplitude'] < 8),
        ('amp<5', lambda t: t['amplitude'] < 5),
        # 组合
        ('dg<=-5+tr<=-0.3', lambda t: t['day_gain'] <= -5 and t['tail_ret'] <= -0.3),
        ('dg<=-5+pr<=0.4', lambda t: t['day_gain'] <= -5 and t['pos_range'] <= 0.4),
        ('dg<=-5+amp>=8', lambda t: t['day_gain'] <= -5 and t['amplitude'] >= 8),
        ('tr<=-1+pr<=0.4', lambda t: t['tail_ret'] <= -1 and t['pos_range'] <= 0.4),
        ('tr<=-0.3+amp>=8', lambda t: t['tail_ret'] <= -0.3 and t['amplitude'] >= 8),
        ('dg<=-5+tr<=-0.3+pr<=0.4', lambda t: t['day_gain'] <= -5 and t['tail_ret'] <= -0.3 and t['pos_range'] <= 0.4),
        ('dg<=-5+tr<=-0.3+amp>=8', lambda t: t['day_gain'] <= -5 and t['tail_ret'] <= -0.3 and t['amplitude'] >= 8),
        ('amp>=8+tr<=-0.3+pr<=0.4', lambda t: t['amplitude'] >= 8 and t['tail_ret'] <= -0.3 and t['pos_range'] <= 0.4),
        # 近板相关 (V2已证明无效, 但验证在score>=8中是否有意义)
        ('dl<=3.5', lambda t: 0 < t['dist_limit'] <= 3.5),
        ('dl<=2', lambda t: 0 < t['dist_limit'] <= 2),
        # 近5日跌幅
        ('p5<=-10', lambda t: t['pre5_gain'] <= -10),
        ('p5<=-5', lambda t: t['pre5_gain'] <= -5),
        # 量比
        ('vr>=2.5', lambda t: t['vol_ratio'] >= 2.5),
        ('vr>=4', lambda t: t['vol_ratio'] >= 4),
    ]

    print(f'  {"子集":<32}{"N":>6}{"%/天":>6}{"胜率%":>7}{"均收%":>8}{"期望%":>8}{"夏普":>6}{"P/D":>7}{"最大亏%":>8}')
    print(f'  {"score>=8基线":<32}{n_s8:>6}{n_s8/58:>5.1f}{base_wr_s8:>6.1f}%{base_exp_s8:>+7.3f}%{base_exp_s8:>+7.3f}%{"--":>6}{"--":>7}{"--":>8}')
    print('-' * 110)

    s8_evals = []
    for name, fn in s8_tests:
        sub = [t for t in s8 if fn(t)]
        e = _eval_s8(sub, name)
        if e is None:
            continue
        s8_evals.append(e)
        print(f'  {name:<32}{e["n"]:>6}{e["per_day"]:>5.1f}{e["wr"]*100:>6.1f}%'
              f'{e["avg_r"]:>+7.3f}%{e["exp"]:>+7.3f}%{e["sh"]:>5.3f}{e["pd"]:>6.3f}{e["mx_loss"]:>+7.2f}%')

    # 按期望收益排名 (正期望, 每天0.5~5笔)
    print(f'\n--- score>=8 精炼排名 (每天0.5~5笔, 按期望收益) ---')
    s8_by_exp = sorted([e for e in s8_evals if e['exp'] > 0 and 0.5 <= e['per_day'] <= 10],
                       key=lambda x: x['exp'], reverse=True)
    for i, e in enumerate(s8_by_exp[:15]):
        print(f'  {i+1:>2}. {e["label"]:<32} 期望={e["exp"]:>+6.3f}%  胜率={e["wr"]*100:>5.1f}%  '
              f'每{e["per_day"]:>4.1f}笔/天  夏普={e["sh"]:>5.3f}  N={e["n"]:>4}')

    # 按夏普排名
    print(f'\n--- score>=8 精炼排名 (每天0.5~5笔, 按夏普) ---')
    s8_by_sh = sorted([e for e in s8_evals if e['exp'] > 0 and 0.5 <= e['per_day'] <= 10],
                      key=lambda x: x['sh'], reverse=True)
    for i, e in enumerate(s8_by_sh[:10]):
        print(f'  {i+1:>2}. {e["label"]:<32} 夏普={e["sh"]:>5.3f}  期望={e["exp"]:>+6.3f}%  '
              f'胜率={e["wr"]*100:>5.1f}%  每{e["per_day"]:>4.1f}笔/天  N={e["n"]:>4}')

    # ========== 入口过滤回测 ==========
    print(f'\n========== 入口过滤回测 (score2-6 + 各过滤条件) ==========')

    # score2-6 + day_gain<=-5 (SMART)
    s_s26_dg5 = _calc_curve(daily_s26_dg5, dates_sorted)
    _print_curve_stats(s_s26_dg5, 'score2-6 + day_gain<=-5 (SMART)')

    # score2-6 + day_gain<=-5 (S6)
    s_s26_dg5_s6 = _calc_curve(daily_s26_dg5_s6, dates_sorted)
    _print_curve_stats(s_s26_dg5_s6, 'score2-6 + day_gain<=-5 (S6)')

    # score2-6 + tail_ret<=-1 (SMART)
    s_s26_tail1 = _calc_curve(daily_s26_tail1, dates_sorted)
    _print_curve_stats(s_s26_tail1, 'score2-6 + tail_ret<=-1 (SMART)')

    # score2-6 + pos_range<=0.4 (SMART)
    s_s26_pos4 = _calc_curve(daily_s26_pos4, dates_sorted)
    _print_curve_stats(s_s26_pos4, 'score2-6 + pos_range<=0.4 (SMART)')

    # score2-6 + amp>=5 + tail<=-0.3 (SMART)
    s_s26_at = _calc_curve(daily_s26_at, dates_sorted)
    _print_curve_stats(s_s26_at, 'score2-6 + amp>=5+tail<=-0.3 (SMART)')

    # 评分2~6 + S6 逐日明细 (输出到文件供绘图)
    s26_s6_daily = []
    for d in dates_sorted:
        rets = daily_score26_s6.get(d, [])
        if rets:
            s26_s6_daily.append({'date': d, 'n': len(rets), 'avg_ret': round(sum(rets)/len(rets), 4)})

    # 保存回测曲线到JSON
    bt_output = {
        'smart': {'total_ret': round(s_smart['total_ret']*100, 2),
                   'ann_ret': round(s_smart['ann_ret']*100, 2),
                   'max_dd': round(s_smart['max_dd']*100, 2),
                   'ann_sharpe': round(s_smart['ann_sharpe'], 3),
                   'n_days': s_smart['n_days'],
                   'curve': [round((v-1)*100, 4) for v in s_smart['curve']]},
        'smart2': {'total_ret': round(s_smart2['total_ret']*100, 2),
                    'ann_ret': round(s_smart2['ann_ret']*100, 2),
                    'max_dd': round(s_smart2['max_dd']*100, 2),
                    'ann_sharpe': round(s_smart2['ann_sharpe'], 3),
                    'n_days': s_smart2['n_days'],
                    'curve': [round((v-1)*100, 4) for v in s_smart2['curve']]},
        'smart2_s26': {'total_ret': round(s_smart2_s26['total_ret']*100, 2),
                        'ann_ret': round(s_smart2_s26['ann_ret']*100, 2),
                        'max_dd': round(s_smart2_s26['max_dd']*100, 2),
                        'ann_sharpe': round(s_smart2_s26['ann_sharpe'], 3),
                        'n_days': s_smart2_s26['n_days'],
                        'curve': [round((v-1)*100, 4) for v in s_smart2_s26['curve']]},
        'raw_open': {'total_ret': round(s_raw['total_ret']*100, 2),
                      'ann_ret': round(s_raw['ann_ret']*100, 2),
                      'max_dd': round(s_raw['max_dd']*100, 2),
                      'ann_sharpe': round(s_raw['ann_sharpe'], 3)},
        'score26': {'total_ret': round(s_s26['total_ret']*100, 2),
                     'ann_ret': round(s_s26['ann_ret']*100, 2),
                     'max_dd': round(s_s26['max_dd']*100, 2),
                     'ann_sharpe': round(s_s26['ann_sharpe'], 3)},
        'score26_s6': {'total_ret': round(s_s26_s6['total_ret']*100, 2),
                        'ann_ret': round(s_s26_s6['ann_ret']*100, 2),
                        'max_dd': round(s_s26_s6['max_dd']*100, 2),
                        'ann_sharpe': round(s_s26_s6['ann_sharpe'], 3),
                        'n_days': s_s26_s6['n_days'],
                        'curve': [round((v-1)*100, 4) for v in s_s26_s6['curve']]},
        'a_rule': {'total_ret': round(s_a['total_ret']*100, 2),
                    'ann_ret': round(s_a['ann_ret']*100, 2),
                    'max_dd': round(s_a['max_dd']*100, 2),
                    'ann_sharpe': round(s_a['ann_sharpe'], 3),
                    'n_days': s_a['n_days']},
        'a_rule_s6': {'total_ret': round(s_a_s6['total_ret']*100, 2),
                       'ann_ret': round(s_a_s6['ann_ret']*100, 2),
                       'max_dd': round(s_a_s6['max_dd']*100, 2),
                       'ann_sharpe': round(s_a_s6['ann_sharpe'], 3),
                       'n_days': s_a_s6['n_days'],
                       'curve': [round((v-1)*100, 4) for v in s_a_s6['curve']]},
        's26_dg5': {'total_ret': round(s_s26_dg5['total_ret']*100, 2),
                     'ann_ret': round(s_s26_dg5['ann_ret']*100, 2),
                     'max_dd': round(s_s26_dg5['max_dd']*100, 2),
                     'ann_sharpe': round(s_s26_dg5['ann_sharpe'], 3),
                     'n_days': s_s26_dg5['n_days'],
                     'curve': [round((v-1)*100, 4) for v in s_s26_dg5['curve']]},
        's26_dg5_s6': {'total_ret': round(s_s26_dg5_s6['total_ret']*100, 2),
                        'ann_ret': round(s_s26_dg5_s6['ann_ret']*100, 2),
                        'max_dd': round(s_s26_dg5_s6['max_dd']*100, 2),
                        'ann_sharpe': round(s_s26_dg5_s6['ann_sharpe'], 3),
                        'n_days': s_s26_dg5_s6['n_days'],
                        'curve': [round((v-1)*100, 4) for v in s_s26_dg5_s6['curve']]},
        's26_tail1': {'total_ret': round(s_s26_tail1['total_ret']*100, 2),
                       'ann_ret': round(s_s26_tail1['ann_ret']*100, 2),
                       'max_dd': round(s_s26_tail1['max_dd']*100, 2),
                       'ann_sharpe': round(s_s26_tail1['ann_sharpe'], 3),
                       'n_days': s_s26_tail1['n_days'],
                       'curve': [round((v-1)*100, 4) for v in s_s26_tail1['curve']]},
        's26_pos4': {'total_ret': round(s_s26_pos4['total_ret']*100, 2),
                      'ann_ret': round(s_s26_pos4['ann_ret']*100, 2),
                      'max_dd': round(s_s26_pos4['max_dd']*100, 2),
                      'ann_sharpe': round(s_s26_pos4['ann_sharpe'], 3),
                      'n_days': s_s26_pos4['n_days'],
                      'curve': [round((v-1)*100, 4) for v in s_s26_pos4['curve']]},
        's26_at': {'total_ret': round(s_s26_at['total_ret']*100, 2),
                    'ann_ret': round(s_s26_at['ann_ret']*100, 2),
                    'max_dd': round(s_s26_at['max_dd']*100, 2),
                    'ann_sharpe': round(s_s26_at['ann_sharpe'], 3),
                    'n_days': s_s26_at['n_days'],
                    'curve': [round((v-1)*100, 4) for v in s_s26_at['curve']]},
        'dates': dates_sorted,
        'score26_s6_daily': s26_s6_daily,
    }

    # ================= 保存中间结果 =================
    import os
    tmp_dir = r'D:\QuantDinger\tmp'
    os.makedirs(tmp_dir, exist_ok=True)

    # 保存规则统计
    rules_output = []
    for s in results_all:
        rules_output.append({
            'name': s['name'], 'n': s['n'], 'cover': round(s['cover'], 4),
            'p_touch': round(s['p_touch'], 2), 'touch_lift': round(s['p_touch'] / base_touch, 3) if base_touch > 0 else 0,
            'win_rate': round(s['win_rate'], 2), 'pl_ratio': round(s['pl_ratio'], 3),
            'avg_ret': round(s['avg_ret'], 4), 'expectancy': round(s['expectancy'], 4),
            'sharpe': round(s['sharpe'], 4), 'max_loss': round(s['max_loss'], 2),
            'avg_win': round(s['avg_win'], 3), 'avg_loss': round(s['avg_loss'], 3),
        })
    with open(os.path.join(tmp_dir, 'rule_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(rules_output, f, ensure_ascii=False, indent=2)
    print(f'\n规则统计已保存: {tmp_dir}\\rule_stats.json')

    # 保存高分样本
    if high_score:
        high_score_out = [{k: v for k, v in t.items()} for t in high_score]
        with open(os.path.join(tmp_dir, 'high_score_samples.json'), 'w', encoding='utf-8') as f:
            json.dump(high_score_out, f, ensure_ascii=False, indent=2)
        print(f'高分样本已保存: {tmp_dir}\\high_score_samples.json')

    # 保存评分分布
    score_dist = []
    for (lo, hi), tag in zip(score_bins, score_labels):
        sub = [t for t in R if lo <= t['score'] < hi]
        if not sub:
            continue
        n = len(sub)
        wins = [t for t in sub if t['ret_open'] > 0]
        losses = [t for t in sub if t['ret_open'] <= 0]
        avg_w = sum(t['ret_open'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['ret_open'] for t in losses) / len(losses) if losses else 0
        score_dist.append({
            'score_range': tag, 'n': n,
            'p_touch': round(sum(1 for t in sub if t['next_touch']) / n * 100, 2),
            'win_rate': round(len(wins) / n * 100, 2),
            'pl_ratio': round(abs(avg_w / avg_l), 3) if avg_l != 0 else None,
            'avg_ret': round(sum(t['ret_open'] for t in sub) / n, 4),
            'expectancy': round((len(wins) / n * avg_w) + (len(losses) / n * avg_l), 4),
        })
    with open(os.path.join(tmp_dir, 'score_distribution.json'), 'w', encoding='utf-8') as f:
        json.dump(score_dist, f, ensure_ascii=False, indent=2)
    print(f'评分分布已保存: {tmp_dir}\\score_distribution.json')

    # 保存回测结果
    with open(os.path.join(tmp_dir, 'backtest.json'), 'w', encoding='utf-8') as f:
        json.dump(bt_output, f, ensure_ascii=False)
    print(f'回测曲线已保存: {tmp_dir}\\backtest.json')

    # ================================================================
    # V2 规则体系: 从"概率vs回撤"出发系统性建立基础规则
    # ================================================================
    print('\n' + '=' * 80)
    print('========== V2 规则体系: 系统性基础规则分析 ==========')
    print('=' * 80)

    # V2 目标: 用 P/D ratio (胜率/最大亏损) 做粗过滤, 再逐条验证
    # 每个因子阈值评估: N, 触板率, P(>=3%), P(>=5%), 胜率, 均收, 最大亏, P/D
    R = all_rows
    base_touch = sum(1 for t in R if t['next_touch']) / len(R) * 100

    print(f'\n--- 基线 (全样本 {len(R)} 笔) ---')
    bt = sum(1 for t in R if t['next_touch']) / len(R)
    b3 = sum(1 for t in R if t['next_max_gain'] >= 3) / len(R)
    b5 = sum(1 for t in R if t['next_max_gain'] >= 5) / len(R)
    bwr = sum(1 for t in R if t['ret_open'] > 0) / len(R)
    bavg = sum(t['ret_open'] for t in R) / len(R)
    bmax_loss = min(t['ret_open'] for t in R)
    print(f'  P(触板)={bt*100:.2f}%  P(>=3%)={b3*100:.2f}%  P(>=5%)={b5*100:.2f}%  '
          f'胜率={bwr*100:.2f}%  均收={bavg:+.3f}%  最大亏={bmax_loss:+.2f}%')

    def _eval_rule(sub, label=''):
        """评估一个子集的风险收益指标"""
        if not sub or len(sub) < 30:
            return None
        n = len(sub)
        p_t = sum(1 for t in sub if t['next_touch']) / n
        p_3 = sum(1 for t in sub if t['next_max_gain'] >= 3) / n
        p_5 = sum(1 for t in sub if t['next_max_gain'] >= 5) / n
        wins = [t for t in sub if t['ret_open'] > 0]
        losses = [t for t in sub if t['ret_open'] <= 0]
        wr = len(wins) / n
        avg_r = sum(t['ret_open'] for t in sub) / n
        avg_w = sum(t['ret_open'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['ret_open'] for t in losses) / len(losses) if losses else 0
        mx_loss = min(t['ret_open'] for t in sub)
        # P/D = 胜率 / |最大亏损| (越大越好)
        pd = wr / abs(mx_loss) if mx_loss < 0 else float('inf')
        # 期望收益
        exp = (len(wins) / n * avg_w) + (len(losses) / n * avg_l)
        # 夏普
        rets = [t['ret_open'] for t in sub]
        mean_r = sum(rets) / n
        var_r = sum((r - mean_r) ** 2 for r in rets) / n
        std_r = var_r ** 0.5
        sh = mean_r / std_r if std_r > 0 else 0
        return {
            'n': n, 'p_t': p_t, 'p_3': p_3, 'p_5': p_5,
            'wr': wr, 'avg_r': avg_r, 'avg_w': avg_w, 'avg_l': avg_l,
            'mx_loss': mx_loss, 'pd': pd, 'exp': exp, 'sh': sh, 'label': label
        }

    # ================= Phase 1: 单因子阈值扫描 =================
    print('\n--- Phase 1: 单因子阈值扫描 (P/D = 胜率/|最大亏|) ---')

    factor_tests = [
        # (因子名, lambda, [(阈值名, 判断函数), ...])
        ('dist_limit', [
            ('0~1', lambda t: 0 < t['dist_limit'] <= 1),
            ('0~2', lambda t: 0 < t['dist_limit'] <= 2),
            ('0~3.5', lambda t: 0 < t['dist_limit'] <= 3.5),
            ('0~5', lambda t: 0 < t['dist_limit'] <= 5),
            ('0~8', lambda t: 0 < t['dist_limit'] <= 8),
            ('>8', lambda t: t['dist_limit'] > 8),
        ]),
        ('touched_today', [
            ('True', lambda t: t['touched_today']),
        ]),
        ('lu_recent', [
            ('>=1', lambda t: t['lu_recent'] >= 1),
            ('>=2', lambda t: t['lu_recent'] >= 2),
            ('>=3', lambda t: t['lu_recent'] >= 3),
            ('=0', lambda t: t['lu_recent'] == 0),
        ]),
        ('vol_ratio', [
            ('>=1.5', lambda t: t['vol_ratio'] >= 1.5),
            ('>=2', lambda t: t['vol_ratio'] >= 2),
            ('>=2.5', lambda t: t['vol_ratio'] >= 2.5),
            ('>=4', lambda t: t['vol_ratio'] >= 4),
            ('<1', lambda t: t['vol_ratio'] < 1),
        ]),
        ('tail_ret', [
            ('<=-2', lambda t: t['tail_ret'] <= -2),
            ('<=-1', lambda t: t['tail_ret'] <= -1),
            ('<=-0.3', lambda t: t['tail_ret'] <= -0.3),
            ('>0.3', lambda t: t['tail_ret'] > 0.3),
        ]),
        ('pos_range', [
            ('0~0.2', lambda t: t['pos_range'] <= 0.2),
            ('0~0.4', lambda t: t['pos_range'] <= 0.4),
            ('0~0.6', lambda t: t['pos_range'] <= 0.6),
            ('>0.6', lambda t: t['pos_range'] > 0.6),
        ]),
        ('day_gain', [
            ('<=-5', lambda t: t['day_gain'] <= -5),
            ('<=-2', lambda t: t['day_gain'] <= -2),
            ('-2~0', lambda t: -2 < t['day_gain'] <= 0),
            ('0~5', lambda t: 0 < t['day_gain'] <= 5),
            ('5~9.5', lambda t: 5 <= t['day_gain'] < 9.5),
            ('>=9.5', lambda t: t['day_gain'] >= 9.5),
        ]),
        ('pre5_gain', [
            ('<=-10', lambda t: t['pre5_gain'] <= -10),
            ('<=-5', lambda t: t['pre5_gain'] <= -5),
            ('-10~0', lambda t: -10 < t['pre5_gain'] <= 0),
            ('0~10', lambda t: 0 < t['pre5_gain'] <= 10),
            ('10~20', lambda t: 10 < t['pre5_gain'] <= 20),
            ('>=20', lambda t: t['pre5_gain'] >= 20),
        ]),
        ('amplitude', [
            ('>=5', lambda t: t['amplitude'] >= 5),
            ('>=8', lambda t: t['amplitude'] >= 8),
            ('>=12', lambda t: t['amplitude'] >= 12),
            ('<5', lambda t: t['amplitude'] < 5),
        ]),
        ('breadth', [
            ('<0.3', lambda t: t['breadth'] < 0.3),
            ('0.3~0.5', lambda t: 0.3 <= t['breadth'] < 0.5),
            ('>=0.5', lambda t: t['breadth'] >= 0.5),
        ]),
        ('vw_frac', [
            ('<0.3', lambda t: t['vw_frac'] < 0.3),
            ('0.3~0.5', lambda t: 0.3 <= t['vw_frac'] < 0.5),
            ('0.5~0.8', lambda t: 0.5 <= t['vw_frac'] < 0.8),
            ('>=0.8', lambda t: t['vw_frac'] >= 0.8),
        ]),
    ]
    # vwap_dist (仅新数据)
    if R[0].get('vwap_dist') is not None:
        factor_tests.append(('vwap_dist', [
            ('<=-3', lambda t: t['vwap_dist'] <= -3),
            ('-3~-1', lambda t: -3 < t['vwap_dist'] <= -1),
            ('-1~0', lambda t: -1 < t['vwap_dist'] <= 0),
            ('0~1', lambda t: 0 < t['vwap_dist'] <= 1),
            ('1~3', lambda t: 1 < t['vwap_dist'] <= 3),
            ('>3', lambda t: t['vwap_dist'] > 3),
        ]))

    print(f'\n{"因子":<15}{"阈值":<12}{"N":>7}{"触板%":>7}{"P(>=3%)":>8}{"P(>=5%)":>8}'
          f'{"胜率%":>7}{"均收%":>8}{"最大亏%":>8}{"P/D":>7}{"期望%":>8}{"夏普":>7}')
    print('-' * 120)

    all_evals = []  # 收集所有评估结果
    for fname, tests in factor_tests:
        for tname, fn in tests:
            sub = [t for t in R if fn(t)]
            e = _eval_rule(sub, f'{fname}={tname}')
            if e is None:
                continue
            e['factor'] = fname
            e['threshold'] = tname
            all_evals.append(e)
            print(f'  {fname:<13}{tname:<12}{e["n"]:>7}{e["p_t"]*100:>6.2f}%'
                  f'{e["p_3"]*100:>7.2f}%{e["p_5"]*100:>7.2f}%'
                  f'{e["wr"]*100:>6.2f}%{e["avg_r"]:>+7.3f}%{e["mx_loss"]:>+7.2f}%'
                  f'{e["pd"]:>7.3f}{e["exp"]:>+7.3f}%{e["sh"]:>6.3f}')

    # ================= Phase 2: 按 P/D 排序找基础规则 =================
    print('\n--- Phase 2: 按 P/D 排序 (胜率/|最大亏|, N>=30) ---')
    by_pd = sorted([e for e in all_evals if e['n'] >= 30],
                   key=lambda x: x['pd'], reverse=True)
    print(f'{"排名":>4}{"因子":<15}{"阈值":<12}{"N":>7}{"P/D":>7}{"胜率%":>7}{"均收%":>8}{"期望%":>8}{"夏普":>7}{"触板%":>7}')
    print('-' * 100)
    for i, e in enumerate(by_pd[:30]):
        print(f'  {i+1:>2}. {e["factor"]:<13}{e["threshold"]:<12}{e["n"]:>7}{e["pd"]:>7.3f}'
              f'{e["wr"]*100:>6.2f}%{e["avg_r"]:>+7.3f}%{e["exp"]:>+7.3f}%{e["sh"]:>6.3f}{e["p_t"]*100:>6.2f}%')

    # ================= Phase 3: 按期望收益排序 =================
    print('\n--- Phase 3: 按期望收益排序 (N>=30, 正期望) ---')
    by_exp = sorted([e for e in all_evals if e['n'] >= 30 and e['exp'] > 0],
                    key=lambda x: x['exp'], reverse=True)
    print(f'{"排名":>4}{"因子":<15}{"阈值":<12}{"N":>7}{"期望%":>8}{"胜率%":>7}{"P/D":>7}{"夏普":>7}')
    print('-' * 80)
    for i, e in enumerate(by_exp[:20]):
        print(f'  {i+1:>2}. {e["factor"]:<13}{e["threshold"]:<12}{e["n"]:>7}'
              f'{e["exp"]:>+7.3f}%{e["wr"]*100:>6.2f}%{e["pd"]:>7.3f}{e["sh"]:>6.3f}')

    # ================= Phase 4: 基础规则组合验证 =================
    print('\n--- Phase 4: 基础规则组合 (V2候选) ---')
    # 基于Phase2/3的top因子, 构建组合规则
    # 核心发现: 胜率最高的因子 = 回调型(day_gain<=-5, tail_ret<=-1, pre5<=-10)
    #           概率最高的因子 = 近板型(dist_limit small, touched_today)
    #           两者结合 = 既有高概率又有好胜率

    v2_rules = [
        # --- 基础规则: 单因子 (验证P/D) ---
        ('V2-R01 大跌<=-5', lambda t: t['day_gain'] <= -5),
        ('V2-R02 尾盘回落<=-1', lambda t: t['tail_ret'] <= -1),
        ('V2-R03 近5日跌>=10', lambda t: t['pre5_gain'] <= -10),
        ('V2-R04 近板<=3.5', lambda t: 0 < t['dist_limit'] <= 3.5),
        ('V2-R05 炸板', lambda t: t['touched_today']),
        ('V2-R06 人气>=1', lambda t: t['lu_recent'] >= 1),
        ('V2-R07 低位<=0.4', lambda t: t['pos_range'] <= 0.4),
        ('V2-R08 振幅>=8', lambda t: t['amplitude'] >= 8),
        ('V2-R09 放量>=2', lambda t: t['vol_ratio'] >= 2),

        # --- 双因子组合: 回调+结构 ---
        ('V2-C01 大跌+低位', lambda t: t['day_gain'] <= -5 and t['pos_range'] <= 0.4),
        ('V2-C02 大跌+尾盘回落', lambda t: t['day_gain'] <= -5 and t['tail_ret'] <= -0.3),
        ('V2-C03 大跌+振幅>=8', lambda t: t['day_gain'] <= -5 and t['amplitude'] >= 8),
        ('V2-C04 大跌+人气', lambda t: t['day_gain'] <= -5 and t['lu_recent'] >= 1),
        ('V2-C05 尾盘回落+低位', lambda t: t['tail_ret'] <= -1 and t['pos_range'] <= 0.4),
        ('V2-C06 尾盘回落+振幅>=8', lambda t: t['tail_ret'] <= -1 and t['amplitude'] >= 8),
        ('V2-C07 近5日跌+大跌', lambda t: t['pre5_gain'] <= -10 and t['day_gain'] <= -5),
        ('V2-C08 近5日跌+低位', lambda t: t['pre5_gain'] <= -10 and t['pos_range'] <= 0.4),
        ('V2-C09 近板+炸板', lambda t: 0 < t['dist_limit'] <= 3.5 and t['touched_today']),
        ('V2-C10 近板+人气', lambda t: 0 < t['dist_limit'] <= 3.5 and t['lu_recent'] >= 1),
        ('V2-C11 近板+低位', lambda t: 0 < t['dist_limit'] <= 3.5 and t['pos_range'] <= 0.4),
        ('V2-C12 炸板+人气', lambda t: t['touched_today'] and t['lu_recent'] >= 1),
        ('V2-C13 振幅+尾盘回落', lambda t: t['amplitude'] >= 5 and t['tail_ret'] <= -0.3),
        ('V2-C14 放量+低位', lambda t: t['vol_ratio'] >= 1.5 and t['pos_range'] <= 0.4),

        # --- 三因子组合 ---
        ('V2-T01 大跌+低位+振幅>=8', lambda t: t['day_gain'] <= -5 and t['pos_range'] <= 0.4 and t['amplitude'] >= 8),
        ('V2-T02 大跌+低位+尾盘回落', lambda t: t['day_gain'] <= -5 and t['pos_range'] <= 0.4 and t['tail_ret'] <= -0.3),
        ('V2-T03 大跌+低位+人气', lambda t: t['day_gain'] <= -5 and t['pos_range'] <= 0.4 and t['lu_recent'] >= 1),
        ('V2-T04 大跌+振幅+尾盘回落', lambda t: t['day_gain'] <= -5 and t['amplitude'] >= 5 and t['tail_ret'] <= -0.3),
        ('V2-T05 近5日跌+大跌+低位', lambda t: t['pre5_gain'] <= -10 and t['day_gain'] <= -5 and t['pos_range'] <= 0.4),
        ('V2-T06 近5日跌+大跌+振幅>=8', lambda t: t['pre5_gain'] <= -10 and t['day_gain'] <= -5 and t['amplitude'] >= 8),
        ('V2-T07 近板+炸板+人气', lambda t: 0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['lu_recent'] >= 1),
        ('V2-T08 近板+炸板+低位', lambda t: 0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['pos_range'] <= 0.4),
        ('V2-T09 振幅+尾盘回落+低位', lambda t: t['amplitude'] >= 5 and t['tail_ret'] <= -0.3 and t['pos_range'] <= 0.4),
        ('V2-T10 大跌+人气+放量', lambda t: t['day_gain'] <= -5 and t['lu_recent'] >= 1 and t['vol_ratio'] >= 1.5),

        # --- vwap_dist组合 (仅新数据) ---
        ('V2-V01 vwap<=-3+大跌', lambda t: t.get('vwap_dist') is not None and t['vwap_dist'] <= -3 and t['day_gain'] <= -5),
        ('V2-V02 vwap<=-3+低位', lambda t: t.get('vwap_dist') is not None and t['vwap_dist'] <= -3 and t['pos_range'] <= 0.4),
        ('V2-V03 vwap<=-1+大跌+低位', lambda t: t.get('vwap_dist') is not None and t['vwap_dist'] <= -1 and t['day_gain'] <= -5 and t['pos_range'] <= 0.4),
    ]

    print(f'\n{"规则":<36}{"N":>7}{"P/D":>7}{"触板%":>7}{"胜率%":>7}{"均收%":>8}{"期望%":>8}{"夏普":>7}{"最大亏%":>8}')
    print('-' * 110)
    v2_results = []
    for name, fn in v2_rules:
        sub = [t for t in R if fn(t)]
        e = _eval_rule(sub, name)
        if e is None:
            continue
        v2_results.append(e)
        print(f'  {name:<34}{e["n"]:>7}{e["pd"]:>6.3f}{e["p_t"]*100:>6.2f}%'
              f'{e["wr"]*100:>6.2f}%{e["avg_r"]:>+7.3f}%{e["exp"]:>+7.3f}%'
              f'{e["sh"]:>6.3f}{e["mx_loss"]:>+7.2f}%')

    # V2 规则排名 (按P/D)
    print('\n--- V2 规则排名: 按 P/D (N>=30) ---')
    v2_by_pd = sorted([e for e in v2_results if e['n'] >= 30],
                      key=lambda x: x['pd'], reverse=True)
    for i, e in enumerate(v2_by_pd[:15]):
        print(f'  {i+1:>2}. {e["label"]:<34} P/D={e["pd"]:>6.3f}  胜率={e["wr"]*100:>5.1f}%  '
              f'均收={e["avg_r"]:>+6.3f}%  N={e["n"]:>5}  夏普={e["sh"]:>5.3f}')

    # V2 规则排名 (按期望收益)
    print('\n--- V2 规则排名: 按期望收益 (N>=30, 正期望) ---')
    v2_by_exp = sorted([e for e in v2_results if e['n'] >= 30 and e['exp'] > 0],
                       key=lambda x: x['exp'], reverse=True)
    for i, e in enumerate(v2_by_exp[:15]):
        print(f'  {i+1:>2}. {e["label"]:<34} 期望={e["exp"]:>+6.3f}%  胜率={e["wr"]*100:>5.1f}%  '
              f'P/D={e["pd"]:>5.3f}  N={e["n"]:>5}  夏普={e["sh"]:>5.3f}')

    # ================= Phase 5: V2 最优规则回测 =================
    print('\n--- Phase 5: V2 最优规则回测 ---')
    if v2_by_exp:
        top_rule = v2_by_exp[0]
        print(f'  冠军规则: {top_rule["label"]}')
        # 按日期分组回测
        daily_v2 = defaultdict(list)
        # 找到对应的lambda
        matched = False
        for name, fn in v2_rules:
            if name == top_rule['label']:
                matched = True
                for t in R:
                    if fn(t):
                        daily_v2[t['date']].append(t['ret_open'])
                break
        print(f'  匹配: {matched}, 交易日: {len(daily_v2)}, 总笔数: {sum(len(v) for v in daily_v2.values())}')
        if daily_v2:
            s_v2 = _calc_curve(daily_v2, dates_sorted)
            _print_curve_stats(s_v2, f'V2冠军: {top_rule["label"]}')

    # V2前3规则的组合回测
    if len(v2_by_exp) >= 3:
        print(f'\n  V2前3规则组合回测 (任一命中即入场):')
        top3_fns = []
        for name, fn in v2_rules:
            for e in v2_by_exp[:3]:
                if name == e['label']:
                    top3_fns.append((name, fn))
                    break
        daily_v2_top3 = defaultdict(list)
        for t in R:
            for name, fn in top3_fns:
                if fn(t):
                    daily_v2_top3[t['date']].append(t['ret_open'])
                    break
        if daily_v2_top3:
            s_v2_top3 = _calc_curve(daily_v2_top3, dates_sorted)
            _print_curve_stats(s_v2_top3, 'V2 Top3组合')

    # V2 P/D Top规则回测 (近板型, 分散度好)
    print(f'\n  V2 P/D Top规则回测 (分散度验证):')
    v2_pd_rules = [
        ('V2-T07 近板+炸板+人气', lambda t: 0 < t['dist_limit'] <= 3.5 and t['touched_today'] and t['lu_recent'] >= 1),
        ('V2-C09 近板+炸板', lambda t: 0 < t['dist_limit'] <= 3.5 and t['touched_today']),
        ('V2-C02 大跌+尾盘回落', lambda t: t['day_gain'] <= -5 and t['tail_ret'] <= -0.3),
        ('V2-C05 尾盘回落+低位', lambda t: t['tail_ret'] <= -1 and t['pos_range'] <= 0.4),
        ('V2-C07 近5日跌+大跌', lambda t: t['pre5_gain'] <= -10 and t['day_gain'] <= -5),
    ]
    for rname, rfn in v2_pd_rules:
        daily_r = defaultdict(list)
        n_trades = 0
        for t in R:
            if rfn(t):
                daily_r[t['date']].append(t['ret_open'])
                n_trades += 1
        if daily_r:
            s_r = _calc_curve(daily_r, dates_sorted)
            n_active = len(daily_r)
            avg_per_day = n_trades / n_active if n_active > 0 else 0
            print(f'\n    [{rname}] N={n_trades} 活跃日={n_active} 均{avg_per_day:.0f}笔/日')
            print(f'      累计={s_r["total_ret"]*100:>+.2f}%  年化={s_r["ann_ret"]*100:>+.2f}%  '
                  f'回撤={s_r["max_dd"]*100:.2f}%  夏普={s_r["ann_sharpe"]:>.3f}  '
                  f'日胜率={s_r["win_day_rate"]:.1f}%')

    print('\n========== V2 分析完成 ==========')


if __name__ == '__main__':
    main()
