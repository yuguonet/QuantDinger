#!/usr/bin/env python3
"""
板块耦合度分析 — numpy 向量化版本

用法:
  cd D:\\QuantDinger
  python scripts/sector_coupling.py
  python scripts/sector_coupling.py --max-lag 5 --top-couplings 30
"""
import sys
import json
import argparse
import os
from pathlib import Path
from collections import defaultdict
from datetime import datetime

_root = Path(__file__).resolve().parent.parent
_backend_root = str(_root / "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    for p in [os.path.join(_backend_root, '.env'), os.path.join(str(_root), '.env')]:
        if os.path.isfile(p):
            load_dotenv(p, override=False); break
except ImportError:
    pass

import math
import numpy as np


# ============================================================
# 数据加载
# ============================================================

def load_sector_daily(start_date=None, end_date=None):
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")

    conditions, params = [], []
    if start_date: conditions.append("date >= %s"); params.append(start_date)
    if end_date:   conditions.append("date <= %s"); params.append(end_date)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with pool.cursor() as cur:
        cur.execute(f"""
            SELECT date, sector_type, sector_name, heat_score, limit_up_count,
                   advance_pct, avg_return, stock_count
            FROM sector_daily_stats {where}
            ORDER BY date, sector_type, sector_name
        """, params)
        rows = cur.fetchall()

    if not rows:
        print("⚠️  sector_daily_stats 无数据"); return None, None

    series = defaultdict(lambda: defaultdict(list))
    for row in rows:
        series[row[1]][row[2]].append((
            str(row[0])[:10], float(row[3] or 0), int(row[4] or 0),
            float(row[5] or 0), float(row[6] or 0), int(row[7] or 0)
        ))

    dates = sorted(set(str(r[0])[:10] for r in rows))
    print(f"  {len(rows)} 条, {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")
    for stype, sectors in series.items():
        print(f"  {stype}: {len(sectors)} 个板块")
    return series, dates


def load_stock_info():
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute("SELECT symbol, name, industry, concepts FROM stock_basic_info WHERE status='active'")
        rows = cur.fetchall()
    result = {}
    for row in rows:
        result[row[0]] = {
            'name': row[1] or '',
            'industries': [c.strip() for c in (row[2] or '').split(',') if c.strip()],
            'concepts': [c.strip() for c in (row[3] or '').split(',') if c.strip()],
        }
    return result


# ============================================================
# numpy 向量化互相关
# ============================================================

def build_heat_matrix(series, dates, stype, min_coverage=0.3):
    """构建板块×日期的热度矩阵 (numpy)
    返回: matrix (n_sectors × n_dates), names (板块名列表)
    NaN 填充为 0 (热度0 = 不热, 比 NaN 更合理)
    """
    sectors = series.get(stype, {})
    n_dates = len(dates)

    valid = []
    for sname, points in sectors.items():
        d_map = {p[0]: p[1] for p in points}
        coverage = sum(1 for d in dates if d in d_map) / n_dates
        if coverage >= min_coverage:
            valid.append((sname, d_map))

    if not valid:
        return None, []

    names = [v[0] for v in valid]
    matrix = np.zeros((len(valid), n_dates), dtype=np.float32)

    for i, (sname, d_map) in enumerate(valid):
        for j, d in enumerate(dates):
            val = d_map.get(d, 0.0)  # 缺失值填 0 (不热)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                val = 0.0
            matrix[i, j] = val

    fill_rate = np.count_nonzero(matrix) / matrix.size
    print(f"  矩阵填充率: {fill_rate:.1%}")
    return matrix, names


def calc_couplings_numpy(matrix, names, max_lag=10, min_overlap=30, top_per_sector=10):
    """向量化计算所有板块对在 lag=1..max_lag 的互相关

    核心优化: 对每个 lag, 一次性计算所有板块对的相关系数矩阵
    复杂度: O(max_lag × n² × T) 但用 numpy 矩阵运算, 实际很快
    """
    n_sectors, n_dates = matrix.shape
    all_pairs = []

    for lag in range(1, max_lag + 1):
        # A[:, :T-lag] vs B[:, lag:]
        a_mat = matrix[:, :n_dates - lag]   # (n, T-lag)
        b_mat = matrix[:, lag:]             # (n, T-lag)

        # 逐列计算有效掩码 (非nan)
        a_valid = ~np.isnan(a_mat)  # (n, T-lag)
        b_valid = ~np.isnan(b_mat)  # (n, T-lag)

        # 对每对 (i, j) 计算 Pearson 相关系数
        # 用向量化方式: 先中心化, 再点积
        for i in range(n_sectors):
            if i % 100 == 0 and i > 0:
                print(f"    {stype_name}: lag={lag} 进度 {i}/{n_sectors}", end='\r')

            a_row = a_mat[i]  # (T-lag,)
            a_mask = a_valid[i]  # (T-lag,)

            for j in range(n_sectors):
                if i == j:
                    continue

                b_row = b_mat[j]
                b_mask = b_valid[j]

                # 两个都有效的交集
                both = a_mask & b_mask
                n = both.sum()
                if n < min_overlap:
                    continue

                a_vals = a_row[both]
                b_vals = b_row[both]

                # Pearson
                a_mean = a_vals.mean()
                b_mean = b_vals.mean()
                a_centered = a_vals - a_mean
                b_centered = b_vals - b_mean

                cov = (a_centered * b_centered).sum() / n
                a_std = np.sqrt((a_centered ** 2).sum() / n)
                b_std = np.sqrt((b_centered ** 2).sum() / n)

                if a_std < 1e-9 or b_std < 1e-9:
                    continue

                corr = cov / (a_std * b_std)

                if abs(corr) >= 0.1:
                    all_pairs.append((names[i], names[j], lag, round(float(corr), 4), int(n)))

        print(f"    lag={lag} 完成, 累计 {len(all_pairs)} 条关系" + " " * 30)

    # 每个 leader 取 top N
    leader_best = defaultdict(list)
    for a, b, lag, corr, n in all_pairs:
        leader_best[a].append((a, b, lag, corr, n))

    result = []
    for a, pairs in leader_best.items():
        pairs.sort(key=lambda x: -abs(x[3]))
        result.extend(pairs[:top_per_sector])

    result.sort(key=lambda x: -abs(x[3]))
    return result


# 全局变量给进度条用
stype_name = ""


# ============================================================
# 优化版: 分块计算, 减少内层循环
# ============================================================

def calc_couplings_fast(matrix, names, max_lag=10, min_overlap=30, top_per_sector=10):
    """向量化计算所有板块对在 lag=1..max_lag 的互相关

    矩阵已无 NaN (填0), 直接用矩阵乘法算相关系数
    """
    n_sectors, n_dates = matrix.shape
    all_pairs = []

    for lag in range(1, max_lag + 1):
        a_mat = matrix[:, :n_dates - lag]   # (n, T)
        b_mat = matrix[:, lag:]             # (n, T)
        T = a_mat.shape[1]

        # 中心化 (减去行均值)
        a_mean = a_mat.mean(axis=1, keepdims=True)  # (n, 1)
        b_mean = b_mat.mean(axis=1, keepdims=True)
        a_centered = a_mat - a_mean
        b_centered = b_mat - b_mean

        # 协方差矩阵
        cov_matrix = a_centered @ b_centered.T / T  # (n, n)

        # 标准差
        a_std = np.sqrt((a_centered ** 2).mean(axis=1))  # (n,)
        b_std = np.sqrt((b_centered ** 2).mean(axis=1))

        # 相关系数矩阵
        std_outer = np.outer(a_std, b_std)  # (n, n)
        safe_std = np.maximum(std_outer, 1e-10)
        corr_matrix = cov_matrix / safe_std

        # 过滤: |corr| >= 0.1 且 i != j
        mask = np.abs(corr_matrix) >= 0.1
        np.fill_diagonal(mask, False)

        rows, cols = np.where(mask)
        for idx in range(len(rows)):
            i, j = rows[idx], cols[idx]
            all_pairs.append((names[i], names[j], lag,
                              round(float(corr_matrix[i, j]), 4), T))

        print(f"    lag={lag} 完成, 有效pair={len(rows)}, 累计 {len(all_pairs)} 条" + " " * 20)

    # 每个 leader 取 top N
    leader_best = defaultdict(list)
    for a, b, lag, corr, n in all_pairs:
        leader_best[a].append((a, b, lag, corr, n))

    result = []
    for a, pairs in leader_best.items():
        pairs.sort(key=lambda x: -abs(x[3]))
        result.extend(pairs[:top_per_sector])

    result.sort(key=lambda x: -abs(x[3]))
    return result


# ============================================================
# 输出
# ============================================================

def print_couplings(couplings, top_n=30):
    print(f"\n{'='*100}")
    print(f"# 板块耦合 Top {min(top_n, len(couplings))}")
    print(f"{'='*100}")
    print(f"  {'类型':<10} {'领先板块':<20} → {'跟随板块':<20} {'lag':>4} {'相关系数':>8} {'样本':>6}")
    print(f"  {'-'*80}")
    for stype, a, b, lag, corr, n in couplings[:top_n]:
        print(f"  {stype:<10} {a:<20} → {b:<20} {lag:>3}天 {corr:>+8.4f} {n:>6}天")


def build_coupling_predictor(couplings, top_n=50):
    pred = defaultdict(list)
    for stype, a, b, lag, corr, n in couplings[:top_n * 3]:
        if corr > 0.1:
            pred[a].append((b, lag, corr))
    return dict(pred)


def predict_with_coupling(series, dates, predictors, target_date, heat_quantile=0.7):
    try:
        di = dates.index(target_date)
    except ValueError:
        return {}

    date_idx = defaultdict(lambda: defaultdict(dict))
    for stype, sectors in series.items():
        for sname, points in sectors.items():
            for p in points:
                date_idx[stype][sname][p[0]] = p[1]

    thresholds = {}
    for stype, sectors in series.items():
        thresholds[stype] = {}
        for sname, points in sectors.items():
            heats = sorted([p[1] for p in points])
            if len(heats) >= 10:
                thresholds[stype][sname] = heats[int(len(heats) * heat_quantile)]

    max_lag = max((lag for pairs in predictors.values() for _, lag, _ in pairs), default=1)
    scores = defaultdict(float)

    for offset in range(0, max_lag + 1):
        lookback_idx = di - offset
        if lookback_idx < 0:
            break
        lookback_date = dates[lookback_idx]

        for stype, thresh_dict in thresholds.items():
            for sname, thresh in thresh_dict.items():
                h = date_idx.get(stype, {}).get(sname, {}).get(lookback_date)
                if h is not None and h >= thresh:
                    for follower, lag, corr in predictors.get(sname, []):
                        if lag == offset:
                            scores[follower] += corr * h

    return dict(scores)


def score_signals(trades, series, dates, stock_info, predictors):
    date_idx = defaultdict(lambda: defaultdict(dict))
    for stype, sectors in series.items():
        for sname, points in sectors.items():
            for p in points:
                date_idx[stype][sname][p[0]] = (p[1], p[2], p[3])

    scored = []
    for t in trades:
        code = t['code']
        entry_date = t['entry_date']
        info = stock_info.get(code, {})
        industries = info.get('industries', [])
        concepts = info.get('concepts', [])

        try:
            di = dates.index(entry_date)
        except ValueError:
            t['_score'] = 0; t['_score_detail'] = {}; scored.append(t); continue

        preds = predict_with_coupling(series, dates, predictors, entry_date)
        scores = {}

        pred_score = 0
        for ind in industries: pred_score += preds.get(ind, 0)
        for con in concepts: pred_score += preds.get(con, 0)
        scores['coupling_pred'] = round(pred_score, 2)

        today_heats = []
        for ind in industries:
            h = date_idx.get('industry', {}).get(ind, {}).get(entry_date)
            if h: today_heats.append(h[0])
        for con in concepts:
            h = date_idx.get('concept', {}).get(con, {}).get(entry_date)
            if h: today_heats.append(h[0])
        scores['today_heat'] = round(sum(today_heats) / len(today_heats) if today_heats else 0, 2)

        trend_scores = []
        for ind in industries:
            heats_5d = []
            for offset in range(-4, 1):
                idx = di + offset
                if 0 <= idx < len(dates):
                    h = date_idx.get('industry', {}).get(ind, {}).get(dates[idx])
                    if h: heats_5d.append(h[0])
            if len(heats_5d) >= 3:
                n5 = len(heats_5d)
                xm = (n5 - 1) / 2
                ym = sum(heats_5d) / n5
                num = sum((k - xm) * (heats_5d[k] - ym) for k in range(n5))
                den = sum((k - xm) ** 2 for k in range(n5))
                trend_scores.append(num / den if den > 0 else 0)
        scores['trend'] = round(sum(trend_scores) / len(trend_scores) if trend_scores else 0, 2)

        composite = scores['coupling_pred'] * 0.5 + scores['today_heat'] * 0.3 + scores['trend'] * 0.2
        scores['composite'] = round(composite, 2)

        t['_score'] = scores['composite']
        t['_score_detail'] = scores
        scored.append(t)

    return scored


def evaluate(scored_trades):
    if not scored_trades: return
    scores = sorted([t['_score'] for t in scored_trades])
    n = len(scores)
    q33, q67 = scores[int(n * 0.33)], scores[int(n * 0.67)]

    print(f"\n{'='*90}")
    print(f"📊 耦合评分 vs 信号表现")
    print(f"{'='*90}")
    print(f"  分数区间: 低分<{q33:.1f}  中分{q33:.1f}~{q67:.1f}  高分≥{q67:.1f}")

    for label, filt in [('低分(<P33)', lambda s: s < q33),
                         ('中分(P33-P67)', lambda s: q33 <= s < q67),
                         ('高分(≥P67)', lambda s: s >= q67)]:
        seg = [t for t in scored_trades if filt(t['_score'])]
        if not seg: continue
        n_t = len(seg)
        wr = sum(1 for t in seg if t['return_pct'] > 0) / n_t * 100
        avg = sum(t['return_pct'] for t in seg) / n_t
        avg_pk = sum(t['peak_return_pct'] for t in seg) / n_t
        ws = [t['return_pct'] for t in seg if t['return_pct'] > 0]
        ls = [t['return_pct'] for t in seg if t['return_pct'] <= 0]
        pr = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls)) if ws and ls else (999 if ws else 0)
        pr_s = f"{pr:.2f}" if pr < 999 else "∞"
        print(f"\n  {label}:")
        print(f"    {n_t:>3}笔  胜率{wr:>5.1f}%  均收益{avg:>+6.2f}%  "
              f"均峰值{avg_pk:>+6.2f}%  盈亏比{pr_s:>6}")

    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in scored_trades if t['path_label'] == pl]
        if len(seg) < 10: continue
        srt = sorted([t['_score'] for t in seg])
        n_s = len(srt)
        q33s, q67s = srt[int(n_s * 0.33)], srt[int(n_s * 0.67)]
        print(f"\n  ── {pl} ({n_s}笔) ──")
        for label, filt in [('低分', lambda s: s < q33s),
                             ('中分', lambda s: q33s <= s < q67s),
                             ('高分', lambda s: s >= q67s)]:
            sub = [t for t in seg if filt(t['_score'])]
            if not sub: continue
            n_t = len(sub)
            wr = sum(1 for t in sub if t['return_pct'] > 0) / n_t * 100
            avg = sum(t['return_pct'] for t in sub) / n_t
            print(f"    {label}: {n_t:>3}笔  胜率{wr:>5.1f}%  均收益{avg:>+6.2f}%")

    srt = sorted(scored_trades, key=lambda x: x['_score'])
    print(f"\n  🔥 最高分:")
    for t in srt[-5:]:
        d = t['_score_detail']
        print(f"    {t['code']} [{t['path_label']}] {t['entry_date']} "
              f"分{t['_score']:>6.1f} 收益{t['return_pct']:>+6.2f}% "
              f"(耦合{d['coupling_pred']:.0f} 热度{d['today_heat']:.0f} 趋势{d['trend']:.1f})")
    print(f"\n  💀 最低分:")
    for t in srt[:5]:
        d = t['_score_detail']
        print(f"    {t['code']} [{t['path_label']}] {t['entry_date']} "
              f"分{t['_score']:>6.1f} 收益{t['return_pct']:>+6.2f}% "
              f"(耦合{d['coupling_pred']:.0f} 热度{d['today_heat']:.0f} 趋势{d['trend']:.1f})")


