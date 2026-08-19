#!/usr/bin/env python3
"""
首板/2连板盘中实时监控
━━━━━━━━━━━━━━━━━━━━━

功能:
  1. 盘前选股: 4类候选 (排除ST, 首板放量)
  2. 盘中实时: 每分钟拉取分时数据, 检测弱转强信号
  3. 买入建议: 触发时输出建议买入价 + 信号类型

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
  排除ST股, 首板量比>=1.5

候选列表缓存到 .openclaw/tmp/realtime_candidates.json (文件内判断日期)

盘中信号规则 (按分类触发):
  昨日2连板 — 高开>0% 且 现价不破VWAP
  非昨日2连板 — 突破VWAP并连续站稳超15分钟
  首板(旧)   — 突破VWAP并连续站稳超15分钟
  首板(新)   — 今日动量 > 昨日动量 × 1.5
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional

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
            if vol_ratio < 1.5:
                continue
            has_recent = _has_recent_limit_up(bars, idx - 1, 5, bt)
            cat = '首板(新)' if not has_recent else '首板(旧)'
            candidates.append({
                'stock_code': code, 'stock_name': stock_names.get(code, ''),
                'source': cat, 'continuous_zt_days': 1,
                'vol_ratio': round(vol_ratio, 2),
            })

        elif yesterday_lu and day_before_lu:
            # 昨日是连板 (2连板或更多)
            # 往前数连续涨停天数
            streak = 2
            while idx - streak >= 1 and _is_limit_up(
                    bars[idx - streak + 1]['close'], bars[idx - streak]['close'], bt):
                streak += 1
            # 取首板日的量比
            first_lu_idx = idx - streak + 1
            vol_ratio = _calc_vol_ratio(bars, first_lu_idx, 5)
            if vol_ratio < 1.5:
                continue
            candidates.append({
                'stock_code': code, 'stock_name': stock_names.get(code, ''),
                'source': '昨日2连板', 'continuous_zt_days': streak,
                'vol_ratio': round(vol_ratio, 2),
            })

        else:
            # 昨日不是涨停, 检查近期是否有2连板
            limit_indices = _find_limit_up_indices(bars, bt)
            if len(limit_indices) < 2:
                continue
            # 找最长连续涨停
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
            if max_streak < 2:
                continue
            # 首板放量
            vol_ratio = _calc_vol_ratio(bars, best_start, 5)
            if vol_ratio < 1.5:
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

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".openclaw", "tmp")
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
        })

    # 按分类排序
    order = {'首板(新)': 0, '首板(旧)': 1, '昨日2连板': 2, '非昨日2连板': 3}
    candidates.sort(key=lambda x: order.get(x['source'], 9))

    # 按分类输出全部候选
    print(f"\n  结果: {len(candidates)}只")
    if candidates:
        for cat in ['首板(新)', '首板(旧)', '昨日2连板', '非昨日2连板']:
            group = [c for c in candidates if c['source'] == cat]
            if not group:
                continue
            cat_emoji = {'首板(新)': '🆕', '首板(旧)': '🔄', '昨日2连板': '🔗', '非昨日2连板': '📌'}
            print(f"\n  {cat_emoji.get(cat, '')} {cat} ({len(group)}只)")
            print(f"  {'代码':>8} {'名称':>8} {'板块':>6} {'量比':>5} {'昨动量':>7}")
            print(f"  {'-'*50}")
            for c in group:
                ym = c['yesterday_momentum']
                vr = c.get('vol_ratio', 0)
                ym_emoji = '💪' if ym >= 5 else ('📈' if ym >= 3 else '')
                vr_str = f"{vr:.1f}x" if vr > 0 else '-'
                print(f"  {c['code']:>8} {c['name']:>8} {c['board']:>6} "
                      f"{vr_str:>5} {ym:>+6.1f}%{ym_emoji}")

    # 保存缓存
    _save_cache(candidates)

    return candidates


# ================================================================
# 实时行情 (批量 + 单股分时)
# ================================================================

_batch_cache = {}  # code -> {price, open, prev_close, change_pct, ...}

def fetch_batch_quotes(codes: List[str]) -> Dict[str, Dict]:
    """批量获取实时行情 (新浪500只/次, 1~2次HTTP搞定)

    返回: {code: {last, open, previousClose, changePercent, volume, amount}}
    """
    try:
        from app.data_sources.coordinator import get_coordinator
        coord = get_coordinator()
        quotes = coord.coordinate_tickers(symbols=codes, market="CNStock", timeout=15)
        result = {}
        for q in quotes:
            sym = q.get('symbol', '')
            if sym:
                result[sym] = q
        return result
    except Exception:
        return {}

def _quick_momentum(code: str, quote: Dict) -> Tuple[float, float]:
    """从批量行情快速计算: (今日动量, 高开幅度)

    今日动量 = (最新价 - 开盘) / 昨收 * 100
    高开幅度 = (开盘 - 昨收) / 昨收 * 100
    """
    last = quote.get('last', 0) or quote.get('price', 0)
    open_p = quote.get('open', 0)
    prev = quote.get('previousClose', 0) or quote.get('prev_close', 0)
    if prev <= 0 or last <= 0:
        return 0.0, 0.0
    momentum = (last - open_p) / prev * 100 if open_p > 0 else 0.0
    gap = (open_p - prev) / prev * 100 if prev > 0 else 0.0
    return round(momentum, 2), round(gap, 2)

def _quick_above_vwap(code: str, quote: Dict) -> bool:
    """粗略判断: 现价 > 开盘价 (代替VWAP判断, 批量行情无法算精确VWAP)"""
    last = quote.get('last', 0) or quote.get('price', 0)
    open_p = quote.get('open', 0)
    return last > 0 and open_p > 0 and last >= open_p


def prefilter_by_rules(candidates: List[Dict]) -> List[Dict]:
    """用批量行情做快速预筛选, 只对通过的再拉分时数据

    规则:
      昨日2连板 — 高开>0% 且 现价>开盘价
      非昨日2连板 / 首板(旧) — 需要VWAP, 无法预筛, 全部保留
      首板(新) — 今动量 > 昨动量*1.5
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
        source = c.get('source', '')
        today_mom, open_gap = _quick_momentum(c['code'], q)
        c['today_momentum'] = today_mom

        if source == '昨日2连板':
            # 高开>0% 且 现价>开盘价
            if open_gap > 0 and _quick_above_vwap(c['code'], q):
                matched.append(c)
        elif source == '首板(新)':
            # 今动量 > 昨动量*1.5
            ym = c.get('yesterday_momentum', 0)
            if ym > 0 and today_mom > ym * 1.5:
                matched.append(c)
        else:
            # 首板(旧) / 非昨日2连板 — 需要VWAP, 全部保留
            matched.append(c)

    return matched


