#!/usr/bin/env python3
"""
追涨停板回测 - 开盘30分钟内首拉追入, 次日开盘卖出 (v5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

策略 (pool=limit_up, 默认):
  池子: 昨日涨停票 (主板涨幅>=9.8%, 创业板/科创板>=19.8%, 排除北交所/ST)
  信号: 开盘后 signal_before_min 分钟内 (默认30, 即09:31~10:00) 当日首次
        分钟级急拉 (当分钟涨幅 >= chg_1m, 默认2%) 且行为分 >= min_score
  入场: 信号确认后下一分钟收盘价追入 (追涨/打板)
  出场: 次日开盘卖出 (T+1)

打板特有处理:
  - 涨停价 = round(昨收 × 1.10/1.20, 2); 入场分钟收盘已达涨停价 → 标记 entry_at_limit
    (实盘中封死买单排队, 未必成交; 统计同时给出 全口径 与 可成交口径)
  - 当日封板判定: 日内最高价触及涨停价 → sealed; 拆分 封板组 vs 炸板组 的 D+1 表现

对照 (pool=rally): 旧的"昨日大涨+放量"池, 参数 --pool rally

用法:
  python backtest_rally.py --days 30 --top-n 5 --min-score 5
  python backtest_rally.py --pool rally --chg-min 3 --vol-min-wan 3   # 旧对照
"""
from __future__ import annotations
import os, sys, json, argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

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
                load_dotenv(p, override=True)
    except Exception:
        pass
_load_env()

# ================================================================
# DB 工具
# ================================================================
_mgr_cache = None
def _get_mgr():
    global _mgr_cache
    if _mgr_cache is not None:
        return _mgr_cache
    from app.utils.db_market import get_market_db_manager
    _mgr_cache = get_market_db_manager()
    return _mgr_cache

def _q(sql: str, params: list = None) -> List[tuple]:
    mgr = _get_mgr()
    pool = mgr._get_pool("CNStock")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            return cur.fetchall()

def _year_tables(prefix: str, start_date: str, end_date: str) -> List[str]:
    y0, y1 = int(start_date[:4]), int(end_date[:4])
    return [f'"{prefix}_{y}"' for y in range(y0, y1 + 1)]

def _union_daily(start_date: str, end_date: str, cols: str = "symbol, time, open, high, low, close, volume") -> str:
    parts = [f"SELECT {cols} FROM {t} WHERE time >= %s AND time < %s" for t in _year_tables("kline_1D", start_date, end_date)]
    return "(" + " UNION ALL ".join(parts) + ")"

def get_all_codes() -> List[str]:
    try:
        from app.utils.db_market import get_market_kline_writer
        writer = get_market_kline_writer()
        stats = writer.stats("CNStock")
        if stats.get("exists"):
            lst = stats.get("symbol_list", [])
            if lst:
                return lst
    except Exception:
        pass
    year = datetime.now().year
    try:
        rows = _q(f'SELECT DISTINCT symbol FROM "kline_1D_{year}"')
        return [r[0] for r in rows]
    except Exception:
        return []

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

# ================================================================
# 交易日历
# ================================================================
def get_trading_days(days: int) -> List[str]:
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    start = max(start, "2026-01-01")  # 分表从2026起
    try:
        sub = _union_daily(start, end, "symbol, time, volume")
        rows = _q(f"SELECT DISTINCT time::date FROM {sub} u WHERE u.symbol='000001' AND u.volume > 0 AND u.time::date <= %s ORDER BY 1",
                  [start, end, end])
        result = [str(r[0]) for r in rows]
        return result[-min(len(result), days):]
    except Exception as e:
        print(f"get_trading_days error: {e}")
        return []

def prev_trading_day(trade_date: str) -> Optional[str]:
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    sub = _union_daily(start, trade_date, "symbol, time, volume")
    rows = _q(f"SELECT MAX(time::date) FROM {sub} u WHERE u.symbol='000001' AND u.volume > 0 AND u.time::date < %s",
              [start, trade_date, trade_date])
    if rows and rows[0][0]:
        return str(rows[0][0])
    return None

