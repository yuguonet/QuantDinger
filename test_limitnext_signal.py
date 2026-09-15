# -*- coding: utf-8 -*-
"""
V2 尾盘策略 信号扫描 (快照版) — 盘中 14:50 即可实时运行, 无需等待 15:30 的 1m 回填
================================================================================
策略规范: docs/V2_TAIL_BUY_STRATEGY.md (唯一规范) | 权威实现: test_limitnext.py (1m 回测口径)
本工具定位: 盘中实时短名单近似 (快照数据源), 边界信号可能与回测口径有出入 (见文末差异说明)

【使用方法】
  盘中实时 (交易日 14:50~14:56 运行, 信号日=今天):
      python test_limitnext_signal.py
  复盘指定日期 (快照表仅保留最近 5 天, 只能查近几天):
      python test_limitnext_signal.py --date 2026-09-03
  输出: 控制台精掐候选 + "score>=8 但未全过"备查名单 + _v2_signal_<日期>.json
  依赖: realtime_snapshot_2026 (快照表), kline_1D (前收盘/近5日),
        test_v2_tail_buy_2stage.fetch_daily_kline (候选股 pre5 复权重算)

【入场规则】 信号日 D 尾盘 14:56 买入 (快照 last 价), 以下条件全部满足:
  归一化 nf: 创业板/科创板 (30/68 开头) = 0.5, 主板 = 1.0; 下列阈值均作用于 x nf 后的值
  ① 评分 >= 8    calc_score 分级加权:
        day_gain  (当日跌幅, 14:56 last / 昨收 - 1):  <=-8 → +4 | <=-5 → +3 | <=-2 → +1.2 | <=0 → +0.5
        tail_ret  (尾盘动能, 见④):                    <=-2 → +3 | <=-1 → +2.5 | <=-0.3 → +1.5
        pos_range (14:56 last 在日内高低点的位置):     <=0.2 → +2 | <=0.4 → +1
        组合项:    振幅>=5 且 tail_ret<=-0.3 → +1
        pre5_gain (近5日):                             <=-10 → +0.3 | <=-5 → +0.1
  ② pre5_norm <= -10   近5日累计跌幅 (score>=8 的候选股用复权日线精确重算, 防除权失真)
  ③ amp_norm  >= 10    日内振幅 = (日高-日低)/昨收 (日高低取自清洗后的快照序列)
  ④ tail_norm ∈ [-2.8, -0.5]   尾盘动能 = last(14:55) / VW(last x 分钟量差, 14:40~14:55) - 1
  排除项: 14:56 已封板 (buy >= 涨停价 x 0.998) 不买; 快照高/低点经涨停边界清洗 (防垃圾 tick)

【出场规则】 持有 1 天, D+1 出场, SMART 二选一:
  A系 (D+1 强势): 盘中距涨停 <= 3.5% + 当日曾触板 + 近3日曾涨停 → 移动止盈 2% (自高点回撤 2% 卖出)
  其余          : D+1 开盘直接卖出
  注: 出场判定用 V1 因子族 (近板/炸板/人气), 与入场 V2 回调因子族不同 — 有意设计, 勿"统一"
      回测中 ret_open 字段已统一为 SMART 口径, 直接与 ret_open 对比即可

【快照口径 vs 1m 回测口径的已知差异】
  - 快照 last 为 14:56:12 时点成交价, 1m 回测用 14:56:00 bar 开盘价 → 时点差
  - 快照价格未复权 (候选股 pre5 已用复权日线重算弥补; tail/振幅为日内比率, 天然不受除权影响)
  - 因此边界信号 (如 tail 恰在 -0.5 附近) 两口径可能翻转 → 本工具结果定位为盘中短名单近似
  - 权威信号: 收盘后 1m 回填完成 (约 15:30+) 跑 test_limitnext.py, 结果与历史回测完全同源
"""
import sys, os, json, argparse
sys.path.insert(0, r'D:\quantdinger')
sys.path.insert(0, r'D:\quantdinger\backend_api_python')
from dotenv import load_dotenv
load_dotenv(r'D:\quantdinger\.env')
if not os.environ.get('DATABASE_URL'):
    load_dotenv(r'D:\quantdinger\backend_api_python\.env')

from collections import defaultdict
from test_v2_tail_buy_2stage import fetch_daily_kline
from backtest_realtime_monitor import _get_writer

writer = _get_writer()
pool = writer._mgr._get_pool('CNStock')


