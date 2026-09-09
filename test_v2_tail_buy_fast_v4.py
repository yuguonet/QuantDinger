#!/usr/bin/env python3
"""
V2 尾盘买入策略 — 快速版 V4 (V2规则 + V3式两阶段提速, 等价性优先)

═══════════════════════════════════════════════════════════════════
  设计 (2026-09-10, 本文件覆盖了 09-09 的旧 v4 规则变体实验,
        旧实验与 v3 同思路把 down_streak/lu_recent 当规则, 已备份
        tmp/_test_v2_tail_buy_fast_v4_old_20260910.py)
═══════════════════════════════════════════════════════════════════

  目标: V3 的速度 (日线预筛 → 候选才拉1m) + V2 的规则 (逐笔等价)。

  ⚠ V3 的教训 (勿重蹈):
    - V3 预筛把 down_streak>=1 / lu_recent==0 当成规则 → 那是"加规则"不是"预筛",
      信号集被改变; 又有单候选日 fetch_1m(start==end) 返回空整股静默跳过的 bug。
    - V4 原则: 预筛只允许 "V2 规则的数学必要条件" (超集过滤, 只丢不可能出信号的
      股票-日期), 验证阶段直接 import 并调用 V2 原版 backtest_stock (零拷贝零漂移)
      → 回测路径上 V4 与 V2 的差异只可能来自预筛误杀, 可用逐笔对比一键验证。

  阶段1 日线预筛 (prescan_stock) — 必要条件的推导:
    V2 信号 = score>=8 + pre5*nf<=-10 + amp*nf>=10 + tail*nf∈[-2.8,-0.5] (14:56, 1m)
    其中硬过滤恒有 pre5 分=0.3、combo 分=1 (amp>=10 ⇒ amp>=5 且 tail<=-0.5 ⇒ tr<=-0.3),
    tail(3分上限)/pos(2分上限) 与 day_gain 无关。
    ⇒ score>=8 要求 day_gain 分 >= 8-3-2-1-0.3 = 1.7, 而 (-5,0] 区间 day_gain 分
      最高 1.2 → **day_gain*nf <= -5 是必要条件**。

    P1 (day_gain*nf <= -5):  buy_price >= 当日最低价
      → 必要: (day_low / prev_close - 1)*100*nf <= -5 (+slack)
    P2 (pre5_gain*nf <= -10): 同理
      → 必要: (day_low / pre5_close - 1)*100*nf <= -10 (+slack)
    P3 (amp*nf >= 10): 14:56前振幅 <= 全天振幅, amp=(high-low)/prev_close,
      prev_close 越小 amp 越大 → 取 lookback 内最小前收
      → 必要: (day_high - day_low) / prev_close_min * 100 * nf >= 10 (-slack)

    ⚠ 边界: V2 的 prev_close / pre5 分母取自 1m 日序列 (days[k-1]/days[k-5]),
      若前几日停牌或 1m 缺数据, 实际分母日会早于日线 D-1 / D-5。P1/P2/P3 的分母
      分别取 lookback 窗口内的极值 (P1/P3 前6日, P2 前10日) 保证仍是超集。
    ⚠ slack: PRESCREEN_SLACK_PCT=0.15pt, 吸收 daily/1m 两次独立查询的 qfq 微差。
      方向为放宽条件 (保留更多), 宁可多拉 1m 也不误杀。
    ⚠ P4: 次日须存在且间隔 <= MAX_DAY_GAP (V2 的 1m next-day gap 检查)。

  阶段2: 幸存股调用 V2 原版 backtest_stock (import 复用)。

  --today 快速扫描 (盘后 kline_1m 未回填时的盘中方案):
    数据源: realtime_snapshot_YYYY (60s 全市场快照, 保留5天, volume 为当日累计量)
    1. 分批 (800只/批) 一条 SQL 读全市场当日快照序列
    2. 快照口径 P1/P3 预筛 + 现价封板排除 (原始价, 同源自洽)
    3. 幸存股才拉 300 日线; 快照序列 prep_minutes(累计量差分) → 按 mi 密集化成
       240 槽位 bar (缺口以前收平价填充, v=0 → 不影响 VWAP/高低点) →
       unadj_to_qfq → 复用 V2 的 calc_day_features / calc_daily_features /
       check_v2_signal (与 test_v2_tail_buy.py --today 同一套判定)
    ⚠ 快照是逐分钟快照非精确1m K线, 缺分钟以前收填充; 判定口径与盘后回填的
      kline_1m 可能有分钟级微差, 以盘后 kline_1m 复权口径为准。
    ⚠ 需 >=236 分钟有数据 (14:56 后) calc_day_features 才有效, 与 V2 相同。

  使用:
    python test_v2_tail_buy_fast_v4.py --source db --days 30            # 快速回测
    python test_v2_tail_buy_fast_v4.py --source db --start-date 2026-08-05 --end-date 2026-09-08
    python test_v2_tail_buy_fast_v4.py --today --source db              # 盘中14:56扫描
    python test_v2_tail_buy_fast_v4.py --source db --no-prescreen       # 关预筛(=V2)
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional

# ================================================================
# 路径初始化 + 复用 V2 原函数 (零拷贝, 等价性由 import 保证)
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from test_v2_tail_buy import (  # noqa: E402
    _load_env, get_all_codes_basicinfo, get_stock_name_map,
    fetch_daily_kline, backtest_stock,
    calc_day_features, calc_daily_features, check_v2_signal,
    get_board_type, get_board_name, day_limit_pct, norm_factor,
    print_stats, print_detail,
    BUY_BAR, MIN_BARS, MAX_DAY_GAP,
)

# ================================================================
# 预筛参数
# ================================================================
PRESCREEN_SLACK_PCT = 0.15   # 容差(百分点, 归一化前): 吸收 daily/1m qfq 微差, 方向=放宽
P1_SPAN = 6                  # P1/P3 前收候选窗口: D-1 ~ D-6 (覆盖前几日停牌/缺1m)
P2_SPAN = 10                 # P2 pre5 分母候选窗口: D-5 ~ D-10


def prescan_stock(daily_bars: List[Dict], code: str,
                  start_date: str, end_date: str) -> bool:
    """日线预筛: 只用 V2 规则的数学必要条件 (超集过滤)。

    返回 True = 该股窗口内存在至少一个"可能出信号"的日期 (须进入阶段2拉1m)。
    返回 False = 数学上不可能出信号, 可安全跳过 (不拉1m)。
    """
    nf = norm_factor(get_board_type(code))
    n = len(daily_bars)
    for i in range(6, n - 1):          # i>=6: V2 的 calc_daily_features 要求 idx>=6
        d0 = daily_bars[i]['time']
        if d0 < start_date:
            continue
        if d0 > end_date:
            break                       # 日线按日期升序, 后面只会更晚
        day_low = float(daily_bars[i]['low'])
        day_high = float(daily_bars[i]['high'])
        if day_low <= 0 or day_high <= 0:
            continue

        # P1: score>=8 ⇒ day_gain*nf <= -5 (推导见文件头)
        prev_closes = [float(daily_bars[j]['close'])
                       for j in range(max(0, i - P1_SPAN + 1), i)]
        pc_max = max(prev_closes) if prev_closes else 0.0
        if pc_max <= 0:
            continue
        if (day_low / pc_max - 1) * 100 * nf > -5 + PRESCREEN_SLACK_PCT:
            continue

        # P2: pre5_gain*nf <= -10 (分母取 D-5 ~ D-10 内最大收盘, 覆盖停牌偏移;
        #     分母<=0 时 V2 的 pre5_gain=0 恒不过 → 直接跳过)
        pre5_closes = [float(daily_bars[j]['close'])
                       for j in range(max(0, i - P2_SPAN), i - 4)]
        pc5_max = max(pre5_closes) if pre5_closes else 0.0
        if pc5_max <= 0:
            continue
        if (day_low / pc5_max - 1) * 100 * nf > -10 + PRESCREEN_SLACK_PCT:
            continue

        # P3: amp*nf >= 10 (14:56前振幅<=全天振幅; prev_close 越小 amp 越大 → 取最小)
        pc_min = min(prev_closes) if prev_closes else 0.0
        if pc_min <= 0:
            continue
        if (day_high - day_low) / pc_min * 100 * nf < 10 - PRESCREEN_SLACK_PCT:
            continue

        # P4: 次日存在且间隔合法 (V2 的 1m next-day gap 检查)
        try:
            gap = (datetime.strptime(daily_bars[i + 1]['time'], '%Y-%m-%d')
                   - datetime.strptime(d0, '%Y-%m-%d')).days
        except (ValueError, TypeError):
            continue
        if gap < 1 or gap > MAX_DAY_GAP:
            continue

        return True
    return False


# ================================================================
# --today: realtime_snapshot 快照数据源
# ================================================================
def _snapshot_table(date_str: str) -> str:
    return f"realtime_snapshot_{date_str[:4]}"


def fetch_today_snapshot_series(codes: List[str], date_str: str) -> Dict[str, List[Dict]]:
    """批量读取当日快照序列 {code: [raw rows]} (复用 dragon_monitor 的查询模式, 分批)"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    out: Dict[str, List[Dict]] = {}
    CHUNK = 800
    for s in range(0, len(codes), CHUNK):
        chunk = codes[s:s + CHUNK]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT symbol, time, open, high, low, "last", "previousClose", volume '
                    f'FROM "{_snapshot_table(date_str)}" '
                    f'WHERE symbol = ANY(%s) AND time >= %s ORDER BY symbol, time',
                    (chunk, f"{date_str} 09:00:00"))
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    out.setdefault(r["symbol"], []).append(r)
    return out


