"""
板块每日扫描卡片 — 脉冲检测 + 热点/衰减/异常分析

数据源: sector_daily_stats (PostgreSQL)
输出: 新热点 / 上升中 / 异常板块 / 衰减中 / 今日降温

前端引用: GET /market-cn/cards/daily-scan
"""
from datetime import datetime, timedelta
from collections import defaultdict
from ._base import CardMeta, register

meta = CardMeta(
    id="daily_scan",
    name="板块每日扫描",
    endpoint="/daily-scan",
    refresh_interval=300,
    order=35,
    requires_hub=False,
)

# ============================================================
# 噪声过滤
# ============================================================

NOISE_KEYWORDS = ['融资融券', '沪股通', '深股通', '转融券']

KEEP_KEYWORDS = [
    'AI', '芯片', '半导体', '机器人', '军工', '新能源', '光伏',
    '锂电', '消费电子', '智能', '数据', '算力', '卫星', '无人机',
    '低空', '量子', '存储', '封装', 'OLED', 'PCB', '液冷',
    '苹果', '小米', '华为', '特斯拉', '比亚迪', '储能', '充电',
    '碳中和', '氢能', '核电', '风电', '医疗', '创新药', 'CXO',
    '信创', '网络安全', '云计算', '物联网', '5G', '6G',
]

PROVINCES = [
    '北京', '上海', '广东', '浙江', '江苏', '山东', '四川', '湖北',
    '湖南', '福建', '安徽', '河南', '河北', '陕西', '辽宁', '重庆',
    '天津', '新疆', '西藏', '云南', '贵州', '甘肃', '海南', '广西',
    '吉林', '黑龙江', '内蒙古', '宁夏', '青海', '江西', '山西',
]


def _is_noise(name):
    if any(kw in name for kw in NOISE_KEYWORDS):
        return True
    if any(kw in name for kw in KEEP_KEYWORDS):
        return False
    if any(p in name for p in PROVINCES):
        return True
    return False


# ============================================================
# 数据加载
# ============================================================

