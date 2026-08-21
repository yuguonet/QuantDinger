#!/usr/bin/env python3
"""
每日候选池筛选器 (盘后运行, 产出 100~300 只候选供 realtime_monitor 盘中使用)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

设计思路:
  日线负责"找方向"(宽筛, 宁多勿漏), 1分钟线负责"找时机"(精确入场).
  每只候选带策略来源标签 + 质量评分, 供盘中信号加权使用.

数据来源 (5条线, 各自独立, 取并集):
  1. 龙虎榜周期活跃 — 历史上榜>=5次, 安静天数>中位间隔 (来自 dragon_cycle.py)
  2. 首板/连板放量   — 昨日涨停+放量, 或近期2连板 (来自 realtime_monitor 原逻辑)
  3. V1强趋势回踩   — 20日涨>30% + 近3日回调-3%~-10% (来自 test_dragon.py V1策略)
  4. 技术形态突破   — MA多头+RSI50~70+OBV上升+近5日新高 (综合技术面)
  5. 板块龙头       — 所属板块当日涨停数>=2的领涨股

输出:
  tmp/daily_candidates.json — 候选列表 (含来源/评分/技术详情)

用法:
  python daily_screener.py                      # 默认筛选
  python daily_screener.py --days 60            # 指定回看天数
  python daily_screener.py --min-score 50       # 最低质量分
  python daily_screener.py --sources 1,2,3      # 只用某些来源
  python daily_screener.py --max 300            # 最大候选数
"""
from __future__ import annotations
import json, argparse, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ================================================================
# 环境初始化
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

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_pool_cache = None
def _get_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    _pool_cache = get_market_db_manager()._get_pool("CNStock")
    return _pool_cache

# ================================================================
# 通用函数
# ================================================================

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"

def get_limit_pct(code: str) -> float:
    return 20.0 if get_board_type(code) == "gem_star" else 10.0

def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98

def load_kline(code: str, days: int = 120) -> List[Dict]:
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

def get_all_codes() -> List[str]:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def load_st_codes() -> set:
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol FROM stock_basic_info WHERE status='active' AND name ILIKE '%%ST%%'")
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()

def load_stock_names() -> Dict[str, str]:
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, name FROM stock_basic_info WHERE status='active'")
            return {row[0]: row[1] or '' for row in cur.fetchall()}
    except Exception:
        return {}

# ================================================================
# 技术指标 (共用)
# ================================================================

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period; avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0

def calc_kdj_k(closes, highs, lows, period=9):
    if len(closes) < period: return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hn = max(highs[i-period+1:i+1]); ln = min(lows[i-period+1:i+1])
        rsvs.append((closes[i] - ln) / (hn - ln) * 100 if hn != ln else 50)
    k_val = 50.0
    for rsv in rsvs: k_val = 2/3 * k_val + 1/3 * rsv
    return k_val

def calc_obv_trend(bars, period=5):
    if len(bars) < period + 1: return "平"
    obv = 0; obv_list = [0]
    for i in range(1, len(bars)):
        if bars[i]['close'] > bars[i-1]['close']: obv += bars[i]['volume']
        elif bars[i]['close'] < bars[i-1]['close']: obv -= bars[i]['volume']
        obv_list.append(obv)
    change = obv_list[-1] - obv_list[-period]
    return "上升" if change > 0 else ("下降" if change < 0 else "平")

def calc_ma5_angle(closes, period=5, days=3):
    if len(closes) < period + days: return None
    ma = [sum(closes[i-period+1:i+1]) / period for i in range(period - 1, len(closes))]
    if len(ma) < days: return None
    recent = ma[-days:]; n = days
    sum_x = n * (n - 1) / 2; sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0
    return slope / recent[-1] * 100 if recent[-1] else None

