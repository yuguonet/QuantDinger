#!/usr/bin/env python3
"""
行业/概念维度分析 — 基于 test_dragon_callback_result.json + stock_basic_info

放在 QuantDinger/scripts/ 下运行:
  cd D:\\QuantDinger
  python scripts/analyze_sector.py
  python scripts/analyze_sector.py --result test_dragon_callback_result.json
  python scripts/analyze_sector.py --min-trades 3 --strategy dragon
"""
import sys
import json
import argparse
import os
from pathlib import Path
from collections import defaultdict

# 路径: 与 test_dragon_callback.py / sync_sector_daily.py 一致
_root = Path(__file__).resolve().parent.parent
_backend_root = str(_root / "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    for p in [os.path.join(_backend_root, '.env'), os.path.join(str(_root), '.env')]:
        if os.path.isfile(p):
            load_dotenv(p, override=False)
            break
except ImportError:
    pass


def load_stock_info():
    """加载 stock_basic_info 全量 (复用 basicinfo_db)"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute(
            "SELECT symbol, name, industry, concepts, circ_shares, total_shares "
            "FROM stock_basic_info WHERE status='active'"
        )
        rows = cur.fetchall()
    result = {}
    for row in rows:
        # industry 可能是逗号分隔的多标签, 如 "机械行业,汽车零部件"
        industries = [c.strip() for c in (row[2] or '').split(',') if c.strip()]
        concepts = [c.strip() for c in (row[3] or '').split(',') if c.strip()]
        result[row[0]] = {
            'name': row[1] or '',
            'industry': industries[0] if industries else '',       # 主行业 (兼容旧逻辑)
            'industries': industries,                               # 全部行业标签
            'concepts': concepts,
            'circ_shares': float(row[4] or 0),
            'total_shares': float(row[5] or 0),
        }
    return result


def calc_stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    returns = [t['return_pct'] for t in trades]
    peaks = [t['peak_return_pct'] for t in trades]
    avg_ret = sum(returns) / n
    avg_peak = sum(peaks) / n
    win_rate = len(wins) / n * 100
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else (999.0 if wins else 0)
    d1_up = sum(1 for t in trades if t.get('d1_limit_up', False))
    return {
        'n': n, 'win_rate': win_rate, 'avg_ret': avg_ret, 'avg_peak': avg_peak,
        'profit_ratio': profit_ratio, 'max_ret': max(returns), 'min_ret': min(returns),
        'd1_up_rate': d1_up / n * 100, 'wins': len(wins), 'losses': len(losses),
    }


def ps(label, stats, indent=2):
    """打印一行统计"""
    if not stats:
        return
    pad = " " * indent
    pr = f"{stats['profit_ratio']:.2f}" if stats['profit_ratio'] < 999 else "∞"
    print(f"{pad}{label:<20} {stats['n']:>3}笔  胜率{stats['win_rate']:>5.1f}%  "
          f"均收益{stats['avg_ret']:>+6.2f}%  均峰值{stats['avg_peak']:>+6.2f}%  "
          f"盈亏比{pr:>6}  最大亏{stats['min_ret']:>+6.2f}%")


def group_and_print(trades, key_fn, group_name, min_trades=2, top_n=20, sort_by='avg_ret'):
    """通用分组统计"""
    groups = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        if k:
            groups[k].append(t)

    items = [(k, v) for k, v in groups.items() if len(v) >= min_trades]
    if sort_by == 'avg_ret':
        items.sort(key=lambda x: -calc_stats(x[1])['avg_ret'])
    elif sort_by == 'count':
        items.sort(key=lambda x: -len(x[1]))
    elif sort_by == 'win_rate':
        items.sort(key=lambda x: -calc_stats(x[1])['win_rate'])

    if not items:
        print(f"  (无满足条件的{group_name}, min_trades={min_trades})")
        return

    print(f"\n  {'='*90}")
    print(f"  按{group_name}分组 (≥{min_trades}笔, 共{len(items)}组)")
    print(f"  {'='*90}")

    for key, tg in items[:top_n]:
        ps(key, calc_stats(tg))

    if len(items) > top_n:
        print(f"  ... 还有 {len(items) - top_n} 组")


def analyze_sector_heat(trades, stock_info):
    """板块协同: 同日同行业多信号 vs 独苗"""
    by_date = defaultdict(list)
    for t in trades:
        by_date[t['entry_date']].append(t)

    solo, cluster = [], []
    for date, dt in by_date.items():
        # 每只股票按其所属行业中, 最大的行业集群决定它归 cluster 还是 solo
        ind_groups = defaultdict(list)
        for t in dt:
            for ind in stock_info.get(t['code'], {}).get('industries', []):
                ind_groups[ind].append(t)

        # 找出每只股票所属行业中, 最大的集群大小
        code_max_cluster = defaultdict(int)
        for ind, it in ind_groups.items():
            size = len(it)
            for t in it:
                if size > code_max_cluster[t['code']]:
                    code_max_cluster[t['code']] = size

        seen = set()
        for t in dt:
            if t['code'] in seen:
                continue
            seen.add(t['code'])
            if code_max_cluster[t['code']] >= 2:
                cluster.append(t)
            else:
                solo.append(t)

    print(f"\n  {'='*90}")
    print(f"  板块协同效应 (同日同行业多信号 vs 独苗)")
    print(f"  {'='*90}")
    if solo:
        ps("独苗信号", calc_stats(solo))
    if cluster:
        ps("集聚信号(≥2)", calc_stats(cluster))

    # 按集聚规模细分 (按日×行业, 避免跨日混计)
    size_groups = defaultdict(list)
    for date, dt in by_date.items():
        ind_groups = defaultdict(list)
        for t in dt:
            inds = stock_info.get(t['code'], {}).get('industries', [])
            for ind in inds:
                ind_groups[ind].append(t)
        for ind, it in ind_groups.items():
            if len(it) >= 2:
                size_groups[len(it)].extend(it)

    if len(size_groups) > 1:
        print(f"\n  按集聚规模:")
        for size in sorted(size_groups.keys()):
            if len(size_groups[size]) >= 2:
                ps(f"  {size}只同板块", calc_stats(size_groups[size]), indent=4)


def main():
    parser = argparse.ArgumentParser(description="行业/概念维度分析")
    parser.add_argument("--result", default="test_dragon_callback_result.json")
    parser.add_argument("--min-trades", type=int, default=2)
    parser.add_argument("--min-concept-trades", type=int, default=3)
    parser.add_argument("--strategy", default="all", choices=["all", "dragon", "v1", "break"])
    args = parser.parse_args()

    print(f"[加载] {args.result}")
    with open(args.result, encoding='utf-8') as f:
        data = json.load(f)

    print(f"[加载] stock_basic_info...")
    stock_info = load_stock_info()
    print(f"  {len(stock_info)} 只股票")

    matched = sum(1 for t in data if t['code'] in stock_info)
    print(f"  匹配: {matched}/{len(data)} 笔有行业信息\n")

    # 筛选策略
    strategy_filter = {
        'dragon': lambda t: t['path'] == 'dragon_callback',
        'v1': lambda t: t['path'] == 'v1',
        'break': lambda t: t['path'] == 'break_buy',
    }
    if args.strategy != 'all':
        data = [t for t in data if strategy_filter[args.strategy](t)]

    # ===== 整体 =====
    print(f"{'='*90}")
    print(f"📊 整体: {len(data)} 笔交易")
    print(f"{'='*90}")
    ps("全部", calc_stats(data))
    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in data if t['path_label'] == pl]
        if seg:
            ps(pl, calc_stats(seg))

    # ===== 行业 =====
    print(f"\n\n{'#'*90}")
    print(f"# 行业分析")
    print(f"{'#'*90}")

    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in data if t['path_label'] == pl]
        if seg:
            print(f"\n  ── {pl} ({len(seg)}笔) ──")
            # industry 也可能是多标签, 与 concepts 同样展开
            ind_trades = defaultdict(list)
            for t in seg:
                for ind in stock_info.get(t['code'], {}).get('industries', []):
                    ind_trades[ind].append(t)
            items = [(k, v) for k, v in ind_trades.items() if len(v) >= args.min_trades]
            items.sort(key=lambda x: -calc_stats(x[1])['avg_ret'])
            if items:
                print(f"\n  {'='*90}")
                print(f"  按行业分组 (≥{args.min_trades}笔, 共{len(items)}组)")
                print(f"  {'='*90}")
                for key, tg in items[:20]:
                    ps(key, calc_stats(tg))
                if len(items) > 20:
                    print(f"  ... 还有 {len(items) - 20} 组")
            else:
                print(f"  (无满足条件的行业, min_trades={args.min_trades})")

    print(f"\n  ── 全部策略合并 ({len(data)}笔) ──")
    ind_trades_all = defaultdict(list)
    for t in data:
        for ind in stock_info.get(t['code'], {}).get('industries', []):
            ind_trades_all[ind].append(t)
    items_all = [(k, v) for k, v in ind_trades_all.items() if len(v) >= args.min_trades]
    items_all.sort(key=lambda x: -calc_stats(x[1])['avg_ret'])
    if items_all:
        print(f"\n  {'='*90}")
        print(f"  按行业分组 (≥{args.min_trades}笔, 共{len(items_all)}组)")
        print(f"  {'='*90}")
        for key, tg in items_all[:20]:
            ps(key, calc_stats(tg))
        if len(items_all) > 20:
            print(f"  ... 还有 {len(items_all) - 20} 组")

    # ===== 概念 =====
    print(f"\n\n{'#'*90}")
    print(f"# 概念分析")
    print(f"{'#'*90}")

    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in data if t['path_label'] == pl]
        if seg:
            print(f"\n  ── {pl} ({len(seg)}笔) ──")
            concept_trades = defaultdict(list)
            for t in seg:
                for c in stock_info.get(t['code'], {}).get('concepts', []):
                    concept_trades[c].append(t)
            items = [(k, v) for k, v in concept_trades.items() if len(v) >= args.min_concept_trades]
            items.sort(key=lambda x: -calc_stats(x[1])['avg_ret'])
            if items:
                print(f"\n  {'='*90}")
                print(f"  按概念分组 (≥{args.min_concept_trades}笔, 共{len(items)}组)")
                print(f"  {'='*90}")
                for key, tg in items[:20]:
                    ps(key, calc_stats(tg))
                if len(items) > 20:
                    print(f"  ... 还有 {len(items) - 20} 组")
            else:
                print(f"  (无满足条件的概念, min_trades={args.min_concept_trades})")

    # ===== 板块协同 =====
    print(f"\n\n{'#'*90}")
    print(f"# 板块协同效应")
    print(f"{'#'*90}")

    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in data if t['path_label'] == pl]
        if seg and len(seg) >= 5:
            print(f"\n  ── {pl} ──")
            analyze_sector_heat(seg, stock_info)

    analyze_sector_heat(data, stock_info)

    # ===== 行业排行 =====
    print(f"\n\n{'#'*90}")
    print(f"# 行业 Top/Bottom 排行")
    print(f"{'#'*90}")

    ind_all = defaultdict(list)
    for t in data:
        for ind in stock_info.get(t['code'], {}).get('industries', []):
            ind_all[ind].append(t)

    ranked = [(k, calc_stats(v)) for k, v in ind_all.items() if len(v) >= args.min_trades]
    ranked.sort(key=lambda x: -x[1]['avg_ret'])

    print(f"\n  🏆 行业收益 Top 10 (≥{args.min_trades}笔):")
    for name, s in ranked[:10]:
        ps(name, s, indent=4)

    if len(ranked) > 10:
        print(f"\n  💀 行业收益 Bottom 10:")
        for name, s in ranked[-10:]:
            ps(name, s, indent=4)

    # ===== 多策略交叉 =====
    by_code = defaultdict(list)
    for t in data:
        by_code[t['code']].append(t)
    multi = {k: v for k, v in by_code.items() if len(v) > 1}
    if multi:
        print(f"\n\n{'#'*90}")
        print(f"# 多策略交叉 ({len(multi)}只)")
        print(f"{'#'*90}")
        for code, ct in sorted(multi.items(), key=lambda x: -len(x[1])):
            ind = stock_info.get(code, {}).get('industry', '?')
            name = stock_info.get(code, {}).get('name', '')
            strats = [f"{t['path_label']}({t['return_pct']:+.1f}%)" for t in ct]
            avg = sum(t['return_pct'] for t in ct) / len(ct)
            print(f"  {code} {name:<8} [{ind}] {' | '.join(strats)}  均{avg:+.1f}%")

    # ===== 导出 =====
    out = {'summary': calc_stats(data), 'by_industry': {}, 'by_concept': {}}
    for name, tg in ind_all.items():
        if len(tg) >= args.min_trades:
            s = calc_stats(tg)
            out['by_industry'][name] = {k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()}
    concept_all = defaultdict(list)
    for t in data:
        for c in stock_info.get(t['code'], {}).get('concepts', []):
            concept_all[c].append(t)
    for name, tg in concept_all.items():
        if len(tg) >= args.min_concept_trades:
            s = calc_stats(tg)
            out['by_concept'][name] = {k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()}

    out_path = Path(args.result).stem + "_sector_analysis.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已导出: {out_path}")


if __name__ == "__main__":
    main()
