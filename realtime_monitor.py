#!/usr/bin/env python3
"""
首板/2连板盘中实时监控 (V2 — 多维过滤版)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

功能:
  1. 盘前选股: 4类候选 (排除ST, 首板放量)
  2. 技术评分: 综合技术分 >= 60 才进入监控 (MA排列/RSI/KDJ/OBV/量比/角度)
  3. 盘中实时: 每分钟从 realtime_snapshot_YYYY 快照表读取分时数据, 检测弱转强信号
  4. 信号强度: 强/中/弱 三级 (基于日内动量, 移植自 V1 策略核心胜率因子)
  5. 买入建议: 触发时输出建议买入价 + 信号类型

数据来源:
  - 批量行情: realtime_snapshot_YYYY (每分钟全市场快照, scheduler 每60s采集)
  - 分时序列: realtime_snapshot_YYYY 当天数据重构 (每行快照 = 1分钟bar)
  - VWAP: 优先从快照 extras.amount 累加计算, 否则回退典型价 (H+L+C)/3 加权
  - 日K线: kline_1m_YYYY/kline_1D_YYYY (DB)
  - 不依赖 mootdx/coordinator 网络拉取 (盘中完全走DB)

用法:
  python realtime_monitor.py                    # 完整运行 (选股+监控)
  python realtime_monitor.py --scan             # 仅选股, 不监控
  python realtime_monitor.py --codes 000001,600519  # 手动指定股票
  python realtime_monitor.py --refresh           # 强制刷新 (忽略当天缓存)
  python realtime_monitor.py --interval 30      # 盘中轮询间隔(秒)

选股池 (4类):
  首板(新) — 昨日涨停+前5日无涨停+放量 (新热点)
  首板(旧) — 昨日涨停+前5日有涨停+放量 (老热点二次启动)
  昨日2连板 — 昨日是连板第2天+放量 (强势确认)
  非昨日2连板 — 近期有2连板但昨日不是涨停 (回调观察)
  排除ST股, 首板量比>=2.0
  技术分过滤: tech_score >= 60 (MA排列+RSI+KDJ+OBV+量比+MA5角度)

候选列表缓存到 tmp/realtime_candidates.json (文件内判断日期)

盘中信号规则 (V2, 多维过滤):
  前置条件 — tech_score >= 60 + 日内动量 > -2%
  昨日2连板 — 高开>0% + 现价不破VWAP + 缩量 + 多头排列
  非昨日2连板 — 突破VWAP站稳>20分钟 + 多头排列 + RSI>50
  首板(旧)   — 突破VWAP站稳>20分钟 + 多头排列 + RSI>50
  首板(新)   — 今日动量 > 昨日动量 × 1.5 + OBV不下降

信号强度 (日内动量, 移植自 V1 策略):
  强 💪 — 日内动量 >= 3% (V1持有组99%胜率)
  中 📊 — 日内动量 0%~3%
  弱 ⚠️ — 日内动量 -2%~0% (谨慎, 建议观望)

技术评分体系 (来自 dragon_d0_alert.py):
  MA排列: 多头+20, 空头-15  |  RSI: 40~60中性+5, >70超买-10
  OBV趋势: 上升+10, 下降-10  |  量比: 1~2x温和+5, >3x过度-5
  MA5角: >0.5%+10, >0.3%+5  |  KDJ: >90+10, >80+5
"""
from __future__ import annotations
import json, time, argparse, os, sys, threading
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

# ================================================================
# 数据库
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache



# ================================================================
# 技术指标 (移植自 dragon_d0_alert.py, 用于信号质量过滤)
# ================================================================

def calc_rsi(closes, period=14):
    """RSI 相对强弱指数"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0


def calc_kdj_k(closes, highs, lows, period=9):
    """KDJ K值"""
    if len(closes) < period:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hn = max(highs[i-period+1:i+1])
        ln = min(lows[i-period+1:i+1])
        c = closes[i]
        rsvs.append((c - ln) / (hn - ln) * 100 if hn != ln else 50)
    k_val = 50.0
    for rsv in rsvs:
        k_val = 2/3 * k_val + 1/3 * rsv
    return k_val


def calc_obv_trend(bars, period=5):
    """OBV趋势: 最近period天OBV变化方向"""
    if len(bars) < period + 1:
        return "平"
    obv = 0
    obv_list = [0]
    for i in range(1, len(bars)):
        if bars[i]['close'] > bars[i-1]['close']:
            obv += bars[i]['volume']
        elif bars[i]['close'] < bars[i-1]['close']:
            obv -= bars[i]['volume']
        obv_list.append(obv)
    change = obv_list[-1] - obv_list[-period]
    if change > 0:
        return "上升"
    elif change < 0:
        return "下降"
    return "平"


def calc_ma5_angle(closes, period=5, days=3):
    """MA5 斜率 (角度)"""
    if len(closes) < period + days:
        return None
    ma = [sum(closes[i-period+1:i+1]) / period for i in range(period - 1, len(closes))]
    if len(ma) < days:
        return None
    recent = ma[-days:]
    n = days
    sum_x = n * (n - 1) / 2
    sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0
    return slope / recent[-1] * 100 if recent[-1] else None


def calc_tech_score(bars, idx):
    """综合技术评分 (0-100, 移植自 dragon_d0_alert.py)

    评分维度:
      MA排列: 多头+20, 空头-15, 交叉+10
      RSI14:  40~60中性+5, >70超买-10, <30超卖+10
      OBV趋势: 上升+10, 下降-10
      量比:   1~2x温和+5, >3x过度-5
      MA5角:  >0.3%+5, >0.5%+10
      KDJ K:  >80+5, >90+10
    """
    if idx < 20 or idx >= len(bars):
        return 0

    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]

    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi14 = calc_rsi(closes, 14)
    kdj_k = calc_kdj_k(closes, highs, lows, 9)
    obv_trend = calc_obv_trend(bars[:idx + 1])
    vol_ratio = _calc_vol_ratio(bars, idx) if idx >= 5 else 1.0
    angle = calc_ma5_angle(closes[-20:], 5, 3) if len(closes) >= 20 else None

    score = 50

    # MA排列
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            score += 20
        elif ma5 < ma10 < ma20:
            score -= 15
        elif ma5 > ma10:
            score += 10

    # RSI
    if rsi14 is not None:
        if 40 <= rsi14 <= 60:
            score += 5
        elif rsi14 > 70:
            score -= 10
        elif rsi14 < 30:
            score += 10

    # OBV趋势
    if obv_trend == "上升":
        score += 10
    elif obv_trend == "下降":
        score -= 10

    # 量比
    if 1.0 <= vol_ratio <= 2.0:
        score += 5
    elif vol_ratio > 3.0:
        score -= 5

    # MA5 角度
    if angle is not None:
        if angle > 0.5:
            score += 10
        elif angle > 0.3:
            score += 5

    # KDJ
    if kdj_k is not None:
        if kdj_k > 90:
            score += 10
        elif kdj_k > 80:
            score += 5

    return max(0, min(100, score))


def calc_daily_tech_details(bars, idx):
    """计算日K线技术指标详情 (用于候选注入和信号输出)"""
    if idx < 20 or idx >= len(bars):
        return {}

    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    highs = [bars[i]['high'] for i in range(max(0, idx - 60), idx + 1)]
    lows = [bars[i]['low'] for i in range(max(0, idx - 60), idx + 1)]

    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma_bull = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)

    rsi14 = calc_rsi(closes, 14)
    kdj_k = calc_kdj_k(closes, highs, lows, 9)
    obv_trend = calc_obv_trend(bars[:idx + 1])
    angle = calc_ma5_angle(closes[-20:], 5, 3) if len(closes) >= 20 else None
    tech_score = calc_tech_score(bars, idx)

    return {
        'ma5': round(ma5, 2) if ma5 else None,
        'ma10': round(ma10, 2) if ma10 else None,
        'ma20': round(ma20, 2) if ma20 else None,
        'ma_bull': ma_bull,
        'rsi14': round(rsi14, 1) if rsi14 else None,
        'kdj_k': round(kdj_k, 1) if kdj_k else None,
        'obv_trend': obv_trend,
        'ma5_angle': round(angle, 2) if angle else None,
        'tech_score': tech_score,
    }


# ================================================================
# 板块判断
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

# ================================================================
# 数据加载
# ================================================================

def get_all_codes_db() -> List[str]:
    """从数据库获取全市场股票代码列表 (抄 test_dragon.py)"""
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []


def _is_limit_up(close: float, prev_close: float, board_type: str) -> bool:
    """判断是否涨停 (抄 test_dragon.py)"""
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) >= threshold * 0.98


def _find_limit_up_indices(bars: List[Dict], board_type: str) -> List[int]:
    """找到所有涨停日的索引 (抄 test_dragon.py)"""
    result = []
    for i in range(1, len(bars)):
        if _is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result


def _load_stock_names() -> Dict[str, str]:
    """加载全量股票名称 (从 stock_basic_info 表)"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, name FROM stock_basic_info WHERE status='active'")
            return {row[0]: row[1] or '' for row in cur.fetchall()}
    except Exception:
        return {}