# ================================================================
# 分时数据 (coordinator 批量1分钟K线)
# ================================================================

def fetch_minute_klines_batch(codes: List[str], count: int = 240) -> Dict[str, List[Dict]]:
    """批量获取1分钟K线 (coordinator 多源并发, 有限流/熔断/重试)

    Args:
        codes: 股票代码列表
        count: 每只股票取多少根K线 (默认240, 约一个交易日)

    Returns:
        {code: [{time, open, high, low, close, volume}, ...]}
    """
    try:
        from app.data_sources.coordinator import get_coordinator
        coord = get_coordinator()
        bars_list = coord.coordinate_market_kline(
            market="CNStock", timeframe="1m", count=count,
            symbols=codes, timeout=30,
        )
        result = {}
        for bar in bars_list:
            sym = bar.get('symbol', '')
            if not sym:
                continue
            if sym not in result:
                result[sym] = []
            result[sym].append({
                'time': str(bar.get('time', '')),
                'open': float(bar.get('open', 0)),
                'high': float(bar.get('high', 0)),
                'low': float(bar.get('low', 0)),
                'close': float(bar.get('close', 0)),
                'volume': float(bar.get('volume', 0)),
            })
        return result
    except Exception:
        return {}


def fetch_realtime_ticks(code: str) -> Optional[List[Dict]]:
    """单股1分钟K线 (兼容旧接口, 内部走coordinator)"""
    result = fetch_minute_klines_batch([code], count=240)
    bars = result.get(code)
    return bars if bars else None


# ================================================================
# 分时指标计算
# ================================================================

def calc_vwap(ticks: List[Dict]) -> float:
    """计算VWAP (成交量加权平均价)

    VWAP = Σ(price × volume) / Σ(volume)
    price = (high + low + close) / 3  (典型价格)
    """
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
# 弱转强信号检测
# ================================================================

