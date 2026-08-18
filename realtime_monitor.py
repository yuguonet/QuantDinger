#!/usr/bin/env python3
"""
强势股盘中实时弱转强监控
━━━━━━━━━━━━━━━━━━━━━━━━━

功能:
  1. 盘前选股: 三均线多头排列 + MA5陡峭上攻 + 近期龙虎榜
  2. 盘中实时: 每分钟拉取分时数据, 检测弱转强信号
  3. 买入建议: 触发时输出建议买入价 + 信号类型

用法:
  python realtime_monitor.py                    # 完整运行 (选股+监控)
  python realtime_monitor.py --scan             # 仅选股, 不监控
  python realtime_monitor.py --codes 000001,600519  # 手动指定股票
  python realtime_monitor.py --dragon-days 10   # 龙虎榜回看天数
  python realtime_monitor.py --angle 1.0        # MA5斜率阈值 (%/天)
  python realtime_monitor.py --interval 30      # 盘中轮询间隔(秒)

弱转强信号:
  A. 分时突破: 开盘弱势(低于VWAP) → 放量突破VWAP → 站稳
  B. 平缓上升: 价格沿VWAP平缓上行 + VWAP持续向上 + 量能温和放大
"""
from __future__ import annotations
import json, time, argparse, os, sys, re, math
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

_pool_cache = None
def _get_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    _pool_cache = get_market_db_manager()._get_pool("CNStock")
    return _pool_cache

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

def load_dragon_tiger(days: int = 15) -> Dict[str, List[Dict]]:
    """加载龙虎榜, 返回 {code: [{trade_date, stock_name, ...}, ...]}"""
    pool = _get_pool()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, stock_code, stock_name, reason, "
            "buy_amount, sell_amount, net_amount, change_percent "
            "FROM cnd_dragon_tiger_list WHERE trade_date >= %s ORDER BY trade_date DESC",
            (cutoff,)
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
    result = defaultdict(list)
    for row in rows:
        r = dict(zip(columns, row))
        result[r['stock_code']].append(r)
    return dict(result)


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
# 技术指标计算
# ================================================================

def calc_ma(bars: List[Dict], period: int, field: str = "close") -> List[Optional[float]]:
    """计算移动平均线, 返回与bars等长的列表"""
    result = []
    for i in range(len(bars)):
        if i < period - 1:
            result.append(None)
        else:
            vals = [bars[j][field] for j in range(i - period + 1, i + 1)]
            result.append(sum(vals) / period)
    return result


