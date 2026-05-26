#!/usr/bin/env python3
"""
每日板块扫描 — 收盘后跑一遍, 输出当天值得关注的板块

输出:
  1. 🔥 新热点: 今天刚进入上升期的板块 (脉冲第1天)
  2. 📈 上升中: 已持续2-4天, 还在爬升的板块
  3. ⚠️ 衰减中: 曾经热过, 现在开始降温的板块
  4. 🚀 异常板块: 峰值远超历史均值
  5. ❄️ 今日降温: 昨天还热, 今天跌出脉冲的板块

用法:
  python daily_scan.py                   # 默认扫描
  python daily_scan.py --quantile 0.75   # 提高阈值, 更严格
  python daily_scan.py --only anomaly    # 只看异常板块
"""
import sys, json, argparse, os
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

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
from collections import defaultdict


# ============================================================
# 数据加载 (独立, 不依赖外部模块)
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


# ============================================================
# 核心: 脉冲检测 (轻量版, 只看最近状态)
# ============================================================

def get_sector_status(lookup, dates, heat_quantile=0.70, max_gap=1):
    """
    对每个板块, 只计算最近的状态:
    - is_active: 当前是否在脉冲中
    - pulse_day: 当前脉冲持续第几天
    - pulse_phase: rising / peak / decay
    - current_heat: 今天热度
    - pulse_peak: 当前脉冲峰值
    - historical_avg_peak: 历史脉冲平均峰值
    - anomaly_ratio: 当前峰值/历史均值
    - was_active_yesterday: 昨天是否在脉冲中
    - just_started: 今天刚进入脉冲
    - just_ended: 今天刚退出脉冲
    """
    today = dates[-1]
    yesterday = dates[-2] if len(dates) >= 2 else None

    results = {}

    for (stype, sname), data in lookup.items():
        # 跳过机制性板块
        if any(kw in sname for kw in ['融资融券', '沪股通', '深股通']):
            continue

        heats_by_date = {str(d)[:10]: v[1] for d, v in data.items()}
        heat_vals = [v for v in heats_by_date.values() if v > 0]
        if len(heat_vals) < 30:
            continue

        threshold = np.percentile(heat_vals, heat_quantile * 100)

        # 今天和昨天的热度
        today_heat = heats_by_date.get(today, 0)
        yesterday_heat = heats_by_date.get(yesterday, 0) if yesterday else 0

        # 历史脉冲统计
        all_peaks = []
        # 简化: 只扫描最近60天找脉冲状态
        recent_dates = dates[-60:]
        is_hot = [heats_by_date.get(d, 0) >= threshold for d in recent_dates]
        heats_recent = [heats_by_date.get(d, 0) for d in recent_dates]

        # 找当前脉冲 (从今天往回看)
        n = len(recent_dates)
        pulse_end = n - 1  # 最后一天
        pulse_start = None

        # 今天不在脉冲中
        if not is_hot[n - 1]:
            # 检查昨天是否在脉冲中
            was_in_pulse = False
            if n >= 2 and is_hot[n - 2]:
                # 往回找昨天的脉冲起点
                j = n - 2
                last_hot = j
                while j >= 0:
                    if is_hot[j]:
                        last_hot = j
                        j -= 1
                    elif j >= 1 and not is_hot[j] and is_hot[j - 1]:
                        j -= 1  # gap=1 允许
                    else:
                        break
                was_in_pulse = True
                pulse_start = j + 1 if j >= 0 else 0

            results[sname] = {
                'type': stype,
                'is_active': False,
                'just_ended': was_in_pulse and today_heat < threshold,
                'was_active_yesterday': was_in_pulse,
                'current_heat': round(today_heat, 1),
                'threshold': round(threshold, 1),
                'pulse_day': 0,
                'pulse_phase': None,
                'pulse_peak': 0,
                'historical_avg_peak': round(np.mean([h for h in heat_vals if h >= threshold]), 1) if any(h >= threshold for h in heat_vals) else 0,
                'anomaly_ratio': 0,
            }
            continue

        # 今天在脉冲中, 往回找起点
        j = n - 1
        last_hot = j
        while j >= 0:
            if is_hot[j]:
                last_hot = j
                j -= 1
            elif j >= 1 and not is_hot[j] and is_hot[j - 1]:
                j -= 1  # gap-fill
            else:
                break
        pulse_start = last_hot

        # 脉冲热度
        pulse_heats = heats_recent[pulse_start:n]
        pulse_peak = max(pulse_heats)
        pulse_day = n - pulse_start

        # 判断阶段
        peak_idx = pulse_heats.index(pulse_peak)
        if pulse_day <= 2:
            phase = 'rising'
        elif peak_idx >= len(pulse_heats) - 2 and pulse_heats[-1] >= pulse_heats[-2]:
            phase = 'rising'
        elif pulse_heats[-1] < pulse_peak * 0.9:
            phase = 'decay'
        else:
            phase = 'peak'

        # 历史脉冲平均峰值 (简化: 用所有超阈值天的均值)
        above_thresh = [h for h in heat_vals if h >= threshold]
        hist_avg_peak = np.mean(above_thresh) if above_thresh else threshold

        # 是否今天刚进入
        just_started = (pulse_day == 1)

        results[sname] = {
            'type': stype,
            'is_active': True,
            'just_started': just_started,
            'just_ended': False,
            'current_heat': round(today_heat, 1),
            'threshold': round(threshold, 1),
            'pulse_day': pulse_day,
            'pulse_phase': phase,
            'pulse_peak': round(pulse_peak, 1),
            'historical_avg_peak': round(hist_avg_peak, 1),
            'anomaly_ratio': round(pulse_peak / hist_avg_peak, 2) if hist_avg_peak > 0 else 0,
        }

    return results