def prescreen_snapshot(rows: List[Dict], code: str) -> bool:
    """当日快照口径预筛: P1(day_gain) + P3(amplitude) + 现价封板排除。

    快照的 previousClose/high/low/last 同源同口径 (原始价), 自洽无需复权处理;
    buy_price >= 当日最低, 14:56前振幅 <= 已实现振幅 → 仍是必要条件。
    """
    if not rows:
        return False
    nf = norm_factor(get_board_type(code))
    r = rows[-1]                        # 快照 high/low 是当日累计极值, 取最后一行即可
    prev_close = float(r.get("previousClose") or 0)
    day_low = float(r.get("low") or 0)
    day_high = float(r.get("high") or 0)
    last = float(r.get("last") or 0)
    if prev_close <= 0 or day_low <= 0 or day_high <= 0 or last <= 0:
        return False
    if (day_low / prev_close - 1) * 100 * nf > -5 + PRESCREEN_SLACK_PCT:
        return False
    if (day_high - day_low) / prev_close * 100 * nf < 10 - PRESCREEN_SLACK_PCT:
        return False
    limit_pct = day_limit_pct(get_board_type(code))
    if last >= round(prev_close * (1 + limit_pct), 2) * 0.998:
        return False
    return True


def _mi_to_hhmm(mi: int) -> str:
    """分钟序号 0~239 → 'HH:MM' (0↔09:31, 119↔11:30, 120↔13:01, 235↔14:56)"""
    minutes = 571 + mi if mi <= 119 else 661 + mi
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _densify_day(mins: List[Dict], date_str: str) -> List[Dict]:
    """prep_minutes 输出 (稀疏, 按 mi 升序) → 240 槽位密集 1m bar 列表。

    缺口以前收平价填充 (v=0): 不影响 VWAP (零量权重) / 日内高低点 (前收已出现) /
    vol_today; tail_ret 在缺分钟窗口会被轻微平滑 — 快照口径固有限度, 盘后以
    kline_1m 为准 (见文件头)。
    """
    by_mi = {b['mi']: b for b in mins}
    if not by_mi:
        return []
    first = by_mi[min(by_mi)]
    bars = []
    prev_c = 0.0
    for mi in range(240):
        b = by_mi.get(mi)
        if b is None:
            c = prev_c if prev_c > 0 else float(first['o'])
            bars.append({'time': f"{date_str} {_mi_to_hhmm(mi)}",
                         'open': c, 'high': c, 'low': c, 'close': c, 'volume': 0.0})
        else:
            bars.append({'time': f"{date_str} {_mi_to_hhmm(mi)}",
                         'open': float(b['o']), 'high': float(b['h']),
                         'low': float(b['l']), 'close': float(b['c']),
                         'volume': float(b['v'])})
            prev_c = float(b['c'])
    return bars