def calc_ma5_angle(ma5_values: List[Optional[float]], idx: int, lookback: int = 5) -> Optional[float]:
    """计算MA5斜率 (线性回归, %/天)

    返回值含义:
      1.0  → MA5每天上涨幅度约1% (对应45度角左右)
      0.5  → 每天约0.5% (温和上升)
      2.0  → 每天约2% (非常陡峭)

    注: 角度 = arctan(slope), slope=1 → 45°
         实际图表角度取决于Y轴缩放, 此处以百分比斜率为准
    """
    if idx < lookback - 1:
        return None
    vals = []
    for i in range(idx - lookback + 1, idx + 1):
        if ma5_values[i] is None:
            return None
        vals.append(ma5_values[i])
    if not vals or vals[0] <= 0:
        return None
    # 线性回归斜率
    n = len(vals)
    sum_x = n * (n - 1) / 2
    sum_y = sum(vals)
    sum_xy = sum(i * vals[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    # 转为百分比斜率 (相对于MA5当前值)
    return slope / vals[-1] * 100 if vals[-1] > 0 else None


def check_bullish_alignment(ma5: float, ma10: float, ma20: float) -> bool:
    """三均线多头排列: MA5 > MA10 > MA20"""
    return ma5 > ma10 > ma20


# ================================================================
# 选股: 盘前筛选
# ================================================================

def screen_candidates(dragon_days: int = 15, angle_threshold: float = 1.0,
                      kline_days: int = 60) -> List[Dict]:
    """盘前选股: 多头排列 + MA5陡峭 + 近期龙虎榜

    Args:
        dragon_days: 龙虎榜回看天数
        angle_threshold: MA5斜率阈值 (%/天), 1.0 ≈ 45度角
        kline_days: 日K线回看天数

    Returns:
        候选股列表, 每项含 code, name, board, ma5, angle, dragon_date 等
    """
    print(f"\n{'='*70}")
    print(f"  📋 盘前选股")
    print(f"  条件: MA5>MA10>MA20 + MA5斜率>{angle_threshold}%/天 + 近{dragon_days}日龙虎榜")
    print(f"{'='*70}")

    # 1. 加载龙虎榜
    print(f"\n  [1/3] 加载龙虎榜数据...")
    dragon_data = load_dragon_tiger(dragon_days)
    dragon_codes = list(dragon_data.keys())
    print(f"        龙虎榜股票: {len(dragon_codes)}只")

    if not dragon_codes:
        print("  ❌ 龙虎榜无数据")
        return []

    # 2. 逐只检查技术面
    print(f"\n  [2/3] 技术面筛选...")
    candidates = []
    for code in dragon_codes:
        bars = load_kline_db(code, kline_days)
        if not bars or len(bars) < 25:
            continue

        # 计算均线
        ma5_list = calc_ma(bars, 5)
        ma10_list = calc_ma(bars, 10)
        ma20_list = calc_ma(bars, 20)

        idx = len(bars) - 1  # 最新一天
        ma5 = ma5_list[idx]
        ma10 = ma10_list[idx]
        ma20 = ma20_list[idx]

        if ma5 is None or ma10 is None or ma20 is None:
            continue

        # 条件1: 多头排列
        if not check_bullish_alignment(ma5, ma10, ma20):
            continue

        # 条件2: MA5斜率 > 阈值
        angle = calc_ma5_angle(ma5_list, idx, lookback=5)
        if angle is None or angle < angle_threshold:
            continue

        # 取最近一次龙虎榜日期
        dragon_entries = dragon_data[code]
        latest_dragon = dragon_entries[0]['trade_date'] if dragon_entries else ""

        name = dragon_entries[0].get('stock_name', '') if dragon_entries else ''
        board = get_board_name(code)

        # 日内动量 (最新一天)
        last_bar = bars[idx]
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        change_pct = (last_bar['close'] / prev_close - 1) * 100 if prev_close > 0 else 0

        candidates.append({
            'code': code,
            'name': name,
            'board': board,
            'limit_pct': get_limit_pct(code),
            'ma5': round(ma5, 2),
            'ma10': round(ma10, 2),
            'ma20': round(ma20, 2),
            'angle': round(angle, 2),
            'last_close': last_bar['close'],
            'last_date': last_bar['time'],
            'change_pct': round(change_pct, 2),
            'dragon_date': latest_dragon,
            'dragon_count': len(dragon_entries),
            'prev_close': prev_close,
        })

    # 按斜率降序
    candidates.sort(key=lambda x: -x['angle'])

    print(f"\n  [3/3] 结果: {len(candidates)}只通过筛选")
    if candidates:
        print(f"\n  {'代码':>8} {'名称':>8} {'板块':>6} {'MA5':>8} {'MA10':>8} {'MA20':>8} "
              f"{'斜率%/天':>8} {'最新涨跌':>8} {'龙虎榜':>10}")
        print(f"  {'-'*82}")
        for c in candidates:
            angle_emoji = '🔥' if c['angle'] >= 2.0 else ('📈' if c['angle'] >= 1.5 else '')
            print(f"  {c['code']:>8} {c['name']:>8} {c['board']:>6} "
                  f"{c['ma5']:>8.2f} {c['ma10']:>8.2f} {c['ma20']:>8.2f} "
                  f"{c['angle']:>7.2f}% {angle_emoji} {c['change_pct']:>+7.1f}% "
                  f"{c['dragon_date']:>10}")

    return candidates


# ================================================================
# 实时分时数据 (东财 push2 API)
# ================================================================

import urllib.request, ssl, json as _json

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Accept": "*/*",
}
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))


def to_em_secid(code: str) -> str:
    """股票代码 → 东财 secid (1.沪 0.深)"""
    c = str(code).zfill(6)
    return f"1.{c}" if c.startswith("6") else f"0.{c}"


def fetch_realtime_ticks(code: str) -> Optional[List[Dict]]:
    """拉取今日1分钟K线 (东财 trends2 API)

    返回: [{time, open, high, low, close, volume, amount}, ...]
    每个交易日约 240 根 (9:30~15:00)
    """
    secid = to_em_secid(code)
    url = (f"https://push2.eastmoney.com/api/qt/stock/trends2/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1")
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with _opener.open(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        d = _json.loads(raw)
        trends = (d.get("data") or {}).get("trends") or []
        if not trends:
            return None
        bars = []
        for t in trends:
            p = t.split(",")
            if len(p) < 7:
                continue
            bars.append({
                "time": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
            })
        return bars if bars else None
    except Exception as e:
        return None


def fetch_realtime_quote(code: str) -> Optional[Dict]:
    """拉取实时行情快照 (东财 push2 单股接口)

    返回: {price, open, high, low, prev_close, volume, amount, change_pct, ...}
    """
    secid = to_em_secid(code)
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?"
           f"secid={secid}&fields=f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,f170")
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with _opener.open(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        d = _json.loads(raw)
        data = d.get("data") or {}
        if not data:
            return None
        # f43=最新价 f44=最高 f45=最低 f46=今开 f47=成交量 f48=成交额
        # f50=量比 f55=涨速 f57=代码 f58=名称 f60=昨收 f170=涨跌幅
        def _p(v):
            return float(v) / 100 if v and v != '-' else 0.0
        return {
            'price': _p(data.get('f43')),
            'high': _p(data.get('f44')),
            'low': _p(data.get('f45')),
            'open': _p(data.get('f46')),
            'volume': float(data.get('f47', 0) or 0),
            'amount': float(data.get('f48', 0) or 0),
            'vol_ratio': float(data.get('f50', 0) or 0) / 100 if data.get('f50') else 0,
            'prev_close': _p(data.get('f60')),
            'change_pct': float(data.get('f170', 0) or 0) / 100 if data.get('f170') else 0,
        }
    except Exception:
        return None


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

def detect_weak_to_strong(ticks: List[Dict], candidate: Dict,
                          lookback: int = 15) -> Optional[Dict]:
    """检测弱转强信号

    信号A - 分时突破:
      条件: 价格曾低于VWAP → 当前突破VWAP → 突破时量能放大
      含义: 早盘弱势, 买盘介入, 放量突破均价线

    信号B - 平缓上升+VWAP向上:
      条件: 价格在VWAP上方平缓上升 → VWAP持续走高 → 量能温和
      含义: 稳定买盘, 趋势延续

    Args:
        ticks: 今日1分钟K线
        candidate: 候选股信息 (含prev_close等)
        lookback: 回看窗口 (分钟)

    Returns:
        信号详情 dict 或 None
    """
    if len(ticks) < 10:
        return None

    current = ticks[-1]
    current_price = current['close']
    prev_close = candidate.get('prev_close', 0)
    if prev_close <= 0:
        return None

    # 当前涨跌幅
    change_pct = (current_price / prev_close - 1) * 100

    # VWAP
    vwap = calc_vwap(ticks)
    if vwap <= 0:
        return None

    # 价格相对VWAP
    price_vs_vwap = (current_price / vwap - 1) * 100

    # 分时均线 (20分钟)
    intraday_ma = calc_intraday_ma(ticks, 20)

    # 最近N分钟的VWAP序列 (用于判断VWAP趋势)
    vwap_seq = []
    for i in range(max(0, len(ticks) - lookback), len(ticks)):
        vwap_seq.append(calc_vwap(ticks[:i + 1]))

    # VWAP趋势: 最近N分钟VWAP是否持续上升
    vwap_rising = False
    if len(vwap_seq) >= 5:
        vwap_changes = [vwap_seq[i] - vwap_seq[i - 1] for i in range(1, len(vwap_seq))]
        rising_count = sum(1 for c in vwap_changes if c > 0)
        vwap_rising = rising_count >= len(vwap_changes) * 0.6  # 60%以上时间VWAP在涨

    # 最近5分钟量比
    vol_ratio_recent = 0.0
    if len(ticks) >= 6:
        recent_vols = [ticks[i]['volume'] for i in range(len(ticks) - 5, len(ticks))]
        prev_vols = [ticks[i]['volume'] for i in range(max(0, len(ticks) - 10), len(ticks) - 5)]
        avg_prev = sum(prev_vols) / len(prev_vols) if prev_vols else 1
        vol_ratio_recent = (sum(recent_vols) / len(recent_vols)) / avg_prev if avg_prev > 0 else 1.0

    signal = None

    # ========== 信号A: 分时突破 ==========
    # 检查: 最近lookback分钟内, 价格从VWAP下方突破到上方
    if len(ticks) >= lookback:
        # 找lookback窗口内VWAP下方最低点
        below_vwap_count = 0
        min_below = 0  # 低于VWAP的最大幅度
        for i in range(len(ticks) - lookback, len(ticks)):
            t = ticks[i]
            v = calc_vwap(ticks[:i + 1])
            if t['close'] < v:
                below_vwap_count += 1
                gap = (t['close'] / v - 1) * 100
                if gap < min_below:
                    min_below = gap

        # 当前在VWAP上方 + 之前有在VWAP下方 + 量能放大
        if (price_vs_vwap > 0.1
                and below_vwap_count >= 3
                and min_below < -0.3
                and vol_ratio_recent >= 1.3):
            signal = {
                'type': 'A_分时突破',
                'label': '突破VWAP',
                'emoji': '🚀',
                'price': current_price,
                'vwap': round(vwap, 3),
                'price_vs_vwap': round(price_vs_vwap, 2),
                'change_pct': round(change_pct, 2),
                'vol_ratio': round(vol_ratio_recent, 2),
                'detail': (f"从VWAP下方{min_below:+.1f}%拉起, "
                           f"当前高于VWAP {price_vs_vwap:+.1f}%, "
                           f"量比{vol_ratio_recent:.1f}x"),
            }

    # ========== 信号B: 平缓上升+VWAP向上 ==========
    if signal is None and len(ticks) >= lookback:
        # 检查: 最近N分钟价格在VWAP上方, 且走势平缓上升
        above_vwap_count = 0
        price_changes = []
        for i in range(len(ticks) - lookback, len(ticks)):
            t = ticks[i]
            v = calc_vwap(ticks[:i + 1])
            if t['close'] >= v:
                above_vwap_count += 1
            if i > len(ticks) - lookback:
                price_changes.append(t['close'] - ticks[i - 1]['close'])

        # 价格在VWAP上方占比 > 70%
        above_ratio = above_vwap_count / lookback

        # 价格变化方向: 上涨分钟数 > 下跌分钟数
        up_moves = sum(1 for c in price_changes if c > 0)
        down_moves = sum(1 for c in price_changes if c < 0)

        # 涨幅不能太大 (平缓: 最近N分钟涨幅 < 3%)
        window_start_price = ticks[len(ticks) - lookback]['close']
        window_change = (current_price / window_start_price - 1) * 100 if window_start_price > 0 else 0

        if (above_ratio >= 0.7
                and vwap_rising
                and up_moves >= down_moves
                and 0.3 < window_change < 3.0):  # 平缓: 涨0.3%~3%
            signal = {
                'type': 'B_平缓上升',
                'label': '趋势延续',
                'emoji': '📈',
                'price': current_price,
                'vwap': round(vwap, 3),
                'price_vs_vwap': round(price_vs_vwap, 2),
                'change_pct': round(change_pct, 2),
                'vol_ratio': round(vol_ratio_recent, 2),
                'detail': (f"VWAP上方{above_ratio * 100:.0f}%, "
                           f"VWAP持续↑, "
                           f"{lookback}分涨{window_change:+.1f}%, "
                           f"量比{vol_ratio_recent:.1f}x"),
            }

    if signal is None:
        return None

    # 补充公共字段
    signal.update({
        'code': candidate['code'],
        'name': candidate['name'],
        'board': candidate['board'],
        'limit_pct': candidate['limit_pct'],
        'angle': candidate['angle'],
        'dragon_date': candidate['dragon_date'],
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
    print(f"  信号A: 分时突破VWAP (放量)")
    print(f"  信号B: 平缓上升+VWAP向上")
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

            for cand in candidates:
                code = cand['code']

                # 拉取分时数据
                ticks = fetch_realtime_ticks(code)
                if not ticks or len(ticks) < 5:
                    continue

                # 检测信号
                signal = detect_weak_to_strong(ticks, cand)
                if signal is None:
                    continue

                # 检查冷却
                if not tracker.should_alert(code, signal['time']):
                    continue

                tracker.record(code, signal['time'])
                triggered_this_round.append(signal)
                all_signals.append(signal)

            # 输出本轮结果
            if triggered_this_round:
                for sig in triggered_this_round:
                    print(f"\n  {'━'*65}")
                    print(f"  {sig['emoji']} 弱转强信号 [{sig['type']}]")
                    print(f"  {'━'*65}")
                    print(f"  股票: {sig['code']} {sig['name']} ({sig['board']})")
                    print(f"  时间: {sig['time']}")
                    print(f"  价格: {sig['price']:.2f}  涨跌: {sig['change_pct']:+.2f}%")
                    print(f"  VWAP: {sig['vwap']:.2f}  价格偏离: {sig['price_vs_vwap']:+.2f}%")
                    print(f"  量比: {sig['vol_ratio']:.1f}x  MA5斜率: {sig['angle']:.2f}%/天")
                    print(f"  龙虎榜: {sig['dragon_date']}")
                    print(f"  详情: {sig['detail']}")

                    # 买入建议
                    limit_pct = sig['limit_pct']
                    board = sig['board']
                    buy_price = sig['price']
                    # 追踪止损建议
                    stop_loss = -5.0 if board in ['沪主板', '深主板'] else -8.0
                    print(f"\n  💡 买入建议:")
                    print(f"     建议价: {buy_price:.2f} (当前价)")
                    print(f"     止损位: {buy_price * (1 + stop_loss / 100):.2f} ({stop_loss}%)")
                    print(f"     涨停价: {sig['price'] / (1 + sig['change_pct'] / 100) * (1 + limit_pct / 100):.2f} ({limit_pct}%)")
                    print(f"  {'━'*65}")
            else:
                # 静默状态行
                latest_price = ""
                if candidates:
                    # 随机取一个显示价格 (避免全部拉取)
                    c = candidates[0]
                    q = fetch_realtime_quote(c['code'])
                    if q:
                        latest_price = f" | {c['code']} {q['price']:.2f} {q['change_pct']:+.1f}%"
                print(f"\r  ⏱ [{scan_count}] {now.strftime('%H:%M:%S')} "
                      f"扫描{len(candidates)}只 无信号{latest_price}    ", end="", flush=True)

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n  ⛔ 手动停止")

    # 汇总
    if all_signals:
        print(f"\n{'='*70}")
        print(f"  📊 今日信号汇总 ({len(all_signals)}个)")
        print(f"{'='*70}")
        by_code = defaultdict(list)
        for s in all_signals:
            by_code[s['code']].append(s)
        for code, sigs in sorted(by_code.items(), key=lambda x: -len(x[1])):
            types = ', '.join(set(s['type'] for s in sigs))
            print(f"  {code} {sigs[0]['name']}: {len(sigs)}次 [{types}]")
    else:
        print(f"\n  📊 今日无信号")

    # 导出
    if all_signals:
        outfile = "realtime_signals.json"
        with open(outfile, "w", encoding="utf-8") as f:
            _json.dump(all_signals, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 信号已导出: {outfile}")


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="强势股盘中实时弱转强监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python realtime_monitor.py                     # 完整运行
  python realtime_monitor.py --scan              # 仅选股
  python realtime_monitor.py --codes 000066,300001  # 手动指定
  python realtime_monitor.py --angle 0.8         # 降低斜率阈值
  python realtime_monitor.py --dragon-days 10    # 龙虎榜10天
        """)
    parser.add_argument("--scan", action="store_true", help="仅选股, 不启动盘中监控")
    parser.add_argument("--codes", type=str, default="", help="手动指定股票代码, 逗号分隔")
    parser.add_argument("--dragon-days", type=int, default=15, help="龙虎榜回看天数 (默认15)")
    parser.add_argument("--angle", type=float, default=1.0, help="MA5斜率阈值 %%/天 (默认1.0, 约45度)")
    parser.add_argument("--interval", type=int, default=60, help="盘中轮询间隔秒数 (默认60)")
    parser.add_argument("--cooldown", type=int, default=30, help="信号冷却时间分钟 (默认30)")
    parser.add_argument("--kline-days", type=int, default=60, help="日K线回看天数 (默认60)")
    args = parser.parse_args()

    print("=" * 70)
    print("  🔍 强势股盘中实时弱转强监控")
    print("=" * 70)
    print(f"  条件: MA5>MA10>MA20 + MA5斜率>{args.angle}%/天(≈45°) + 近{args.dragon_days}日龙虎榜")
    print(f"  信号A: 分时突破VWAP (放量突破均价线)")
    print(f"  信号B: 平缓上升+VWAP向上 (趋势延续)")

    # 选股
    if args.codes:
        # 手动指定代码
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        candidates = []
        for code in codes:
            bars = load_kline_db(code, args.kline_days)
            if not bars or len(bars) < 25:
                print(f"  ⚠️ {code}: K线数据不足")
                continue
            ma5_list = calc_ma(bars, 5)
            ma10_list = calc_ma(bars, 10)
            ma20_list = calc_ma(bars, 20)
            idx = len(bars) - 1
            ma5, ma10, ma20 = ma5_list[idx], ma10_list[idx], ma20_list[idx]
            if ma5 is None or ma10 is None or ma20 is None:
                continue
            angle = calc_ma5_angle(ma5_list, idx, 5)
            last_bar = bars[idx]
            prev_close = bars[idx - 1]['close'] if idx > 0 else 0
            change_pct = (last_bar['close'] / prev_close - 1) * 100 if prev_close > 0 else 0
            candidates.append({
                'code': code, 'name': '', 'board': get_board_name(code),
                'limit_pct': get_limit_pct(code),
                'ma5': round(ma5, 2), 'ma10': round(ma10, 2), 'ma20': round(ma20, 2),
                'angle': round(angle, 2) if angle else 0,
                'last_close': last_bar['close'], 'last_date': last_bar['time'],
                'change_pct': round(change_pct, 2),
                'dragon_date': '(手动指定)', 'dragon_count': 0,
                'prev_close': prev_close,
            })
        print(f"\n  手动指定: {len(candidates)}只")
    else:
        candidates = screen_candidates(
            dragon_days=args.dragon_days,
            angle_threshold=args.angle,
            kline_days=args.kline_days,
        )

    if args.scan:
        print(f"\n  ✅ 选股完成 (--scan 模式, 不启动监控)")
        return

    # 盘中监控
    run_monitor(candidates, interval=args.interval, cooldown=args.cooldown)


if __name__ == "__main__":
    main()
