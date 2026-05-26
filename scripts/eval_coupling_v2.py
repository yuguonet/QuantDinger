#!/usr/bin/env python3
"""
板块自耦合准确率评估 (v2 — 优化版)

核心改进:
  1. 板块类型过滤: 默认只分析 行业+概念, 排除地区/风格等噪声
  2. 提高统计门槛: 默认 n_a_hot≥20, cp>0.1, lift>1.2
  3. numpy 向量化: 去掉 Python 双重循环, 用矩阵 mask 直接筛
  4. BH-FDR 多重比较校正
  5. --exclude-concept 排除特定概念 (如次新股)
  6. 时间稳定性检验 (--stability)
  7. 排序改为 提升度 × log(样本量), 惩罚小样本
"""
import sys, json, argparse, os
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

import numpy as np
import math


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
    return series, dates


def build_lookup(series, dates):
    lookup = {}
    for stype, sectors in series.items():
        for sname, points in sectors.items():
            key = (stype, sname)
            lookup[key] = {p[0]: p for p in points}
    return lookup


def build_date_index(dates):
    return {d: i for i, d in enumerate(dates)}


# ============================================================
# BH-FDR 校正
# ============================================================

def bh_fdr(pvalues, alpha=0.05):
    """Benjamini-Hochberg FDR 校正, 返回 rejected 布尔数组"""
    n = len(pvalues)
    if n == 0:
        return np.array([], dtype=bool)
    sorted_idx = np.argsort(pvalues)
    sorted_p = np.array(pvalues)[sorted_idx]
    thresholds = alpha * np.arange(1, n + 1) / n
    # 找到最大的 k 使得 p_(k) <= threshold(k)
    passed = sorted_p <= thresholds
    if not passed.any():
        return np.zeros(n, dtype=bool)
    max_k = np.max(np.where(passed))
    rejected = np.zeros(n, dtype=bool)
    rejected[sorted_idx[:max_k + 1]] = True
    return rejected


# ============================================================
# 自耦合评估 (向量化版)
# ============================================================