def calc_score(t, nf):
    score = 0.0
    dg = t['day_gain'] * nf
    if dg <= -8: score += 4.0
    elif dg <= -5: score += 3.0
    elif dg <= -2: score += 1.2
    elif dg <= 0: score += 0.5
    tr = t['tail_ret'] * nf
    if tr <= -2: score += 3.0
    elif tr <= -1: score += 2.5
    elif tr <= -0.3: score += 1.5
    pr = t['pos_range']
    if pr <= 0.2: score += 2.0
    elif pr <= 0.4: score += 1.0
    amp_n = t['amplitude'] * nf
    if amp_n >= 5 and tr <= -0.3: score += 1.0
    p5_n = t['pre5_gain'] * nf
    if p5_n <= -10: score += 0.3
    elif p5_n <= -5: score += 0.1
    return round(score, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default='')
    args = ap.parse_args()

    with pool.cursor() as cur:
        if args.date:
            target = args.date
        else:
            cur.execute('SELECT MAX("time")::date FROM realtime_snapshot_2026')
            target = str(cur.fetchone()[0])
        print('信号日期:', target)

        cur.execute("""
            SELECT symbol, "time", "last", open, high, low, "previousClose", volume
            FROM realtime_snapshot_2026
            WHERE "time"::date = %s AND "time"::time BETWEEN '09:30:00' AND '14:56:59'
            ORDER BY symbol, "time"
        """, (target,))
        rows = cur.fetchall()
    print('快照行数:', len(rows))

    by_sym = defaultdict(list)
    for sym, t, last, o, h, l, pc, vol in rows:
        by_sym[sym].append((str(t)[11:16], float(last or 0), float(o or 0),
                            float(h or 0), float(l or 0), float(pc or 0), float(vol or 0)))

    signals = []
    evaluated = 0
    pre5_needed = []
    for sym, pts in by_sym.items():
        if len(pts) < 200:
            continue
        prev_close = next((pc for (_, _, _, _, _, pc, _) in pts if pc > 0), None)
        if not prev_close or prev_close <= 0:
            continue
        board = 'gem_star' if sym.startswith(('30', '68')) else 'main'
        nf = 0.5 if board == 'gem_star' else 1.0
        limit_pct = 0.20 if board == 'gem_star' else 0.10
        limit_price = round(prev_close * (1 + limit_pct), 2)
        limit_dn = round(prev_close * (1 - limit_pct), 2)
        # 垃圾tick清洗: 快照存在异常 high/low, 会污染日内高低点 (600721 案例确诊)
        seq = [(hm, last, h, l, v) for (hm, last, o, h, l, pc, v) in pts
               if hm <= '14:55' and 0 < l <= h <= limit_price * 1.01
               and l >= limit_dn * 0.99 and 0 < last <= limit_price * 1.01]
        if len(seq) < 200:
            continue
        last55 = seq[-1][1]
        buy_price = next((last for (hm, last, *_ ) in pts
                          if hm >= '14:56' and 0 < last <= limit_price * 1.01), None)
        if not buy_price or buy_price <= 0:
            continue
        if buy_price >= limit_price * 0.998:
            continue

        day_gain = (buy_price / prev_close - 1) * 100
        day_high = max(h for (_, _, h, _, _) in seq)
        day_low = min(l for (_, _, _, l, _) in seq)
        amplitude = (day_high - day_low) / prev_close * 100
        pos = (buy_price - day_low) / (day_high - day_low) if day_high > day_low else 0.5

        tail_pv = tail_v = 0.0
        prev_v = None
        for hm, last, h, l, v in seq:
            dv = (v - prev_v) if prev_v is not None else 0.0
            prev_v = v
            if '14:40' <= hm <= '14:55':
                tail_pv += last * dv   # 快照 high/low 是当日累计值, 用 last×分钟量差
                tail_v += dv
        tail_avg = tail_pv / tail_v if tail_v > 0 else last55
        tail_ret = (last55 / tail_avg - 1) * 100

        t = {'day_gain': round(day_gain, 2), 'tail_ret': round(tail_ret, 3),
             'amplitude': round(amplitude, 2), 'pos_range': round(pos, 3), 'board': board,
             'pre5_gain': 0.0}
        score = calc_score(t, nf)

        signals.append({
            'code': sym, 'board': board, 'date': target,
            'buy_price': buy_price, 'day_gain': round(day_gain, 2),
            'tail_ret': round(tail_ret, 3), 'amplitude': round(amplitude, 2),
            'pos_range': round(pos, 3), 'score': score,
            'limit_price': limit_price,
        })
        evaluated += 1

    print('评估股票:', evaluated)

    codes = [s['code'] for s in signals]
    pre5_map = {}
    if codes:
        CH = 500
        with pool.cursor() as cur:
            for i in range(0, len(codes), CH):
                chunk = codes[i:i + CH]
                cur.execute("""
                    SELECT symbol, "time"::date AS d, close
                    FROM "kline_1D_2026"
                    WHERE symbol = ANY(%s) AND "time" >= '2026-07-01'
                    ORDER BY symbol, "time"
                """, (chunk,))
                for sym, d, cl in cur.fetchall():
                    pre5_map.setdefault(sym, []).append((str(d), float(cl)))
    import bisect
    for s in signals:
        series = pre5_map.get(s['code'], [])
        j = bisect.bisect_left([d for d, _ in series], s['date']) if series else 0
        if j >= 6:
            c_pre5 = series[j - 6][1]
            s['pre5_gain'] = round((s['buy_price'] / c_pre5 - 1) * 100, 2) if c_pre5 > 0 else 0.0
        else:
            s['pre5_gain'] = 0.0
        nf = 0.5 if s['board'] == 'gem_star' else 1.0
        t = {'day_gain': s['day_gain'], 'tail_ret': s['tail_ret'],
             'amplitude': s['amplitude'], 'pos_range': s['pos_range'],
             'board': s['board'], 'pre5_gain': s['pre5_gain']}
        s['score'] = calc_score(t, nf)

    # pre5 复权重算 (score>=8 短名单, 用复权日线 — raw 比例在除权股失真)
    shortlist = [s for s in signals if s['score'] >= 8]
    for s in shortlist:
        try:
            daily_bars = fetch_daily_kline(s['code'], 60)
            if len(daily_bars) < 7:
                continue
            idx = None
            for i, b in enumerate(daily_bars):
                if b['time'] == s['date']:
                    idx = i
                    break
            if idx is None or idx < 6:
                continue
            c_pre5 = float(daily_bars[idx - 5]['close'])
            if c_pre5 > 0:
                s['pre5_gain'] = round((s['buy_price'] / c_pre5 - 1) * 100, 2)
        except Exception:
            continue

    def precision_cut(s):
        nf = 0.5 if s['board'] == 'gem_star' else 1.0
        return (s['score'] >= 8
                and s['pre5_gain'] * nf <= -10
                and s['amplitude'] * nf >= 10
                and -2.8 <= s['tail_ret'] * nf <= -0.5)

    sel = [s for s in signals if precision_cut(s)]
    sel.sort(key=lambda s: -s['score'])

    print()
    print(f'========== V2 精掐候选 ({target}) ==========')
    print(f'评估 {evaluated} 只 → 精掐 {len(sel)} 只')
    print()
    for s in sel:
        print(f"  {s['code']:<8} [{s['board']:<4}] score={s['score']:>5.1f} "
              f"当日{s['day_gain']:>+6.2f}% 尾盘{s['tail_ret']:>+6.3f}% 振幅{s['amplitude']:>5.2f}% "
              f"近5日{s['pre5_gain']:>+7.2f}% pos={s['pos_range']:.2f} "
              f"14:56价 {s['buy_price']:>8.2f}")
    if not sel:
        print('  (无精掐候选)')

    near = [s for s in signals if s['score'] >= 8 and s not in sel]
    if near:
        print(f'\n-- score>=8 但未全过精掐 ({len(near)}只, 备查) --')
        for s in sorted(near, key=lambda x: -x['score'])[:10]:
            why = []
            nf = 0.5 if s['board'] == 'gem_star' else 1.0
            if s['pre5_gain'] * nf > -10: why.append('pre5未达')
            if s['amplitude'] * nf < 10: why.append('amp未达')
            if not (-2.8 <= s['tail_ret'] * nf <= -0.5): why.append('tail区间外')
            print(f"  {s['code']:<8} score={s['score']:>5.1f} 当日{s['day_gain']:>+6.2f}% "
                  f"尾盘{s['tail_ret']:>+6.3f}% 振幅{s['amplitude']:>5.2f}% "
                  f"近5日{s['pre5_gain']:>+7.2f}% 未过: {','.join(why)}")

    out = {'date': target, 'evaluated': evaluated, 'signals': sel, 'near_miss': near[:20]}
    fn = rf'D:\quantdinger\_v2_signal_{target}.json'
    json.dump(out, open(fn, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'\n已保存: {fn}')


if __name__ == '__main__':
    main()