def _count_minutes_above_vwap(ticks: List[Dict]) -> int:
    """从当前往回数, 连续站稳VWAP上方的分钟数"""
    count = 0
    for i in range(len(ticks) - 1, -1, -1):
        v = calc_vwap(ticks[:i + 1])
        if ticks[i]['close'] >= v:
            count += 1
        else:
            break
    return count


def detect_signal(ticks: List[Dict], candidate: Dict) -> Optional[Dict]:
    """按候选分类检测信号 (不同分类不同规则)

    规则:
      昨日2连板 — 高开>0% 且 现价不破VWAP
      非昨日2连板 — 突破VWAP并连续站稳超15分钟
      首板(旧)   — 突破VWAP并连续站稳超15分钟
      首板(新)   — 今日动量 > 昨日动量 * 1.5

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
    vwap = calc_vwap(ticks)
    if vwap <= 0:
        return None
    price_vs_vwap = (current_price / vwap - 1) * 100
    today_open = ticks[0]['open'] if ticks else 0
    open_gap = (today_open / prev_close - 1) * 100 if prev_close > 0 else 0
    today_momentum = (current_price - today_open) / prev_close * 100 if prev_close > 0 else 0
    yesterday_momentum = candidate.get('yesterday_momentum', 0)

    signal = None

    # ========== 昨日2连板: 高开>0% 且 现价不破VWAP ==========
    if source == '昨日2连板':
        if open_gap > 0 and current_price >= vwap:
            signal = {
                'type': '2连板_高开不破均线',
                'label': '高开不破均线',
                'emoji': '🔗',
                'detail': (f"高开{open_gap:+.1f}%, "
                           f"现价{current_price:.2f} VWAP{vwap:.2f} "
                           f"({price_vs_vwap:+.1f}%)"),
            }

    # ========== 非昨日2连板 / 首板(旧): 突破VWAP站稳>20分钟 ==========
    elif source in ('非昨日2连板', '首板(旧)'):
        minutes_above = _count_minutes_above_vwap(ticks)
        if minutes_above >= 15 and current_price >= vwap:
            signal = {
                'type': f'{source}_站稳均线',
                'label': '站稳VWAP>15min',
                'emoji': '📈' if source == '首板(旧)' else '📌',
                'above_vwap_minutes': minutes_above,
                'detail': (f"连续{minutes_above}分钟站稳VWAP, "
                           f"当前高于VWAP {price_vs_vwap:+.1f}%"),
            }

    # ========== 首板(新): 今日动量 > 昨日动量 * 1.5 ==========
    elif source == '首板(新)':
        if yesterday_momentum > 0 and today_momentum > yesterday_momentum * 1.5:
            signal = {
                'type': '首板新_动量加速',
                'label': '动量加速',
                'emoji': '🆕',
                'detail': (f"今日动量{today_momentum:+.1f}% "
                           f"> 昨日{yesterday_momentum:+.1f}%×1.5="
                           f"{yesterday_momentum * 1.5:+.1f}%"),
            }

    if signal is None:
        return None

    # 补充公共字段
    signal.update({
        'code': candidate['code'],
        'name': candidate['name'],
        'board': candidate['board'],
        'limit_pct': candidate['limit_pct'],
        'source': source,
        'yesterday_momentum': yesterday_momentum,
        'today_momentum': round(today_momentum, 2),
        'price': current_price,
        'vwap': round(vwap, 3),
        'price_vs_vwap': round(price_vs_vwap, 2),
        'change_pct': round(change_pct, 2),
        'open_gap': round(open_gap, 2),
        'time': current['time'],
        'intraday_high': max(t['high'] for t in ticks),
        'intraday_low': min(t['low'] for t in ticks),
    })

    return signal


# ================================================================
# 状态跟踪 (避免重复信号)
# ================================================================

class SignalTracker:
    """跟踪已触发信号, 避免每分钟重复报警"""

    def __init__(self, cooldown_minutes: int = 30):
        self._triggered = {}  # code -> last_trigger_time
        self._cooldown = cooldown_minutes

    def should_alert(self, code: str, current_time: str) -> bool:
        """是否应该发出警报"""
        last = self._triggered.get(code)
        if last is None:
            return True
        # 解析时间差
        try:
            t_now = datetime.strptime(current_time, "%Y-%m-%d %H:%M")
            t_last = datetime.strptime(last, "%Y-%m-%d %H:%M")
            return (t_now - t_last).total_seconds() >= self._cooldown * 60
        except Exception:
            return True

    def record(self, code: str, current_time: str):
        self._triggered[code] = current_time


# ================================================================
# 主监控循环
# ================================================================

def run_monitor(candidates: List[Dict], interval: int = 60,
                cooldown: int = 30):
    """盘中实时监控循环

    Args:
        candidates: 候选股列表 (来自screen_candidates)
        interval: 轮询间隔(秒)
        cooldown: 同一股票信号冷却时间(分钟)
    """
    if not candidates:
        print("\n  ❌ 无候选股, 退出监控")
        return

    tracker = SignalTracker(cooldown)
    all_signals = []

    print(f"\n{'='*70}")
    print(f"  🔴 盘中实时监控已启动")
    print(f"  候选: {len(candidates)}只 | 间隔: {interval}秒 | 冷却: {cooldown}分钟")
    print(f"  规则: 2连板高开不破均线 | 首板(旧)/非昨日2连板站稳VWAP>20min | 首板(新)动量加速")
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
        while True:
            now = datetime.now()

            # 非交易时间
            if not is_trading_time():
                t = now.hour * 100 + now.minute
                if 1130 < t < 1300:
                    print(f"\r  ⏸  午休中... ({now.strftime('%H:%M')})", end="", flush=True)
                    time.sleep(30)
                    continue
                elif t > 1500:
                    print(f"\n\n  📊 收盘, 监控结束")
                    break
                elif t < 925:
                    wait_sec = ((9 - now.hour) * 60 + (25 - now.minute)) * 60
                    print(f"\r  ⏳ 等待开盘... ({now.strftime('%H:%M')}, 约{wait_sec // 60}分钟后)", end="", flush=True)
                    time.sleep(min(60, max(10, wait_sec)))
                    continue

            scan_count += 1
            current_time = now.strftime("%Y-%m-%d %H:%M")
            triggered_this_round = []

            # Step1: 批量行情预筛选 (1次HTTP, 过滤掉大部分)
            quick_matched = prefilter_by_rules(candidates)

            # Step2: 只对需要VWAP的股票拉分时数据
            for cand in quick_matched:
                code = cand['code']
                source = cand.get('source', '')

                # 昨日2连板和首板(新) 已在预筛选中判断, 直接构建信号
                if source in ('昨日2连板', '首板(新)'):
                    q = cand.get('_quote', {})
                    last = q.get('last', 0) or q.get('price', 0)
                    prev = q.get('previousClose', 0) or q.get('prev_close', 0)
                    open_p = q.get('open', 0)
                    change_pct = (last / prev - 1) * 100 if prev > 0 else 0
                    open_gap = (open_p / prev - 1) * 100 if prev > 0 else 0
                    today_mom = cand.get('today_momentum', 0)
                    ym = cand.get('yesterday_momentum', 0)

                    if source == '昨日2连板':
                        sig = {
                            'type': '2连板_高开不破均线', 'label': '高开不破均线',
                            'emoji': '🔗', 'source': source,
                            'code': code, 'name': cand.get('name', ''),
                            'board': cand.get('board', ''), 'limit_pct': cand.get('limit_pct', 10),
                            'price': last, 'vwap': 0, 'price_vs_vwap': 0,
                            'change_pct': round(change_pct, 2),
                            'open_gap': round(open_gap, 2),
                            'today_momentum': today_mom, 'yesterday_momentum': ym,
                            'time': current_time,
                            'detail': f"高开{open_gap:+.1f}%, 现价{last:.2f}",
                        }
                    else:  # 首板(新)
                        sig = {
                            'type': '首板新_动量加速', 'label': '动量加速',
                            'emoji': '🆕', 'source': source,
                            'code': code, 'name': cand.get('name', ''),
                            'board': cand.get('board', ''), 'limit_pct': cand.get('limit_pct', 10),
                            'price': last, 'vwap': 0, 'price_vs_vwap': 0,
                            'change_pct': round(change_pct, 2),
                            'open_gap': round(open_gap, 2),
                            'today_momentum': today_mom, 'yesterday_momentum': ym,
                            'time': current_time,
                            'detail': f"今动量{today_mom:+.1f}% > 昨{ym:+.1f}%×1.5={ym*1.5:+.1f}%",
                        }

                    if tracker.should_alert(code, current_time):
                        tracker.record(code, current_time)
                        triggered_this_round.append(sig)
                        all_signals.append(sig)
                    continue

                # 首板(旧) / 非昨日2连板 — 需要VWAP, 标记待拉分时
                cand['_need_vwap'] = True

            # Step3: 批量拉取需要VWAP的股票的1分钟K线 (1次并发请求)
            vwap_candidates = [c for c in quick_matched if c.get('_need_vwap')]
            if vwap_candidates:
                vwap_codes = [c['code'] for c in vwap_candidates]
                minute_data = fetch_minute_klines_batch(vwap_codes, count=240)
                for cand in vwap_candidates:
                    code = cand['code']
                    ticks = minute_data.get(code)
                    if not ticks or len(ticks) < 5:
                        continue
                    signal = detect_signal(ticks, cand)
                    if signal is None:
                        continue
                    if not tracker.should_alert(code, current_time):
                        continue
                    tracker.record(code, current_time)
                    triggered_this_round.append(signal)
                    all_signals.append(signal)

            # 输出本轮结果 (表格, 每股一行)
            if triggered_this_round:
                # 分类简称
                _src_short = {'首板(新)': '首板新', '首板(旧)': '首板旧',
                              '昨日2连板': '2连板', '非昨日2连板': '非昨2连'}

                # 首次触发时打印表头
                if not hasattr(run_monitor, '_header_printed'):
                    run_monitor._header_printed = True
                    print(f"\n  {'#':>3} {'时间':>5} {'代码':>7} {'名称':>6} {'分类':>7} {'价格':>7} {'涨跌':>6} "
                          f"{'高开':>5} {'动量':>5} {'站稳':>4} {'止损':>7} {'涨停':>7}")
                    print(f"  {'-'*84}")

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
                    count = sum(1 for s in all_signals if s['code'] == sig['code']) + 1
                    print(f"  {count:>3} {time_str:>5} {sig['code']:>7} {name:>6} {src:>7} "
                          f"{buy_price:>7.2f} {sig['change_pct']:>+5.1f}% {sig.get('open_gap', 0):>+5.1f}% "
                          f"{sig.get('today_momentum', 0):>+5.1f}% {above_str:>4} {stop_price:>7.2f} {limit_price:>7.2f}")
            else:
                # 静默状态行
                latest_price = ""
                if candidates:
                    c = candidates[0]
                    q = _batch_cache.get(c['code'])
                    if q:
                        last = q.get('last', 0) or q.get('price', 0)
                        chg = q.get('changePercent', q.get('change_pct', 0))
                        latest_price = f" | {c['code']} {last:.2f} {chg:+.1f}%"
                print(f"\r  ⏱ [{scan_count}] {now.strftime('%H:%M:%S')} "
                      f"扫描{len(candidates)}只 无信号{latest_price}    ", end="", flush=True)

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n  ⛔ 手动停止")

    if all_signals:
        print(f"\n  📊 今日共 {len(all_signals)} 个信号")
    else:
        print(f"\n  📊 今日无信号")

    # 导出
    if all_signals:
        outfile = "realtime_signals.json"
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(all_signals, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 信号已导出: {outfile}")


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="首板/2连板盘中实时监控",
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
    parser.add_argument("--cooldown", type=int, default=30, help="信号冷却时间分钟 (默认30)")
    parser.add_argument("--kline-days", type=int, default=30, help="日K线回看天数 (默认30)")
    args = parser.parse_args()

    print("=" * 70)
    print("  🔍 首板/2连板盘中实时监控")
    print("=" * 70)
    print(f"  选股池: 昨日首板 + 5日内2连板")
    print(f"  规则: 2连板高开不破均线 | 首板(旧)/非昨日2连板站稳VWAP>20min | 首板(新)动量加速")

    # 选股
    if args.codes:
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
            change_pct = (last_bar['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
            candidates.append({
                'code': code, 'name': '', 'board': get_board_name(code),
                'limit_pct': get_limit_pct(code),
                'last_close': last_bar['close'], 'last_date': last_bar['time'],
                'change_pct': round(change_pct, 2),
                'source': '(手动指定)',
                'prev_close': prev_close,
            })
        print(f"\n  手动指定: {len(candidates)}只")
    else:
        candidates = screen_candidates(kline_days=args.kline_days, force_refresh=args.refresh)

    if args.scan:
        print(f"\n  ✅ 选股完成 (--scan 模式, 不启动监控)")
        return

    # 盘中监控
    run_monitor(candidates, interval=args.interval, cooldown=args.cooldown)


if __name__ == "__main__":
    main()