def check_today_signal_snapshot(code: str, rows: List[Dict],
                                daily_bars: List[Dict]) -> Optional[Dict]:
    """快照版今日信号判定 — 判定流程与 V2 check_today_signal 一致, 仅 1m 数据
    来源换成 realtime_snapshot 合成 bar。"""
    board_type = get_board_type(code)
    limit_pct = day_limit_pct(board_type)
    nf = norm_factor(board_type)

    if len(daily_bars) < 7:
        return None

    # 快照日期必须与日线最新日期一致 (否则数据不同步, 宁缺毋滥)
    snap_date = str(rows[-1]['time'])[:10]
    today_date = daily_bars[-1]['time']
    if snap_date != today_date:
        return None

    prev_close = float(daily_bars[-2]['close'])
    if prev_close <= 0:
        return None

    # 快照序列 → 密集 240 槽位 bar → 前复权 (与 fetch_1m 同处理)
    from app.market_cn.auto.intraday_core import prep_minutes   # 纯函数, 零IO
    mins = prep_minutes([{'time': str(r['time']), 'open': r.get('open') or 0,
                          'high': r.get('high') or 0, 'low': r.get('low') or 0,
                          'close': r.get('last') or 0, 'volume': r.get('volume') or 0}
                         for r in rows], volume_cumulative=True)
    bars = _densify_day(mins, today_date)
    if not bars:
        return None
    try:
        from app.data_sources.provider.adjustment import unadj_to_qfq
        bars = unadj_to_qfq(bars, code)
    except Exception:
        pass                                    # 复权失败退回原始价 (非除权日无差)

    # 盘中特征 (calc_day_features 内含 14:56 封板排除 / len>BUY_BAR 检查)
    feat = calc_day_features(bars, prev_close, limit_pct)
    if feat is None:
        return None

    daily_feat = calc_daily_features(daily_bars, today_date, board_type)
    if daily_feat is None:
        return None

    # pre5_gain 分子用 14:56 实价 (与 V2 --today / 回测口径一致)
    pre5_close = daily_feat.get('pre5_close', 0)
    if pre5_close > 0 and feat['buy_price'] > 0:
        daily_feat['pre5_gain'] = round((feat['buy_price'] / pre5_close - 1) * 100, 2)

    if not check_v2_signal(feat, daily_feat, board_type):
        return None

    return {
        'code': code,
        'board': get_board_name(code),
        'signal_date': today_date,
        'close': round(feat['buy_price'], 3),
        'entry_price': feat['buy_price'],
        'day_gain': feat['day_gain'],
        'tail_ret': feat['tail_ret'],
        'pos_range': feat['pos_range'],
        'amplitude': feat['amplitude'],
        'dist_limit': feat['dist_limit'],
        'pre5_gain': daily_feat['pre5_gain'],
        'down_streak': daily_feat['down_streak'],
        'lu_recent': daily_feat['lu_recent'],
        'touched': feat['touched'],
        'norm_day_gain': round(feat['day_gain'] * nf, 2),
        'norm_tail_ret': round(feat['tail_ret'] * nf, 3),
        'norm_amp': round(feat['amplitude'] * nf, 2),
        'norm_pre5': round(daily_feat['pre5_gain'] * nf, 2),
    }