def evaluate_self_coupling(series, dates, max_lag=5, heat_quantile=0.7,
                           min_coverage=0.5, sector_types=None,
                           exclude_concepts=None, min_a_hot=20,
                           min_cp=0.15, min_lift=1.5, min_samples=5):
    """
    评估板块自耦合预测准确率 (向量化)

    改进:
      - 按 sector_type 过滤
      - 排除指定概念
      - numpy 矩阵操作替代 Python 循环
    """
    lookup = build_lookup(series, dates)
    date_idx = build_date_index(dates)
    n_dates = len(dates)

    if exclude_concepts is None:
        exclude_concepts = set()

    # 自动检测 sector_type 分布
    type_counts = defaultdict(int)
    for key in lookup:
        type_counts[key[0]] += 1
    print(f"  数据库中的板块类型:")
    for stype, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {stype}: {cnt} 个板块")

    # 计算每个板块的热度阈值
    thresholds = {}
    for key, data in lookup.items():
        stype, sname = key
        # 类型过滤 (None = 不过滤)
        if sector_types is not None and stype not in sector_types:
            continue
        # 排除特定概念 (对所有类型都检查关键词匹配)
        if exclude_concepts and any(exc in sname for exc in exclude_concepts):
            continue
        heats = [v[1] for v in data.values() if v[1] > 0]
        if len(heats) >= 20:
            thresholds[key] = np.percentile(heats, heat_quantile * 100)

    print(f"  有效板块数: {len(thresholds)}")
    if sector_types is not None:
        print(f"  限定板块类型: {sector_types}")
    else:
        print(f"  板块类型: 全部")
    if exclude_concepts:
        print(f"  已排除关键词: {exclude_concepts}")

    # 构建热度矩阵
    valid_keys = list(thresholds.keys())
    key_to_idx = {k: i for i, k in enumerate(valid_keys)}
    n_sectors = len(valid_keys)

    hot_matrix = np.zeros((n_sectors, n_dates), dtype=np.float32)
    heat_matrix = np.zeros((n_sectors, n_dates), dtype=np.float32)

    for i, key in enumerate(valid_keys):
        data = lookup[key]
        thresh = thresholds[key]
        for j, d in enumerate(dates):
            if d in data:
                heat = data[d][1]
                heat_matrix[i, j] = heat
                if heat >= thresh:
                    hot_matrix[i, j] = 1.0

    print(f"  热度矩阵: {n_sectors} 板块 × {n_dates} 天")

    # 统计基线
    baseline = {}
    for i, key in enumerate(valid_keys):
        hot_days = hot_matrix[i].sum()
        baseline[key] = hot_days / n_dates

    # 向量化计算
    results = []

    for lag in range(1, max_lag + 1):
        a_hot = hot_matrix[:, :n_dates - lag]      # (n, T-lag)
        b_hot = hot_matrix[:, lag:]                 # (n, T-lag)
        b_heat = heat_matrix[:, lag:]               # (n, T-lag)

        a_hot_count = a_hot.sum(axis=1)             # (n,)
        both_hot = a_hot @ b_hot.T                  # (n, n)
        b_hot_count = b_hot.sum(axis=1)             # (n,)
        total = n_dates - lag

        safe_a_count = np.maximum(a_hot_count, 1)
        cond_prob = both_hot / safe_a_count[:, None]   # (n, n)
        base_prob = b_hot_count / total                 # (n,)
        safe_base = np.maximum(base_prob, 0.001)
        lift = cond_prob / safe_base[None, :]           # (n, n)

        # 向量化筛选: 满足条件的 (i, j) 对
        # 条件: a_hot_count >= min_a_hot, i != j, cond_prob > min_cp, lift > min_lift,
        #       n_samples >= min_samples
        mask = (
            (a_hot_count[:, None] >= min_a_hot) &
            (np.arange(n_sectors)[:, None] != np.arange(n_sectors)[None, :]) &
            (cond_prob > min_cp) &
            (lift > min_lift) &
            (both_hot >= min_samples)
        )

        pairs_i, pairs_j = np.where(mask)

        # A热时B的平均热度 (向量化)
        # 需要对每对 (i, j) 计算 A热天里B的平均热度
        # 这部分无法完全向量化 (每行mask不同), 但只对通过筛选的对计算
        a_hot_mask = a_hot.astype(bool)  # (n, T-lag)

        for idx in range(len(pairs_i)):
            i, j = int(pairs_i[idx]), int(pairs_j[idx])
            cp = float(cond_prob[i, j])
            bp = float(base_prob[j])
            lf = float(lift[i, j])
            n_samples = int(both_hot[i, j])
            n_a = int(a_hot_count[i])

            # A热时B的平均热度
            b_heats_when_a_hot = b_heat[j, a_hot_mask[i]]
            avg_heat_when_hot = float(b_heats_when_a_hot.mean()) if len(b_heats_when_a_hot) > 0 else 0
            b_heats_positive = b_heat[j, b_heat[j] > 0]
            avg_heat_all = float(b_heats_positive.mean()) if len(b_heats_positive) > 0 else 0

            # 简化 p 值估计: 用正态近似
            # H0: P(B热|A热) = P(B热), 检验统计量 z = (cp - bp) / sqrt(bp*(1-bp)/n_a)
            if bp > 0 and bp < 1 and n_a > 0:
                se = math.sqrt(bp * (1 - bp) / n_a)
                z = (cp - bp) / se if se > 0 else 0
                # 双尾 p 值 (近似)
                p_value = 2 * (1 - _norm_cdf(abs(z)))
            else:
                p_value = 1.0

            results.append({
                'leader_stype': valid_keys[i][0],
                'leader': valid_keys[i][1],
                'follower_stype': valid_keys[j][0],
                'follower': valid_keys[j][1],
                'lag': lag,
                'cond_prob': round(cp, 4),
                'base_prob': round(bp, 4),
                'lift': round(lf, 4),
                'avg_heat_when_hot': round(avg_heat_when_hot, 2),
                'avg_heat_all': round(avg_heat_all, 2),
                'heat_boost': round(avg_heat_when_hot - avg_heat_all, 2),
                'n_samples': n_samples,
                'n_a_hot': n_a,
                'p_value': round(p_value, 6),
            })

        print(f"    lag={lag} 完成, 通过筛选 {len(pairs_i)} 对, 累计 {len(results)} 条")

    return results