def calc_tech_score(bars, idx):
    if idx < 20 or idx >= len(bars): return 0
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi14 = calc_rsi(closes, 14); kdj_k = calc_kdj_k(closes, highs, lows, 9)
    obv = calc_obv_trend(bars[:idx + 1])
    avg5 = sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5 if idx >= 5 else 1
    vr = bars[idx]['volume'] / avg5 if avg5 > 0 else 1.0
    angle = calc_ma5_angle(closes[-20:], 5, 3) if len(closes) >= 20 else None
    score = 50
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: score += 20
        elif ma5 < ma10 < ma20: score -= 15
        elif ma5 > ma10: score += 10
    if rsi14 is not None:
        if 40 <= rsi14 <= 60: score += 5
        elif rsi14 > 70: score -= 10
        elif rsi14 < 30: score += 10
    if obv == "上升": score += 10
    elif obv == "下降": score -= 10
    if 1.0 <= vr <= 2.0: score += 5
    elif vr > 3.0: score -= 5
    if angle is not None:
        if angle > 0.5: score += 10
        elif angle > 0.3: score += 5
    if kdj_k is not None:
        if kdj_k > 90: score += 10
        elif kdj_k > 80: score += 5
    return max(0, min(100, score))

def calc_tech_details(bars, idx):
    """计算完整技术指标详情"""
    if idx < 20 or idx >= len(bars): return {}
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma_bull = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    rsi14 = calc_rsi(closes, 14)
    kdj_k = calc_kdj_k(closes, highs, lows, 9)
    obv = calc_obv_trend(bars[:idx + 1])
    angle = calc_ma5_angle(closes[-20:], 5, 3) if len(closes) >= 20 else None
    avg5 = sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5 if idx >= 5 else 1
    vol_ratio = bars[idx]['volume'] / avg5 if avg5 > 0 else 1.0
    # 5日新高
    high5 = max(bars[i]['high'] for i in range(max(0, idx-4), idx+1))
    near_high5 = bars[idx]['close'] >= high5 * 0.98
    # 20日涨幅
    ret20 = (bars[idx]['close'] / bars[max(0, idx-20)]['close'] - 1) * 100 if idx >= 20 and bars[max(0, idx-20)]['close'] > 0 else 0
    return {
        'ma5': round(ma5, 2) if ma5 else None,
        'ma10': round(ma10, 2) if ma10 else None,
        'ma20': round(ma20, 2) if ma20 else None,
        'ma_bull': ma_bull,
        'rsi14': round(rsi14, 1) if rsi14 else None,
        'kdj_k': round(kdj_k, 1) if kdj_k else None,
        'obv_trend': obv,
        'ma5_angle': round(angle, 2) if angle else None,
        'vol_ratio': round(vol_ratio, 2),
        'near_high5': near_high5,
        'ret20': round(ret20, 2),
        'tech_score': calc_tech_score(bars, idx),
    }

# ================================================================
# 来源1: 龙虎榜周期活跃 (来自 dragon_cycle.py)
# ================================================================