def _load_st_codes() -> set:
    """加载ST股代码集合 (从 stock_basic_info 表)"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM stock_basic_info "
                "WHERE status='active' AND name ILIKE '%%ST%%'"
            )
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()


def _calc_vol_ratio(bars: List[Dict], limit_idx: int, window: int = 5) -> float:
    """计算涨停日量比: 涨停日成交量 / 前N日均量"""
    if limit_idx < window + 1:
        return 0.0
    avg_vol = sum(bars[j]['volume'] for j in range(limit_idx - window, limit_idx)) / window
    if avg_vol <= 0:
        return 0.0
    return bars[limit_idx]['volume'] / avg_vol


def _has_recent_limit_up(bars: List[Dict], before_idx: int, lookback: int, board_type: str) -> bool:
    """检查 before_idx 之前 lookback 天内是否有涨停"""
    start = max(0, before_idx - lookback)
    for i in range(start, before_idx):
        if _is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            return True
    return False


def load_all_candidates(kline_days: int = 30) -> List[Dict]:
    """从日K线数据提取候选股, 分4类 (排除ST, 首板放量)

    分类:
      1. 首板(新) — 昨日涨停+前日没涨停+前5日无涨停+放量
      2. 首板(旧) — 昨日涨停+前日没涨停+前5日有涨停+放量
      3. 昨日2连板 — 昨日是连板的第2天+放量
      4. 非昨日2连板 — 近期有2连板但昨日不是涨停日
    """
    all_codes = get_all_codes_db()
    st_codes = _load_st_codes()
    stock_names = _load_stock_names()
    print(f"        全市场: {len(all_codes)}只, ST: {len(st_codes)}只")

    candidates = []
    for code in all_codes:
        if code in st_codes:
            continue
        bars = load_kline_db(code, kline_days)
        if not bars or len(bars) < 8:
            continue
        bt = get_board_type(code)
        idx = len(bars) - 1  # 最新一天 (昨日)
        yesterday_lu = _is_limit_up(bars[idx]['close'], bars[idx-1]['close'], bt)
        day_before_lu = idx >= 2 and _is_limit_up(bars[idx-1]['close'], bars[idx-2]['close'], bt)

        if yesterday_lu and not day_before_lu:
            # 昨日首板 (涨停+前日没涨停)
            vol_ratio = _calc_vol_ratio(bars, idx, 5)
            if vol_ratio < 2.0:
                continue
            has_recent = _has_recent_limit_up(bars, idx - 1, 5, bt)
            cat = '首板(新)' if not has_recent else '首板(旧)'
            candidates.append({
                'stock_code': code, 'stock_name': stock_names.get(code, ''),
                'source': cat, 'continuous_zt_days': 1,
                'vol_ratio': round(vol_ratio, 2),
            })

        elif yesterday_lu and day_before_lu:
            # 昨日是连板 — 排除3连板以上: 前天之前一天不能是涨停
            day_before2_lu = idx >= 3 and _is_limit_up(bars[idx-2]['close'], bars[idx-3]['close'], bt)
            if day_before2_lu:
                continue  # 3连板以上, 跳过
            streak = 2
            # 取首板日的量比
            first_lu_idx = idx - streak + 1
            vol_ratio = _calc_vol_ratio(bars, first_lu_idx, 5)
            if vol_ratio < 2.0:
                continue
            candidates.append({
                'stock_code': code, 'stock_name': stock_names.get(code, ''),
                'source': '昨日2连板', 'continuous_zt_days': 2,
                'vol_ratio': round(vol_ratio, 2),
            })

        else:
            # 昨日不是涨停, 检查近期是否有2连板
            limit_indices = _find_limit_up_indices(bars, bt)
            if len(limit_indices) < 2:
                continue
            # 找最长连续涨停 (索引必须严格相邻)
            max_streak = 1
            cur_streak = 1
            best_start = limit_indices[0]
            for j in range(1, len(limit_indices)):
                if limit_indices[j] == limit_indices[j-1] + 1:
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                        best_start = limit_indices[j - cur_streak + 1]
                else:
                    cur_streak = 1
            if max_streak != 2:
                continue
            # 时间窗口: 2连板距今不超过10个交易日
            days_since = idx - (best_start + 1)
            if days_since > 10:
                continue
            # 跌幅限制: 昨日跌幅不超过-8%
            last_change = (bars[idx]['close'] / bars[idx-1]['close'] - 1) * 100
            if last_change < -8.0:
                continue
            # 额外校验: 确保是真正的2连板 (不是中间隔了天的)
            # best_start 和 best_start+1 都必须是涨停日
            if not _is_limit_up(bars[best_start]['close'], bars[best_start-1]['close'], bt):
                continue
            if not _is_limit_up(bars[best_start+1]['close'], bars[best_start]['close'], bt):
                continue
            # best_start-1 不能是涨停 (否则是3连板)
            if best_start >= 2 and _is_limit_up(bars[best_start-1]['close'], bars[best_start-2]['close'], bt):
                continue
            # 首板放量
            vol_ratio = _calc_vol_ratio(bars, best_start, 5)
            if vol_ratio < 2.0:
                continue
            candidates.append({
                'stock_code': code, 'stock_name': stock_names.get(code, ''),
                'source': '非昨日2连板', 'continuous_zt_days': max_streak,
                'vol_ratio': round(vol_ratio, 2),
            })

    return candidates


def load_kline_db(code: str, days: int = 60) -> List[Dict]:
    """从DB加载日K线 (前复权)"""
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


# ================================================================
# 候选缓存 (当天只分析一次, 文件内判断日期)
# ================================================================

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
_CACHE_FILE = "realtime_candidates.json"

def _cache_path() -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, _CACHE_FILE)

def _load_cache() -> Optional[List[Dict]]:
    """加载缓存, 文件内日期是今天则有效, 否则返回 None"""
    path = _cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('date') != datetime.now().strftime("%Y-%m-%d"):
            return None
        cands = data.get('candidates', [])
        print(f"  📦 从缓存加载 ({len(cands)}只, 保存于 {data.get('saved_at', '?')})")
        return cands
    except Exception:
        return None

def _save_cache(candidates: List[Dict]):
    """保存候选列表到缓存 (固定文件名, 内部写日期)"""
    path = _cache_path()
    data = {
        'date': datetime.now().strftime("%Y-%m-%d"),
        'saved_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'count': len(candidates),
        'candidates': candidates,
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  💾 已缓存到 {path}")
    except Exception as e:
        print(f"  ⚠️ 缓存保存失败: {e}")

# ================================================================
# 选股: 盘前筛选 (首板 + 2连板)
# ================================================================

def screen_candidates(kline_days: int = 30, force_refresh: bool = False) -> List[Dict]:
    """盘前选股: 4类候选 (排除ST, 首板放量)

    分类:
      首板(新) — 昨日涨停+前5日无涨停+放量
      首板(旧) — 昨日涨停+前5日有涨停+放量
      昨日2连板 — 昨日是连板第2天+放量
      非昨日2连板 — 近期有2连板但昨日不是涨停

    Args:
        kline_days: 日K线回看天数
        force_refresh: 强制刷新, 忽略缓存
    """
    print(f"\n{'='*70}")
    print(f"  📋 盘前选股")
    print(f"  选股池: 首板(新/旧) + 昨日2连板 + 非昨日2连板")
    print(f"  过滤: 排除ST, 首板放量(量比>=1.5)")
    print(f"{'='*70}")

    # 0. 检查缓存
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached

    # 1. 加载候选
    print(f"\n  [1/2] 扫描全市场...")
    raw = load_all_candidates(kline_days)

    # 统计
    counts = {}
    for c in raw:
        counts[c['source']] = counts.get(c['source'], 0) + 1
    for cat in ['首板(新)', '首板(旧)', '昨日2连板', '非昨日2连板']:
        if counts.get(cat):
            print(f"        {cat}: {counts[cat]}只")

    if not raw:
        print("  ❌ 无候选")
        return []

    # 2. 构建候选列表 (只取K线历史数据, 实时数据在过滤阶段获取)
    print(f"\n  [2/2] 构建候选 ({len(raw)}只)...")
    candidates = []
    for info in raw:
        code = info['stock_code']
        bars = load_kline_db(code, kline_days)
        if not bars or len(bars) < 2:
            continue

        idx = len(bars) - 1
        last_bar = bars[idx]
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        change_pct = (last_bar['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
        yesterday_momentum = (last_bar['close'] - last_bar['open']) / prev_close * 100 if prev_close > 0 else 0

        candidates.append({
            'code': code,
            'name': info.get('stock_name', ''),
            'board': get_board_name(code),
            'limit_pct': get_limit_pct(code),
            'source': info['source'],
            'continuous_zt_days': int(info.get('continuous_zt_days', 1) or 1),
            'last_close': last_bar['close'],
            'last_date': last_bar['time'],
            'change_pct': round(change_pct, 2),
            'yesterday_momentum': round(yesterday_momentum, 2),
            'vol_ratio': info.get('vol_ratio', 0),
            'prev_close': prev_close,
            'last_volume': last_bar['volume'],
        })

    # 按分类排序
    order = {'首板(新)': 0, '首板(旧)': 1, '昨日2连板': 2, '非昨日2连板': 3}
    candidates.sort(key=lambda x: order.get(x['source'], 9))

    # 按分类输出全部候选
    # 注入技术分 (tech_score), 过滤低分候选
    print(f"\n  [3/3] 技术评分过滤 (tech_score >= 60)...")
    filtered = []
    for c in candidates:
        code = c['code']
        bars = load_kline_db(code, kline_days)
        if not bars or len(bars) < 20:
            continue
        idx = len(bars) - 1
        tech = calc_daily_tech_details(bars, idx)
        c['tech_score'] = tech.get('tech_score', 0)
        c['ma_bull'] = tech.get('ma_bull', False)
        c['rsi14'] = tech.get('rsi14', 0)
        c['kdj_k'] = tech.get('kdj_k', 0)
        c['obv_trend'] = tech.get('obv_trend', '平')
        c['ma5_angle'] = tech.get('ma5_angle', 0)

        # 技术分过滤: 低于60分的候选不进入监控
        if c['tech_score'] < 60:
            continue
        filtered.append(c)

    dropped = len(candidates) - len(filtered)
    if dropped > 0:
        print(f"        过滤掉 {dropped} 只低技术分候选 (tech_score < 60)")
    candidates = filtered

    print(f"\n  结果: {len(candidates)}只")
    if candidates:
        for cat in ['首板(新)', '首板(旧)', '昨日2连板', '非昨日2连板']:
            group = [c for c in candidates if c['source'] == cat]
            if not group:
                continue
            cat_emoji = {'首板(新)': '🆕', '首板(旧)': '🔄', '昨日2连板': '🔗', '非昨日2连板': '📌'}
            print(f"\n  {cat_emoji.get(cat, '')} {cat} ({len(group)}只)")
            print(f"  {'代码':>8} {'名称':>8} {'板块':>6} {'量比':>5} {'昨动量':>7} {'技分':>4} {'多头':>3} {'RSI':>5} {'OBV':>3}")
            print(f"  {'-'*65}")
            for c in group:
                ym = c['yesterday_momentum']
                vr = c.get('vol_ratio', 0)
                ym_emoji = '💪' if ym >= 5 else ('📈' if ym >= 3 else '')
                vr_str = f"{vr:.1f}x" if vr > 0 else '-'
                obv_short = {'上升':'↑','下降':'↓','平':'—'}.get(c.get('obv_trend',''), '—')
                bull = '✓' if c.get('ma_bull') else '✗'
                rsi_str = f"{c.get('rsi14', 0):.0f}" if c.get('rsi14') else '-'
                print(f"  {c['code']:>8} {c['name']:>8} {c['board']:>6} "
                      f"{vr_str:>5} {ym:>+6.1f}%{ym_emoji} {c.get('tech_score',0):>4} {bull:>3} {rsi_str:>5} {obv_short:>3}")

    # 保存缓存 (含技术分)
    _save_cache(candidates)

    return candidates


# ================================================================
# 实时行情 (批量 + 单股分时)
# ================================================================

_batch_cache = {}  # code -> {price, open, prev_close, change_pct, ...}

def _snapshot_table_name() -> str:
    """返回当前年份的快照表名 (realtime_snapshot_YYYY)"""
    return f"realtime_snapshot_{datetime.now().year}"


def fetch_batch_quotes(codes: List[str]) -> Dict[str, Dict]:
    """批量获取实时行情 — 从 realtime_snapshot_YYYY 读取每只股票的最新快照。

    返回: {code: {last, open, high, low, previousClose, volume, ...}}
    """
    if not codes:
        return {}
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        table = _snapshot_table_name()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 用 DISTINCT ON 取每只股票今天的最新快照
        placeholders = ", ".join(["%s"] * len(codes))
        sql = f"""
            SELECT DISTINCT ON (symbol)
                symbol, "last", open, high, low, "previousClose", volume, extras, time
            FROM "{table}"
            WHERE symbol IN ({placeholders})
              AND time >= %s
            ORDER BY symbol, time DESC
        """
        params = list(codes) + [f"{today_str} 00:00:00"]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        result = {}
        for row in rows:
            sym = row[0]
            last = float(row[1] or 0)
            if last <= 0:
                continue

            extras = row[7]
            if isinstance(extras, str):
                import json as _json
                try:
                    extras = _json.loads(extras)
                except Exception:
                    extras = {}

            result[sym] = {
                'symbol': sym,
                'last': last,
                'open': float(row[2] or 0),
                'high': float(row[3] or 0),
                'low': float(row[4] or 0),
                'previousClose': float(row[5] or 0),
                'volume': float(row[6] or 0),
                'time': str(row[8]),
            }
            # 把 extras 中的字段也合并进去
            if isinstance(extras, dict):
                for k, v in extras.items():
                    if k not in result[sym] and v is not None:
                        result[sym][k] = v

        return result
    except Exception as e:
        print(f"  ⚠️ 从快照表读取批量行情失败: {e}")
        return {}

def _get_quote_price(quote: Dict) -> float:
    """从行情dict取最新价, 兼容不同源的字段名"""
    return quote.get('last', 0) or quote.get('price', 0) or quote.get('close', 0)


def _get_quote_prev_close(quote: Dict) -> float:
    """从行情dict取昨收, 兼容不同源的字段名"""
    return quote.get('previousClose', 0) or quote.get('prev_close', 0)


def _calc_change_pct(quote: Dict) -> float:
    """实时计算涨跌幅, 不依赖源的 changePercent/change_pct 字段(可能过期)"""
    last = _get_quote_price(quote)
    prev = _get_quote_prev_close(quote)
    if prev <= 0 or last <= 0:
        return 0.0
    return round((last / prev - 1) * 100, 2)


def _quick_momentum(code: str, quote: Dict) -> Tuple[float, float]:
    """从批量行情快速计算: (今日动量, 高开幅度)

    今日动量 = (最新价 - 开盘) / 昨收 * 100
    高开幅度 = (开盘 - 昨收) / 昨收 * 100
    """
    last = _get_quote_price(quote)
    open_p = quote.get('open', 0)
    prev = _get_quote_prev_close(quote)
    if prev <= 0 or last <= 0:
        return 0.0, 0.0
    momentum = (last - open_p) / prev * 100 if open_p > 0 else 0.0
    gap = (open_p - prev) / prev * 100 if prev > 0 else 0.0
    return round(momentum, 2), round(gap, 2)

def _quick_above_vwap(code: str, quote: Dict) -> bool:
    """粗略判断: 现价 > 开盘价 (代替VWAP判断, 批量行情无法算精确VWAP)"""
    last = _get_quote_price(quote)
    open_p = quote.get('open', 0)
    return last > 0 and open_p > 0 and last >= open_p


def prefilter_by_rules(candidates: List[Dict]) -> List[Dict]:
    """用批量行情做快速预筛选, 只对通过的再拉分时数据

    规则:
      所有分类全部保留 — 实时信号由 detect_signal 统一判断
      预筛选仅用于获取批量行情缓存
    """
    global _batch_cache
    codes = [c['code'] for c in candidates]
    quotes = fetch_batch_quotes(codes)
    if not quotes:
        return candidates  # 批量失败, 降级为全部
    _batch_cache = quotes  # 缓存供状态行使用

    matched = []
    for c in candidates:
        q = quotes.get(c['code'])
        if not q:
            continue
        c['_quote'] = q
        today_mom, open_gap = _quick_momentum(c['code'], q)
        c['today_momentum'] = today_mom
        c['realtime_change_pct'] = _calc_change_pct(q)
        matched.append(c)

    return matched


# ================================================================
# 分时数据 (从 realtime_snapshot_YYYY 快照表读取)
# ================================================================

def fetch_minute_klines_batch(codes: List[str], count: int = 240) -> Dict[str, List[Dict]]:
    """批量获取当天分时序列 — 从 realtime_snapshot_YYYY 读取当天快照重构。

    每行快照的 "last"(最新价) 作为 close, open/high/low 直接取。
    快照表 volume 是当日累计成交量，需要转换为每分钟增量 volume。
    extras.amount 同理是累计值，也转为增量。

    Args:
        codes: 股票代码列表
        count: 预留参数 (兼容旧接口), 实际返回当天所有快照

    Returns:
        {code: [{time, open, high, low, close, volume, amount}, ...]}
    """
    if not codes:
        return {}
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        table = _snapshot_table_name()
        today_str = datetime.now().strftime("%Y-%m-%d")

        placeholders = ", ".join(["%s"] * len(codes))
        sql = f"""
            SELECT symbol, time, "last", open, high, low, volume, extras
            FROM "{table}"
            WHERE symbol IN ({placeholders})
              AND time >= %s
            ORDER BY symbol, time ASC
        """
        params = list(codes) + [f"{today_str} 00:00:00"]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        # 先按 symbol 分组收集原始数据
        raw = {}  # symbol -> [(time, last, open, high, low, cum_vol, cum_amount), ...]
        for row in rows:
            sym = row[0]
            if sym not in raw:
                raw[sym] = []

            last = float(row[2] or 0)
            cum_vol = float(row[6] or 0)

            # 从 extras 中提取累计 amount
            cum_amount = 0.0
            extras = row[7]
            if isinstance(extras, str):
                import json as _json
                try:
                    extras = _json.loads(extras)
                except Exception:
                    extras = {}
            if isinstance(extras, dict):
                cum_amount = float(extras.get('amount', 0) or 0)

            raw[sym].append((str(row[1]), last, float(row[3] or 0),
                             float(row[4] or 0), float(row[5] or 0),
                             cum_vol, cum_amount))

        # 转换: 累计量 → 增量
        result = {}
        for sym, bars in raw.items():
            ticks = []
            prev_vol = 0.0
            prev_amount = 0.0
            for (t, last, o, h, l, cum_vol, cum_amount) in bars:
                incr_vol = max(0, cum_vol - prev_vol)
                incr_amount = max(0, cum_amount - prev_amount)
                ticks.append({
                    'time': t,
                    'open': o, 'high': h, 'low': l,
                    'close': last,
                    'volume': incr_vol,
                    'amount': incr_amount,
                })
                prev_vol = cum_vol
                prev_amount = cum_amount
            if ticks:
                result[sym] = ticks
        return result
    except Exception as e:
        print(f"  ⚠️ 从快照表读取分时序列失败: {e}")
        return {}


def fetch_realtime_ticks(code: str) -> Optional[List[Dict]]:
    """单股当天分时序列 (兼容旧接口, 从快照表读取)"""
    result = fetch_minute_klines_batch([code])
    bars = result.get(code)
    return bars if bars else None


# ================================================================
# 分时指标计算
# ================================================================

def calc_vwap(ticks: List[Dict]) -> float:
    """计算VWAP (成交量加权平均价)

    优先用 成交额/成交量 (最准确), amount 不可用时回退到典型价格.
    """
    total_amount = 0.0
    total_vol = 0.0
    has_amount = False
    for t in ticks:
        amt = t.get('amount', 0)
        vol = t.get('volume', 0)
        if amt > 0 and vol > 0:
            total_amount += amt
            total_vol += vol
            has_amount = True
    if has_amount and total_vol > 0:
        return total_amount / total_vol
    # 回退: 典型价格加权
    total_pv = 0.0
    total_vol = 0.0
    for t in ticks:
        typical = (t['high'] + t['low'] + t['close']) / 3
        total_pv += typical * t['volume']
        total_vol += t['volume']
    return total_pv / total_vol if total_vol > 0 else 0.0


def calc_intraday_ma(ticks: List[Dict], period: int = 20) -> List[Optional[float]]:
    """计算分时移动平均 (基于close)"""
    result = []
    for i in range(len(ticks)):
        if i < period - 1:
            result.append(None)
        else:
            vals = [ticks[j]['close'] for j in range(i - period + 1, i + 1)]
            result.append(sum(vals) / period)
    return result


def calc_intraday_vol_ratio(ticks: List[Dict], idx: int, window: int = 5) -> float:
    """分时量比: 最近1分钟量 vs 前window分钟均量"""
    if idx < window or window <= 0:
        return 1.0
    avg_vol = sum(ticks[i]['volume'] for i in range(idx - window, idx)) / window
    if avg_vol <= 0:
        return 1.0
    return ticks[idx]['volume'] / avg_vol


# ================================================================
# VWAP 精确计算 (从快照表 extras.amount)
# ================================================================

def fetch_vwap_from_snapshot(codes: List[str]) -> Dict[str, float]:
    """从 realtime_snapshot_YYYY 快照表计算当日 VWAP。

    快照表的 volume 和 extras.amount 是当日累计值。
    VWAP = 最后一行的 cumsum(amount) / 最后一行的 cumsum(volume)。
    extras 无 amount 时该 symbol 跳过。

    Returns:
        {code: vwap_price}  — 无 amount 数据时返回空 dict
    """
    if not codes:
        return {}
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        table = _snapshot_table_name()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 用 DISTINCT ON 取每只股票今天的最后一条快照 (累计值)
        placeholders = ", ".join(["%s"] * len(codes))
        sql = f"""
            SELECT DISTINCT ON (symbol)
                symbol, volume, extras
            FROM "{table}"
            WHERE symbol IN ({placeholders})
              AND time >= %s
            ORDER BY symbol, time DESC
        """
        params = list(codes) + [f"{today_str} 00:00:00"]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        result = {}
        for row in rows:
            sym = row[0]
            cum_vol = float(row[1] or 0)
            if cum_vol <= 0:
                continue

            extras = row[2]
            if isinstance(extras, str):
                import json as _json
                try:
                    extras = _json.loads(extras)
                except Exception:
                    extras = {}
            cum_amount = 0.0
            if isinstance(extras, dict):
                cum_amount = float(extras.get('amount', 0) or 0)

            if cum_amount > 0:
                result[sym] = cum_amount / cum_vol

        return result
    except Exception:
        return {}


# ================================================================
# 弱转强信号检测
# ================================================================

def _count_minutes_above_vwap(ticks: List[Dict], vwap: float = 0) -> int:
    """从当前往回数, 连续站稳VWAP上方的分钟数

    用给定的 VWAP (或从 ticks 计算), 往回逐分钟检查 close >= VWAP.
    遇到第一根 close < VWAP 即中断, 返回连续站稳的分钟数.
    """
    if not ticks:
        return 0
    if vwap <= 0:
        vwap = calc_vwap(ticks)
    if vwap <= 0:
        return 0
    count = 0
    for i in range(len(ticks) - 1, -1, -1):
        if ticks[i]['close'] >= vwap:
            count += 1
        else:
            break
    return count


def detect_signal(ticks: List[Dict], candidate: Dict) -> Optional[Dict]:
    """盘中信号检测 (简化版)

    日线筛选已做过滤, 盘中只判断入场时机:
      核心条件: 现价 > VWAP + 日内动量 > 0%
      强度分级: 强(>=3%) 中(0~3%) 弱(<0%)

    Returns:
        信号详情 dict 或 None
    """
    if len(ticks) < 5:
        return None

    current = ticks[-1]
    current_price = current['close']
    prev_close = candidate.get('prev_close', 0)
    if prev_close <= 0:
        return None

    source = candidate.get('source', '')
    change_pct = (current_price / prev_close - 1) * 100
    vwap = candidate.get('_precise_vwap', 0)
    if vwap <= 0:
        vwap = calc_vwap(ticks)
    if vwap <= 0:
        return None
    price_vs_vwap = (current_price / vwap - 1) * 100
    today_open = ticks[0]['open'] if ticks else 0
    open_gap = (today_open / prev_close - 1) * 100 if prev_close > 0 else 0
    today_momentum = (current_price - today_open) / prev_close * 100 if prev_close > 0 else 0

    # 日内动量分级
    if today_momentum >= 3.0:
        strength = '强'
    elif today_momentum >= 0:
        strength = '中'
    else:
        strength = '弱'

    # ===== 核心信号: 现价 > VWAP + 日内动量 > 0% =====
    if current_price < vwap or today_momentum < 0:
        return None

    signal = {
        'type': f'{source}_入场',
        'label': f'VWAP上方+动量{today_momentum:+.1f}%',
        'emoji': '💪' if strength == '强' else ('📊' if strength == '中' else '⚠️'),
        'strength': strength,
        'detail': (f"现价{current_price:.2f} VWAP{vwap:.2f}({price_vs_vwap:+.1f}%) "
                   f"高开{open_gap:+.1f}% 日内{today_momentum:+.1f}%[{strength}]"),
    }

    signal.update({
        'code': candidate['code'],
        'name': candidate.get('name', ''),
        'board': candidate.get('board', ''),
        'limit_pct': candidate.get('limit_pct', 10),
        'source': source,
        'today_momentum': round(today_momentum, 2),
        'price': current_price,
        'vwap': round(vwap, 3),
        'price_vs_vwap': round(price_vs_vwap, 2),
        'change_pct': round(change_pct, 2),
        'open_gap': round(open_gap, 2),
        'time': current['time'],
        'intraday_high': max(t['high'] for t in ticks),
        'intraday_low': min(t['low'] for t in ticks),
        'intraday_momentum': round(today_momentum, 2),
    })

    return signal


# ================================================================
# 状态跟踪 (避免重复信号)
# ================================================================

class ConsecutiveTracker:
    """跟踪每只股票连续符合信号的次数

    - 每次扫描到信号: count += 1, 显示
    - 中断(本轮未扫到): count 归零, 下次从 1 重新计数
    - 同一分钟内同一只股票只计一次
    """

    def __init__(self):
        self._counts = {}      # code -> 连续符合次数
        self._this_round = set()  # 本轮已记录的 code

    def start_round(self):
        """每轮扫描开始时调用"""
        self._this_round = set()

    def record(self, code: str) -> int:
        """记录一次符合, 返回当前连续次数"""
        if code in self._this_round:
            return self._counts.get(code, 1)
        self._this_round.add(code)
        self._counts[code] = self._counts.get(code, 0) + 1
        return self._counts[code]

    def finalize_round(self):
        """本轮扫描结束, 清零本轮未出现的股票"""
        to_remove = [c for c in self._counts if c not in self._this_round]
        for c in to_remove:
            del self._counts[c]


# ================================================================
# 主监控循环
# ================================================================

def run_monitor(candidates: List[Dict], interval: int = 60, force: bool = False):
    """盘中实时监控循环

    Args:
        candidates: 候选股列表 (来自screen_candidates)
        interval: 轮询间隔(秒)
        force: 忽略交易时间限制 (用于午休/盘前测试)
    """
    if not candidates:
        print("\n  ❌ 无候选股, 退出监控")
        return

    tracker = ConsecutiveTracker()
    all_signals = []
    _stop_event = threading.Event()

    print(f"\n{'='*70}")
    print(f"  🔴 盘中实时监控已启动")
    print(f"  候选: {len(candidates)}只 | 间隔: {interval}秒")
    print(f"  规则: 2连板高开不破均线 | 首板(旧)/非昨2连板站稳VWAP>15min | 首板(新)动量加速")
    print(f"  按 Ctrl+C 停止")
    print(f"{'='*70}\n")

    # 交易时间判断
    def is_trading_time() -> bool:
        now = datetime.now()
        t = now.hour * 100 + now.minute
        # 9:25~11:30, 13:00~15:00
        return (925 <= t <= 1130) or (1300 <= t <= 1500)

    scan_count = 0

    try:
        while not _stop_event.is_set():
            now = datetime.now()

            # 非交易时间 (force 模式跳过)
            if not force and not is_trading_time():
                t = now.hour * 100 + now.minute
                if 1130 < t < 1300:
                    print(f"\r  ⏸  午休中... ({now.strftime('%H:%M')})", end="", flush=True)
                    _stop_event.wait(timeout=30)
                    continue
                elif t > 1500:
                    print(f"\n\n  📊 收盘, 监控结束")
                    break
                elif t < 925:
                    wait_sec = ((9 - now.hour) * 60 + (25 - now.minute)) * 60
                    print(f"\r  ⏳ 等待开盘... ({now.strftime('%H:%M')}, 约{wait_sec // 60}分钟后)", end="", flush=True)
                    _stop_event.wait(timeout=min(60, max(10, wait_sec)))
                    continue

            scan_count += 1
            current_time = now.strftime("%Y-%m-%d %H:%M")
            triggered_this_round = []
            tracker.start_round()

            # Step1: 批量行情 (1次HTTP, 获取实时价格)
            quick_matched = prefilter_by_rules(candidates)

            if quick_matched:
                vwap_codes = [c['code'] for c in quick_matched]

                # Step2: 从快照表计算精确 VWAP (extras.amount)
                snapshot_vwap = {}
                try:
                    snapshot_vwap = fetch_vwap_from_snapshot(vwap_codes)
                except Exception:
                    pass

                # Step3: 从快照表读取当天全量分时序列
                minute_data = fetch_minute_klines_batch(vwap_codes, count=240)

                for cand in quick_matched:
                    code = cand['code']

                    ticks = minute_data.get(code)
                    if not ticks or len(ticks) < 5:
                        continue

                    # 如果快照表有精确 VWAP，注入到 candidate 供 detect_signal 使用
                    if code in snapshot_vwap:
                        cand['_precise_vwap'] = snapshot_vwap[code]

                    signal = detect_signal(ticks, cand)
                    if signal is None:
                        continue
                    count = tracker.record(code)
                    signal['_consecutive'] = count
                    # 保存调试数据 (含原始 ticks 用于分析)
                    vwap_val = cand.get('_precise_vwap', 0) or calc_vwap(ticks)
                    signal['_debug'] = {
                        'ticks_count': len(ticks),
                        'first_tick': ticks[0] if ticks else None,
                        'last_tick': ticks[-1] if ticks else None,
                        'calc_vwap': round(calc_vwap(ticks), 4),
                        'snapshot_vwap': cand.get('_precise_vwap', 0),
                        'open': ticks[0]['open'] if ticks else 0,
                        'high': max(t['high'] for t in ticks) if ticks else 0,
                        'low': min(t['low'] for t in ticks) if ticks else 0,
                        # 保存全部 ticks (精简: 只存 close 和 amount)
                        'ticks': [round(t['close'], 4) for t in ticks],
                        'tick_times': ticks[0]['time'][-8:] + '~' + ticks[-1]['time'][-8:],
                        'vwap': round(vwap_val, 4),
                    }
                    triggered_this_round.append(signal)
                    all_signals.append(signal)

            # 保存全量候选 VWAP 状态 (每轮)
            _vwap_debug = []
            for cand in quick_matched:
                code = cand['code']
                q = _batch_cache.get(code, {})
                last = _get_quote_price(q)
                vwap_val = cand.get('_precise_vwap', 0)
                if vwap_val <= 0:
                    tks = minute_data.get(code)
                    if tks:
                        vwap_val = calc_vwap(tks)
                _vwap_debug.append({
                    'code': code, 'name': cand.get('name', ''),
                    'source': cand.get('source', ''),
                    'price': last, 'vwap': round(vwap_val, 4),
                    'above': last >= vwap_val if vwap_val > 0 else None,
                    'data_source': 'snapshot',
                })
            try:
                _vwap_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'tmp', 'realtime_vwap_status.json')
                with open(_vwap_file, 'w', encoding='utf-8') as f:
                    json.dump({'time': current_time, 'count': len(_vwap_debug),
                               'stocks': _vwap_debug}, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            # 本轮结束, 清零未出现的
            tracker.finalize_round()

            # 输出本轮结果
            if triggered_this_round:
                _src_short = {'首板(新)': '首板新', '首板(旧)': '首板旧',
                              '昨日2连板': '2连板', '非昨日2连板': '非昨2连'}

                if not hasattr(run_monitor, '_header_printed'):
                    run_monitor._header_printed = True
                    print(f"  {'连续':>4} {'时间':>5} {'代码':>7} {'名称':>6} {'分类':>7} {'强度':>4} {'技分':>4} "
                          f"{'价格':>7} {'涨跌':>6} {'高开':>5} {'日内':>5} {'站稳':>4} {'止损':>7} {'涨停':>7}")
                    print(f"  {'-'*96}")
                elif hasattr(run_monitor, '_prev_round'):
                    print(f"  {'·'*88}")

                run_monitor._prev_round = True

                for sig in triggered_this_round:
                    limit_pct = sig['limit_pct']
                    board = sig['board']
                    buy_price = sig['price']
                    stop_loss = -5.0 if board in ['沪主板', '深主板'] else -8.0
                    stop_price = buy_price * (1 + stop_loss / 100)
                    limit_price = buy_price / (1 + sig['change_pct'] / 100) * (1 + limit_pct / 100) if sig['change_pct'] != -100 else 0
                    time_str = now.strftime('%H:%M')
                    above_min = sig.get('above_vwap_minutes', 0)
                    above_str = f"{above_min}m" if above_min > 0 else '-'
                    src = _src_short.get(sig['source'], sig['source'])
                    name = sig.get('name', '')[:4]
                    c = sig['_consecutive']
                    strength = sig.get('strength', '-')
                    strength_emoji = {'强': '💪', '中': '📊', '弱': '⚠️'}.get(strength, '')
                    ts = sig.get('tech_score', 0)
                    intra = sig.get('intraday_momentum', sig.get('today_momentum', 0))
                    print(f"  {c:>4} {time_str:>5} {sig['code']:>7} {name:>6} {src:>7} "
                          f"{strength_emoji:>2}{strength:>2} {ts:>4} "
                          f"{buy_price:>7.2f} {sig['change_pct']:>+5.1f}% {sig.get('open_gap', 0):>+5.1f}% "
                          f"{intra:>+5.1f}% {above_str:>4} {stop_price:>7.2f} {limit_price:>7.2f}")

                # 保存本轮调试数据
                _debug_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           'tmp', 'realtime_debug.json')
                _debug_data = []
                for sig in triggered_this_round:
                    dbg = sig.get('_debug', {})
                    _debug_data.append({
                        'time': sig.get('time', ''),
                        'code': sig.get('code', ''),
                        'name': sig.get('name', ''),
                        'source': sig.get('source', ''),
                        'strength': sig.get('strength', '-'),
                        'tech_score': sig.get('tech_score', 0),
                        'price': sig.get('price', 0),
                        'change_pct': sig.get('change_pct', 0),
                        'open_gap': sig.get('open_gap', 0),
                        'today_momentum': sig.get('today_momentum', 0),
                        'intraday_momentum': sig.get('intraday_momentum', 0),
                        'vwap': sig.get('vwap', 0),
                        'above_vwap_minutes': sig.get('above_vwap_minutes', 0),
                        'calc_vwap': dbg.get('calc_vwap', 0),
                        'snapshot_vwap': dbg.get('snapshot_vwap', 0),
                        'ticks_count': dbg.get('ticks_count', 0),
                        'day_open': dbg.get('open', 0),
                        'day_high': dbg.get('high', 0),
                        'day_low': dbg.get('low', 0),
                        'first_tick': dbg.get('first_tick'),
                        'last_tick': dbg.get('last_tick'),
                        'ticks': dbg.get('ticks', []),
                    })
                try:
                    os.makedirs(os.path.dirname(_debug_file), exist_ok=True)
                    with open(_debug_file, 'w', encoding='utf-8') as f:
                        json.dump({'scan_time': current_time, 'signals': _debug_data},
                                  f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            else:
                # 静默: 仅显示计数, 不显示个股价格
                print(f"\r  ⏱ [{scan_count}] {now.strftime('%H:%M:%S')} "
                      f"扫描{len(candidates)}只 无信号    ", end="", flush=True)

            # 可中断的 sleep
            _stop_event.wait(timeout=interval)

    except KeyboardInterrupt:
        print(f"\n\n  ⛔ 手动停止")

    # 清理 + 汇总 (含 V1 日内动量出场建议)
    if all_signals:
        codes_set = set(s['code'] for s in all_signals)
        print(f"\n  📊 今日共 {len(all_signals)} 个信号 (去重{len(codes_set)}只)")

        # 按信号强度统计
        strong = [s for s in all_signals if s.get('strength') == '强']
        medium = [s for s in all_signals if s.get('strength') == '中']
        weak = [s for s in all_signals if s.get('strength') == '弱']
        print(f"  💪 强信号(日内>=3%): {len(strong)}个 | "
              f"📊 中信号(0~3%): {len(medium)}个 | "
              f"⚠️ 弱信号(<0%): {len(weak)}个")

        # V1 日内动量出场建议
        if strong:
            print(f"\n  💡 V1出场规则 (来自 test_dragon.py, 99%胜率持有组):")
            print(f"     日内动量>=3% → 继续持有, 追踪止损-5%, 持仓上限20天")
            print(f"     日内动量<3%  → 明日开盘清仓 (盘中买盘不足)")
            print(f"     建议关注: {[s['code'] for s in strong]}")
    else:
        print(f"\n  📊 今日无信号")

    # 导出 (精简版, 不含大字段)
    if all_signals:
        outfile = "realtime_signals.json"
        export = []
        for s in all_signals:
            d = {k: v for k, v in s.items() if k not in ('_debug',)}
            export.append(d)
        try:
            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=2)
            print(f"\n  💾 信号已导出: {outfile}")
        except Exception as e:
            print(f"\n  ⚠️ 导出失败: {e}")


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="首板/2连板盘中实时监控 (V2 — 多维过滤版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python realtime_monitor.py                     # 完整运行
  python realtime_monitor.py --scan              # 仅选股
  python realtime_monitor.py --codes 000066,300001  # 手动指定
        """)
    parser.add_argument("--scan", action="store_true", help="仅选股, 不启动盘中监控")
    parser.add_argument("--codes", type=str, default="", help="手动指定股票代码, 逗号分隔")
    parser.add_argument("--refresh", action="store_true", help="强制刷新, 忽略当天缓存")
    parser.add_argument("--interval", type=int, default=60, help="盘中轮询间隔秒数 (默认60)")
    parser.add_argument("--force", action="store_true", help="强制运行, 忽略交易时间限制")
    parser.add_argument("--kline-days", type=int, default=30, help="日K线回看天数 (默认30)")
    parser.add_argument("--pool", type=str, default="", help="加载 daily_screener 产出的候选池 JSON")
    args = parser.parse_args()

    print("=" * 70)
    print("  🔍 首板/2连板盘中实时监控")
    print("=" * 70)

    # 候选池加载
    if args.pool:
        # 从 daily_screener 产出的候选池加载
        with open(args.pool, 'r', encoding='utf-8') as f:
            pool_data = json.load(f)
        raw = pool_data.get('candidates', [])
        print(f"  📦 从候选池加载: {len(raw)}只 (生成于 {pool_data.get('generated_at', '?')})")
        candidates = []
        for c in raw:
            candidates.append({
                'code': c['code'],
                'name': c.get('name', ''),
                'board': get_board_name(c['code']),
                'limit_pct': get_limit_pct(c['code']),
                'source': c.get('primary_source', c.get('source', '')),
                'sources': c.get('sources', []),
                'quality_score': c.get('quality_score', 0),
                'prev_close': 0,  # 盘中实时获取
                'last_close': 0,
                'last_volume': 0,
                'yesterday_momentum': 0,
                'vol_ratio': c.get('vol_ratio', 0),
                'tech_score': c.get('tech_score', 0),
            })
        print(f"        来源分布: {dict(pool_data.get('sources', {}))}")
    elif args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        candidates = []
        for code in codes:
            bars = load_kline_db(code, args.kline_days)
            if not bars or len(bars) < 2:
                print(f"  ⚠️ {code}: K线数据不足")
                continue
            idx = len(bars) - 1
            last_bar = bars[idx]
            prev_close = bars[idx - 1]['close'] if idx > 0 else 0
            candidates.append({
                'code': code, 'name': '', 'board': get_board_name(code),
                'limit_pct': get_limit_pct(code),
                'last_close': last_bar['close'], 'last_date': last_bar['time'],
                'change_pct': round((last_bar['close'] / prev_close - 1) * 100, 2) if prev_close > 0 else 0,
                'source': '(手动指定)',
                'prev_close': prev_close,
                'last_volume': last_bar['volume'],
                'yesterday_momentum': 0,
                'vol_ratio': 0,
                'tech_score': 0,
            })
        print(f"  手动指定: {len(candidates)}只")
    else:
        candidates = screen_candidates(kline_days=args.kline_days, force_refresh=args.refresh)

    print(f"  信号规则: 现价>VWAP + 日内动量>0%")
    print(f"  强度分级: 强(日内>=3%) 中(0~3%) 弱(<0%)")

    if args.scan:
        print(f"\n  ✅ 选股完成 (--scan 模式, 不启动监控)")
        return

    # 盘中监控
    run_monitor(candidates, interval=args.interval, force=args.force)

    # 强制退出: mootdx/coordinator/数据库连接池的非守护线程会阻止进程退出
    os._exit(0)


if __name__ == "__main__":
    main()