def _norm_cdf(x):
    """标准正态CDF (无需scipy)"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ============================================================
# 后过滤: 万能接收器 + 双向信号
# ============================================================

def post_filter(results, max_follower_pct=0.10, remove_bidirectional=True):
    """
    过滤掉:
      1. 万能接收器: 被超过 max_follower_pct 比例的 leader 指向的 follower
      2. 双向信号: A→B 且 B→A 都显著 (说明是共变而非领先)
    """
    if not results:
        return results

    original_count = len(results)

    # 1. 万能接收器过滤
    leaders = set(r['leader'] for r in results)
    n_leaders = len(leaders)
    follower_count = defaultdict(int)
    for r in results:
        follower_count[r['follower']] += 1

    universal_threshold = max(1, int(n_leaders * max_follower_pct))
    universal_followers = {f for f, c in follower_count.items() if c >= universal_threshold}

    if universal_followers:
        print(f"\n  后过滤: 万能接收器 (≥{max_follower_pct:.0%} leader 指向)")
        print(f"    唯一 leader 数: {n_leaders}")
        print(f"    阈值: ≥{universal_threshold} 个 leader")
        print(f"    剔除 follower: {len(universal_followers)} 个")
        for f in sorted(universal_followers, key=lambda x: -follower_count[x])[:10]:
            print(f"      {f:<50} ({follower_count[f]} 个 leader)")

        results = [r for r in results if r['follower'] not in universal_followers]
        print(f"    过滤后: {len(results)} 条 (去掉 {original_count - len(results)} 条)")

    # 2. 双向信号过滤
    if remove_bidirectional:
        # 建立 (leader, follower, lag) → result 的索引
        pair_map = {}
        for r in results:
            pair_map[(r['leader'], r['follower'], r['lag'])] = r

        bidirectional_pairs = set()
        for r in results:
            reverse_key = (r['follower'], r['leader'], r['lag'])
            if reverse_key in pair_map:
                # 标记这对为双向
                pair_key = tuple(sorted([r['leader'], r['follower']])) + (r['lag'],)
                bidirectional_pairs.add(pair_key)

        if bidirectional_pairs:
            # 只保留单向的 (保留 lift 更高的那个方向)
            remove_set = set()
            for pair_key in bidirectional_pairs:
                a, b, lag = pair_key[0], pair_key[1], pair_key[2]
                fwd = pair_map.get((a, b, lag))
                rev = pair_map.get((b, a, lag))
                if fwd and rev:
                    # 剔除 lift 较低的那个方向
                    if fwd['lift'] >= rev['lift']:
                        remove_set.add((b, a, lag))
                    else:
                        remove_set.add((a, b, lag))

            before = len(results)
            results = [r for r in results if (r['leader'], r['follower'], r['lag']) not in remove_set]
            print(f"\n  后过滤: 双向信号 (A→B 且 B→A)")
            print(f"    发现 {len(bidirectional_pairs)} 对双向关系")
            print(f"    剔除较弱方向: {before - len(results)} 条")
            print(f"    过滤后: {len(results)} 条")

    print(f"\n  后过滤总计: {original_count} → {len(results)} 条")
    return results


# ============================================================
# 时间稳定性检验
# ============================================================

def check_stability(series, dates, max_lag=3, heat_quantile=0.7,
                    sector_types=None, exclude_concepts=None, min_a_hot=10,
                    min_stable_lift=1.8, max_lift_ratio=2.0):
    """
    将时间分为前半/后半, 看同一关系在两段是否都成立

    稳定标准:
      - 两段 lift 都 >= min_stable_lift (默认1.8)
      - 两段 lift 比值 <= max_lift_ratio (默认2.0, 即波动不超过2倍)
      - 两段 CP 都 >= 0.20
    """
    n = len(dates)
    mid = n // 2
    dates_early = dates[:mid]
    dates_late = dates[mid:]

    print(f"\n  前半段: {dates_early[0]} ~ {dates_early[-1]} ({len(dates_early)}天)")
    print(f"  后半段: {dates_late[0]} ~ {dates_late[-1]} ({len(dates_late)}天)")
    print(f"  稳定标准: 两段 lift≥{min_stable_lift}, lift比值≤{max_lift_ratio}x, 两段 CP≥0.20")

    def _eval_half(dates_half):
        lookup = build_lookup(series, dates_half)
        n_dates = len(dates_half)

        if exclude_concepts is None:
            exclude_concepts_local = set()
        else:
            exclude_concepts_local = exclude_concepts

        thresholds = {}
        for key, data in lookup.items():
            stype, sname = key
            if sector_types is not None and stype not in sector_types:
                continue
            if exclude_concepts_local and any(exc in sname for exc in exclude_concepts_local):
                continue
            heats = [v[1] for v in data.values() if v[1] > 0]
            if len(heats) >= 10:
                thresholds[key] = np.percentile(heats, heat_quantile * 100)

        valid_keys = list(thresholds.keys())
        n_sectors = len(valid_keys)

        hot_matrix = np.zeros((n_sectors, n_dates), dtype=np.float32)
        heat_matrix = np.zeros((n_sectors, n_dates), dtype=np.float32)

        for i, key in enumerate(valid_keys):
            data = lookup[key]
            thresh = thresholds[key]
            for j, d in enumerate(dates_half):
                if d in data:
                    heat = data[d][1]
                    heat_matrix[i, j] = heat
                    if heat >= thresh:
                        hot_matrix[i, j] = 1.0

        half_results = {}
        for lag in range(1, max_lag + 1):
            a_hot = hot_matrix[:, :n_dates - lag]
            b_hot = hot_matrix[:, lag:]
            a_hot_count = a_hot.sum(axis=1)
            both_hot = a_hot @ b_hot.T
            b_hot_count = b_hot.sum(axis=1)
            total = n_dates - lag

            safe_a_count = np.maximum(a_hot_count, 1)
            cond_prob = both_hot / safe_a_count[:, None]
            base_prob = b_hot_count / total
            safe_base = np.maximum(base_prob, 0.001)
            lift = cond_prob / safe_base[None, :]

            mask = (
                (a_hot_count[:, None] >= min_a_hot) &
                (np.arange(n_sectors)[:, None] != np.arange(n_sectors)[None, :]) &
                (cond_prob > 0.15) &
                (lift > min_stable_lift)
            )
            pairs_i, pairs_j = np.where(mask)

            for idx in range(len(pairs_i)):
                i, j = int(pairs_i[idx]), int(pairs_j[idx])
                key_pair = (valid_keys[i], valid_keys[j], lag)
                half_results[key_pair] = {
                    'lift': float(lift[i, j]),
                    'cond_prob': float(cond_prob[i, j]),
                    'n_samples': int(both_hot[i, j]),
                }
        return half_results

    print("  计算前半段...")
    early = _eval_half(dates_early)
    print(f"    前半段信号: {len(early)} 个")
    print("  计算后半段...")
    late = _eval_half(dates_late)
    print(f"    后半段信号: {len(late)} 个")

    # 找交集
    common_keys = set(early.keys()) & set(late.keys())
    print(f"  两段都出现的信号: {len(common_keys)} 个")

    stable = []
    for key_pair in common_keys:
        e, l = early[key_pair], late[key_pair]
        leader, follower, lag = key_pair

        lift_e, lift_l = e['lift'], l['lift']
        cp_e, cp_l = e['cond_prob'], l['cond_prob']

        # 严格稳定性标准
        if lift_e < min_stable_lift or lift_l < min_stable_lift:
            continue
        if cp_e < 0.20 or cp_l < 0.20:
            continue

        # lift 比值: max/min 不能超过阈值
        lift_ratio = max(lift_e, lift_l) / max(min(lift_e, lift_l), 0.01)
        if lift_ratio > max_lift_ratio:
            continue

        stable.append({
            'leader': leader[1],
            'follower': follower[1],
            'lag': lag,
            'lift_early': round(lift_e, 2),
            'lift_late': round(lift_l, 2),
            'lift_ratio': round(lift_ratio, 2),
            'cp_early': round(cp_e, 4),
            'cp_late': round(cp_l, 4),
            'n_early': e['n_samples'],
            'n_late': l['n_samples'],
            'avg_lift': round((lift_e + lift_l) / 2, 2),
        })

    # 按平均 lift 排序
    stable.sort(key=lambda x: -x['avg_lift'])

    if stable:
        print(f"\n  ✅ 稳定信号 (两段 lift≥{min_stable_lift}, 比值≤{max_lift_ratio}x, CP≥20%): {len(stable)} 个")
        print(f"\n  {'领先板块':<25} → {'跟随板块':<25} {'lag':>3} {'前半lift':>8} {'后半lift':>8} {'比值':>5} {'前半CP':>6} {'后半CP':>6} {'样本':>8}")
        print(f"  {'-'*115}")
        for s in stable[:25]:
            print(f"  {s['leader']:<25} → {s['follower']:<25} {s['lag']:>3}天 "
                  f"{s['lift_early']:>7.1f}x {s['lift_late']:>7.1f}x {s['lift_ratio']:>4.1f} "
                  f"{s['cp_early']:>5.1%} {s['cp_late']:>5.1%} {s['n_early']:>3}/{s['n_late']:<3}")

        # 统计
        avg_ratio = sum(s['lift_ratio'] for s in stable) / len(stable)
        print(f"\n  平均 lift 比值: {avg_ratio:.2f} (越接近1越稳定)")
    else:
        print(f"\n  ❌ 没有满足严格稳定标准的信号")
        # 降级: 显示"半稳定"的 (两段 lift>1.5 但比值可能大)
        semi = []
        for key_pair in common_keys:
            e, l = early[key_pair], late[key_pair]
            leader, follower, lag = key_pair
            if e['lift'] > 1.5 and l['lift'] > 1.5:
                ratio = max(e['lift'], l['lift']) / max(min(e['lift'], l['lift']), 0.01)
                semi.append({
                    'leader': leader[1], 'follower': follower[1], 'lag': lag,
                    'lift_early': e['lift'], 'lift_late': l['lift'],
                    'lift_ratio': ratio,
                    'cp_early': e['cond_prob'], 'cp_late': l['cond_prob'],
                })
        semi.sort(key=lambda x: x['lift_ratio'])
        if semi:
            print(f"  降级显示 (两段 lift>1.5, 按稳定性排序): {len(semi)} 个")
            print(f"\n  {'领先板块':<25} → {'跟随板块':<25} {'lag':>3} {'前半lift':>8} {'后半lift':>8} {'比值':>5}")
            print(f"  {'-'*90}")
            for s in semi[:15]:
                print(f"  {s['leader']:<25} → {s['follower']:<25} {s['lag']:>3}天 "
                      f"{s['lift_early']:>7.1f}x {s['lift_late']:>7.1f}x {s['lift_ratio']:>4.1f}")

    return stable


# ============================================================
# 输出分析
# ============================================================

def print_evaluation(results, top_n=30):
    if not results:
        print("无有效结果"); return

    # BH-FDR 校正
    pvals = [r['p_value'] for r in results]
    rejected = bh_fdr(pvals, alpha=0.05)
    n_sig = rejected.sum()
    print(f"\n  BH-FDR 校正 (α=0.05): {n_sig}/{len(results)} 条显著")

    # 只保留 FDR 显著的
    sig_results = [r for r, sig in zip(results, rejected) if sig]
    if not sig_results:
        print("  FDR 校正后无显著关系, 降低阈值显示全部...")
        sig_results = results

    # 按 "提升度 × log(样本量)" 排序, 惩罚小样本
    for r in sig_results:
        r['score'] = r['lift'] * math.log(max(r['n_samples'], 2))
    by_score = sorted(sig_results, key=lambda x: -x['score'])

    print(f"\n{'='*120}")
    print(f"# 板块自耦合评估 — 综合排序 Top {min(top_n, len(by_score))}  (提升度×log样本量)")
    print(f"{'='*120}")
    print(f"  {'领先板块':<25} → {'跟随板块':<25} {'lag':>3} {'条件概率':>8} {'基线':>6} {'提升度':>6} {'热度增':>6} {'样本':>5} {'A热天':>5} {'p值':>8}")
    print(f"  {'-'*120}")
    for r in by_score[:top_n]:
        print(f"  {r['leader']:<25} → {r['follower']:<25} {r['lag']:>3}天 "
              f"{r['cond_prob']:>7.1%} {r['base_prob']:>5.1%} {r['lift']:>5.1f}x "
              f"{r['heat_boost']:>+5.1f} {r['n_samples']:>5} {r['n_a_hot']:>5} {r['p_value']:>8.4f}")

    # 整体统计 (只用 FDR 显著的)
    print(f"\n{'='*120}")
    print(f"# 整体统计 (FDR显著)")
    print(f"{'='*120}")
    lifts = [r['lift'] for r in sig_results]
    cps = [r['cond_prob'] for r in sig_results]
    scores = [r['score'] for r in sig_results]
    print(f"  显著关系数: {len(sig_results)}")
    print(f"  提升度: min={min(lifts):.2f} P25={np.percentile(lifts,25):.2f} P50={np.percentile(lifts,50):.2f} P75={np.percentile(lifts,75):.2f} max={max(lifts):.2f}")
    print(f"  条件概率: min={min(cps):.1%} P50={np.percentile(cps,50):.1%} max={max(cps):.1%}")

    # 按 lag 分组
    print(f"\n  按lag分组:")
    for lag in sorted(set(r['lag'] for r in sig_results)):
        seg = [r for r in sig_results if r['lag'] == lag]
        avg_lift = np.mean([r['lift'] for r in seg])
        avg_cp = np.mean([r['cond_prob'] for r in seg])
        n_lift_gt1 = sum(1 for r in seg if r['lift'] > 1.0)
        pct = n_lift_gt1 / len(seg) * 100
        print(f"    lag={lag}: {len(seg):>5}条  平均提升度{avg_lift:.2f}x  平均条件概率{avg_cp:.1%}  提升度>1占比{pct:.0f}%")

    # 提升度分布
    print(f"\n  提升度分布 (FDR显著):")
    for lo, hi, label in [(0,0.5,'<0.5x(反向)'), (0.5,0.8,'0.5-0.8x(弱)'), (0.8,1.2,'0.8-1.2x(无效果)'), (1.2,2.0,'1.2-2.0x(有效)'), (2.0,5.0,'2.0-5.0x(强)'), (5.0,999,'5.0+x(超强)')]:
        seg = [r for r in sig_results if lo <= r['lift'] < hi]
        if seg:
            print(f"    {label}: {len(seg):>5}条 ({len(seg)/len(sig_results)*100:.0f}%)")

    # 可靠信号 (综合排序前20)
    print(f"\n{'='*120}")
    print(f"# 可靠信号 Top 20 (FDR显著, 按综合得分排序)")
    print(f"{'='*120}")
    for r in by_score[:20]:
        print(f"  {r['leader']:<25} → {r['follower']:<25} lag={r['lag']}天 "
              f"提升{r['lift']:.1f}x 条件概率{r['cond_prob']:.0%} 基线{r['base_prob']:.0%} "
              f"样本{r['n_samples']}/{r['n_a_hot']} p={r['p_value']:.4f}")

    # 反向信号
    print(f"\n{'='*120}")
    print(f"# 反向信号 (FDR显著, 提升度<0.5x)")
    print(f"{'='*120}")
    reverse = [r for r in sig_results if r['lift'] < 0.5 and r['n_samples'] >= 10]
    reverse.sort(key=lambda x: x['lift'])
    if reverse:
        for r in reverse[:10]:
            print(f"  {r['leader']:<25} → {r['follower']:<25} lag={r['lag']}天 "
                  f"提升{r['lift']:.1f}x 条件概率{r['cond_prob']:.0%} 基线{r['base_prob']:.0%}")
    else:
        print(f"  无显著反向信号")


def main():
    parser = argparse.ArgumentParser(description="板块自耦合准确率评估 (v2)")
    parser.add_argument("--start", type=str, default="2024-01-01")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--max-lag", type=int, default=5)
    parser.add_argument("--heat-quantile", type=float, default=0.7, help="热度阈值分位数")
    parser.add_argument("--min-coverage", type=float, default=0.5)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--sector-types", type=str, default="industry,concept",
                        help="分析的板块类型, 逗号分隔 (默认: industry,concept)")
    parser.add_argument("--exclude-concept", type=str, default="次新股",
                        help="排除的概念关键词, 逗号分隔 (默认: 次新股)")
    parser.add_argument("--min-a-hot", type=int, default=20,
                        help="板块A最少热天数 (默认20)")
    parser.add_argument("--min-cp", type=float, default=0.20,
                        help="最低条件概率 (默认0.20)")
    parser.add_argument("--min-lift", type=float, default=1.5,
                        help="最低提升度 (默认1.5)")
    parser.add_argument("--min-samples", type=int, default=5,
                        help="最少共同热天样本数 (默认5)")
    parser.add_argument("--max-follower-pct", type=float, default=0.10,
                        help="剔除被超过此比例leader指向的follower (默认0.10)")
    parser.add_argument("--remove-bidir", action="store_true", default=True,
                        help="剔除双向信号中的较弱方向 (默认开启)")
    parser.add_argument("--keep-bidir", action="store_true",
                        help="保留双向信号 (不剔除)")
    parser.add_argument("--min-stable-lift", type=float, default=1.8,
                        help="稳定性检验: 两段最低lift (默认1.8)")
    parser.add_argument("--max-lift-ratio", type=float, default=2.0,
                        help="稳定性检验: 两段lift最大比值 (默认2.0)")
    parser.add_argument("--stability", action="store_true",
                        help="运行时间稳定性检验")
    parser.add_argument("--fdr-alpha", type=float, default=0.05,
                        help="BH-FDR 显著性阈值 (默认0.05)")
    parser.add_argument("--all-pairs", action="store_true",
                        help="不过滤板块类型, 跑全量 (慢)")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    if args.all_pairs or not args.sector_types:
        sector_types = None  # 不过滤
    else:
        sector_types = set(args.sector_types.split(','))
    exclude_concepts = set(args.exclude_concept.split(',')) if args.exclude_concept else set()
    if args.keep_bidir:
        args.remove_bidir = False

    print(f"[1/3] 加载板块历史 ({args.start} ~ {end_date})...")
    series, dates = load_sector_daily(args.start, end_date)
    if not series or not dates:
        print("⚠️  数据不足"); return

    print(f"\n[2/3] 计算自耦合 (max_lag={args.max_lag}, heat_quantile={args.heat_quantile}, "
          f"min_a_hot={args.min_a_hot}, min_cp={args.min_cp}, min_lift={args.min_lift}, "
          f"min_samples={args.min_samples})...")
    results = evaluate_self_coupling(
        series, dates, args.max_lag, args.heat_quantile, args.min_coverage,
        sector_types=sector_types, exclude_concepts=exclude_concepts,
        min_a_hot=args.min_a_hot, min_cp=args.min_cp,
        min_lift=args.min_lift, min_samples=args.min_samples,
    )

    # 后过滤: 万能接收器 + 双向信号
    results = post_filter(results, max_follower_pct=args.max_follower_pct,
                          remove_bidirectional=args.remove_bidir)

    print(f"\n[3/3] 输出评估...")
    print_evaluation(results, args.top)

    # 稳定性检验
    if args.stability:
        print(f"\n{'#'*120}")
        print(f"# 时间稳定性检验")
        print(f"{'#'*120}")
        check_stability(series, dates, max_lag=min(3, args.max_lag),
                        heat_quantile=args.heat_quantile,
                        sector_types=sector_types, exclude_concepts=exclude_concepts,
                        min_a_hot=max(10, args.min_a_hot // 2),
                        min_stable_lift=args.min_stable_lift,
                        max_lift_ratio=args.max_lift_ratio)

    # 导出
    out_path = "sector_self_coupling_eval.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 {out_path} ({len(results)}条)")


if __name__ == "__main__":
    main()
