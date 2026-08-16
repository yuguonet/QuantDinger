#!/usr/bin/env python3
"""龙虎榜游资D0策略 v5

核心逻辑:
  D0 = 游资首次大量买入日（龙虎榜），需同时满足:
    ① D0 涨停（涨幅 ≥ 9.5%）
    ② D0 收盘 > 前5天最高价（突破前高）
    ③ 游资净买入 > 5000万
    ④ D0 量比 < 2x（温和放量，非天量）

  D1 入场:
    D1 高开(>0%) → D1 开盘买入
    D1 低开(≤0%) → 放弃, 不参与统计

  出场:
    追踪止损 -8%, 止盈 +15%, 持仓上限 7 天
    (数据显示所有组合峰值均在7天内出现)

数据验证 (半年 2295 只):
  精选(4条件全满足): 278只, 均涨+18.7%, 胜率>5%=85%, 回撤-9.9%
  D1高开高走: +24.9%, 98%胜率
  D1低开低走: +6.0%, 38%胜率（放弃）
  峰值天数: 4.5~7.0天 (全部组合)

时间线:
  D0 涨停 + 游资买入 (盘后龙虎榜确认信号)
  D1 买入日 (高开→开盘买; 低开→放弃)
  D2~D7 持有期 (追踪止损/止盈, 最多7天)

两种回测模式:
  默认: 从龙虎榜反查, 直接用D0事件回测
  --full-scan: 全市场每日扫描预筛选池 → D0确认 → D1入场 (更贴近实战)
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

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

_pool_cache = None
def _get_cnstock_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    _pool_cache = mgr._get_pool("CNStock")
    return _pool_cache

# ================================================================
# 数据加载
# ================================================================

def fetch_dragon_tiger_from_db(limit: int = 50000) -> List[Dict]:
    """从数据库加载龙虎榜数据"""
    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date, stock_code, stock_name, reason, "
                "buy_amount, sell_amount, net_amount, change_percent, "
                "close_price, turnover_rate, amount, buy_seat_count, sell_seat_count "
                "FROM cnd_dragon_tiger_list ORDER BY trade_date DESC LIMIT %s",
                (limit,)
            )
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        print(f"  [DB] 龙虎榜查询失败: {e}")
        return []


def fetch_kline_db(code: str, days: int = 300) -> List[Dict]:
    """从数据库加载K线(前复权)"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = []
        for r in data:
            bars.append({
                "time": str(r["time"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


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


def is_limit_up(close: float, prev_close: float, board_type: str) -> bool:
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) >= threshold * 0.98


# ================================================================
# 全市场预筛选模式
# ================================================================
PRESCAN_MODES = {
    'entry': {'name': '入门', 'conds': {'ma_bull': True, 'min_trend': 10, 'min_position': 80}},
    'select': {'name': '精选', 'conds': {'ma_bull': True, 'min_trend': 10, 'min_position': 80, 'max_vol_5_20': 1}},
    'elite': {'name': '精英RSI', 'conds': {'ma_bull': True, 'min_trend': 10, 'min_position': 80, 'max_vol_5_20': 1, 'min_rsi14': 80}},
    'elite_kdj': {'name': '精英KDJ', 'conds': {'ma_bull': True, 'min_trend': 10, 'min_position': 80, 'max_vol_5_20': 1, 'kdj_overbought': True}},
    'elite_full': {'name': '全维度', 'conds': {'ma_bull': True, 'min_trend': 10, 'min_position': 80, 'max_vol_5_20': 1, 'never_below_ma10': True}},
    # === 新增: 多头+MA5角+RSI/KDJ 组合 ===
    'trend_a': {'name': '多头+MA5角>0.5%', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5}},
    'trend_b': {'name': '多头+MA5角>0.7%', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.7}},
    'trend_c': {'name': '多头+MA5角>1.0%', 'conds': {'ma_bull': True, 'min_ma5_angle': 1.0}},
    'trend_rsi_a': {'name': '多头+MA5角>0.5%+RSI>55', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 55}},
    'trend_rsi_b': {'name': '多头+MA5角>0.5%+RSI>60', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60}},
    'trend_rsi_c': {'name': '多头+MA5角>0.7%+RSI>55', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.7, 'min_rsi14': 55}},
    'trend_kdj_a': {'name': '多头+MA5角>0.5%+KDJ>60', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_kdj_k': 60}},
    'trend_kdj_b': {'name': '多头+MA5角>0.5%+KDJ>70', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_kdj_k': 70}},
    'trend_kdj_c': {'name': '多头+MA5角>0.7%+KDJ>60', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.7, 'min_kdj_k': 60}},
    'trend_3f_a': {'name': '多头+MA5角>0.3%+RSI>60+KDJ>70', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.3, 'min_rsi14': 60, 'min_kdj_k': 70}},
    'trend_3f_b': {'name': '多头+MA5角>0.5%+RSI>60+KDJ>70', 'conds': {'ma_bull': True, 'min_ma5_angle': 0.5, 'min_rsi14': 60, 'min_kdj_k': 70}},
}


def fetch_all_stock_codes() -> List[str]:
    """获取全市场股票代码列表"""
    pool = _get_cnstock_pool()
    with pool.connection() as conn:
        cur = conn.cursor()
        # 方法1: 从龙虎榜获取 (至少覆盖有游资活动的股票)
        cur.execute("SELECT DISTINCT stock_code FROM cnd_dragon_tiger_list ORDER BY stock_code")
        codes = [r[0] for r in cur.fetchall()]
        cur.close()
    if codes:
        print(f"  股票列表(龙虎榜): {len(codes)}只", file=sys.stderr)
        return codes
    return []


def fetch_all_market_codes() -> List[str]:
    """获取全市场股票代码 (从K线表扫描)"""
    pool = _get_cnstock_pool()
    with pool.connection() as conn:
        cur = conn.cursor()
        year = datetime.now().year
        table = f'kline_1D_{year}'
        try:
            cur.execute(f'SELECT DISTINCT symbol FROM "{table}" ORDER BY symbol')
            codes = [r[0] for r in cur.fetchall()]
            cur.close()
            print(f"  全市场股票: {len(codes)}只", file=sys.stderr)
            return codes
        except Exception:
            cur.close()
            return fetch_all_stock_codes()