def screen_dragon_cycle(all_codes: List[str], st_codes: set,
                        min_count: int = 5, k: float = 1.0,
                        lookback_days: int = 180) -> List[Dict]:
    """龙虎榜常客, 安静天数接近历史周期"""
    try:
        pool = _get_pool()
        cutoff = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date, stock_code, stock_name, net_amount "
                "FROM cnd_dragon_tiger_list WHERE trade_date >= %s ORDER BY trade_date",
                (cutoff,)
            )
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        print(f"  ⚠️ 龙虎榜数据加载失败: {e}")
        return []

    by_code = defaultdict(list)
    for r in rows:
        by_code[r[1]].append({'date': r[0], 'name': r[2], 'net': float(r[3] or 0)})

    latest = max(r[0] for r in rows) if rows else None
    if not latest:
        return []
    latest_dt = datetime.strptime(latest, '%Y-%m-%d')
    lb_cutoff = (latest_dt - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

    candidates = []
    for code, recs in by_code.items():
        if code in st_codes or len(recs) < min_count:
            continue
        dates = sorted(r['date'] for r in recs)
        prior = [d for d in dates if d < latest and d >= lb_cutoff]
        if len(prior) < 2:
            continue
        last = prior[-1]
        days_since = (latest_dt - datetime.strptime(last, '%Y-%m-%d')).days
        gaps = [(datetime.strptime(prior[i], '%Y-%m-%d') - datetime.strptime(prior[i-1], '%Y-%m-%d')).days
                for i in range(1, len(prior))]
        if not gaps:
            continue
        import numpy as np
        median_gap = float(np.median(gaps))
        if median_gap <= 0:
            continue
        ratio = days_since / median_gap
        if ratio <= k or ratio >= k * 4:
            continue
        # 质量分: ratio 越高越接近爆发, 上限10分
        score = min(10, int(ratio * 5))
        candidates.append({
            'code': code, 'source': '龙虎榜周期', 'source_id': 1,
            'quality_score': score,
            'detail': f"上榜{len(recs)}次 安静{days_since}天 中位{median_gap:.0f}天 倍数{ratio:.1f}x",
        })

    return candidates

# ================================================================
# 来源2: 首板/连板放量 (来自 realtime_monitor 原逻辑, 放宽条件)
# ================================================================

def screen_limit_up(all_codes: List[str], st_codes: set,
                    stock_names: Dict[str, str]) -> List[Dict]:
    """首板/连板 + 放量, 条件放宽 (量比>=1.5, 不限前5日)"""
    candidates = []
    for code in all_codes:
        if code in st_codes:
            continue
        bars = load_kline(code, 30)
        if not bars or len(bars) < 8:
            continue
        bt = get_board_type(code)
        idx = len(bars) - 1
        bar = bars[idx]; prev = bars[idx - 1]
        if not is_limit_up(bar['close'], prev['close'], bt):
            continue
        # 量比
        avg5 = sum(bars[j]['volume'] for j in range(max(0, idx-5), idx)) / 5 if idx >= 5 else 1
        vr = bar['volume'] / avg5 if avg5 > 0 else 0
        if vr < 1.5:
            continue
        # 判断首板 vs 连板
        prev2 = bars[idx - 2] if idx >= 2 else None
        day_before_lu = prev2 and is_limit_up(prev['close'], prev2['close'], bt)
        if day_before_lu:
            # 连板 — 排除3板以上
            prev3 = bars[idx - 3] if idx >= 3 else None
            if prev3 and is_limit_up(prev2['close'], prev3['close'], bt):
                continue
            src = '2连板'; score = 8
        else:
            # 首板
            has_recent = any(is_limit_up(bars[j]['close'], bars[j-1]['close'], bt)
                            for j in range(max(1, idx-5), idx))
            src = '首板(新)' if not has_recent else '首板(旧)'
            score = 6 if has_recent else 5

        # 技术分加成
        tech = calc_tech_score(bars, idx)
        score += int(tech / 20)  # 0~5分加成

        candidates.append({
            'code': code, 'name': stock_names.get(code, ''),
            'source': src, 'source_id': 2,
            'quality_score': min(15, score),
            'vol_ratio': round(vr, 2),
            'detail': f"{src} 量比{vr:.1f}x 技分{tech}",
        })

    return candidates

# ================================================================
# 来源3: V1强趋势回踩 (来自 test_dragon.py)
# ================================================================

def screen_v1_pullback(all_codes: List[str], st_codes: set,
                       stock_names: Dict[str, str],
                       ret_20d_min: float = 25.0,
                       pullback_min: float = -12.0,
                       pullback_max: float = -2.0) -> List[Dict]:
    """20日涨>25% + 近3日回调-12%~-2%"""
    candidates = []
    for code in all_codes:
        if code in st_codes:
            continue
        bars = load_kline(code, 60)
        if not bars or len(bars) < 25:
            continue
        idx = len(bars) - 1
        close = bars[idx]['close']
        # 20日涨幅
        if idx < 20 or bars[idx-20]['close'] <= 0:
            continue
        ret20 = (close / bars[idx-20]['close'] - 1) * 100
        if ret20 < ret_20d_min:
            continue
        # 近3日回调幅度 (从3日前收盘到今日收盘)
        if idx < 3:
            continue
        d_3_close = bars[idx-3]['close']
        pullback = (close / d_3_close - 1) * 100
        if pullback < pullback_min or pullback >= pullback_max:
            continue
        # 技术分
        tech = calc_tech_score(bars, idx)
        if tech < 40:
            continue
        # OBV趋势
        obv = calc_obv_trend(bars[:idx + 1])
        score = 7
        if ret20 >= 40: score += 2
        if obv == '上升': score += 2
        if tech >= 60: score += 1

        candidates.append({
            'code': code, 'name': stock_names.get(code, ''),
            'source': 'V1回踩', 'source_id': 3,
            'quality_score': min(15, score),
            'ret20': round(ret20, 2), 'pullback': round(pullback, 2),
            'detail': f"20日涨{ret20:+.1f}% 回调{pullback:+.1f}% 技分{tech} OBV{obv}",
        })

    return candidates

# ================================================================
# 来源4: 技术形态突破
# ================================================================

def screen_tech_breakout(all_codes: List[str], st_codes: set,
                         stock_names: Dict[str, str]) -> List[Dict]:
    """MA多头 + RSI 50~70 + OBV上升 + 近5日新高"""
    candidates = []
    for code in all_codes:
        if code in st_codes:
            continue
        bars = load_kline(code, 60)
        if not bars or len(bars) < 25:
            continue
        idx = len(bars) - 1
        tech = calc_tech_details(bars, idx)
        if not tech:
            continue
        # 条件: 多头排列 + RSI 50~70 + OBV上升 + 5日新高附近
        if not tech.get('ma_bull'):
            continue
        rsi = tech.get('rsi14', 0)
        if not rsi or rsi < 50 or rsi > 70:
            continue
        if tech.get('obv_trend') != '上升':
            continue
        if not tech.get('near_high5'):
            continue
        ts = tech.get('tech_score', 0)
        if ts < 55:
            continue
        score = 6
        if ts >= 70: score += 2
        if tech.get('ma5_angle', 0) and tech['ma5_angle'] > 0.5: score += 2
        if tech.get('vol_ratio', 0) and 1.0 <= tech['vol_ratio'] <= 2.0: score += 1

        candidates.append({
            'code': code, 'name': stock_names.get(code, ''),
            'source': '技术突破', 'source_id': 4,
            'quality_score': min(15, score),
            'tech_score': ts,
            'detail': f"多头 RSI{(rsi or 0):.0f} OBV↑ 角度{(tech.get('ma5_angle') or 0):.2f} 技分{ts}",
        })

    return candidates

# ================================================================
# 来源5: 板块龙头 (需要板块涨停数据)
# ================================================================

def screen_sector_leaders(all_codes: List[str], st_codes: set,
                          stock_names: Dict[str, str]) -> List[Dict]:
    """所属板块有>=2只涨停的领涨股 (非涨停, 但技术面强)"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                "SELECT symbol, industry FROM stock_basic_info WHERE status='active' AND industry != ''"
            )
            industry_map = {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return []

    # 先找今日涨停的板块
    limit_up_industries = defaultdict(int)
    limit_up_codes = set()
    for code in all_codes:
        if code in st_codes:
            continue
        bars = load_kline(code, 10)
        if not bars or len(bars) < 2:
            continue
        bt = get_board_type(code)
        idx = len(bars) - 1
        if is_limit_up(bars[idx]['close'], bars[idx-1]['close'], bt):
            ind = industry_map.get(code, '')
            if ind:
                limit_up_industries[ind] += 1
            limit_up_codes.add(code)

    hot_industries = {ind for ind, cnt in limit_up_industries.items() if cnt >= 2}
    if not hot_industries:
        return []

    # 找热门板块中技术面强的非涨停股
    candidates = []
    for code in all_codes:
        if code in st_codes or code in limit_up_codes:
            continue
        ind = industry_map.get(code, '')
        if ind not in hot_industries:
            continue
        bars = load_kline(code, 60)
        if not bars or len(bars) < 25:
            continue
        idx = len(bars) - 1
        tech = calc_tech_score(bars, idx)
        if tech < 55:
            continue
        # 今日涨幅 > 3% (领涨但未涨停)
        change = (bars[idx]['close'] / bars[idx-1]['close'] - 1) * 100
        if change < 3:
            continue
        score = 5
        if change >= 5: score += 2
        if tech >= 70: score += 2

        candidates.append({
            'code': code, 'name': stock_names.get(code, ''),
            'source': '板块龙头', 'source_id': 5,
            'quality_score': min(15, score),
            'industry': ind, 'change': round(change, 2),
            'detail': f"板块[{ind}]涨{change:+.1f}% 技分{tech} 板块涨停{limit_up_industries[ind]}只",
        })

    return candidates

# ================================================================
# 合并去重 + 评分排序
# ================================================================

def merge_and_rank(all_candidates: List[Dict], max_count: int = 300) -> List[Dict]:
    """多来源合并去重, 按质量分排序, 截取 top N"""
    by_code = {}
    for c in all_candidates:
        code = c['code']
        if code in by_code:
            # 已存在: 保留更高分的来源, 但记录所有来源
            existing = by_code[code]
            if c['quality_score'] > existing['quality_score']:
                existing['quality_score'] = c['quality_score']
                existing['primary_source'] = c['source']
            existing['sources'] = existing.get('sources', []) + [c['source']]
            existing['source_ids'] = list(set(existing.get('source_ids', []) + [c.get('source_id', 0)]))
        else:
            c['sources'] = [c['source']]
            c['source_ids'] = [c.get('source_id', 0)]
            c['primary_source'] = c['source']
            by_code[code] = c

    merged = list(by_code.values())
    # 多来源加分: 被2个以上来源选中的股票额外加分
    for c in merged:
        src_count = len(c.get('sources', []))
        if src_count >= 3:
            c['quality_score'] = min(20, c['quality_score'] + 3)
        elif src_count >= 2:
            c['quality_score'] = min(20, c['quality_score'] + 1)

    merged.sort(key=lambda x: (-x['quality_score'], x['code']))
    return merged[:max_count]

# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="每日候选池筛选器 (盘后运行)")
    parser.add_argument("--days", type=int, default=120, help="日线回看天数 (默认120)")
    parser.add_argument("--min-score", type=int, default=0, help="最低质量分 (默认0=全部)")
    parser.add_argument("--sources", default="1,2,3,4,5", help="启用来源 (逗号分隔, 默认全部)")
    parser.add_argument("--max", type=int, default=300, help="最大候选数 (默认300)")
    parser.add_argument("--scan", action="store_true", help="仅扫描, 不保存缓存")
    args = parser.parse_args()

    enabled_sources = set(int(x) for x in args.sources.split(",") if x.strip().isdigit())

    print("=" * 70)
    print("  🔍 每日候选池筛选器")
    print("  日线宽筛 → 100~300只候选 → 盘中1分钟实时分析")
    print("=" * 70)

    # 加载基础数据
    print("\n  [1/6] 加载基础数据...")
    all_codes = get_all_codes()
    st_codes = load_st_codes()
    stock_names = load_stock_names()
    codes = [c for c in all_codes if c not in st_codes]
    print(f"        全市场: {len(all_codes)}只, 排除ST: {len(codes)}只")

    all_candidates = []

    # 来源1: 龙虎榜周期
    if 1 in enabled_sources:
        print("\n  [2/6] 龙虎榜周期活跃...")
        r1 = screen_dragon_cycle(codes, st_codes)
        print(f"        → {len(r1)}只")
        all_candidates.extend(r1)
    else:
        print("\n  [2/6] 龙虎榜周期 — 跳过")

    # 来源2: 首板/连板
    if 2 in enabled_sources:
        print("\n  [3/6] 首板/连板放量...")
        r2 = screen_limit_up(codes, st_codes, stock_names)
        print(f"        → {len(r2)}只")
        all_candidates.extend(r2)
    else:
        print("\n  [3/6] 首板/连板 — 跳过")

    # 来源3: V1回踩
    if 3 in enabled_sources:
        print("\n  [4/6] V1强趋势回踩...")
        r3 = screen_v1_pullback(codes, st_codes, stock_names)
        print(f"        → {len(r3)}只")
        all_candidates.extend(r3)
    else:
        print("\n  [4/6] V1回踩 — 跳过")

    # 来源4: 技术突破
    if 4 in enabled_sources:
        print("\n  [5/6] 技术形态突破...")
        r4 = screen_tech_breakout(codes, st_codes, stock_names)
        print(f"        → {len(r4)}只")
        all_candidates.extend(r4)
    else:
        print("\n  [5/6] 技术突破 — 跳过")

    # 来源5: 板块龙头
    if 5 in enabled_sources:
        print("\n  [6/6] 板块龙头...")
        r5 = screen_sector_leaders(codes, st_codes, stock_names)
        print(f"        → {len(r5)}只")
        all_candidates.extend(r5)
    else:
        print("\n  [6/6] 板块龙头 — 跳过")

    # 合并去重排序
    print(f"\n  合并: 原始{len(all_candidates)}只 → 去重...")
    merged = merge_and_rank(all_candidates, args.max)

    # 质量分过滤
    if args.min_score > 0:
        merged = [c for c in merged if c['quality_score'] >= args.min_score]

    print(f"  最终: {len(merged)}只候选")

    # 统计
    src_counts = defaultdict(int)
    for c in merged:
        for s in c.get('sources', []):
            src_counts[s] += 1
    print(f"\n  来源分布:")
    for src in ['龙虎榜周期', '首板(新)', '首板(旧)', '2连板', 'V1回踩', '技术突破', '板块龙头']:
        if src_counts.get(src):
            print(f"    {src}: {src_counts[src]}只")

    # 质量分分布
    scores = [c['quality_score'] for c in merged]
    if scores:
        print(f"\n  质量分: 最高{max(scores)} 最低{min(scores)} 平均{sum(scores)/len(scores):.1f}")

    # 输出 Top 30
    print(f"\n  {'排名':>4} {'代码':>8} {'名称':>8} {'来源':>12} {'分数':>4} {'详情'}")
    print(f"  {'-'*70}")
    for i, c in enumerate(merged[:30]):
        src_str = '+'.join(c.get('sources', []))
        print(f"  {i+1:>4} {c['code']:>8} {c.get('name',''):>8} {src_str:>12} "
              f"{c['quality_score']:>4} {c.get('detail', '')[:40]}")
    if len(merged) > 30:
        print(f"  ... 共{len(merged)}只")

    # 保存
    if not args.scan:
        _cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
        os.makedirs(_cache_dir, exist_ok=True)
        outfile = os.path.join(_cache_dir, "daily_candidates.json")
        export = {
            'date': datetime.now().strftime("%Y-%m-%d"),
            'generated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'count': len(merged),
            'sources': dict(src_counts),
            'candidates': merged,
        }
        with open(outfile, 'w', encoding='utf-8') as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 已保存: {outfile}")
        print(f"  📋 盘中运行: python realtime_monitor.py --pool {outfile}")

    return merged


if __name__ == "__main__":
    main()