# ============================================================
# 过滤噪声板块
# ============================================================

NOISE_KEYWORDS = [
    '板块', '地区', '地域', '概念', '风格',
    '沪股通', '深股通', '融资融券', '转融券',
    '昨日', '今日', '涨停', '跌停',
]

# 保留的概念板块 (不过滤)
KEEP_KEYWORDS = [
    'AI', '芯片', '半导体', '机器人', '军工', '新能源', '光伏',
    '锂电', '消费电子', '智能', '数据', '算力', '卫星', '无人机',
    '低空', '量子', '存储', '封装', 'OLED', 'PCB', '液冷',
    '苹果', '小米', '华为', '特斯拉', '比亚迪', '储能', '充电',
    '碳中和', '氢能', '核电', '风电', '医疗', '创新药', 'CXO',
    '信创', '网络安全', '云计算', '物联网', '5G', '6G',
]


def is_noise_sector(name):
    """判断是否是噪声板块"""
    # 保留的关键词直接留
    if any(kw in name for kw in KEEP_KEYWORDS):
        return False
    # 纯地区板块
    provinces = ['北京', '上海', '广东', '浙江', '江苏', '山东', '四川', '湖北',
                 '湖南', '福建', '安徽', '河南', '河北', '陕西', '辽宁', '重庆',
                 '天津', '新疆', '西藏', '云南', '贵州', '甘肃', '海南', '广西',
                 '吉林', '黑龙江', '内蒙古', '宁夏', '青海', '江西', '山西']
    if any(p in name for p in provinces):
        return True
    return False


# ============================================================
# 输出
# ============================================================