def fetch_all_klines(days: int = 200) -> Dict[str, List[Dict]]:
    """加载全市场K线, 返回 {code: [bars]}"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    codes = fetch_all_stock_codes()
    print(f"  加载K线...", file=sys.stderr)
    writer = _get_writer()
    klines = {}
    for i, code in enumerate(codes):
        if (i + 1) % 500 == 0:
            print(f"  加载进度: {i+1}/{len(codes)}", file=sys.stderr)
        try:
            data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
            if not data:
                continue
            bars = []
            for r in data:
                bars.append({
                    "time": str(r["time"])[:10],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                })
            klines[code] = unadj_to_qfq(bars, code)
        except Exception:
            continue
    print(f"  K线加载完成: {len(klines)}只", file=sys.stderr)
    return klines


def fetch_dragon_tiger_by_date() -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """加载龙虎榜, 返回 (by_date, by_code)
    by_date: {date: [{code, name, net_amount, ...}]}
    by_code: {code: [{date, ...}]}
    """
    pool = _get_cnstock_pool()
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, stock_code, stock_name, reason, "
            "buy_amount, sell_amount, net_amount, change_percent, "
            "close_price, turnover_rate, amount, buy_seat_count, sell_seat_count "
            "FROM cnd_dragon_tiger_list ORDER BY trade_date"
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
    by_date = defaultdict(list)
    by_code = defaultdict(list)
    for row in rows:
        d = dict(zip(columns, row))
        by_date[d['trade_date']].append(d)
        by_code[d['stock_code']].append(d)
    print(f"  龙虎榜: {len(rows)}条, {len(by_date)}个交易日", file=sys.stderr)
    return dict(by_date), dict(by_code)


def calc_ma5_angle(closes: List[float], period: int = 5, days: int = 3) -> Optional[float]:
    """计算MA5斜率: 最近N天的MA5变化率(标准化为%/天)"""
    if len(closes) < period + days:
        return None
    ma_series = [sum(closes[i-period+1:i+1]) / period for i in range(period - 1, len(closes))]
    if len(ma_series) < days:
        return None
    recent = ma_series[-days:]
    x = list(range(days))
    n = days
    sum_x = n * (n - 1) / 2
    sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x) if (n * sum_x2 - sum_x * sum_x) != 0 else 0
    if recent[-1] == 0:
        return None
    return slope / recent[-1] * 100


def calc_kdj_k(closes: List[float], highs: List[float], lows: List[float], period: int = 9) -> Optional[float]:
    """计算KDJ的K值 (带平滑)"""
    if len(closes) < period:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hn = max(highs[i-period+1:i+1])
        ln = min(lows[i-period+1:i+1])
        c = closes[i]
        rsvs.append((c - ln) / (hn - ln) * 100 if hn != ln else 50)
    k_val = 50.0
    d_val = 50.0
    for rsv in rsvs:
        k_val = 2 / 3 * k_val + 1 / 3 * rsv
        d_val = 2 / 3 * d_val + 1 / 3 * k_val
    return k_val


def check_prescan(bars_20: List[Dict], conditions: Dict) -> bool:
    """检查20天K线是否符合预筛选条件"""
    if len(bars_20) < 20:
        return False
    closes = [b['close'] for b in bars_20]
    volumes = [b['volume'] for b in bars_20]
    highs = [b['high'] for b in bars_20]
    lows = [b['low'] for b in bars_20]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes) / 20
    ma_bull = ma5 > ma10 > ma20
    if conditions.get('ma_bull') and not ma_bull:
        return False
    pre20_open = bars_20[0]['open']
    pre20_close = bars_20[-1]['close']
    pre20_high = max(highs)
    pre20_low = min(lows)
    pre20_trend = (pre20_close / pre20_open - 1) * 100 if pre20_open > 0 else 0
    pre20_position = (pre20_close - pre20_low) / (pre20_high - pre20_low) * 100 if pre20_high > pre20_low else 50
    if 'min_trend' in conditions and pre20_trend < conditions['min_trend']:
        return False
    if 'min_position' in conditions and pre20_position < conditions['min_position']:
        return False
    avg_vol = sum(volumes) / len(volumes)
    last5_vol = sum(volumes[-5:]) / 5
    vol_ratio_5_20 = last5_vol / avg_vol if avg_vol > 0 else 1
    if 'max_vol_5_20' in conditions and vol_ratio_5_20 >= conditions['max_vol_5_20']:
        return False
    if 'min_rsi14' in conditions:
        rsi = calc_rsi(closes, 14)
        if rsi is None or rsi < conditions['min_rsi14']:
            return False
    # MA5斜率
    if 'min_ma5_angle' in conditions:
        angle = calc_ma5_angle(closes, 5, 3)
        if angle is None or angle < conditions['min_ma5_angle']:
            return False
    # KDJ K值
    if 'min_kdj_k' in conditions:
        kdj_k = calc_kdj_k(closes, highs, lows, 9)
        if kdj_k is None or kdj_k < conditions['min_kdj_k']:
            return False
    if conditions.get('kdj_overbought'):
        kdj_bars = bars_20[-9:] if len(bars_20) >= 9 else bars_20
        high9 = max(b['high'] for b in kdj_bars)
        low9 = min(b['low'] for b in kdj_bars)
        if high9 == low9:
            return False
        rsv = (closes[-1] - low9) / (high9 - low9) * 100
        if rsv <= 80:
            return False
    if conditions.get('never_below_ma10'):
        ma10_arr = []
        for i in range(max(0, len(closes)-10), len(closes)):
            start = max(0, i - 9)
            ma10_arr.append(sum(closes[start:i+1]) / (i - start + 1))
        above = sum(1 for i, c in enumerate(closes[-10:]) if c >= ma10_arr[i] * 0.995)
        if above < 9:
            return False
    return True


# ================================================================
# 技术指标 (保留, 供评分用)
# ================================================================

def calc_ma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
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
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def calc_obv_trend(bars: List[Dict], period: int = 5) -> str:
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
    if change > 0: return "上升"
    elif change < 0: return "下降"
    return "平"


def calc_vol_ratio(bars: List[Dict], idx: int, period: int = 5) -> float:
    if idx < period or period <= 0:
        return 1.0
    avg_vol = sum(bars[i]['volume'] for i in range(idx - period, idx)) / period
    if avg_vol <= 0:
        return 1.0
    return bars[idx]['volume'] / avg_vol


def analyze_tech(bars: List[Dict], idx: int) -> Dict:
    """技术分析评分 (0~100)"""
    if idx < 20 or idx >= len(bars):
        return {"tech_score": 0}

    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    bar = bars[idx]
    prev_bar = bars[idx - 1] if idx > 0 else None

    ma5 = calc_ma(closes[-5:], 5)
    ma10 = calc_ma(closes[-10:], 10)
    ma20 = calc_ma(closes[-20:], 20)
    rsi14 = calc_rsi(closes, 14)
    obv_trend = calc_obv_trend(bars[:idx + 1])
    vol_ratio = calc_vol_ratio(bars, idx)

    change_pct = 0
    if prev_bar and prev_bar['close'] > 0:
        change_pct = (bar['close'] / prev_bar['close'] - 1) * 100

    ma_align = "交叉"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: ma_align = "多头排列"
        elif ma5 < ma10 < ma20: ma_align = "空头排列"

    score = 50
    if ma5 and ma10 and ma5 > ma10: score += 10
    if ma10 and ma20 and ma10 > ma20: score += 10
    if ma_align == "多头排列": score += 10
    elif ma_align == "空头排列": score -= 15
    if rsi14:
        if 40 <= rsi14 <= 60: score += 5
        elif rsi14 > 70: score -= 10
        elif rsi14 < 30: score += 10
    if obv_trend == "上升": score += 10
    elif obv_trend == "下降": score -= 10
    if 1.0 <= vol_ratio <= 2.0: score += 5
    elif vol_ratio > 3.0: score -= 5

    return {
        "ma5": round(ma5, 2) if ma5 else 0,
        "ma10": round(ma10, 2) if ma10 else 0,
        "ma20": round(ma20, 2) if ma20 else 0,
        "ma_align": ma_align,
        "rsi14": round(rsi14, 1) if rsi14 else 0,
        "obv_trend": obv_trend,
        "vol_ratio": round(vol_ratio, 2),
        "change_pct": round(change_pct, 2),
        "tech_score": max(0, min(100, score)),
    }


# ================================================================
# D0 筛选条件
# ================================================================

def check_d0_conditions(
    bars: List[Dict],
    d0_idx: int,
    net_amount: float,
    board_type: str,
    min_net_wan: float = 5000,
) -> Optional[Dict]:
    """检查D0是否满足4个条件

    Args:
        bars: K线数据
        d0_idx: D0在K线中的索引
        net_amount: 游资净买入额(元)
        board_type: 板块类型
        min_net_wan: 最小净买入额(万元), 默认5000万

    Returns:
        满足条件返回特征字典, 不满足返回None
    """
    if d0_idx < 5 or d0_idx >= len(bars):
        return None

    d0 = bars[d0_idx]
    prev_close = bars[d0_idx - 1]['close']

    # 条件①: D0涨停
    if not is_limit_up(d0['close'], prev_close, board_type):
        return None

    # 条件②: D0收盘 > 前5天最高价（突破前高）
    pre5_bars = bars[max(0, d0_idx - 5):d0_idx]
    pre5_high = max(b['high'] for b in pre5_bars) if pre5_bars else 0
    if d0['close'] <= pre5_high:
        return None

    # 条件③: 净买入 > min_net_wan万
    net_wan = net_amount / 10000
    if net_wan < min_net_wan:
        return None

    # 条件④: 量比 < 2x（温和放量）
    pre5_vols = [b['volume'] for b in pre5_bars if b['volume'] > 0]
    avg_pre_vol = sum(pre5_vols) / len(pre5_vols) if pre5_vols else 1
    vol_ratio = d0['volume'] / avg_pre_vol if avg_pre_vol > 0 else 999
    if vol_ratio >= 2.0:
        return None

    # 全部满足, 返回特征
    change_pct = (d0['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
    return {
        "d0_change": round(change_pct, 2),
        "d0_close": d0['close'],
        "d0_high": d0['high'],
        "d0_low": d0['low'],
        "d0_volume": int(d0['volume']),
        "vol_ratio": round(vol_ratio, 2),
        "net_wan": round(net_wan, 2),
        "pre5_high": round(pre5_high, 3),
        "breakout_pct": round((d0['close'] / pre5_high - 1) * 100, 2) if pre5_high > 0 else 0,
    }


# ================================================================
# D1 入场逻辑
# ================================================================

def get_d1_entry(
    bars: List[Dict],
    d0_idx: int,
) -> Optional[Dict]:
    """D1入场: 只做高开(>0%), 低开放弃

    Returns:
        {buy_price, buy_idx, buy_time, entry_type, d1_gap, d1_intraday}
        不入场返回 None
    """
    d1_idx = d0_idx + 1
    if d1_idx >= len(bars):
        return None

    d0_close = bars[d0_idx]['close']
    d1 = bars[d1_idx]

    if d1['open'] <= 0:
        return None

    d1_gap = (d1['open'] / d0_close - 1) * 100
    d1_intraday = (d1['close'] - d1['open']) / d1['open'] * 100 if d1['open'] > 0 else 0

    # 只做高开, 低开放弃
    if d1_gap <= 0:
        return None

    return {
        "buy_price": d1['open'],
        "buy_idx": d1_idx,
        "buy_time": d1['time'],
        "entry_type": "高开买入",
        "d1_gap": round(d1_gap, 2),
        "d1_intraday": round(d1_intraday, 2),
    }


# ================================================================
# 窗口数据
# ================================================================

def extract_window(bars: List[Dict], center_idx: int, before: int = 5, after: int = 10) -> List[Dict]:
    start = max(0, center_idx - before)
    end = min(len(bars), center_idx + after + 1)
    window = []
    for i in range(start, end):
        b = bars[i]
        window.append({
            "time": b['time'],
            "open": round(b['open'], 3),
            "high": round(b['high'], 3),
            "low": round(b['low'], 3),
            "close": round(b['close'], 3),
            "volume": int(b['volume']),
            "offset": i - center_idx,
        })
    return window


# ================================================================
# 回测引擎
# ================================================================

def run_backtest_v5(
    bars: List[Dict],
    buy_idx: int,
    buy_price: float,
    hold_days: int = 10,
    stop_loss: float = -8.0,
    trailing_stop: float = -8.0,
    take_profit: float = 15.0,
) -> Optional[Dict]:
    """v5回测: 从buy_idx开始持有, 追踪止损/止盈

    Args:
        buy_idx: 买入日索引
        buy_price: 买入价
    """
    if buy_price <= 0 or buy_idx >= len(bars):
        return None

    exit_p = buy_price
    exit_d = 0
    exit_reason = "持仓到期"
    peak = buy_price
    daily_returns = []

    for d in range(0, hold_days + 1):
        idx = buy_idx + d
        if idx >= len(bars):
            break

        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        daily_ret = (b['close'] / buy_price - 1) * 100
        daily_returns.append(round(daily_ret, 2))

        # 止盈
        if b['high'] >= buy_price * (1 + take_profit / 100):
            exit_p = buy_price * (1 + take_profit / 100)
            exit_d = d
            exit_reason = "止盈"
            break

        # 追踪止损 (从第2天开始)
        if d > 0 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100)
            exit_d = d
            exit_reason = "追踪止损"
            break

        # 止损
        if b['low'] <= buy_price * (1 + stop_loss / 100):
            exit_p = buy_price * (1 + stop_loss / 100)
            exit_d = d
            exit_reason = "止损"
            break

        exit_p = b['close']
        exit_d = d

    return_pct = (exit_p / buy_price - 1) * 100
    peak_pct = (peak / buy_price - 1) * 100

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round(return_pct, 2),
        'peak_return_pct': round(peak_pct, 2),
        'drawdown': round(peak_pct - return_pct, 2),
        'daily_returns': daily_returns,
    }


# ================================================================
# 统计
# ================================================================

def calc_stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {}
    total = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    win_rate = len(wins) / total * 100
    avg_ret = sum(t['return_pct'] for t in trades) / total
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    plr = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.get('exit_reason', '未知')] += 1

    return {
        'total': total, 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(win_rate, 1),
        'avg_return': round(avg_ret, 2),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'plr': round(plr, 2),
        'avg_hold': round(sum(t['exit_day'] for t in trades) / total, 1),
        'max_return': round(max(t['return_pct'] for t in trades), 2),
        'min_return': round(min(t['return_pct'] for t in trades), 2),
        'exit_reasons': dict(exit_reasons),
    }


def print_stats(stats: Dict, label: str):
    if not stats:
        print(f"  {label}: 无数据")
        return
    print(f"  {label}: {stats['total']}笔")
    print(f"    胜率: {stats['win_rate']}% | 盈亏比: {stats['plr']}")
    print(f"    均收益: {stats['avg_return']:+.2f}% | 均盈利: {stats['avg_win']:+.2f}% | 均亏损: {stats['avg_loss']:+.2f}%")
    print(f"    最大: {stats['max_return']:+.2f}% | 最小: {stats['min_return']:+.2f}%")
    print(f"    均持仓: {stats['avg_hold']:.1f}天")
    if stats.get('exit_reasons'):
        print(f"    出场: {' | '.join(f'{k}:{v}' for k, v in stats['exit_reasons'].items())}")


# ================================================================
# 策略
# ================================================================

def strategy_v5(
    dragon_data: List[Dict],
    kline_cache: Dict[str, List[Dict]],
    window_days: int = 20,
    min_net_wan: float = 5000,
    hold_days: int = 10,
    stop_loss: float = -8.0,
    trailing_stop: float = -8.0,
    take_profit: float = 15.0,
    show_detail: bool = False,
    today_only: bool = False,
) -> List[Dict]:
    """v5策略: 游资D0选股 + D1动态入场

    流程:
      1. 按股票聚合龙虎榜, 找每只股票在window_days内的首次大量买入日
      2. 检查D0是否满足4个条件: 涨停 + 突破前高 + 净买入>5000万 + 量比<2x
      3. D1入场: 高开→开盘买; 低开放弃
      4. 回测出场
    """
    # 计算窗口起始日期
    all_dates = sorted(set(r.get('trade_date', '') for r in dragon_data), reverse=True)
    if not all_dates:
        return []
    latest_date = all_dates[0]
    try:
        cutoff = (datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=window_days * 1.5)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = ""

    # 按股票聚合
    by_code: Dict[str, List[Dict]] = defaultdict(list)
    for row in dragon_data:
        code = row.get('stock_code', '')
        if code:
            by_code[code].append(row)

    trades = []

    for code, rows in by_code.items():
        rows.sort(key=lambda x: x.get('trade_date', ''))

        # 过滤窗口期内的记录
        window_rows = [r for r in rows if r.get('trade_date', '') >= cutoff]
        if not window_rows:
            continue

        # D0 = 窗口期内第一条记录（首次出现）
        d0_row = window_rows[0]
        d0_date = d0_row.get('trade_date', '')

        # 加载K线
        if code not in kline_cache:
            bars = fetch_kline_db(code, 300)
            if bars:
                kline_cache[code] = bars
        bars = kline_cache.get(code)
        if not bars:
            continue

        # 找D0在K线中的索引
        d0_idx = None
        for j, b in enumerate(bars):
            if b['time'] == d0_date:
                d0_idx = j
                break
        if d0_idx is None:
            continue

        board_type = get_board_type(code)
        net_amount = float(d0_row.get('net_amount', 0) or 0)

        # 检查D0的4个条件
        d0_features = check_d0_conditions(bars, d0_idx, net_amount, board_type, min_net_wan)
        if d0_features is None:
            continue

        tech = analyze_tech(bars, d0_idx)

        # === today_only模式: D0盘后, 输出信号 ===
        if today_only:
            score = tech.get('tech_score', 50)
            # D0条件全满足 +10
            score += 10
            # 突破幅度加分
            if d0_features['breakout_pct'] > 3:
                score += 5
            if d0_features['breakout_pct'] > 10:
                score += 5

            trades.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'board': get_board_name(code),
                'strategy': 'v5',
                'signal_date': d0_date,
                'signal_type': 'D0游资买入',
                'score': min(100, score),
                'd0_features': d0_features,
                'tech': tech,
                'd0_info': {
                    'reason': d0_row.get('reason', ''),
                    'buy_amount_wan': round(float(d0_row.get('buy_amount', 0) or 0) / 10000, 2),
                    'sell_amount_wan': round(float(d0_row.get('sell_amount', 0) or 0) / 10000, 2),
                    'buy_seat_count': int(d0_row.get('buy_seat_count', 0) or 0),
                    'sell_seat_count': int(d0_row.get('sell_seat_count', 0) or 0),
                },
            })
            continue

        # === 正常回测模式 ===
        entry = get_d1_entry(bars, d0_idx)
        if entry is None:
            continue

        buy_price = entry['buy_price']
        buy_idx = entry['buy_idx']

        # 回测
        result = run_backtest_v5(bars, buy_idx, buy_price,
                                  hold_days, stop_loss, trailing_stop, take_profit)
        if not result:
            continue

        score = tech.get('tech_score', 50)
        score += 10  # D0条件全满足
        if d0_features['breakout_pct'] > 3:
            score += 5
        if d0_features['breakout_pct'] > 10:
            score += 5
        if entry['entry_type'] == "高开买入":
            score += 5  # 高开买入加分
        if entry['d1_gap'] > 3:
            score += 5

        trades.append({
            'code': code,
            'name': d0_row.get('stock_name', ''),
            'board': get_board_name(code),
            'strategy': 'v5',
            'signal_date': d0_date,
            'signal_type': 'D0游资买入',
            'score': min(100, score),
            'd0_features': d0_features,
            'tech': tech,
            'd0_info': {
                'reason': d0_row.get('reason', ''),
                'buy_amount_wan': round(float(d0_row.get('buy_amount', 0) or 0) / 10000, 2),
                'sell_amount_wan': round(float(d0_row.get('sell_amount', 0) or 0) / 10000, 2),
                'buy_seat_count': int(d0_row.get('buy_seat_count', 0) or 0),
                'sell_seat_count': int(d0_row.get('sell_seat_count', 0) or 0),
            },
            'entry_type': entry['entry_type'],
            'd1_gap': entry['d1_gap'],
            'd1_intraday': entry['d1_intraday'],
            'entry_date': entry['buy_time'],
            'entry_price': round(buy_price, 3),
            **result,
        })

        if show_detail:
            print(f"    {code} {d0_date} D0涨{d0_features['d0_change']:.0f}% "
                  f"突破{d0_features['breakout_pct']:+.1f}% "
                  f"净买{d0_features['net_wan']:.0f}万 "
                  f"量比{d0_features['vol_ratio']:.1f}x "
                  f"-> {entry['entry_type']} gap{entry['d1_gap']:+.1f}% "
                  f"-> {result['exit_reason']} 收益{result['return_pct']:+.1f}%")

    return trades


# ================================================================
# 全市场扫描回测 (--full-scan 模式)
# ================================================================

def full_scan_backtest(
    mode: str = 'select',
    min_net_wan: float = 5000,
    hold_days: int = 7,
    stop_loss: float = -8.0,
    trailing_stop: float = -8.0,
    take_profit: float = 15.0,
    show_detail: bool = False,
    hit_rate_mode: bool = False,
) -> List[Dict]:
    """全市场扫描回测

    流程:
      每天盘后 → 全市场预筛选 → 次日D0确认 → D1入场 → 出场

    hit_rate_mode: 命中率统计模式
      不要求D0涨停, 只要上龙虎榜就算命中
      统计: 池大小、D0命中数、命中率
    """
    mode_cfg = PRESCAN_MODES.get(mode, PRESCAN_MODES['select'])
    conditions = mode_cfg['conds']
    print(f"📊 全市场扫描模式: {mode_cfg['name']}", file=sys.stderr)
    print(f"   条件: {conditions}", file=sys.stderr)
    if hit_rate_mode:
        print(f"   模式: 命中率统计 (不要求D0涨停, 上龙虎榜即算命中)", file=sys.stderr)
    else:
        print(f"   净买入>{min_net_wan}万 | 持仓{hold_days}天 | 止损{stop_loss}%", file=sys.stderr)

    # 加载数据
    print(f"\n📋 加载数据...", file=sys.stderr)
    t0 = time.time()
    # hit_rate_mode 用全市场代码, 否则用龙虎榜代码
    if hit_rate_mode:
        all_codes = fetch_all_market_codes()
        klines = {}
        from app.data_sources.provider.adjustment import unadj_to_qfq
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
        writer = _get_writer()
        for i, code in enumerate(all_codes):
            if (i + 1) % 500 == 0:
                print(f"  加载进度: {i+1}/{len(all_codes)}", file=sys.stderr)
            try:
                data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
                if not data:
                    continue
                bars = []
                for r in data:
                    bars.append({
                        "time": str(r["time"])[:10],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]),
                    })
                klines[code] = unadj_to_qfq(bars, code)
            except Exception:
                continue
        print(f"  K线加载完成: {len(klines)}只", file=sys.stderr)
    else:
        klines = fetch_all_klines(200)
    dragon_by_date, dragon_by_code = fetch_dragon_tiger_by_date()
    print(f"  耗时: {time.time()-t0:.1f}秒", file=sys.stderr)

    # 建立时间索引: {code: {date: bar_idx}}
    code_date_idx = {}
    for code, bars in klines.items():
        idx_map = {}
        for i, b in enumerate(bars):
            idx_map[b['time']] = i
        code_date_idx[code] = idx_map

    all_dates = sorted(set(d for d in dragon_by_date.keys()))
    if not all_dates:
        print("  ❌ 无交易日数据", file=sys.stderr)
        return []
    print(f"  交易日: {len(all_dates)}天 ({all_dates[0]} ~ {all_dates[-1]})", file=sys.stderr)
    print(f"  全市场: {len(klines)}只股票\n", file=sys.stderr)

    # 命中率统计
    total_pool_size = 0       # 所有日的池大小累加
    total_d0_hits = 0         # 池中上龙虎榜的次数
    total_d0_any = 0          # 当日龙虎榜总数
    daily_stats = []          # 每日统计
    trades = []

    for di in range(len(all_dates) - 1):
        date = all_dates[di]
        next_date = all_dates[di + 1]

        # Step 1: 扫描预筛选池
        prescan_pool = set()
        for code, bars in klines.items():
            date_idx = code_date_idx.get(code, {}).get(date)
            if date_idx is None or date_idx < 20:
                continue
            window = bars[date_idx - 19:date_idx + 1]
            if check_prescan(window, conditions):
                prescan_pool.add(code)

        # 次日龙虎榜
        dragon_today = dragon_by_date.get(next_date, [])
        dragon_codes = set(d_rec['stock_code'] for d_rec in dragon_today)

        if hit_rate_mode:
            # 命中率统计模式
            hits = prescan_pool & dragon_codes
            total_pool_size += len(prescan_pool)
            total_d0_hits += len(hits)
            total_d0_any += len(dragon_codes)
            hit_rate = len(hits) / len(prescan_pool) * 100 if prescan_pool else 0
            recall = len(hits) / len(dragon_codes) * 100 if dragon_codes else 0
            daily_stats.append({
                'date': next_date,
                'pool': len(prescan_pool),
                'dragon': len(dragon_codes),
                'hits': len(hits),
                'hit_rate': hit_rate,
                'recall': recall,
            })
            if show_detail and prescan_pool:
                print(f"  {next_date}: 池{len(prescan_pool):>4d} | 龙虎榜{len(dragon_codes):>3d} | "
                      f"命中{len(hits):>3d} | 命中率{hit_rate:>5.1f}% | 召回{recall:>5.1f}%",
                      file=sys.stderr)
            continue

        # 正常回测模式
        if not prescan_pool:
            continue

        for d_rec in dragon_today:
            code = d_rec['stock_code']
            net_amount = float(d_rec.get('net_amount', 0) or 0)
            change_pct = float(d_rec.get('change_percent', 0) or 0)

            if code not in prescan_pool:
                continue
            if change_pct < 9.5:
                continue
            if net_amount / 10000 < min_net_wan:
                continue

            bars = klines.get(code)
            if not bars:
                continue
            d0_idx = code_date_idx.get(code, {}).get(next_date)
            if d0_idx is None or d0_idx < 1 or d0_idx >= len(bars) - 1:
                continue

            board_type = get_board_type(code)
            prev_close = bars[d0_idx - 1]['close']
            if not is_limit_up(bars[d0_idx]['close'], prev_close, board_type):
                continue

            # D1入场
            entry = get_d1_entry(bars, d0_idx)
            if entry is None:
                continue

            buy_price = entry['buy_price']
            buy_idx = entry['buy_idx']

            # 出场
            result = run_backtest_v5(bars, buy_idx, buy_price,
                                     hold_days, stop_loss, trailing_stop, take_profit)
            if not result:
                continue

            trade = {
                'code': code,
                'name': d_rec.get('stock_name', ''),
                'd0_date': next_date,
                'entry_type': entry['entry_type'],
                'd1_gap': entry['d1_gap'],
                'd1_intraday': entry['d1_intraday'],
                'buy_price': round(buy_price, 3),
                'net_wan': round(net_amount / 10000, 0),
                **result,
            }
            trades.append(trade)

            if show_detail:
                emoji = '✅' if result['return_pct'] > 0 else '❌'
                print(f"  {emoji} {code} {next_date} 净买{net_amount/10000:.0f}万 "
                      f"-> {entry['entry_type']} gap{entry['d1_gap']:+.1f}% "
                      f"-> {result['exit_reason']} {result['return_pct']:+.1f}%",
                      file=sys.stderr)

    # === 输出统计 ===
    if hit_rate_mode:
        if not daily_stats:
            print("\n❌ 无数据", file=sys.stderr)
            return []

        avg_pool = total_pool_size / len(daily_stats)
        avg_hit_rate = total_d0_hits / total_pool_size * 100 if total_pool_size > 0 else 0
        avg_recall = total_d0_hits / total_d0_any * 100 if total_d0_any > 0 else 0
        base_rate = total_d0_any / (len(klines) * len(daily_stats)) * 100 if daily_stats else 0

        print(f"\n{'='*70}", file=sys.stderr)
        print(f"📊 命中率统计结果: {mode_cfg['name']}", file=sys.stderr)
        print(f"{'='*70}", file=sys.stderr)
        print(f"  统计天数: {len(daily_stats)}天", file=sys.stderr)
        print(f"  全市场: {len(klines)}只", file=sys.stderr)
        print(f"  日均池大小: {avg_pool:.0f}只", file=sys.stderr)
        print(f"  日均龙虎榜: {total_d0_any/len(daily_stats):.1f}只", file=sys.stderr)
        print(f"  日均命中: {total_d0_hits/len(daily_stats):.1f}只", file=sys.stderr)
        print(f"  命中率(池中D0占比): {avg_hit_rate:.2f}%", file=sys.stderr)
        print(f"  召回率(池覆盖D0): {avg_recall:.2f}%", file=sys.stderr)
        print(f"  基准命中率(无过滤): {base_rate:.2f}%", file=sys.stderr)
        if base_rate > 0:
            print(f"  提升倍数: {avg_hit_rate/base_rate:.2f}x", file=sys.stderr)

        # 按月统计
        print(f"\n  月度统计:", file=sys.stderr)
        monthly = defaultdict(lambda: {'pool': 0, 'hits': 0, 'dragon': 0, 'days': 0})
        for s in daily_stats:
            m = s['date'][:7]
            monthly[m]['pool'] += s['pool']
            monthly[m]['hits'] += s['hits']
            monthly[m]['dragon'] += s['dragon']
            monthly[m]['days'] += 1
        for m in sorted(monthly.keys()):
            d = monthly[m]
            avg_p = d['pool'] / d['days']
            hr = d['hits'] / d['pool'] * 100 if d['pool'] > 0 else 0
            rc = d['hits'] / d['dragon'] * 100 if d['dragon'] > 0 else 0
            print(f"    {m}: 池均{avg_p:>5.0f}只 | 命中率{hr:>5.1f}% | 召回{rc:>5.1f}% "
                  f"| 龙虎榜{d['dragon']:>4d} | 命中{d['hits']:>4d}", file=sys.stderr)

        return daily_stats

    # 正常回测模式输出
    print(f"\n📊 回测完成:", file=sys.stderr)
    print(f"  成交: {len(trades)}笔", file=sys.stderr)
    return trades


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="龙虎榜游资D0策略 v5")
    parser.add_argument("--days", type=int, default=20, help="D0搜索窗口(交易日)")
    parser.add_argument("--hold-days", type=int, default=7, help="持仓天数")
    parser.add_argument("--stop-loss", type=float, default=-8.0, help="止损%%")
    parser.add_argument("--trailing-stop", type=float, default=-8.0, help="追踪止损%%")
    parser.add_argument("--take-profit", type=float, default=15.0, help="止盈%%")
    parser.add_argument("--min-net", type=float, default=5000, help="最小净买入额(万)")
    parser.add_argument("--today", action="store_true", help="D0盘后信号模式")
    parser.add_argument("--today-date", type=str, default="", help="指定D0日期")
    parser.add_argument("--all-trades", action="store_true", help="输出交易明细")
    parser.add_argument("--detail", action="store_true", help="输出详细匹配信息")
    parser.add_argument("--compare", action="store_true", help="对比不同参数")
    parser.add_argument("--full-scan", action="store_true", help="全市场扫描回测(需数据库)")
    parser.add_argument("--prescan-mode", type=str, default="select",
                        choices=list(PRESCAN_MODES.keys()),
                        help="预筛选模式: entry/select/elite/elite_kdj/elite_full/trend_a/trend_b/...")
    parser.add_argument("--hit-rate", action="store_true",
                        help="命中率统计模式 (不要求D0涨停, 统计池中龙虎榜占比)")
    args = parser.parse_args()

    print("=" * 80)
    print("龙虎榜游资D0策略 v5")
    print("D0: 涨停 + 突破前高 + 净买入>5000万 + 量比<2x")
    print("D1: 高开(>0%)→开盘买 | 低开(≤0%)→放弃")
    print("=" * 80)

    # === 全市场扫描回测模式 ===
    if args.full_scan:
        print(f"\n🔄 全市场扫描回测...")
        trades = full_scan_backtest(
            mode=args.prescan_mode,
            min_net_wan=args.min_net,
            hold_days=args.hold_days,
            stop_loss=args.stop_loss,
            trailing_stop=args.trailing_stop,
            take_profit=args.take_profit,
            show_detail=args.detail,
            hit_rate_mode=args.hit_rate,
        )
        if args.hit_rate:
            # 命中率模式已输出, 这里只需保存结果
            if trades:
                outfile = "hit_rate_stats.json"
                with open(outfile, "w", encoding="utf-8") as f:
                    json.dump(trades, f, ensure_ascii=False, indent=2)
                print(f"\n💾 {outfile} ({len(trades)}天)")
            return

        if not trades:
            print("\n❌ 无交易")
            return

        print(f"\n{'=' * 80}")
        print(f"📊 全市场扫描回测结果")
        print(f"{'=' * 80}")

        stats = calc_stats(trades)
        print_stats(stats, PRESCAN_MODES.get(args.prescan_mode, {}).get('name', args.prescan_mode))

        total_ret = sum(t['return_pct'] for t in trades)
        total_days = sum(t['exit_day'] for t in trades)
        rpd = total_ret / total_days if total_days > 0 else 0
        print(f"\n    单位时间: 日均{rpd:+.3f}% | 年化{rpd * 250:+.1f}%")

        # 入场类型
        print(f"\n  入场类型: 高开买入")
        seg = [t for t in trades if t.get('entry_type') == '高开买入']
        if seg:
            s = calc_stats(seg)
            seg_rpd = sum(t['return_pct'] for t in seg) / sum(t['exit_day'] for t in seg) if sum(t['exit_day'] for t in seg) > 0 else 0
            print(f"    {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 日均{seg_rpd:>+.3f}%")

        # 月度统计
        print(f"\n  月度统计:")
        monthly = defaultdict(list)
        for t in trades:
            monthly[t['d0_date'][:7]].append(t)
        for month in sorted(monthly.keys()):
            seg = monthly[month]
            s = calc_stats(seg)
            total_r = sum(t['return_pct'] for t in seg)
            print(f"    {month}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 总收益{total_r:>+8.2f}%")

        if args.all_trades:
            print(f"\n{'=' * 80}")
            print(f"📋 交易明细 ({len(trades)}笔)")
            print(f"{'=' * 80}")
            for t in sorted(trades, key=lambda x: x['d0_date']):
                emoji = '✅' if t['return_pct'] > 0 else '❌'
                print(f"  {emoji} {t['code']:>8} {t['d0_date']} "
                      f"净买{t['net_wan']:.0f}万 {t['entry_type'][:4]} gap{t['d1_gap']:+.1f}% "
                      f"买{t['buy_price']:>7.2f} -> {t['exit_reason'][:6]} "
                      f"持{t['exit_day']}天 收益{t['return_pct']:>+6.2f}%")
        return

    # === 原有模式: 龙虎榜反查 ===
    print(f"\n📊 加载龙虎榜数据 (窗口={args.days}天)...")
    dragon_data = fetch_dragon_tiger_from_db()
    print(f"  龙虎榜: {len(dragon_data)}条")

    if not dragon_data:
        print("\n❌ 无数据")
        return

    kline_cache = {}

    # 对比模式
    if args.compare:
        print(f"\n{'=' * 80}")
        print(f"📊 参数对比")
        print(f"{'=' * 80}")

        for min_net in [1000, 3000, 5000, 8000, 10000]:
            trades = strategy_v5(dragon_data, kline_cache,
                                  window_days=args.days, min_net_wan=min_net,
                                  hold_days=args.hold_days, stop_loss=args.stop_loss,
                                  trailing_stop=args.trailing_stop, take_profit=args.take_profit)
            if trades:
                stats = calc_stats(trades)
                total_ret = sum(t['return_pct'] for t in trades)
                total_days = sum(t['exit_day'] for t in trades)
                rpd = total_ret / total_days if total_days > 0 else 0
                print(f"  净买入>{min_net:>6}万: "
                      f"{stats['total']:>4}笔 胜率{stats['win_rate']:>5.1f}% "
                      f"均收益{stats['avg_return']:>+6.2f}% 日均{rpd:>+.3f}%")
        return

    # === today模式 ===
    if args.today:
        d0_str = args.today_date or datetime.now().strftime("%Y-%m-%d")

        print(f"\n🔄 筛选 {d0_str} D0信号 (净买入>{args.min_net}万)...")
        trades = strategy_v5(dragon_data, kline_cache,
                              window_days=args.days, min_net_wan=args.min_net,
                              today_only=True)
        trades = [t for t in trades if t.get('signal_date') == d0_str]

        print(f"\n{'=' * 80}")
        print(f"📅 {d0_str} 盘后信号 -> D1操作")
        print(f"   {len(trades)} 只股票满足D0条件")
        print(f"{'=' * 80}")

        if trades:
            trades.sort(key=lambda x: -x.get('score', 0))

            print(f"\n  {'排名':>4} {'代码':>8} {'板块':>6} {'评分':>4} "
                  f"{'D0涨幅':>7} {'突破':>7} {'净买入':>8} {'量比':>5} {'操作建议'}")
            print(f"  {'-' * 90}")

            for rank, t in enumerate(trades, 1):
                d0f = t.get('d0_features', {})
                tech = t.get('tech', {})

                action = "D1高开→开盘买"
                if tech.get('rsi14', 50) > 70:
                    action += " ⚠️RSI高"

                print(f"  {rank:>4} {t['code']:>8} {t['board']:>6} {t['score']:>4} "
                      f"{d0f.get('d0_change', 0):>+6.1f}% "
                      f"{d0f.get('breakout_pct', 0):>+6.1f}% "
                      f"{d0f.get('net_wan', 0):>7.0f}万 "
                      f"{d0f.get('vol_ratio', 0):>4.1f}x "
                      f"{action}")

            print(f"\n  操作建议:")
            print(f"  1. D1 高开(>0%) → 开盘买入")
            print(f"  2. D1 低开(≤0%) → 放弃不买")
            print(f"  3. 买入后: 追踪止损-8%, 止盈+15%, 持仓上限7天")
        else:
            print(f"  今日无符合条件的D0信号")
        return

    # === 正常回测 ===
    print(f"  参数: 窗口{args.days}天 净买入>{args.min_net}万 "
          f"持仓{args.hold_days}天 止损{args.stop_loss}% 止盈{args.take_profit}%")

    print(f"\n🔄 运行策略...")
    trades = strategy_v5(dragon_data, kline_cache,
                          window_days=args.days, min_net_wan=args.min_net,
                          hold_days=args.hold_days, stop_loss=args.stop_loss,
                          trailing_stop=args.trailing_stop, take_profit=args.take_profit,
                          show_detail=args.detail)

    # 结果
    print(f"\n{'=' * 80}")
    print(f"📊 回测结果")
    print(f"{'=' * 80}")

    stats = calc_stats(trades)
    print_stats(stats, "v5策略")

    if trades:
        total_ret = sum(t['return_pct'] for t in trades)
        total_days = sum(t['exit_day'] for t in trades)
        rpd = total_ret / total_days if total_days > 0 else 0
        print(f"\n    单位时间: 日均{rpd:+.3f}% | 年化{rpd * 250:+.1f}%")

    # 入场类型
    if trades:
        print(f"\n  入场类型: 高开买入")
        seg = [t for t in trades if t.get('entry_type') == '高开买入']
        if seg:
            s = calc_stats(seg)
            seg_rpd = sum(t['return_pct'] for t in seg) / sum(t['exit_day'] for t in seg) if sum(t['exit_day'] for t in seg) > 0 else 0
            print(f"    {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 均持仓{s['avg_hold']:.1f}天 日均{seg_rpd:>+.3f}%")

    # 按出场原因分组
    if trades:
        print(f"\n  出场原因:")
        for reason in ['止盈', '追踪止损', '止损', '持仓到期']:
            seg = [t for t in trades if t.get('exit_reason') == reason]
            if seg:
                s = calc_stats(seg)
                seg_rpd = sum(t['return_pct'] for t in seg) / sum(t['exit_day'] for t in seg) if sum(t['exit_day'] for t in seg) > 0 else 0
                print(f"    {reason:>6}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                      f"均收益{s['avg_return']:>+6.2f}% 均持仓{s['avg_hold']:.1f}天 日均{seg_rpd:>+.3f}%")

    # D1 gap分段
    if trades:
        print(f"\n  D1开盘gap分段:")
        for lo, hi in [(-10, 0), (0, 1), (1, 3), (3, 5), (5, 10)]:
            seg = [t for t in trades if lo <= t.get('d1_gap', 0) < hi]
            if seg:
                s = calc_stats(seg)
                print(f"    {lo:>+3}%~{hi:>+3}%: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% 均收益{s['avg_return']:>+6.2f}%")

    # 交易明细
    if args.all_trades and trades:
        print(f"\n{'=' * 80}")
        print(f"📋 交易明细 ({len(trades)}笔)")
        print(f"{'=' * 80}")
        for t in sorted(trades, key=lambda x: x.get('entry_date', '')):
            emoji = '✅' if t['return_pct'] > 0 else '❌'
            d0f = t.get('d0_features', {})
            print(f"  {emoji} {t['code']:>8} {t['board']:>6} "
                  f"{t['signal_date']} D0涨{d0f.get('d0_change', 0):.0f}% "
                  f"突破{d0f.get('breakout_pct', 0):+.0f}% "
                  f"净买{d0f.get('net_wan', 0):.0f}万 "
                  f"-> {t.get('entry_type', '')} {t.get('d1_gap', 0):+.1f}% "
                  f"买{t['entry_price']:>7.2f} "
                  f"-> {t['exit_reason'][:6]} 收益{t['return_pct']:>+6.2f}%")

    # 导出
    if trades:
        outfile = "test_dragon_hot_result.json"
        # 清理不可序列化的内容
        export = []
        for t in trades:
            row = {k: v for k, v in t.items() if k != 'ohlcv_window'}
            export.append(row)
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {outfile} ({len(trades)}笔)")


if __name__ == "__main__":
    main()