# ============================================================
# 主流程
# ============================================================

def main():
    global stype_name

    parser = argparse.ArgumentParser(description="板块耦合度分析 (numpy加速)")
    parser.add_argument("--result", default="test_dragon_callback_result.json")
    parser.add_argument("--start", type=str, default="2024-01-01")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--max-lag", type=int, default=5)
    parser.add_argument("--min-history", type=int, default=30)
    parser.add_argument("--top-couplings", type=int, default=30)
    parser.add_argument("--min-coverage", type=float, default=0.3, help="板块最少覆盖率")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    print(f"[1/5] 加载板块历史 ({args.start} ~ {end_date})...")
    series, dates = load_sector_daily(args.start, end_date)
    if not series or not dates or len(dates) < args.min_history:
        print("⚠️  数据不足"); return

    print(f"\n[2/5] 加载 stock_basic_info...")
    stock_info = load_stock_info()
    print(f"  {len(stock_info)} 只")

    all_couplings = []

    for stype in series.keys():
        print(f"\n[3/5] 构建 {stype} 热度矩阵...")
        stype_name = stype
        matrix, names = build_heat_matrix(series, dates, stype, min_coverage=args.min_coverage)
        if matrix is None:
            print(f"  ⚠️ {stype} 无有效数据"); continue
        print(f"  {len(names)} 个板块 × {len(dates)} 天, 矩阵大小 {matrix.shape}")

        print(f"\n[4/5] 计算 {stype} 互相关 (max_lag={args.max_lag})...")
        couplings = calc_couplings_fast(matrix, names, max_lag=args.max_lag)
        all_couplings.extend([(stype, *c) for c in couplings])

    # 排序
    all_couplings.sort(key=lambda x: -abs(x[4]))

    # 输出
    print_couplings(all_couplings, top_n=args.top_couplings)

    # 导出
    coupling_path = Path(args.result).stem + "_couplings.json"
    with open(coupling_path, 'w', encoding='utf-8') as f:
        json.dump([{'stype': s, 'leader': a, 'follower': b, 'lag': l, 'corr': c, 'days': n}
                    for s, a, b, l, c, n in all_couplings], f, ensure_ascii=False, indent=2)
    print(f"\n💾 {coupling_path}")

    # 评分
    print(f"\n[5/5] 信号评分...")
    predictors = build_coupling_predictor(all_couplings)
    print(f"  {sum(len(v) for v in predictors.values())} 条预测规则")

    with open(args.result, encoding='utf-8') as f:
        trades = json.load(f)

    scored = score_signals(trades, series, dates, stock_info, predictors)
    evaluate(scored)

    out_path = Path(args.result).stem + "_coupling_scored.json"
    export = [{k: v for k, v in t.items() if k in ('code', 'path_label', 'entry_date',
               'return_pct', 'peak_return_pct', '_score', '_score_detail')} for t in scored]
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n💾 {out_path}")


if __name__ == "__main__":
    main()