def _load_series(start_date, end_date):
    """从数据库加载板块热度数据"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")

    conditions, params = [], []
    if start_date:
        conditions.append("date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("date <= %s")
        params.append(end_date)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with pool.cursor() as cur:
        cur.execute(f"""
            SELECT date, sector_type, sector_name, heat_score
            FROM sector_daily_stats {where}
            ORDER BY date, sector_type, sector_name
        """, params)
        rows = cur.fetchall()

    if not rows:
        return None, None

    series = defaultdict(lambda: defaultdict(list))
    for row in rows:
        series[row[1]][row[2]].append((
            str(row[0])[:10], float(row[3] or 0),
        ))

    dates = sorted(set(str(r[0])[:10] for r in rows))
    return series, dates


def _build_lookup(series):
    lookup = {}
    for stype, sectors in series.items():
        for sname, points in sectors.items():
            lookup[(stype, sname)] = {p[0]: p[1] for p in points}
    return lookup


# ============================================================
# 脉冲检测 (轻量版)
# ============================================================

def _get_sector_status(lookup, dates, heat_quantile=0.70, max_gap=1):
    today = dates[-1]
    yesterday = dates[-2] if len(dates) >= 2 else None
    results = {}

    for (stype, sname), heats_by_date in lookup.items():
        if _is_noise(sname):
            continue

        heat_vals = [v for v in heats_by_date.values() if v > 0]
        if len(heat_vals) < 30:
            continue

        threshold = __import__('numpy').percentile(heat_vals, heat_quantile * 100)
        today_heat = heats_by_date.get(today, 0)
        yesterday_heat = heats_by_date.get(yesterday, 0) if yesterday else 0

        # 扫描最近60天
        recent_dates = dates[-60:]
        n = len(recent_dates)
        is_hot = [heats_by_date.get(d, 0) >= threshold for d in recent_dates]
        heats_recent = [heats_by_date.get(d, 0) for d in recent_dates]

        # 今天不在脉冲中
        if not is_hot[n - 1]:
            was_in_pulse = False
            if n >= 2 and is_hot[n - 2]:
                was_in_pulse = True
            results[sname] = {
                'type': stype,
                'is_active': False,
                'just_ended': was_in_pulse and today_heat < threshold,
                'current_heat': round(today_heat, 1),
                'threshold': round(threshold, 1),
                'pulse_day': 0,
                'pulse_phase': None,
                'pulse_peak': 0,
                'hist_avg_peak': 0,
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
                j -= 1
            else:
                break
        pulse_start = last_hot

        pulse_heats = heats_recent[pulse_start:n]
        pulse_peak = max(pulse_heats)
        pulse_day = n - pulse_start

        # 判断阶段
        if pulse_day <= 2:
            phase = 'rising'
        elif pulse_heats[-1] < pulse_peak * 0.9:
            phase = 'decay'
        else:
            phase = 'peak'

        above_thresh = [h for h in heat_vals if h >= threshold]
        hist_avg_peak = __import__('numpy').mean(above_thresh) if above_thresh else threshold

        results[sname] = {
            'type': stype,
            'is_active': True,
            'just_started': (pulse_day == 1),
            'current_heat': round(today_heat, 1),
            'threshold': round(threshold, 1),
            'pulse_day': pulse_day,
            'pulse_phase': phase,
            'pulse_peak': round(pulse_peak, 1),
            'hist_avg_peak': round(float(hist_avg_peak), 1),
            'anomaly_ratio': round(pulse_peak / float(hist_avg_peak), 2) if hist_avg_peak > 0 else 0,
        }

    return results


# ============================================================
# 分类汇总
# ============================================================

def _categorize(results):
    new_hot = []
    rising = []
    anomalies = []
    decaying = []
    just_ended = []

    for name, r in results.items():
        if r['is_active']:
            if r.get('just_started'):
                new_hot.append((name, r))
            if r['pulse_phase'] == 'rising' and r['pulse_day'] >= 2:
                rising.append((name, r))
            if r['anomaly_ratio'] >= 1.3:
                anomalies.append((name, r))
            if r['pulse_phase'] == 'decay':
                decaying.append((name, r))
        elif r.get('just_ended'):
            just_ended.append((name, r))

    new_hot.sort(key=lambda x: -x[1]['current_heat'])
    rising.sort(key=lambda x: -x[1]['anomaly_ratio'])
    anomalies.sort(key=lambda x: -x[1]['anomaly_ratio'])
    decaying.sort(key=lambda x: -x[1]['pulse_day'])
    just_ended.sort(key=lambda x: -x[1]['current_heat'])

    return new_hot, rising, anomalies, decaying, just_ended


def _fmt_list(items, limit=15):
    out = []
    for name, r in items[:limit]:
        item = {
            'name': name,
            'type': r['type'],
            'heat': r['current_heat'],
            'threshold': r['threshold'],
        }
        if 'pulse_day' in r and r['pulse_day']:
            item['day'] = r['pulse_day']
        if 'pulse_peak' in r and r['pulse_peak']:
            item['peak'] = r['pulse_peak']
        if 'anomaly_ratio' in r and r['anomaly_ratio']:
            item['anomaly'] = r['anomaly_ratio']
        if 'pulse_phase' in r and r['pulse_phase']:
            item['phase'] = r['pulse_phase']
        if r.get('just_ended'):
            item['just_ended'] = True
        out.append(item)
    return out


# ============================================================
# fetch 入口
# ============================================================

def fetch(quantile=0.70, limit=15):
    try:
        start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")

        series, dates = _load_series(start, end)
        if not series or not dates:
            return _empty()

        lookup = _build_lookup(series)
        results = _get_sector_status(lookup, dates, quantile)
        new_hot, rising, anomalies, decaying, just_ended = _categorize(results)

        return {
            'date': dates[-1],
            'prev_date': dates[-2] if len(dates) >= 2 else '',
            'summary': {
                'new_hot': len(new_hot),
                'rising': len(rising),
                'anomaly': len(anomalies),
                'decaying': len(decaying),
                'just_ended': len(just_ended),
            },
            'new_hot': _fmt_list(new_hot, limit),
            'rising': _fmt_list(rising, limit),
            'anomaly': _fmt_list(anomalies, limit),
            'decaying': _fmt_list(decaying, limit),
            'just_ended': _fmt_list(just_ended, limit),
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("daily_scan fetch error: %s", e)
        return _empty()


def _empty():
    return {
        'date': '',
        'prev_date': '',
        'summary': {'new_hot': 0, 'rising': 0, 'anomaly': 0, 'decaying': 0, 'just_ended': 0},
        'new_hot': [], 'rising': [], 'anomaly': [], 'decaying': [], 'just_ended': [],
    }


register(meta, lambda: fetch())