def print_daily_report(results, dates, top_n=15):
    """输出每日扫描报告"""
    today = dates[-1]
    yesterday = dates[-2] if len(dates) >= 2 else 'N/A'

    # 过滤噪声
    filtered = {k: v for k, v in results.items() if not is_noise_sector(k)}

    print(f"\n{'='*70}")
    print(f"  📊 板块每日扫描  {today}  (前一交易日: {yesterday})")
    print(f"{'='*70}")

    # ---- 1. 新热点 (今天刚进入脉冲) ----
    new_hot = [(n, r) for n, r in filtered.items()
               if r['is_active'] and r.get('just_started')]
    new_hot.sort(key=lambda x: -x[1]['current_heat'])

    print(f"\n  🔥 新热点 (今天刚起来) — {len(new_hot)} 个")
    if new_hot:
        print(f"  {'板块':<20} {'热度':>6} {'阈值':>6} {'类型':<10}")
        print(f"  {'-'*50}")
        for name, r in new_hot[:top_n]:
            print(f"  {name:<20} {r['current_heat']:>5.1f} {r['threshold']:>5.1f} {r['type']:<10}")
    else:
        print(f"  (无)")

    # ---- 2. 上升中 (持续2-4天, 还在涨) ----
    rising = [(n, r) for n, r in filtered.items()
              if r['is_active'] and r['pulse_phase'] == 'rising' and r['pulse_day'] >= 2]
    rising.sort(key=lambda x: -x[1]['anomaly_ratio'])

    print(f"\n  📈 上升中 (持续2-4天, 还在爬) — {len(rising)} 个")
    if rising:
        print(f"  {'板块':<20} {'第几天':>6} {'热度':>6} {'峰值':>6} {'异常':>6}")
        print(f"  {'-'*55}")
        for name, r in rising[:top_n]:
            flag = " 🚀" if r['anomaly_ratio'] >= 1.3 else ""
            print(f"  {name:<20} {r['pulse_day']:>5}天 {r['current_heat']:>5.1f} "
                  f"{r['pulse_peak']:>5.1f} {r['anomaly_ratio']:>5.1f}x{flag}")
    else:
        print(f"  (无)")

    # ---- 3. 异常板块 (峰值远超历史) ----
    anomalies = [(n, r) for n, r in filtered.items()
                 if r['is_active'] and r['anomaly_ratio'] >= 1.3]
    anomalies.sort(key=lambda x: -x[1]['anomaly_ratio'])

    print(f"\n  🚀 异常板块 (峰值≥历史1.3倍) — {len(anomalies)} 个")
    if anomalies:
        print(f"  {'板块':<20} {'峰值':>6} {'历史均值':>8} {'倍数':>6} {'持续':>6} {'阶段':<8}")
        print(f"  {'-'*60}")
        for name, r in anomalies[:top_n]:
            phase_icon = {'rising': '↑', 'peak': '→', 'decay': '↓'}.get(r['pulse_phase'], '?')
            print(f"  {name:<20} {r['pulse_peak']:>5.1f} {r['historical_avg_peak']:>7.1f} "
                  f"{r['anomaly_ratio']:>5.1f}x {r['pulse_day']:>5}天 {phase_icon} {r['pulse_phase']}")
    else:
        print(f"  (无)")

    # ---- 4. 衰减中 ----
    decaying = [(n, r) for n, r in filtered.items()
                if r['is_active'] and r['pulse_phase'] == 'decay']
    decaying.sort(key=lambda x: -x[1]['pulse_day'])

    print(f"\n  ⚠️ 衰减中 (峰值已过, 开始降温) — {len(decaying)} 个")
    if decaying:
        print(f"  {'板块':<20} {'第几天':>6} {'热度':>6} {'峰值':>6} {'回落':>6}")
        print(f"  {'-'*55}")
        for name, r in decaying[:top_n]:
            drop_pct = (r['pulse_peak'] - r['current_heat']) / r['pulse_peak'] * 100 if r['pulse_peak'] > 0 else 0
            print(f"  {name:<20} {r['pulse_day']:>5}天 {r['current_heat']:>5.1f} "
                  f"{r['pulse_peak']:>5.1f} -{drop_pct:.0f}%")
    else:
        print(f"  (无)")

    # ---- 5. 今日降温 ----
    just_ended = [(n, r) for n, r in filtered.items() if r.get('just_ended')]
    just_ended.sort(key=lambda x: -x[1]['current_heat'])

    print(f"\n  ❄️ 今日降温 (昨天还在脉冲, 今天跌出) — {len(just_ended)} 个")
    if just_ended:
        print(f"  {'板块':<20} {'今天热度':>8} {'阈值':>6}")
        print(f"  {'-'*40}")
        for name, r in just_ended[:top_n]:
            print(f"  {name:<20} {r['current_heat']:>7.1f} {r['threshold']:>5.1f}")
    else:
        print(f"  (无)")

    # ---- 汇总 ----
    print(f"\n{'='*70}")
    print(f"  汇总: 新热点 {len(new_hot)} | 上升中 {len(rising)} | "
          f"异常 {len(anomalies)} | 衰减 {len(decaying)} | 降温 {len(just_ended)}")
    print(f"{'='*70}")


def export_json(results, dates, path="daily_scan_result.json"):
    """导出扫描结果"""
    today = dates[-1]
    filtered = {k: v for k, v in results.items() if not is_noise_sector(k)}

    output = {
        'date': today,
        'summary': {
            'new_hot': len([r for r in filtered.values() if r['is_active'] and r.get('just_started')]),
            'rising': len([r for r in filtered.values() if r['is_active'] and r['pulse_phase'] == 'rising' and r['pulse_day'] >= 2]),
            'anomaly': len([r for r in filtered.values() if r['is_active'] and r['anomaly_ratio'] >= 1.3]),
            'decaying': len([r for r in filtered.values() if r['is_active'] and r['pulse_phase'] == 'decay']),
            'just_ended': len([r for r in filtered.values() if r.get('just_ended')]),
        },
        'sectors': filtered,
    }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n💾 导出: {path}")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="每日板块扫描")
    parser.add_argument("--start", type=str, default="",
                        help="数据起始日 (默认: 自动取90天前)")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--quantile", type=float, default=0.70,
                        help="热度阈值分位数 (默认0.70)")
    parser.add_argument("--only", type=str, default="",
                        help="只显示某个分类: new/rising/anomaly/decay/ended")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--top", type=int, default=15, help="每个分类显示数量")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")
    if not args.start:
        start_date = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    else:
        start_date = args.start

    print(f"[1/2] 加载数据 ({start_date} ~ {end_date})...")
    series, dates = load_sector_daily(start_date, end_date)
    if not series or not dates:
        print("⚠️  数据不足"); return

    print(f"\n[2/2] 扫描板块状态...")
    lookup = build_lookup(series, dates)
    results = get_sector_status(lookup, dates, args.quantile)

    print_daily_report(results, dates, args.top)

    if args.export:
        export_json(results, dates)


if __name__ == "__main__":
    main()