def run_today_scan(codes: List[str]) -> List[Dict]:
    """--today 快速扫描: 全市场快照一次读入 → 快照预筛 → 幸存股拉日线判定"""
    t0 = time.time()
    print(f"  读取 realtime_snapshot 快照序列 ({len(codes)}只, 800只/批)...")
    series = fetch_today_snapshot_series(codes, datetime.now().strftime("%Y-%m-%d"))
    print(f"  快照覆盖 {len(series)} 只, 耗时 {time.time()-t0:.1f}s")

    survivors = [c for c in codes if prescreen_snapshot(series.get(c, []), c)]
    print(f"  快照预筛存活 {len(survivors)} 只 (P1+P3+封板), 拉日线验证...")

    hits = []
    for i, code in enumerate(survivors):
        daily_bars = fetch_daily_kline(code, 300)
        if not daily_bars or len(daily_bars) < 7:
            continue
        sig = check_today_signal_snapshot(code, series[code], daily_bars)
        if sig:
            hits.append(sig)
        if (i + 1) % 200 == 0:
            print(f"  已验证 {i + 1}/{len(survivors)} ...")
    print(f"  验证完成, 耗时 {time.time()-t0:.1f}s")
    return hits


# ================================================================
# 统计输出 (复用 V2 的 print_stats / print_detail)
# ================================================================
def print_top(trades: List[Dict], top: int):
    n_top = min(top, len(trades))
    print(f"\n  TOP{n_top} 盈利:")
    for t in sorted(trades, key=lambda x: -x['return_pct'])[:n_top]:
        print(f"    {t['code']:<8} {t['board']:<6} "
              f"{t['signal_date']}  入{t['entry_price']:>7.2f} → 出{t['exit_price']:>7.2f}  "
              f"收益{t['return_pct']:>+6.2f}%  day_gain={t['day_gain']:+.1f}%  tail={t['tail_ret']:+.2f}%")
    print(f"\n  TOP{n_top} 亏损:")
    for t in sorted(trades, key=lambda x: x['return_pct'])[:n_top]:
        print(f"    {t['code']:<8} {t['board']:<6} "
              f"{t['signal_date']}  入{t['entry_price']:>7.2f} → 出{t['exit_price']:>7.2f}  "
              f"收益{t['return_pct']:>+6.2f}%  day_gain={t['day_gain']:+.1f}%  tail={t['tail_ret']:+.2f}%")


