#!/usr/bin/env python3
"""
板块热度脉冲分析

核心思路:
  不看板块间关系，只看每个板块自己的"热脉冲"——
  什么时候热起来、持续多久、多强、怎么衰减的。

脉冲定义:
  连续 N 天热度 >= 阈值 (默认70分位), 算一次脉冲
  中间断1天允许gap-fill (避免因单天噪声把一个脉冲切成两半)

输出:
  1. 每个板块的脉冲历史 (起止日期、持续天数、峰值、均值)
  2. 脉冲类型分布 (1-2天闪热 / 3-7天短波 / 8-30天中波 / 30天+长趋势)
  3. 脉冲衰减模式 (从峰值跌回阈值的速度)
  4. 当前活跃脉冲 (正在发生的)
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
from eval_coupling_v2 import load_sector_daily, build_lookup


# ============================================================
# 脉冲检测
# ============================================================

def detect_pulses(heats_by_date, dates, threshold, max_gap=1):
    """
    检测热脉冲

    参数:
      heats_by_date: {date_str: heat_score}
      dates: 有序日期列表
      threshold: 热度阈值
      max_gap: 允许的最大断档天数 (gap-fill)

    返回:
      脉冲列表, 每个脉冲 = {
        start, end, duration, peak, mean, 
        peak_date, heats: [每日热度],
        rise_days, decay_days, decay_rate
      }
    """
    n = len(dates)
    # 标记每天是否"热"
    is_hot = [heats_by_date.get(d, 0) >= threshold for d in dates]
    heats = [heats_by_date.get(d, 0) for d in dates]

    pulses = []
    i = 0

    while i < n:
        if not is_hot[i]:
            i += 1
            continue

        # 找脉冲起点
        start = i
        j = i
        last_hot = i

        while j < n:
            if is_hot[j]:
                last_hot = j
                j += 1
            elif j - last_hot <= max_gap:
                # gap-fill: 断档在允许范围内, 继续
                j += 1
            else:
                break

        end = last_hot  # 脉冲最后一天热的日期

        # 提取脉冲期间的热度
        pulse_heats = heats[start:end + 1]
        hot_heats = [h for h in pulse_heats if h >= threshold]

        if len(hot_heats) < 1:
            i = j
            continue

        duration = end - start + 1
        peak = max(pulse_heats)
        mean_heat = np.mean(hot_heats)
        peak_idx_local = pulse_heats.index(peak)
        peak_date = dates[start + peak_idx_local]

        # 上升期: 从起点到峰值
        rise_days = peak_idx_local
        # 衰减期: 从峰值到脉冲结束
        decay_days = duration - peak_idx_local - 1

        # 衰减率: 峰值到结束, 每天平均跌多少
        if decay_days > 0:
            decay_rate = (peak - pulse_heats[-1]) / decay_days
        else:
            decay_rate = 0

        pulses.append({
            'start': dates[start],
            'end': dates[end],
            'duration': duration,
            'peak': round(peak, 1),
            'mean': round(mean_heat, 1),
            'peak_date': peak_date,
            'rise_days': rise_days,
            'decay_days': decay_days,
            'decay_rate': round(decay_rate, 2),
            'heats': [round(h, 1) for h in pulse_heats],
            'hot_days': len(hot_heats),
            'gap_days': duration - len(hot_heats),
        })

        i = j

    return pulses


# ============================================================
# 脉冲分类
# ============================================================

def classify_pulse(duration):
    """按持续时间分类"""
    if duration <= 2:
        return "闪热(1-2天)"
    elif duration <= 7:
        return "短波(3-7天)"
    elif duration <= 30:
        return "中波(8-30天)"
    else:
        return "长趋势(30天+)"


# ============================================================
# 衰减模式分析
# ============================================================

def analyze_decay_patterns(all_pulses):
    """
    分析脉冲衰减模式:
      1. 从峰值跌回阈值的速度
      2. 是否有"二次冲顶"
    """
    patterns = {
        "急涨急跌": 0,    # rise<=1, decay<=2
        "急涨缓跌": 0,    # rise<=1, decay>2
        "缓涨急跌": 0,    # rise>1, decay<=2
        "缓涨缓跌": 0,    # rise>1, decay>2
        "对称": 0,        # rise ≈ decay
    }

    for p in all_pulses:
        r, d = p['rise_days'], p['decay_days']
        if r <= 1 and d <= 2:
            patterns["急涨急跌"] += 1
        elif r <= 1 and d > 2:
            patterns["急涨缓跌"] += 1
        elif r > 1 and d <= 2:
            patterns["缓涨急跌"] += 1
        elif r > 1 and d > 2:
            patterns["缓涨缓跌"] += 1
        if abs(r - d) <= 1 and r > 0:
            patterns["对称"] += 1

    return patterns


# ============================================================
# 主分析
# ============================================================

def analyze_all(series, dates, heat_quantile=0.70, max_gap=1,
                sector_types=None, exclude_concepts=None):
    """对所有板块做脉冲分析"""

    lookup = build_lookup(series, dates)

    # 类型过滤
    if sector_types is None:
        sector_types_filter = None
    else:
        sector_types_filter = set(sector_types.split(','))

    exclude = set(exclude_concepts.split(',')) if exclude_concepts else set()

    results = {}  # sector_name → {threshold, pulses, stats}

    for (stype, sname), data in lookup.items():
        if sector_types_filter and stype not in sector_types_filter:
            continue
        if any(e in sname for e in exclude):
            continue

        heats_by_date = {str(d)[:10]: v[1] for d, v in data.items()}
        heat_vals = [v for v in heats_by_date.values() if v > 0]
        if len(heat_vals) < 30:
            continue

        threshold = np.percentile(heat_vals, heat_quantile * 100)
        pulses = detect_pulses(heats_by_date, dates, threshold, max_gap)

        if not pulses:
            continue

        # 统计
        durations = [p['duration'] for p in pulses]
        peaks = [p['peak'] for p in pulses]

        type_dist = defaultdict(int)
        for p in pulses:
            type_dist[classify_pulse(p['duration'])] += 1

        # 当前状态
        last_pulse = pulses[-1]
        is_active = last_pulse['end'] == dates[-1] or \
                    (len(dates) >= 2 and last_pulse['end'] == dates[-2])

        results[sname] = {
            'type': stype,
            'threshold': round(threshold, 1),
            'n_pulses': len(pulses),
            'avg_duration': round(np.mean(durations), 1),
            'median_duration': round(np.median(durations), 1),
            'max_duration': max(durations),
            'avg_peak': round(np.mean(peaks), 1),
            'max_peak': max(peaks),
            'type_distribution': dict(type_dist),
            'pulses': pulses,
            'is_active': is_active,
            'last_pulse': last_pulse,
        }

    return results


# ============================================================
# 输出
# ============================================================

def print_report(results, dates):
    """输出分析报告"""

    print(f"\n{'='*100}")
    print(f"# 板块热度脉冲分析  (数据范围: {dates[0]} ~ {dates[-1]}, {len(dates)}天)")
    print(f"{'='*100}")

    # 1. 按脉冲数量排序
    by_count = sorted(results.items(), key=lambda x: -x[1]['n_pulses'])

    print(f"\n## 1. 板块脉冲概览 (共 {len(results)} 个板块)")
    print(f"\n  {'板块':<20} {'类型':<8} {'脉冲数':>6} {'平均持续':>8} {'最长':>6} {'平均峰值':>8} {'最高峰值':>8} {'当前活跃':>8}")
    print(f"  {'-'*90}")
    for name, r in by_count[:40]:
        active = "🔥" if r['is_active'] else ""
        print(f"  {name:<20} {r['type']:<8} {r['n_pulses']:>6} {r['avg_duration']:>7.1f}天 "
              f"{r['max_duration']:>5}天 {r['avg_peak']:>7.1f} {r['max_peak']:>7.1f} {active:>6}")

    # 2. 脉冲类型分布 (全局)
    print(f"\n## 2. 脉冲类型分布 (全局)")
    global_types = defaultdict(int)
    for r in results.values():
        for t, c in r['type_distribution'].items():
            global_types[t] += c

    total_pulses = sum(global_types.values())
    for t in ["闪热(1-2天)", "短波(3-7天)", "中波(8-30天)", "长趋势(30天+)"]:
        cnt = global_types.get(t, 0)
        pct = cnt / total_pulses * 100 if total_pulses else 0
        bar = "█" * int(pct / 2)
        print(f"  {t:<18} {cnt:>5} ({pct:>5.1f}%) {bar}")

    # 3. 衰减模式
    print(f"\n## 3. 脉冲衰减模式")
    all_pulses = []
    for r in results.values():
        all_pulses.extend(r['pulses'])

    patterns = analyze_decay_patterns(all_pulses)
    for name, cnt in sorted(patterns.items(), key=lambda x: -x[1]):
        pct = cnt / len(all_pulses) * 100 if all_pulses else 0
        bar = "█" * int(pct / 2)
        print(f"  {name:<12} {cnt:>5} ({pct:>5.1f}%) {bar}")

    # 4. 长趋势板块 (持续30天+的脉冲)
    print(f"\n## 4. 长趋势板块 (有过30天+持续脉冲)")
    long_trend_sectors = []
    for name, r in results.items():
        for p in r['pulses']:
            if p['duration'] >= 30:
                long_trend_sectors.append((name, p))
    long_trend_sectors.sort(key=lambda x: -x[1]['duration'])

    if long_trend_sectors:
        for name, p in long_trend_sectors[:20]:
            print(f"  {name:<20} {p['start']}~{p['end']} ({p['duration']:>3}天) "
                  f"峰值={p['peak']:.0f} 均值={p['mean']:.0f} "
                  f"上升={p['rise_days']}天 衰减={p['decay_days']}天")
    else:
        print(f"  无")

    # 5. 闪热板块 (1-2天就结束)
    print(f"\n## 5. 闪热板块 (近期1-2天脉冲, 可能是假信号)")
    flash_sectors = []
    for name, r in results.items():
        for p in r['pulses'][-5:]:  # 只看最近5个脉冲
            if p['duration'] <= 2:
                flash_sectors.append((name, p))
    flash_sectors.sort(key=lambda x: -x[1]['peak'])

    if flash_sectors:
        for name, p in flash_sectors[:20]:
            print(f"  {name:<20} {p['start']} 峰值={p['peak']:.0f} (只热了{p['duration']}天)")
    else:
        print(f"  无")

    # 6. 当前活跃脉冲 (正在进行的)
    print(f"\n## 6. 当前活跃脉冲 🔥")
    active = [(name, r) for name, r in results.items() if r['is_active']]
    active.sort(key=lambda x: -x[1]['last_pulse']['peak'])

    if active:
        print(f"\n  {'板块':<20} {'起始':>12} {'已持续':>8} {'当前热度':>8} {'峰值':>8} {'上升':>6} {'衰减':>6}")
        print(f"  {'-'*85}")
        for name, r in active:
            lp = r['last_pulse']
            print(f"  {name:<20} {lp['start']:>12} {lp['duration']:>7}天 "
                  f"{lp['heats'][-1]:>7.1f} {lp['peak']:>7.1f} "
                  f"{lp['rise_days']:>5}天 {lp['decay_days']:>5}天")
    else:
        print(f"  当前无活跃脉冲")

    # 7. 按板块的脉冲详细历史 (选几个典型)
    print(f"\n## 7. 典型板块脉冲历史 (脉冲数最多的10个)")
    for name, r in by_count[:10]:
        print(f"\n  [{name}] 阈值={r['threshold']:.0f}, 共{r['n_pulses']}个脉冲:")
        for i, p in enumerate(r['pulses'][-10:]):  # 最近10个
            duration_bar = "█" * min(p['duration'], 60)
            active_mark = " ← 当前" if p == r['last_pulse'] and r['is_active'] else ""
            print(f"    #{len(r['pulses'])-min(10,len(r['pulses']))+i+1:>3} "
                  f"{p['start']}~{p['end']} ({p['duration']:>3}天) "
                  f"峰值={p['peak']:>5.1f} 均值={p['mean']:>5.1f} "
                  f"{duration_bar}{active_mark}")

    # 8. 板块持续时长排名
    print(f"\n## 8. 板块「耐力」排名 (平均脉冲持续时间)")
    by_avg_dur = sorted(results.items(), key=lambda x: -x[1]['avg_duration'])
    print(f"\n  {'板块':<20} {'平均持续':>8} {'最长持续':>8} {'脉冲数':>6}")
    print(f"  {'-'*50}")
    for name, r in by_avg_dur[:20]:
        print(f"  {name:<20} {r['avg_duration']:>7.1f}天 {r['max_duration']:>7}天 {r['n_pulses']:>6}")


def export_json(results, path="sector_pulse_analysis.json"):
    """导出JSON (精简版: 只保留摘要 + 最近10个脉冲)"""
    export = {}
    for name, r in results.items():
        # 只保留最近10个脉冲, 且去掉 heats 数组
        recent_pulses = []
        for p in r['pulses'][-10:]:
            recent_pulses.append({
                'start': p['start'], 'end': p['end'],
                'duration': p['duration'], 'peak': p['peak'],
                'mean': p['mean'], 'peak_date': p['peak_date'],
                'rise_days': p['rise_days'], 'decay_days': p['decay_days'],
            })
        export[name] = {
            'type': r['type'],
            'threshold': r['threshold'],
            'n_pulses': r['n_pulses'],
            'avg_duration': r['avg_duration'],
            'max_duration': r['max_duration'],
            'avg_peak': r['avg_peak'],
            'is_active': r['is_active'],
            'type_distribution': r['type_distribution'],
            'recent_pulses': recent_pulses,
        }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n💾 导出: {path}")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="板块热度脉冲分析")
    parser.add_argument("--start", type=str, default="2024-01-01")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--heat-quantile", type=float, default=0.70)
    parser.add_argument("--max-gap", type=int, default=1,
                        help="脉冲内允许的最大断档天数 (默认1)")
    parser.add_argument("--sector-types", type=str, default="industry,concept")
    parser.add_argument("--exclude-concept", type=str, default="次新股")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    print(f"[1/2] 加载数据 ({args.start} ~ {end_date})...")
    series, dates = load_sector_daily(args.start, end_date)
    if not series or not dates:
        print("⚠️  数据不足"); return

    print(f"\n[2/2] 脉冲分析 (quantile={args.heat_quantile}, max_gap={args.max_gap})...")
    results = analyze_all(
        series, dates, args.heat_quantile, args.max_gap,
        sector_types=args.sector_types,
        exclude_concepts=args.exclude_concept,
    )

    print_report(results, dates)

    if args.export:
        export_json(results)


if __name__ == "__main__":
    main()