# ================================================================
# 预筛选
# ================================================================
def daily_prescreen_limitup(prev_date: str) -> Tuple[List[str], Dict[str, float], Dict[str, float]]:
    """昨日涨停池: 主板>=9.8%, 创业板/科创板>=19.8%; 排除北交所(8/4/92开头)

    Returns: (codes, prev_vol_map, prev_close_map)
    """
    start_buf = (datetime.strptime(prev_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")
    next_of_prev = (datetime.strptime(prev_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    sub = _union_daily(start_buf, next_of_prev, "symbol, time, close, volume")
    sql = f"""
        SELECT symbol, chg, volume, prev_close FROM (
            SELECT symbol, time, volume,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY time) AS prev_close,
                   (close / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY time), 0) - 1) * 100 AS chg
            FROM {sub} u
        ) t
        WHERE t.time >= %s AND t.time < %s
          AND (t.symbol LIKE '00%%' OR t.symbol LIKE '60%%' OR t.symbol LIKE '30%%' OR t.symbol LIKE '68%%')
          AND t.chg >= CASE WHEN t.symbol LIKE '30%%' OR t.symbol LIKE '68%%' THEN 19.8 ELSE 9.8 END
        ORDER BY t.chg DESC
    """
    rows = _q(sql, [start_buf, next_of_prev, prev_date, next_of_prev])
    codes = [r[0] for r in rows]
    prev_vol_map = {r[0]: float(r[2]) for r in rows}
    prev_close_map = {r[0]: float(r[3]) for r in rows}
    return codes, prev_vol_map, prev_close_map

def daily_prescreen_sql(prev_date: str, chg_min: float, vol_min_shares: float) -> Tuple[List[str], Dict[str, float], Dict[str, float]]:
    """旧池: T-1 涨幅>=chg_min% 且 量>=vol_min_shares"""
    start_buf = (datetime.strptime(prev_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")
    next_of_prev = (datetime.strptime(prev_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    sub = _union_daily(start_buf, next_of_prev, "symbol, time, close, volume")
    sql = f"""
        SELECT symbol, chg, volume, prev_close FROM (
            SELECT symbol, time, volume,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY time) AS prev_close,
                   (close / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY time), 0) - 1) * 100 AS chg
            FROM {sub} u
        ) t
        WHERE t.time >= %s AND t.time < %s
          AND t.chg IS NOT NULL AND t.chg >= %s AND t.volume >= %s
        ORDER BY t.chg DESC
    """
    rows = _q(sql, [start_buf, next_of_prev, prev_date, next_of_prev, chg_min, vol_min_shares])
    codes = [r[0] for r in rows]
    prev_vol_map = {r[0]: float(r[2]) for r in rows}
    prev_close_map = {r[0]: float(r[3]) for r in rows}
    return codes, prev_vol_map, prev_close_map

def limit_price_of(code: str, prev_close: float) -> float:
    """涨停价: 主板10%, 创业板/科创板20% (四舍五入到分)"""
    if prev_close <= 0:
        return 0.0
    pct = 1.2 if code.startswith('30') or code.startswith('68') else 1.1
    return round(prev_close * pct, 2)

def load_daily_feats(codes: List[str], prev_date: str) -> Dict[str, Dict]:
    """批量计算日线前置特征: board_h / ma_bull / lu_vol_ratio / rsi (截至 prev_date)"""
    if not codes:
        return {}
    start_buf = (datetime.strptime(prev_date, "%Y-%m-%d") - timedelta(days=130)).strftime("%Y-%m-%d")
    next_of_prev = (datetime.strptime(prev_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    sub = _union_daily(start_buf, next_of_prev, "symbol, time, close, volume")
    placeholders = ", ".join(["%s"] * len(codes))
    sql = f"SELECT symbol, time, close, volume FROM {sub} u WHERE u.symbol IN ({placeholders}) ORDER BY symbol, time"
    try:
        rows = _q(sql, [start_buf, next_of_prev] + list(codes))
    except Exception:
        return {}
    bars = defaultdict(list)
    for r in rows:
        bars[r[0]].append((str(r[1])[:10], float(r[2]), float(r[3])))
    feats = {}
    for code, bl in bars.items():
        idxs = [i for i, b in enumerate(bl) if b[0] <= prev_date]
        if len(idxs) < 30:
            continue
        k = idxs[-1]
        closes = [b[1] for b in bl[:k+1]]
        vols = [b[2] for b in bl[:k+1]]
        th = 0.198 if code.startswith('30') or code.startswith('68') else 0.098
        # 连板高度
        h = 0
        j = len(closes) - 1
        while j >= 1 and (closes[j]/closes[j-1]-1) >= th*0.98:
            h += 1
            j -= 1
        # MA多头
        def ma(n):
            return sum(closes[-n:])/n if len(closes) >= n else None
        m5, m10, m20, m60 = ma(5), ma(10), ma(20), ma(60)
        ma_bull = 1 if (m5 and m10 and m20 and m60 and m5 > m10 > m20 > m60) else 0
        # 涨停日量比
        avg5 = sum(vols[-6:-1])/5 if len(vols) >= 6 else 0
        lvr = round(vols[-1]/avg5, 2) if avg5 > 0 else None
        # RSI
        rsi = None
        if len(closes) >= 15:
            gains, losses = [], []
            for q in range(len(closes)-15, len(closes)):
                dd = closes[q]-closes[q-1]
                gains.append(max(dd, 0)); losses.append(max(-dd, 0))
            ag, al = sum(gains)/14, sum(losses)/14
            rsi = round(100-100/(1+ag/al), 1) if al > 0 else 100.0
        feats[code] = {'board_h': h, 'ma_bull': ma_bull, 'lu_vol_ratio': lvr, 'rsi': rsi}
    return feats

# ================================================================
# 批量 1m K线
# ================================================================
def fetch_1m_kline_batch(codes: List[str], date: str) -> Dict[str, List[Dict]]:
    if not codes:
        return {}
    table = f'"kline_1m_{date[:4]}"'
    placeholders = ", ".join(["%s"] * len(codes))
    sql = f"""
        SELECT symbol, time, open, high, low, close, volume
        FROM {table}
        WHERE symbol IN ({placeholders})
          AND time >= %s AND time < %s
        ORDER BY symbol, time ASC
    """
    try:
        rows = _q(sql, list(codes) + [f"{date} 00:00:00", f"{date} 23:59:59"])
    except Exception as e:
        print(f"  fetch_1m_kline_batch error: {e}")
        return {}
    grouped: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        ts = str(row[1])
        t = ts[11:16] if len(ts) >= 16 else ""
        if t and not ("09:30" <= t <= "15:00"):
            continue
        grouped[row[0]].append({
            'time': ts,
            'open': float(row[2] or 0),
            'high': float(row[3] or 0),
            'low': float(row[4] or 0),
            'close': float(row[5] or 0),
            'volume': float(row[6] or 0),
        })
    return dict(grouped)

def fetch_next_day_stats(codes: List[str], next_date: str) -> Dict[str, Dict]:
    if not codes:
        return {}
    placeholders = ", ".join(["%s"] * len(codes))
    nd = (datetime.strptime(next_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    sub = _union_daily(next_date, nd, "symbol, time, open, high")
    sql = f"SELECT symbol, open, high FROM {sub} u WHERE u.symbol IN ({placeholders})"
    try:
        rows = _q(sql, [next_date, nd] + list(codes))
    except Exception as e:
        print(f"  fetch_next_day_stats error: {e}")
        return {}
    return {r[0]: {'open': float(r[1] or 0), 'high': float(r[2] or 0)} for r in rows}

# ================================================================
# 龙虎榜
# ================================================================
def load_lhb_dates(start_date: str, end_date: str) -> Dict[str, List[str]]:
    sql = "SELECT stock_code, trade_date FROM cnd_dragon_tiger_list WHERE trade_date >= %s AND trade_date < %s"
    try:
        rows = _q(sql, [start_date, end_date])
        result: Dict[str, List[str]] = defaultdict(list)
        for code, d in rows:
            result[code].append(str(d)[:10])
        return result
    except Exception as e:
        print(f"  load_lhb_dates error: {e}")
        return {}

def lhb_count_in(lhb_dates: Dict[str, List[str]], code: str, trade_date: str, window_days: int = 40) -> int:
    try:
        lo = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
        return sum(1 for d in lhb_dates.get(code, []) if lo <= d < trade_date)
    except Exception:
        return 0

# ================================================================
# 评分逻辑 (快照用)
# ================================================================
def calc_rally_score(series: List[Dict], prev_vol: float, lhb_in_40d: bool, lhb_count: int,
                     prev_close: float = 0.0) -> Dict:
    if len(series) < 5:
        return {"score": 0, "rise_score": 0, "volume_score": 0,
                "lhb_score": 0, "est_vol_score": 0, "est_ratio": 0.0}

    # 1. 快速拉升
    rise_scores = []
    rise_streak = 0
    max_rise_streak = 0
    for i in range(1, len(series)):
        prev = series[i - 1]['close']
        cur = series[i]['close']
        if prev <= 0:
            continue
        change = (cur - prev) / prev * 100
        if change >= 2.0:
            rise_streak += 1
            max_rise_streak = max(max_rise_streak, rise_streak)
            rise_scores.append(change * rise_streak)
        else:
            rise_streak = 0
    rise_score = min(sum(rise_scores), 30)

    # 2. 放量拉升
    volumes = [s['volume'] for s in series]
    vol_scores = []
    vol_streak = 0
    max_vol_streak = 0
    for i in range(5, len(volumes)):
        avg_vol_5 = sum(volumes[i - 5:i]) / 5
        if avg_vol_5 <= 0:
            continue
        vol_ratio = volumes[i] / avg_vol_5
        if vol_ratio >= 1.2:
            vol_streak += 1
            max_vol_streak = max(max_vol_streak, vol_streak)
            vol_scores.append(vol_ratio * vol_streak)
        else:
            vol_streak = 0
    volume_score = min(sum(vol_scores), 25)

    # 3. 龙虎榜
    lhb_score = 0
    if lhb_in_40d:
        lhb_score = 5 + min(lhb_count * 2, 15)

    # 4. 预估成交量
    est_vol_score = 0
    est_ratio = 0.0
    if prev_vol > 0 and volumes:
        elapsed = len(volumes)
        est_total_vol = sum(volumes) * (240.0 / elapsed)
        est_ratio = est_total_vol / prev_vol
        if est_ratio >= 1.2:
            est_vol_score = min((est_ratio - 1) * 20, 20)
        elif est_ratio >= 1.0:
            est_vol_score = (est_ratio - 1.0) * 10

    total_score = rise_score + volume_score + lhb_score + est_vol_score

    # 日内涨幅(含跳空, 相对昨收)
    intraday_gain = 0.0
    if prev_close > 0 and series:
        intraday_gain = (series[-1]['close'] / prev_close - 1) * 100

    return {
        "score": round(total_score, 1),
        "rise_score": round(rise_score, 1),
        "rise_streak": max_rise_streak,
        "volume_score": round(volume_score, 1),
        "vol_streak": max_vol_streak,
        "lhb_score": lhb_score,
        "lhb_in_40d": lhb_in_40d,
        "lhb_count": lhb_count,
        "est_vol_score": round(est_vol_score, 1),
        "est_ratio": round(est_ratio, 2),
        "intraday_gain": round(intraday_gain, 2),
    }

# ================================================================
# 首拉信号检测 (逐分钟增量)
# ================================================================
def find_first_signal(bars: List[Dict], prev_vol: float, min_score: float,
                      signal_after_min: int = 0, signal_before_min: int = 30,
                      chg_1m: float = 2.0, prev_close: float = 0.0,
                      max_intraday_gain: float = 0.0) -> Optional[Dict]:
    """开盘后 [signal_after_min+1, signal_before_min] 分钟窗口内, 当日首次急拉

    急拉定义: 当分钟涨幅 >= chg_1m (相对前一分钟收盘)
    附加: 行为分 >= min_score; 若 max_intraday_gain>0, 要求信号时日内涨幅(含跳空) <= 该值
    返回: {'t': 信号分钟下标, 'intraday_gain': 信号时日内涨幅, 'minute_chg': 当分钟涨幅} 或 None
    """
    if prev_vol <= 0:
        return None
    cum_vol = 0.0
    rise_streak = 0
    vol_streak = 0
    rise_score = 0.0
    volume_score = 0.0
    for t in range(1, len(bars)):
        if t + 1 > signal_before_min:
            break
        prev_c = bars[t - 1]['close']
        cur_c = bars[t]['close']
        minute_chg = 0.0
        if prev_c > 0:
            minute_chg = (cur_c - prev_c) / prev_c * 100
            if minute_chg >= 2.0:
                rise_streak += 1
                rise_score += minute_chg * rise_streak
            else:
                rise_streak = 0
        rise_score = min(rise_score, 30)

        if t >= 5:
            avg5 = sum(bars[i]['volume'] for i in range(t - 5, t)) / 5
            if avg5 > 0:
                vr = bars[t]['volume'] / avg5
                if vr >= 1.2:
                    vol_streak += 1
                    volume_score += vr * vol_streak
                else:
                    vol_streak = 0
        volume_score = min(volume_score, 25)

        cum_vol += bars[t]['volume']
        elapsed = t + 1
        est_total = cum_vol * (240.0 / elapsed)
        est_ratio = est_total / prev_vol
        if est_ratio >= 1.2:
            est_vol_score = min((est_ratio - 1) * 20, 20)
        elif est_ratio >= 1.0:
            est_vol_score = (est_ratio - 1.0) * 10
        else:
            est_vol_score = 0.0

        if t + 1 <= signal_after_min:
            continue

        intraday_gain = ((cur_c / prev_close) - 1) * 100 if prev_close > 0 else 0.0
        if max_intraday_gain > 0 and intraday_gain > max_intraday_gain:
            continue
        if minute_chg >= chg_1m:
            behavior = rise_score + volume_score + est_vol_score
            if behavior >= min_score:
                return {'t': t, 'intraday_gain': round(intraday_gain, 2), 'minute_chg': round(minute_chg, 2)}
    return None

# ================================================================
# 回测核心
# ================================================================
def run_backtest(top_n: int = 5, min_score: float = 5.0, days: int = 30,
                 no_lhb: bool = False, chg_min: float = 3.0, vol_min_wan: float = 3.0,
                 pool: str = 'limit_up', signal_after_min: int = 0,
                 signal_before_min: int = 30, chg_1m: float = 2.0,
                 max_intraday_gain: float = 0.0, output: str = None,
                 require_ma_bull: bool = False, mild_lu: bool = False,
                 store_feats: bool = False) -> Dict:
    vol_min_shares = vol_min_wan * 10000 * 100

    trading_days = get_trading_days(days)
    if len(trading_days) < 5:
        print(f"WARNING: 交易日不足: {len(trading_days)}")
        return {}

    print(f"回测区间: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} 个交易日)")
    if pool == 'limit_up':
        print(f"池子: 昨日涨停 (主板9.8%/创科19.8%, 排除北交所)")
    else:
        print(f"池子: 昨日涨幅>={chg_min}% 且 量>={vol_min_wan}万手")
    print(f"信号: 开盘第{signal_after_min + 1}~{signal_before_min}分钟内首次分钟急拉(>={chg_1m}%)"
          + (f", 日内涨幅<={max_intraday_gain}%" if max_intraday_gain > 0 else ""))
    print(f"入场: 信号下一分钟收盘价追入 | 出场: 次日开盘卖")
    print(f"每日: top-{top_n} (行为分+龙虎榜分排序, 同分信号早者优先), 行为分门槛 {min_score}")
    print()

    print("预加载基础数据...")
    st_set = load_st_codes()
    name_map = load_stock_names()
    print(f"  ST: {len(st_set)} 只, 名称: {len(name_map)} 只")

    lhb_dates: Dict[str, List[str]] = {}
    if not no_lhb:
        lhb_start = (datetime.strptime(trading_days[0], "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
        lhb_end = (datetime.strptime(trading_days[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        lhb_dates = load_lhb_dates(lhb_start, lhb_end)
        print(f"  龙虎榜: {sum(len(v) for v in lhb_dates.values())} 条记录 / {len(lhb_dates)} 只")
    print()

    daily_stats = []
    all_trades = []
    total_screened = 0
    total_signals = 0

    for day_idx, trade_date in enumerate(trading_days[:-1]):
        next_date = trading_days[day_idx + 1]
        prev_date = prev_trading_day(trade_date)
        if not prev_date:
            continue

        # ---- 预筛 ----
        if pool == 'limit_up':
            screened, prev_vol_map, prev_close_map = daily_prescreen_limitup(prev_date)
        else:
            screened, prev_vol_map, prev_close_map = daily_prescreen_sql(prev_date, chg_min, vol_min_shares)
        screened = [c for c in screened if c not in st_set]
        total_screened += len(screened)

        # ---- 日线前置特征 (可选) ----
        daily_feats = {}
        if require_ma_bull or mild_lu or store_feats:
            daily_feats = load_daily_feats(screened, prev_date)

        # 涨停价表
        limit_price_map = {c: limit_price_of(c, prev_close_map.get(c, 0)) for c in screened}

        # ---- 当日 1m ----
        daily_1m = fetch_1m_kline_batch(screened, trade_date)

        # ---- 首拉信号 + 追入 ----
        candidates = []
        for code in screened:
            df = daily_feats.get(code, {})
            if require_ma_bull and df.get('ma_bull') != 1:
                continue
            if mild_lu and (df.get('lu_vol_ratio') is None or df.get('lu_vol_ratio') >= 2):
                continue
            bars = daily_1m.get(code)
            if not bars or len(bars) < 3:
                continue
            sig = find_first_signal(bars, prev_vol_map.get(code, 0), min_score,
                                    signal_after_min, signal_before_min, chg_1m,
                                    prev_close_map.get(code, 0), max_intraday_gain)
            if sig is None:
                continue
            t = sig['t']
            if t + 1 >= len(bars):
                continue
            lhb_cnt = 0 if no_lhb else lhb_count_in(lhb_dates, code, trade_date, 40)
            snap = calc_rally_score(bars[:t + 1], prev_vol_map.get(code, 0), lhb_cnt > 0, lhb_cnt,
                                    prev_close_map.get(code, 0))
            entry_bar = bars[t + 1]
            limit_price = limit_price_map.get(code, 0)
            day_high = max(b['high'] for b in bars)
            snap.update({
                'code': code,
                'name': name_map.get(code, ""),
                'entry_price': entry_bar['close'],
                'entry_time': entry_bar['time'],
                'signal_time': bars[t]['time'],
                'signal_minute': t + 1,
                'minute_chg': sig['minute_chg'],
                'intraday_gain_at_signal': sig['intraday_gain'],
                'limit_price': limit_price,
                'entry_at_limit': (limit_price > 0 and entry_bar['close'] >= limit_price - 0.001),
                'sealed_today': (limit_price > 0 and day_high >= limit_price - 0.001),
                'board_h': df.get('board_h'), 'ma_bull': df.get('ma_bull'),
                'lu_vol_ratio': df.get('lu_vol_ratio'), 'rsi': df.get('rsi'),
            })
            candidates.append(snap)

        total_signals += len(candidates)
        if not candidates:
            print(f"  [{trade_date}] 池={len(screened)} 信号=0")
            continue

        candidates.sort(key=lambda x: (-x['score'], x['signal_minute'], x['code']))
        selected = candidates[:top_n]

        # ---- 次日开盘卖出 ----
        daily_profits = []
        if selected:
            next_stats = fetch_next_day_stats([s['code'] for s in selected], next_date)
            for s in selected:
                ns = next_stats.get(s['code'])
                if not ns or ns['open'] <= 0:
                    continue
                ret = (ns['open'] / s['entry_price'] - 1) * 100
                s['exit_price'] = ns['open']
                s['exit_time'] = f"{next_date} 09:30"
                s['peak_return_pct'] = round((ns['high'] / s['entry_price'] - 1) * 100, 2) if ns['high'] > 0 else round(ret, 2)
                s['return_pct'] = round(ret, 2)
                s['trade_date'] = trade_date
                s['next_date'] = next_date
                daily_profits.append(ret)
                all_trades.append(s)

        wr = (sum(1 for p in daily_profits if p > 0) / len(daily_profits) * 100) if daily_profits else 0.0
        ar = (sum(daily_profits) / len(daily_profits)) if daily_profits else 0.0
        if daily_profits:
            daily_stats.append({'date': trade_date, 'n': len(daily_profits),
                                'wins': sum(1 for p in daily_profits if p > 0),
                                'win_rate': round(wr, 1), 'avg_ret': round(ar, 2)})

        sel_sealed = sum(1 for s in selected if s.get('sealed_today'))
        print(f"  [{trade_date}] 池={len(screened)} 信号={len(candidates)} 入选={len(selected)} 成交={len(daily_profits)} "
              f"封板{sel_sealed} 日胜率={wr:.0f}%日均={ar:+.2f}%")

    # ================================================================
    # 汇总
    # ================================================================
    print(f"\n池子日均: {total_screened / max(1, len(trading_days) - 1):.0f} 只 | 信号日均: {total_signals / max(1, len(trading_days) - 1):.0f} 只")

    if not all_trades:
        print("WARNING: 无有效交易")
        return {}

    def _stats(seg: List[Dict]) -> Tuple[float, float, float, float]:
        if not seg:
            return 0, 0, 0, 0
        n = len(seg)
        wr = sum(1 for t in seg if t['return_pct'] > 0) / n * 100
        ar = sum(t['return_pct'] for t in seg) / n
        wl = [t['return_pct'] for t in seg if t['return_pct'] > 0]
        ll = [t['return_pct'] for t in seg if t['return_pct'] < 0]
        pr = abs(sum(wl) / len(wl) / (sum(ll) / len(ll))) if wl and ll else float('inf')
        return n, wr, ar, pr

    total, win_rate, avg_ret, profit_ratio = _stats(all_trades)
    avg_peak = sum(t.get('peak_return_pct', t['return_pct']) for t in all_trades) / total

    print(f"\n{'=' * 60}")
    print(f"回测结果: {len(trading_days) - 1} 个交易日, {total} 笔交易")
    print(f"胜率: {win_rate:.1f}%  平均收益: {avg_ret:+.2f}%  盈亏比: {profit_ratio:.2f}")
    print(f"D+1 平均峰值: {avg_peak:+.2f}%")

    # 打板核心: 封板 vs 炸板
    sealed = [t for t in all_trades if t.get('sealed_today')]
    unsealed = [t for t in all_trades if not t.get('sealed_today')]
    n1, wr1, ar1, pr1 = _stats(sealed)
    n2, wr2, ar2, pr2 = _stats(unsealed)
    print(f"\n当日封板情况 (打板策略核心):")
    print(f"  封板: n={n1} ({n1 / total * 100:.0f}%)  D+1胜率={wr1:.1f}%  均收益={ar1:+.2f}%  盈亏比={pr1:.2f}")
    print(f"  炸板: n={n2} ({n2 / total * 100:.0f}%)  D+1胜率={wr2:.1f}%  均收益={ar2:+.2f}%  盈亏比={pr2:.2f}")

    # 可成交口径 (排除涨停价买入)
    fillable = [t for t in all_trades if not t.get('entry_at_limit')]
    at_limit = [t for t in all_trades if t.get('entry_at_limit')]
    nf, wrf, arf, prf = _stats(fillable)
    nl, wrl, arl, prl = _stats(at_limit)
    if at_limit:
        print(f"\n成交口径:")
        print(f"  可成交(入场价<涨停): n={nf}  胜率={wrf:.1f}%  均收益={arf:+.2f}%")
        print(f"  涨停价排队(可能买不到): n={nl}  胜率={wrl:.1f}%  均收益={arl:+.2f}%")

    # 信号时段细分
    print(f"\n按信号触发时间:")
    for lo, hi, label in [(1, 10, '09:31-09:40'), (10, 20, '09:40-09:50'), (20, 31, '09:50-10:00')]:
        seg = [t for t in all_trades if lo <= t.get('signal_minute', 0) < hi]
        n, wr, ar, pr = _stats(seg)
        if n:
            print(f"  {label}: n={n}  胜率={wr:.1f}%  均收益={ar:+.2f}%")

    # 信号时日内涨幅
    print(f"\n按信号时日内涨幅(含跳空):")
    for lo, hi, label in [(-99, 2, '<2%'), (2, 5, '2-5%'), (5, 8, '5-8%'), (8, 99.9, '8%+')]:
        seg = [t for t in all_trades if lo <= t.get('intraday_gain_at_signal', 0) < hi]
        n, wr, ar, pr = _stats(seg)
        if n:
            print(f"  {label}: n={n}  胜率={wr:.1f}%  均收益={ar:+.2f}%  盈亏比={pr:.2f}")

    # 封板组内部细分: 连板高度视角(昨日涨幅即板高代理)
    print(f"\n封板组 D+1 明细分布:")
    buckets = [(0, 5, '<5%'), (5, 10, '5-10%'), (10, 99, '10%+')]
    for lo, hi, label in buckets:
        seg = [t for t in all_trades if lo <= t['return_pct'] < hi]
        n, wr, ar, pr = _stats(seg)
        if n:
            print(f"  D+1收益{label}: n={n}  胜率={wr:.1f}%  均收益={ar:+.2f}%")

    sorted_trades = sorted(all_trades, key=lambda x: x['return_pct'], reverse=True)
    print(f"\n盈利TOP5:")
    for t in sorted_trades[:5]:
        print(f"  {t['code']} {t['name']:<6} 信号{str(t['signal_time'])[11:16]} 买{t['entry_price']:.2f} "
              f"卖{t['exit_price']:.2f} {t['return_pct']:+.2f}% {'[封板]' if t.get('sealed_today') else '[炸板]'}")
    print(f"\n亏损TOP5:")
    for t in sorted_trades[-5:]:
        print(f"  {t['code']} {t['name']:<6} 信号{str(t['signal_time'])[11:16]} 买{t['entry_price']:.2f} "
              f"卖{t['exit_price']:.2f} {t['return_pct']:+.2f}% {'[封板]' if t.get('sealed_today') else '[炸板]'}")

    result = {
        'params': {'mode': 'limit_up_first_pull' if pool == 'limit_up' else 'rally_pool',
                   'pool': pool, 'signal_after_min': signal_after_min,
                   'signal_before_min': signal_before_min, 'chg_1m': chg_1m,
                   'max_intraday_gain': max_intraday_gain,
                   'top_n': top_n, 'min_score': min_score,
                   'days': days, 'no_lhb': no_lhb, 'chg_min': chg_min, 'vol_min_wan': vol_min_wan},
        'summary': {'total_trades': total, 'trading_days': len(trading_days) - 1,
                    'win_rate': round(win_rate, 2), 'avg_return': round(avg_ret, 2),
                    'profit_ratio': round(profit_ratio, 2), 'avg_peak': round(avg_peak, 2),
                    'sealed_count': n1, 'sealed_win_rate': round(wr1, 2), 'sealed_avg_return': round(ar1, 2),
                    'unsealed_count': n2, 'unsealed_win_rate': round(wr2, 2), 'unsealed_avg_return': round(ar2, 2),
                    'fillable_count': nf, 'fillable_win_rate': round(wrf, 2), 'fillable_avg_return': round(arf, 2)},
        'daily_stats': daily_stats,
        'trades': all_trades,
    }

    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {output}")

    return result


# ================================================================
# 主程序
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="追涨停板回测 (开盘30分钟首拉追入)")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数")
    parser.add_argument("--pool", type=str, default="limit_up", choices=["limit_up", "rally"], help="预筛池")
    parser.add_argument("--signal-after-min", type=int, default=0, help="信号最早分钟 (默认0=开盘即扫)")
    parser.add_argument("--signal-before-min", type=int, default=30, help="信号最晚分钟 (默认30=10:00前)")
    parser.add_argument("--chg-1m", type=float, default=2.0, help="首拉阈值: 分钟涨幅%%")
    parser.add_argument("--max-intraday-gain", type=float, default=0.0, help="信号时日内涨幅上限%% (0=不限)")
    parser.add_argument("--require-ma-bull", action="store_true", help="要求MA多头排列")
    parser.add_argument("--mild-lu", action="store_true", help="要求涨停日量比<2 (温和板)")
    parser.add_argument("--store-feats", action="store_true", help="在交易记录中存储日线特征")
    parser.add_argument("--top-n", type=int, default=5, help="每日选N只")
    parser.add_argument("--min-score", type=float, default=5.0, help="行为分门槛")
    parser.add_argument("--chg-min", type=float, default=3.0, help="[rally池] 昨日涨幅下限%%")
    parser.add_argument("--vol-min-wan", type=float, default=3.0, help="[rally池] 昨日成交量下限(万手)")
    parser.add_argument("--no-lhb", action="store_true", help="忽略龙虎榜维度")
    parser.add_argument("--output", type=str, default="", help="输出JSON文件")
    args = parser.parse_args()

    output_file = args.output or "limitup_backtest_result.json"

    run_backtest(
        top_n=args.top_n,
        min_score=args.min_score,
        days=args.days,
        no_lhb=args.no_lhb,
        chg_min=args.chg_min,
        vol_min_wan=args.vol_min_wan,
        pool=args.pool,
        signal_after_min=args.signal_after_min,
        signal_before_min=args.signal_before_min,
        chg_1m=args.chg_1m,
        max_intraday_gain=args.max_intraday_gain,
        require_ma_bull=args.require_ma_bull,
        mild_lu=args.mild_lu,
        store_feats=args.store_feats,
        output=output_file,
    )


if __name__ == "__main__":
    main()