# ================================================================
# 主函数
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="V2 尾盘买入策略快速回测 (日线预筛 + 1m验证, 与 V2 逐笔等价)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test_v2_tail_buy_fast_v4.py --source db --days 30
  python test_v2_tail_buy_fast_v4.py --source db --start-date 2026-08-05 --end-date 2026-09-08
  python test_v2_tail_buy_fast_v4.py --today --source db
  python test_v2_tail_buy_fast_v4.py --source db --no-prescreen   # 关预筛(=V2全量)
        """)
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=60, help="回看天数 (默认60)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual")
    parser.add_argument("--no-filter-st", action="store_true")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="",
                        help="回测截止日期 (默认今天; 固定窗口复现时用)")
    parser.add_argument("--today", action="store_true",
                        help="实时扫描今日信号 (realtime_snapshot 数据源)")
    parser.add_argument("--no-prescreen", action="store_true",
                        help="关闭日线预筛 (退化为 V2 全量, 用于A/B计时)")
    parser.add_argument("--all-trades", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    _load_env()

    # ---- 股票列表 (与 V2 相同来源) ----
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        stock_source = "手动指定"
    elif args.source == "db":
        print("  全市场扫描: 加载股票列表...")
        filter_st = not args.no_filter_st
        codes = get_all_codes_basicinfo(filter_st=filter_st)
        stock_source = f"basicinfo_db ({'排除ST' if filter_st else '含ST'})"
        print(f"   {stock_source}: {len(codes)} 只")
    else:
        print("  manual 模式请用 --codes 指定股票 (V4 无内置测试池)")
        return

    # ================================================================
    # --today 模式
    # ================================================================
    if args.today:
        print(f"{'=' * 80}")
        print(f"V2 尾盘买入 — 今日信号快速扫描 (realtime_snapshot 数据源)")
        print(f"{'=' * 80}")
        print(f"股票来源: {stock_source} ({len(codes)}只)")
        print(f"入场规则: score>=8 + pre5<=-10 + amp>=10 + tail_ret(-2.8~-0.5%) (创/科板×0.5)\n")

        hits = run_today_scan(codes)

        name_map = {}
        try:
            name_map = get_stock_name_map()
        except Exception:
            pass
        for h in hits:
            h['name'] = name_map.get(h['code'], '')

        print(f"\n{'=' * 80}")
        print(f"扫描完成: {len(codes)}只, 符合V2条件 {len(hits)} 只")
        print(f"{'=' * 80}")

        if hits:
            hits.sort(key=lambda x: x['norm_day_gain'])
            print(f"\n{'代码':<8} {'名称':<8} {'板块':<6} {'14:56价':>8} "
                  f"{'day_gain':>9} {'tail_ret':>9} {'pos':>5} {'amp':>6} {'pre5':>7}")
            print(f"{'-' * 80}")
            for h in hits:
                print(f"{h['code']:<8} {h['name']:<8} {h['board']:<6} "
                      f"{h['entry_price']:>8.2f} "
                      f"{h['day_gain']:>+8.2f} {h['tail_ret']:>+8.3f} "
                      f"{h['pos_range']:>5.3f} {h['amplitude']:>6.2f} {h['pre5_gain']:>+6.2f}")
            out_file = args.json or "test_v2_tail_buy_fast_v4_today.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(hits, f, ensure_ascii=False, indent=2)
            print(f"\n  导出: {out_file}")
        else:
            print("  今日无符合V2条件的股票 (14:56 前运行属正常 — 14:56特征尚不完整)。")
        return

    # ================================================================
    # 回测模式 (预筛 → V2 原版 backtest_stock)
    # ================================================================
    end_date = args.end_date or datetime.now().strftime("%Y-%m-%d")
    if args.start_date:
        start_date = args.start_date
    else:
        start_date = (datetime.now() - timedelta(days=int(args.days * 1.6))).strftime("%Y-%m-%d")

    print(f"{'=' * 80}")
    print(f"V2 尾盘买入策略快速回测 V4 (日线预筛 → 1m验证, 等价 V2)")
    print(f"{'=' * 80}")
    print(f"股票来源: {stock_source} ({len(codes)}只)")
    print(f"回测区间: {start_date} ~ {end_date}")
    print(f"预筛: {'ON (P1 day_gain<=-5 + P2 pre5<=-10 + P3 amp>=10 必要条件)' if not args.no_prescreen else 'OFF'}")
    print(f"入场规则: score>=8 + pre5<=-10 + amp>=10 + tail_ret(-2.8~-0.5%) (创/科板×0.5)")
    print(f"卖出规则: D+1 开盘价\n")

    name_map = {}
    try:
        name_map = get_stock_name_map()
    except Exception:
        pass

    all_trades = []
    n_prescreen_pass = 0
    t0 = time.time()

    for i, code in enumerate(codes):
        daily_bars = fetch_daily_kline(code, 300)
        if not daily_bars or len(daily_bars) < 8:
            continue

        # 阶段1: 日线预筛 (必要条件超集) — 不过则不拉1m
        if not args.no_prescreen and not prescan_stock(daily_bars, code, start_date, end_date):
            continue
        n_prescreen_pass += 1

        # 阶段2: V2 原版回测 (import 复用, 保证逐笔等价)
        trades = backtest_stock(code, start_date, end_date, daily_bars)
        all_trades.extend(trades)

        if trades:
            sname = name_map.get(code, '')
            tag = f"({sname})" if sname else ""
            print(f"[{i+1}/{len(codes)}] {code}{tag} ({get_board_name(code)}) → {len(trades)}笔信号")

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  进度: {i+1}/{len(codes)}  预筛存活{n_prescreen_pass}  "
                  f"信号{len(all_trades)}笔  耗时{elapsed:.0f}s")

    elapsed = time.time() - t0

    print(f"\n{'=' * 80}")
    print(f"回测完成: {stock_source}, 扫描{len(codes)}只, "
          f"预筛存活{n_prescreen_pass}只 ({n_prescreen_pass/max(len(codes),1)*100:.1f}%), "
          f"耗时{elapsed:.0f}s")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "V2 精掐规则 (V4快速版)")
        print_top(all_trades, args.top)
        if args.all_trades:
            print_detail(all_trades)
    else:
        print("  无交易信号。")

    out_file = args.json or "test_v2_tail_buy_fast_v4_result.json"
    if all_trades:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  导出: {out_file}")


if __name__ == "__main__":
    main()
