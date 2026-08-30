#!/usr/bin/env python3
"""龙虎榜游资D0策略 v3 — 15分钟盘中入场

在 v2 基础上新增15分钟K线分析，支持D0盘中买入（不等D1开盘）。

三种D0盘中入场模式:

  模式A — 冲板入场（激进）
    D0盘中，15m bar显示:
      ① 当前bar涨幅 >= 5%（有冲板动能）
      ② 当前bar的high距离涨停价 <= 3%
      ③ 当前bar收盘 > 前一根bar高点（突破）
      ④ 量能放大（当前量 > 前一根×1.2）
      ⑤ 入场bar时间 < 11:30（早盘冲板质量显著高于午后）
    → 确认后买入

  模式B — 首封回踩入场（稳健）
    D0盘中，15m bar显示:
      ① 曾触及涨停价（当日最高≥涨停价×0.998）
      ② 当前bar low从涨停价回踩 <= 5%
      ③ 回踩时量能萎缩（当前量 < 前bar量×0.9）
      ④ 收盘站上当日VWAP
      ⑤ D0量比 < 1.3（放量回踩多为出货，放弃）
      ⑥ 买入时日内涨幅 <= 11%（过滤20cm追涨）
    → 确认后买入

  模式C — 早盘确认入场（最稳）
    D0早盘（9:30~10:15），15m bar显示:
      ① 开盘后前2根15m bar全部阳线
      ② 累计涨幅 > 3%
      ③ 量能放大（第2根量 > 第1根×1.2）
    → 确认后买入

出场规则:
  止损: -12% | 追踪止损: -12% | 止盈: +20% | 持仓上限: 7天

入场过滤(所有模式):
  D1/降级入场: 高开幅度 <= --max-gap (默认5%, 高开过大追高失败率高)

数据源:
  日线: kline_1D_YYYY (前复权)
  15分钟: kline_15m_YYYY (表名按年分)

用法:
  # 对比三种入场模式 vs D1开盘
  python test_dragon_hot_v3.py --compare-modes

  # 指定模式回测
  python test_dragon_hot_v3.py --mode A
  python test_dragon_hot_v3.py --mode B
  python test_dragon_hot_v3.py --mode C

  # 全模式输出明细
  python test_dragon_hot_v3.py --mode A --all-trades
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# 临时评估: VWAP状态×信号假设收益 (仅 --compare-modes + vwap_debug 时填充)
VWAP_DEBUG = None  # type: Optional[Dict[str, List[Dict]]]

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
    """从数据库加载日线K线(前复权)"""
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


def fetch_kline_15m(code: str, start_date: str, end_date: str) -> List[Dict]:
    """从 kline_15m_YYYY 表加载15分钟K线

    表名按年分: kline_15m_2025, kline_15m_2026, ...
    跨年时合并多张表。

    Returns:
        按时间升序的15m bar列表, 每条: {time, open, high, low, close, volume}
    """
    pool = _get_cnstock_pool()
    # 计算涉及的年份
    try:
        y_start = int(start_date[:4])
        y_end = int(end_date[:4])
    except (ValueError, IndexError):
        print(f"  [15m] {code} 日期解析失败: {start_date}~{end_date}", file=sys.stderr)
        return []

    all_bars = []
    for year in range(y_start, y_end + 1):
        table = f"kline_15m_{year}"
        try:
            with pool.connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f'SELECT time, open, high, low, close, volume '
                    f'FROM "{table}" '
                    f'WHERE symbol = %s AND time >= %s AND time <= %s '
                    f'ORDER BY time',
                    (code, start_date + " 09:00:00", end_date + " 16:00:00")
                )
                rows = cur.fetchall()
                cur.close()
                if rows:
                    print(f"  [15m] {code} {table}: {len(rows)}条", file=sys.stderr)
                for r in rows:
                    t = r[0]
                    all_bars.append({
                        "time": t.strftime("%Y-%m-%d %H:%M:%S") if isinstance(t, datetime) else str(t),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                    })
        except Exception as e:
            print(f"  [15m] {code} {table} 查询失败: {e}", file=sys.stderr)
            continue

    if not all_bars:
        print(f"  [15m] {code} {start_date}~{end_date} 无数据", file=sys.stderr)
    all_bars.sort(key=lambda b: b["time"])
    return all_bars


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


def get_limit_price(prev_close: float, board_type: str) -> float:
    """计算涨停价"""
    pct = 0.10 if board_type == "main" else 0.20
    return round(prev_close * (1 + pct), 2)


# ================================================================
# D0 筛选条件（与v2一致）
# ================================================================

def check_d0_conditions(
    bars: List[Dict],
    d0_idx: int,
    net_amount: float,
    board_type: str,
    min_net_wan: float = 5000,
) -> Optional[Dict]:
    """检查D0是否满足4个条件"""
    if d0_idx < 5 or d0_idx >= len(bars):
        return None

    d0 = bars[d0_idx]
    prev_close = bars[d0_idx - 1]['close']

    if not is_limit_up(d0['close'], prev_close, board_type):
        return None

    pre5_bars = bars[max(0, d0_idx - 5):d0_idx]
    pre5_high = max(b['high'] for b in pre5_bars) if pre5_bars else 0
    if d0['close'] <= pre5_high:
        return None

    net_wan = net_amount / 10000
    if net_wan < min_net_wan:
        return None

    pre5_vols = [b['volume'] for b in pre5_bars if b['volume'] > 0]
    avg_pre_vol = sum(pre5_vols) / len(pre5_vols) if pre5_vols else 1
    vol_ratio = d0['volume'] / avg_pre_vol if avg_pre_vol > 0 else 999
    if vol_ratio >= 2.0:
        return None

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
# 技术指标
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
        d = closes[i] - closes[i - 1]
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


def analyze_tech(bars: List[Dict], idx: int) -> Dict:
    """技术分析评分"""
    if idx < 20 or idx >= len(bars):
        return {"tech_score": 0}
    closes = [bars[i]['close'] for i in range(max(0, idx - 60), idx + 1)]
    bar = bars[idx]
    prev_bar = bars[idx - 1] if idx > 0 else None
    ma5 = calc_ma(closes[-5:], 5)
    ma10 = calc_ma(closes[-10:], 10)
    ma20 = calc_ma(closes[-20:], 20)
    rsi14 = calc_rsi(closes, 14)
    change_pct = 0
    if prev_bar and prev_bar['close'] > 0:
        change_pct = (bar['close'] / prev_bar['close'] - 1) * 100
    score = 50
    if ma5 and ma10 and ma5 > ma10: score += 10
    if ma10 and ma20 and ma10 > ma20: score += 10
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20: score += 10
    if rsi14 and 40 <= rsi14 <= 60: score += 5
    if rsi14 and rsi14 > 70: score -= 10
    if rsi14 and rsi14 < 30: score += 10
    return {
        "ma5": round(ma5, 2) if ma5 else 0,
        "ma10": round(ma10, 2) if ma10 else 0,
        "ma20": round(ma20, 2) if ma20 else 0,
        "rsi14": round(rsi14, 1) if rsi14 else 0,
        "change_pct": round(change_pct, 2),
        "tech_score": max(0, min(100, score)),
    }


# ================================================================
# 15分钟入场信号检测
# ================================================================

def _split_15m_by_date(bars_15m: List[Dict]) -> Dict[str, List[Dict]]:
    """将15m bar按日期分组"""
    by_date = defaultdict(list)
    for b in bars_15m:
        date_str = b["time"][:10]
        by_date[date_str].append(b)
    return dict(by_date)


def _calc_vwap(day_bars: List[Dict]) -> float:
    """计算当日VWAP (成交量加权平均价)"""
    total_pv = 0.0
    total_vol = 0.0
    for b in day_bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        total_pv += typical * b["volume"]
        total_vol += b["volume"]
    return total_pv / total_vol if total_vol > 0 else 0


def analyze_vwap_strength(
    bars_15m_day: List[Dict],
    bar_idx: int,
    buy_price: float,
    prev_close: float,
    board_type: str,
) -> Optional[Dict]:
    """日内VWAP强弱状态分类（弱转强核心）

    强信号(保留15m入场):
      strong_up      强势沿VWAP: 近3根close全>VWAP 且 VWAP斜率>0.1(价格站上VWAP并拉动VWAP向上)
      strong_reclaim 回踩快速收复: 当前bar low刺破VWAP(1-2根内), close收回>VWAP, VWAP持平或向上
      strong_pierce  弱转强刺破: 前根close在VWAP下方, 当前close向上刺破站上VWAP,
                    贴近VWAP(dist<=3.5%)且VWAP持平或向上

    危险(回调风险, 放弃信号->降级D1):
      danger_far     现价离VWAP>5% 且未贴近涨停(距涨停>1.5%)——高位远离均线易回调

    震荡/走弱(放弃信号->降级D1):
      weak_below     close在VWAP下方(未收复)
      weak_slope     VWAP斜率<-0.1(均线走弱)
      oscillation    近3bar在VWAP上下反复穿越>=2次(震荡)

    中性:
      hold_above     价格在VWAP上方但无强势特征(未拉动均线, 无回踩无刺破)
    """
    if bar_idx < 2 or bar_idx >= len(bars_15m_day):
        return None

    # 计算到当前bar为止的累积VWAP
    bars_so_far = bars_15m_day[:bar_idx + 1]
    total_pv = 0.0
    total_vol = 0.0
    vwap_series = []
    for b in bars_so_far:
        typical = (b['high'] + b['low'] + b['close']) / 3
        total_pv += typical * b['volume']
        total_vol += b['volume']
        vwap_series.append(total_pv / total_vol if total_vol > 0 else 0)

    current_vwap = vwap_series[-1]
    if current_vwap <= 0:
        return None

    # VWAP斜率（最近3根bar的VWAP变化, 用%）
    if len(vwap_series) >= 3:
        vwap_slope = (vwap_series[-1] - vwap_series[-3]) / vwap_series[-3] * 100
    else:
        vwap_slope = 0.0

    bar = bars_15m_day[bar_idx]
    prev_bar = bars_15m_day[bar_idx - 1] if bar_idx > 0 else None
    price = bar['close']
    low = bar['low']

    # 收盘价离VWAP的距离(%), 涨停价距离(%)
    dist_vwap = (price - current_vwap) / current_vwap * 100
    limit_price = get_limit_price(prev_close, board_type)
    dist_to_limit = (limit_price - price) / limit_price * 100

    # 震荡计数: 最近3根bar在VWAP上下穿越次数
    crosses = 0
    if len(vwap_series) >= 3:
        for i in range(-3, 0):
            b = bars_15m_day[bar_idx + i]
            prev_v = vwap_series[bar_idx + i]
            if (b['close'] > prev_v) != (bars_15m_day[bar_idx + i + 1]['close'] > vwap_series[bar_idx + i + 1]):
                crosses += 1

    # 近3根close是否全部在VWAP上方
    recent_above_vwap = True
    if len(vwap_series) >= 3:
        for i in range(-3, 0):
            if bars_15m_day[bar_idx + i]['close'] < vwap_series[bar_idx + i]:
                recent_above_vwap = False
                break
    else:
        recent_above_vwap = price >= current_vwap

    prev_above_vwap = (prev_bar is not None and len(vwap_series) >= 2
                       and prev_bar['close'] >= vwap_series[-2])

    reasons = []

    # ---- 状态判定（优先级从危险到强） ----
    if dist_vwap > 5 and dist_to_limit > 1.5:
        state = "danger_far"
        reasons.append(f"远离VWAP({dist_vwap:.1f}%)未涨停")
    elif price < current_vwap:
        # 收盘在VWAP下方——未收复
        if vwap_slope < -0.1:
            state = "weak_slope"
            reasons.append(f"VWAP降{vwap_slope:.2f}%")
        elif crosses >= 2:
            state = "oscillation"
            reasons.append("VWAP震荡")
        else:
            state = "weak_below"
            reasons.append("收盘<VWAP")
    else:
        # 收盘在VWAP上方或贴合
        if crosses >= 2:
            state = "oscillation"
            reasons.append("VWAP震荡")
        elif low <= current_vwap:
            # 回踩快速收复: low刺破VWAP, close站上, VWAP持平或向上
            if vwap_slope >= -0.1:
                state = "strong_reclaim"
                reasons.append("回踩VWAP快速收复")
            else:
                state = "weak_slope"
                reasons.append(f"回收但VWAP降{vwap_slope:.2f}%")
        elif prev_bar is not None and not prev_above_vwap:
            # 向上刺破弱转强: 前根在VWAP下方, 当前站上(需贴近且VWAP不弱)
            if dist_vwap <= 3.5 and vwap_slope >= -0.1:
                state = "strong_pierce"
                reasons.append(f"向上刺破VWAP({dist_vwap:.1f}%)")
            else:
                state = "hold_above"
                reasons.append(f"刺破但距离{dist_vwap:.1f}%")
        elif recent_above_vwap and vwap_slope > 0.1:
            state = "strong_up"
            reasons.append("沿VWAP均线上行")
        else:
            state = "hold_above"
            reasons.append("VWAP上方走平")

    pass_states = {"strong_up", "strong_reclaim", "strong_pierce", "hold_above"}

    return {
        "state": state,
        "pass": state in pass_states,
        "vwap": round(current_vwap, 3),
        "vwap_slope": round(vwap_slope, 3),
        "dist_vwap": round(dist_vwap, 2),
        "dist_to_limit": round(dist_to_limit, 2),
        "crosses": crosses,
        "reasons": reasons,
    }


def detect_signal_A(
    bars_15m_day: List[Dict],
    prev_close: float,
    board_type: str,
) -> Optional[Dict]:
    """模式A — 冲板入场（追涨型）

    条件:
      ① 当前bar涨幅 >= 5%（有冲板动能）
      ② 当前bar的high距离涨停价 <= 2%（接近涨停）
      ③ 当前bar收盘 > 前一根bar高点（突破）
      ④ 量能放大（当前量 > 前一根×1.2）
      ⑤ 入场bar时间 < 11:30（早盘冲板信号质量显著高于午后）

    Returns:
        {bar_idx, bar_time, buy_price, intraday_pct, signal_type}
    """
    if len(bars_15m_day) < 2 or prev_close <= 0:
        return None

    limit_price = get_limit_price(prev_close, board_type)
    latest_entry = "11:30"

    for i in range(1, len(bars_15m_day)):
        bar = bars_15m_day[i]
        bar_time = bar["time"][11:16]

        if bar_time >= latest_entry:
            break

        intraday_pct = (bar["close"] / prev_close - 1) * 100
        high_pct = (bar["high"] / prev_close - 1) * 100
        dist_to_limit = (limit_price - bar["high"]) / limit_price * 100

        # 条件①: 涨幅 5~12%（太低没动能，太高追涨被套）
        if intraday_pct < 5.0 or intraday_pct > 12.0:
            continue

        # 条件②: high距离涨停 <= 5%（放宽，让更多接近涨停的股入选）
        if dist_to_limit > 5.0:
            continue

        # 条件③: 突破前bar高点
        if bar["close"] <= bars_15m_day[i - 1]["high"]:
            continue

        # 条件④: 量能不极度萎缩（放宽到30%）
        if bars_15m_day[i - 1]["volume"] > 0 and bar["volume"] < bars_15m_day[i - 1]["volume"] * 0.3:
            continue

        return {
            "bar_idx": i,
            "bar_time": bar["time"],
            "buy_price": bar["close"],
            "intraday_pct": round(intraday_pct, 2),
            "signal_type": "A_冲板",
            "dist_to_limit": round(dist_to_limit, 2),
        }

    return None


def detect_signal_B(
    bars_15m_day: List[Dict],
    prev_close: float,
    board_type: str,
    vol_ratio: Optional[float] = None,
) -> Optional[Dict]:
    """模式B — 首封回踩入场（稳健型）

    条件:
      ① 曾触及涨停价（当日high >= 涨停价×0.998）
      ② 当前bar从涨停价回踩（low距涨停 <= 5%）
      ③ 回踩时量能萎缩（当前量 < 前bar量×0.9）
      ④ 当前收盘站上当日VWAP
      ⑤ D0量比 < 1.3（放量回踩多为出货，放弃）
      ⑥ 买入时日内涨幅 <= 11%（过滤20cm追涨，仅保留主波动票）

    Returns:
        {bar_idx, bar_time, buy_price, intraday_pct, signal_type, pullback_pct}
    """
    if len(bars_15m_day) < 3 or prev_close <= 0:
        return None

    limit_price = get_limit_price(prev_close, board_type)
    touch_threshold = limit_price * 0.998

    # 找到第一次触及涨停的bar
    first_touch_idx = None
    for i, bar in enumerate(bars_15m_day):
        if bar["high"] >= touch_threshold:
            first_touch_idx = i
            break

    if first_touch_idx is None:
        return None

    latest_entry = "14:30"

    for i in range(first_touch_idx + 1, len(bars_15m_day)):
        bar = bars_15m_day[i]
        bar_time = bar["time"][11:16]

        if bar_time >= latest_entry:
            break

        # 用low价算回撤（盘中实际最大回撤）
        pullback_pct = (limit_price - bar["low"]) / limit_price * 100

        # 条件②: 回撤不超过5%（放宽，覆盖更多场景）
        if pullback_pct > 5.0:
            continue

        # 条件③: 量能不明显放大（允许持平，拒绝放量出货）
        if bar["volume"] >= bars_15m_day[i - 1]["volume"] * 1.3:
            continue

        # 条件④: 收盘站上当日VWAP
        vwap = _calc_vwap(bars_15m_day[:i + 1])
        if bar["close"] < vwap:
            continue

        intraday_pct = (bar["close"] / prev_close - 1) * 100

        # 条件⑤: D0量比 < 1.3（放量回踩多为出货，放弃）
        if vol_ratio is not None and vol_ratio >= 1.3:
            return None

        # 条件⑥: 买入时日内涨幅 <= 11%（过滤20cm追涨）
        if intraday_pct > 11.0:
            continue

        return {
            "bar_idx": i,
            "bar_time": bar["time"],
            "buy_price": bar["close"],
            "intraday_pct": round(intraday_pct, 2),
            "signal_type": "B_回踩",
            "pullback_pct": round(pullback_pct, 2),
            "vwap": round(vwap, 3),
        }

    return None


def detect_signal_C(
    bars_15m_day: List[Dict],
    prev_close: float,
) -> Optional[Dict]:
    """模式C — 早盘确认入场（最稳）

    条件:
      ① 开盘后前2根15m bar全部阳线
      ② 累计涨幅 > 3%
      ③ 量能放大（第2根量 > 第1根×1.2）

    Returns:
        {bar_idx, bar_time, buy_price, intraday_pct, signal_type}
    """
    if len(bars_15m_day) < 2 or prev_close <= 0:
        return None

    b0 = bars_15m_day[0]
    b1 = bars_15m_day[1]

    # 确认是早盘bar（前2根应该在9:30~10:00）
    t0 = b0["time"][11:16]
    if t0 > "10:15":
        return None

    # 条件①: 全部阳线
    if not (b0["close"] > b0["open"] and b1["close"] > b1["open"]):
        return None

    # 条件②: 累计涨幅 > 3%
    intraday_pct = (b1["close"] / prev_close - 1) * 100
    if intraday_pct < 3.0:
        return None

    # 条件③: 量能放大
    if b0["volume"] > 0 and b1["volume"] <= b0["volume"] * 1.2:
        return None

    return {
        "bar_idx": 1,
        "bar_time": b1["time"],
        "buy_price": b1["close"],
        "intraday_pct": round(intraday_pct, 2),
        "signal_type": "C_早盘",
        "vol_ratio": round(b1["volume"] / b0["volume"], 2) if b0["volume"] > 0 else 0,
    }


# ================================================================
# D1入场（v2原有）
# ================================================================

def get_d1_entry(bars: List[Dict], d0_idx: int, max_gap: float = 5.0) -> Optional[Dict]:
    """D1入场: 只做高开(>0%), 高开幅度≤max_gap%"""
    d1_idx = d0_idx + 1
    if d1_idx >= len(bars):
        return None
    d0_close = bars[d0_idx]['close']
    d1 = bars[d1_idx]
    if d1['open'] <= 0:
        return None
    d1_gap = (d1['open'] / d0_close - 1) * 100
    if d1_gap <= 0:
        return None
    if max_gap > 0 and d1_gap > max_gap:
        return None
    return {
        "buy_price": d1['open'],
        "buy_idx": d1_idx,
        "buy_time": d1['time'],
        "entry_type": "D1高开",
        "d1_gap": round(d1_gap, 2),
    }


# ================================================================
# 回测引擎
# ================================================================

def run_backtest(
    bars: List[Dict],
    buy_idx: int,
    buy_price: float,
    hold_days: int = 7,
    stop_loss: float = -12.0,
    trailing_stop: float = -12.0,
    take_profit: float = 20.0,
) -> Optional[Dict]:
    """回测: 从buy_idx开始持有"""
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

        if b['high'] >= buy_price * (1 + take_profit / 100):
            exit_p = buy_price * (1 + take_profit / 100)
            exit_d = d
            exit_reason = "止盈"
            break
        if d > 0 and b['low'] <= peak * (1 + trailing_stop / 100):
            exit_p = peak * (1 + trailing_stop / 100)
            exit_d = d
            exit_reason = "追踪止损"
            break
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
    total_days = sum(t['exit_day'] for t in trades)
    rpd = sum(t['return_pct'] for t in trades) / total_days if total_days > 0 else 0
    return {
        'total': total, 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(win_rate, 1),
        'avg_return': round(avg_ret, 2),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'plr': round(plr, 2),
        'avg_hold': round(total_days / total, 1),
        'max_return': round(max(t['return_pct'] for t in trades), 2),
        'min_return': round(min(t['return_pct'] for t in trades), 2),
        'exit_reasons': dict(exit_reasons),
        'rpd': round(rpd, 4),
    }


def print_stats(stats: Dict, label: str):
    if not stats:
        print(f"  {label}: 无数据")
        return
    print(f"  {label}: {stats['total']}笔")
    print(f"    胜率: {stats['win_rate']}% | 盈亏比: {stats['plr']}")
    print(f"    均收益: {stats['avg_return']:+.2f}% | 均盈利: {stats['avg_win']:+.2f}% | 均亏损: {stats['avg_loss']:+.2f}%")
    print(f"    最大: {stats['max_return']:+.2f}% | 最小: {stats['min_return']:+.2f}%")
    print(f"    均持仓: {stats['avg_hold']:.1f}天 | 日均: {stats['rpd']:+.4f}%")
    if stats.get('exit_reasons'):
        print(f"    出场: {' | '.join(f'{k}:{v}' for k, v in stats['exit_reasons'].items())}")


# ================================================================
# 核心策略
# ================================================================

def strategy_v3(
    dragon_data: List[Dict],
    kline_cache: Dict[str, List[Dict]],
    mode: str = "D1",
    window_days: int = 20,
    min_net_wan: float = 5000,
    hold_days: int = 7,
    stop_loss: float = -12.0,
    trailing_stop: float = -12.0,
    take_profit: float = 20.0,
    max_gap: float = 5.0,
    show_detail: bool = False,
    vwap_debug: bool = False,
) -> List[Dict]:
    """v3策略: 支持D0盘中15m入场 + D1开盘入场

    Args:
        mode: "D1" | "A" | "B" | "C"
            D1 = v2原有模式（D1高开买入）
            A = 冲板入场
            B = 首封回踩入场
            C = 早盘确认入场
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

    # 按股票聚合龙虎榜
    by_code: Dict[str, List[Dict]] = defaultdict(list)
    for row in dragon_data:
        code = row.get('stock_code', '')
        if code:
            by_code[code].append(row)

    trades = []
    _15m_cache: Dict[str, List[Dict]] = {}  # code -> 15m bars

    for code, rows in by_code.items():
        rows.sort(key=lambda x: x.get('trade_date', ''))

        window_rows = [r for r in rows if r.get('trade_date', '') >= cutoff]
        if not window_rows:
            continue

        d0_row = window_rows[0]
        d0_date = d0_row.get('trade_date', '')

        # 加载日线
        if code not in kline_cache:
            bars = fetch_kline_db(code, 300)
            if bars:
                kline_cache[code] = bars
        bars = kline_cache.get(code)
        if not bars:
            continue

        # 找D0在日线中的索引
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
        prev_close = bars[d0_idx - 1]['close'] if d0_idx > 0 else 0

        # === D1模式 ===
        if mode == "D1":
            entry = get_d1_entry(bars, d0_idx, max_gap)
            if entry is None:
                continue
            result = run_backtest(bars, entry['buy_idx'], entry['buy_price'],
                                  hold_days, stop_loss, trailing_stop, take_profit)
            if not result:
                continue
            trades.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'board': get_board_name(code),
                'signal_date': d0_date,
                'score': tech.get('tech_score', 50),
                'd0_features': d0_features,
                'tech': tech,
                'entry_type': entry['entry_type'],
                'entry_price': round(entry['buy_price'], 3),
                'entry_time': entry['buy_time'],
                'd1_gap': entry['d1_gap'],
                **result,
            })
            if show_detail:
                print(f"    {code} {d0_date} D0涨{d0_features['d0_change']:.0f}% "
                      f"-> {entry['entry_type']} gap{entry['d1_gap']:+.1f}% "
                      f"-> {result['exit_reason']} {result['return_pct']:+.1f}%")
            continue

        # === D0盘中模式 (A/B/C) ===
        # 加载15m数据
        if code not in _15m_cache:
            # 加载D0当天及前后几天的15m数据
            d0_dt = datetime.strptime(d0_date, "%Y-%m-%d")
            start_15m = (d0_dt - timedelta(days=2)).strftime("%Y-%m-%d")
            end_15m = (d0_dt + timedelta(days=2)).strftime("%Y-%m-%d")
            _15m_cache[code] = fetch_kline_15m(code, start_15m, end_15m)

        bars_15m_all = _15m_cache.get(code, [])
        if not bars_15m_all:
            # 15m数据不可用，降级到D1
            entry = get_d1_entry(bars, d0_idx, max_gap)
            if entry is None:
                continue
            result = run_backtest(bars, entry['buy_idx'], entry['buy_price'],
                                  hold_days, stop_loss, trailing_stop, take_profit)
            if not result:
                continue
            trades.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'board': get_board_name(code),
                'signal_date': d0_date,
                'score': tech.get('tech_score', 50),
                'd0_features': d0_features,
                'tech': tech,
                'entry_type': 'D1降级',
                'entry_price': round(entry['buy_price'], 3),
                'entry_time': entry['buy_time'],
                'd1_gap': entry['d1_gap'],
                'fallback': True,
                **result,
            })
            continue

        # 按日期分组15m bar
        bars_15m_by_date = _split_15m_by_date(bars_15m_all)
        bars_15m_day = bars_15m_by_date.get(d0_date, [])

        if not bars_15m_day:
            print(f"  [信号] {code} {d0_date} 无15m bar (15m_by_date keys: {list(bars_15m_by_date.keys())[:3]})", file=sys.stderr)
            continue

        print(f"  [信号] {code} {d0_date} 15m有{len(bars_15m_day)}根bar, 检测{mode}信号...", file=sys.stderr)

        # 检测信号
        signal = None
        if mode == "A":
            signal = detect_signal_A(bars_15m_day, prev_close, board_type)
        elif mode == "B":
            signal = detect_signal_B(bars_15m_day, prev_close, board_type,
                                     vol_ratio=d0_features.get('vol_ratio'))
        elif mode == "C":
            signal = detect_signal_C(bars_15m_day, prev_close)
        elif mode == "ALL":
            # 优先级: C > B > A（越稳越优先）
            signal = (detect_signal_C(bars_15m_day, prev_close)
                      or detect_signal_B(bars_15m_day, prev_close, board_type,
                                         vol_ratio=d0_features.get('vol_ratio'))
                      or detect_signal_A(bars_15m_day, prev_close, board_type))

        if signal is None:
            if bars_15m_day:
                print(f"  [信号] {code} {d0_date} 15m有{len(bars_15m_day)}根bar但{mode}信号未触发", file=sys.stderr)
            # 没有15m信号，降级到D1
            entry = get_d1_entry(bars, d0_idx, max_gap)
            if entry is None:
                continue
            result = run_backtest(bars, entry['buy_idx'], entry['buy_price'],
                                  hold_days, stop_loss, trailing_stop, take_profit)
            if not result:
                continue
            trades.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'board': get_board_name(code),
                'signal_date': d0_date,
                'score': tech.get('tech_score', 50),
                'd0_features': d0_features,
                'tech': tech,
                'entry_type': 'D1降级',
                'entry_price': round(entry['buy_price'], 3),
                'entry_time': entry['buy_time'],
                'd1_gap': entry['d1_gap'],
                'fallback': True,
                **result,
            })
            continue

        # === VWAP日内均线过滤 ===
        vwap_check = analyze_vwap_strength(
            bars_15m_day, signal['bar_idx'], signal['buy_price'], prev_close, board_type)
        if vwap_debug and vwap_check is not None and VWAP_DEBUG is not None:
            hr = run_backtest(bars, d0_idx, signal['buy_price'],
                              hold_days, stop_loss, trailing_stop, take_profit)
            VWAP_DEBUG.setdefault(mode, []).append({
                'code': code, 'signal_date': d0_date, 'signal_type': signal['signal_type'],
                'state': vwap_check['state'],
                'accepted': vwap_check['pass'],
                'vwap_slope': vwap_check['vwap_slope'],
                'dist_vwap': vwap_check['dist_vwap'],
                'hypothetical_return': hr['return_pct'] if hr else None,
            })
        if vwap_check is not None and not vwap_check['pass']:
            if show_detail:
                print(f"    {code} {d0_date} {signal['signal_type']} VWAP[{vwap_check['state']}]: {', '.join(vwap_check['reasons'])}")
            # VWAP不健康，降级到D1
            entry = get_d1_entry(bars, d0_idx, max_gap)
            if entry is None:
                continue
            result = run_backtest(bars, entry['buy_idx'], entry['buy_price'],
                                  hold_days, stop_loss, trailing_stop, take_profit)
            if not result:
                continue
            trades.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'board': get_board_name(code),
                'signal_date': d0_date,
                'score': tech.get('tech_score', 50),
                'd0_features': d0_features,
                'tech': tech,
                'entry_type': 'D1降级',
                'entry_price': round(entry['buy_price'], 3),
                'entry_time': entry['buy_time'],
                'd1_gap': entry['d1_gap'],
                'fallback': True,
                'vwap_filtered': True,
                **result,
            })
            continue

        # 有15m信号且VWAP健康 → 用信号价格作为买入价
        buy_price = signal['buy_price']
        signal_time = signal['bar_time']

        # 找买入日在日线中的索引（D0当天）
        buy_day_idx = d0_idx  # D0盘中买入，持有从D0开始计算
        # 但回测需要从D0的下一天开始（因为D0当天已经买入，持有的是D0之后的K线）
        # 实际上买入在D0盘中，所以从D0当天的日线收盘就可以开始追踪

        result = run_backtest(bars, d0_idx, buy_price,
                              hold_days, stop_loss, trailing_stop, take_profit)
        if not result:
            continue

        trades.append({
            'code': code,
            'name': d0_row.get('stock_name', ''),
            'board': get_board_name(code),
            'signal_date': d0_date,
            'score': tech.get('tech_score', 50),
            'd0_features': d0_features,
            'tech': tech,
            'entry_type': signal['signal_type'],
            'entry_price': round(buy_price, 3),
            'entry_time': signal_time,
            'intraday_pct': signal['intraday_pct'],
            'vwap_info': vwap_check,
            'signal_detail': {k: v for k, v in signal.items()
                              if k not in ('bar_idx', 'buy_price', 'bar_time')},
            **result,
        })

        if show_detail:
            d1_entry = get_d1_entry(bars, d0_idx, max_gap)
            d1_price = d1_entry['buy_price'] if d1_entry else buy_price
            savings = (d1_price - buy_price) / d1_price * 100 if d1_price > 0 else 0
            print(f"    {code} {d0_date} {signal['signal_type']} "
                  f"买{buy_price:.2f}@{signal_time[11:16]} "
                  f"(日涨{signal['intraday_pct']:.1f}% 比D1省{savings:+.1f}%) "
                  f"-> {result['exit_reason']} {result['return_pct']:+.1f}%")

    return trades


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="龙虎榜游资D0策略 v3 — 15m盘中入场")
    parser.add_argument("--mode", type=str, default="D1",
                        choices=["D1", "A", "B", "C", "ALL"],
                        help="入场模式: D1=D1开盘, A=冲板, B=回踩, C=早盘, ALL=自动选最优")
    parser.add_argument("--compare-modes", action="store_true",
                        help="对比所有入场模式")
    parser.add_argument("--days", type=int, default=20, help="D0搜索窗口(交易日)")
    parser.add_argument("--hold-days", type=int, default=7, help="持仓天数")
    parser.add_argument("--stop-loss", type=float, default=-12.0, help="止损%%")
    parser.add_argument("--trailing-stop", type=float, default=-12.0, help="追踪止损%%")
    parser.add_argument("--take-profit", type=float, default=20.0, help="止盈%%")
    parser.add_argument("--min-net", type=float, default=5000, help="最小净买入额(万)")
    parser.add_argument("--max-gap", type=float, default=5.0, help="最大D1高开幅度%%(超过则放弃)")
    parser.add_argument("--debug-vwap", action="store_true", help="输出VWAP状态×信号假设收益判别力表")
    parser.add_argument("--all-trades", action="store_true", help="输出交易明细")
    parser.add_argument("--detail", action="store_true", help="输出详细匹配信息")
    parser.add_argument("--export", type=str, default="", help="导出JSON文件路径")
    args = parser.parse_args()

    print("=" * 80)
    print("龙虎榜游资D0策略 v3 — 15分钟盘中入场")
    print("D0: 涨停 + 突破前高 + 净买入>5000万 + 量比<2x")
    print(f"入场模式: {args.mode} | 止损{args.stop_loss}% | 追踪{args.trailing_stop}% | 止盈{args.take_profit}%")
    print("=" * 80)

    # 加载数据
    print(f"\n📊 加载龙虎榜数据 (窗口={args.days}天)...")
    dragon_data = fetch_dragon_tiger_from_db()
    print(f"  龙虎榜: {len(dragon_data)}条")

    if not dragon_data:
        print("\n❌ 无数据")
        return

    kline_cache = {}

    # === 对比模式 ===
    if args.compare_modes:
        print(f"\n{'=' * 80}")
        print(f"📊 入场模式对比")
        print(f"   止损{args.stop_loss}% | 追踪{args.trailing_stop}% | 止盈{args.take_profit}% | 持仓{args.hold_days}天")
        print(f"{'=' * 80}\n")

        mode_names = {
            "D1": "D1高开买入 (v2原策略)",
            "A": "A_冲板 (7~9%+量能递增+突破)",
            "B": "B_回踩 (首封涨停后回踩≤3%)",
            "C": "C_早盘 (前3bar全阳+涨>5%)",
        }

        all_results = {}
        global VWAP_DEBUG
        VWAP_DEBUG = {}
        for m in ["D1", "A", "B", "C"]:
            kline_cache_copy = dict(kline_cache)
            trades = strategy_v3(
                dragon_data, kline_cache_copy, mode=m,
                window_days=args.days, min_net_wan=args.min_net,
                hold_days=args.hold_days, stop_loss=args.stop_loss,
                trailing_stop=args.trailing_stop, take_profit=args.take_profit,
                max_gap=args.max_gap,
                show_detail=False,
                vwap_debug=args.debug_vwap,
            )
            kline_cache.update(kline_cache_copy)
            all_results[m] = trades

            if trades:
                stats = calc_stats(trades)
                print(f"  [{m}] {mode_names.get(m, m)}")
                print_stats(stats, f"    ")
                print()
            else:
                print(f"  [{m}] {mode_names.get(m, m)}")
                print(f"    无交易\n")

        # 汇总对比表
        print(f"\n{'=' * 80}")
        print(f"📊 汇总对比")
        print(f"{'=' * 80}")
        print(f"  {'模式':<25} {'笔数':>4} {'胜率':>6} {'均收益':>8} {'日均':>8} {'最大':>8} {'最小':>8}")
        print(f"  {'-' * 75}")
        for m in ["D1", "A", "B", "C"]:
            trades = all_results.get(m, [])
            if trades:
                s = calc_stats(trades)
                print(f"  {mode_names.get(m, m):<25} {s['total']:>4} {s['win_rate']:>5.1f}% "
                      f"{s['avg_return']:>+7.2f}% {s['rpd']:>+7.4f}% "
                      f"{s['max_return']:>+7.2f}% {s['min_return']:>+7.2f}%")
            else:
                print(f"  {mode_names.get(m, m):<25}    0")

        # 导出
        if args.export:
            export_data = {}
            for m, trades in all_results.items():
                export_data[m] = [{k: v for k, v in t.items() if k != 'daily_returns'} for t in trades]
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            print(f"\n💾 导出: {args.export}")
        else:
            outfile = "test_dragon_hot_v3_compare.json"
            export_data = {}
            for m, trades in all_results.items():
                export_data[m] = [{k: v for k, v in t.items() if k != 'daily_returns'} for t in trades]
            if VWAP_DEBUG:
                export_data['_vwap_debug'] = VWAP_DEBUG
            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            print(f"\n💾 {outfile}")

        # VWAP状态×假设收益判别力表
        if VWAP_DEBUG:
            print(f"\n{'=' * 80}")
            print(f"📐 VWAP状态判别力 (信号假设收益, 是否被VWAP拒绝)")
            print(f"{'=' * 80}")
            for m in ["A", "B", "C"]:
                rows = [r for r in VWAP_DEBUG.get(m, []) if r.get('hypothetical_return') is not None]
                if not rows:
                    continue
                print(f"  [{m}]")
                states = sorted(set(r['state'] for r in rows))
                for st in states:
                    grp = [r for r in rows if r['state'] == st]
                    acc = [r for r in grp if r['accepted']]
                    rej = [r for r in grp if not r['accepted']]
                    def line(tag, g):
                        if not g:
                            print(f"    {st:<16} {tag}: -")
                            return
                        w = sum(1 for r in g if r['hypothetical_return'] > 0)
                        print(f"    {st:<16} {tag}: {len(g):>3}笔 WR{w/len(g)*100:>4.0f}% "
                              f"avg{sum(r['hypothetical_return'] for r in g)/len(g):+.2f}%")
                    line("保留", acc)
                    line("拒绝", rej)
            print()

        return

    # === 单模式 ===
    print(f"\n🔄 运行策略 (模式={args.mode})...")
    t0 = time.time()
    trades = strategy_v3(
        dragon_data, kline_cache, mode=args.mode,
        window_days=args.days, min_net_wan=args.min_net,
        hold_days=args.hold_days, stop_loss=args.stop_loss,
        trailing_stop=args.trailing_stop, take_profit=args.take_profit,
        max_gap=args.max_gap,
        show_detail=args.detail,
    )
    elapsed = time.time() - t0

    print(f"\n{'=' * 80}")
    print(f"📊 回测结果 (模式={args.mode}, 耗时{elapsed:.1f}秒)")
    print(f"{'=' * 80}")

    stats = calc_stats(trades)
    print_stats(stats, f"v3-{args.mode}")

    if trades:
        total_ret = sum(t['return_pct'] for t in trades)
        total_days = sum(t['exit_day'] for t in trades)
        rpd = total_ret / total_days if total_days > 0 else 0
        print(f"\n    单位时间: 日均{rpd:+.3f}% | 年化{rpd * 250:+.1f}%")

        # 入场类型统计
        entry_types = defaultdict(list)
        for t in trades:
            entry_types[t.get('entry_type', '未知')].append(t)
        print(f"\n  入场类型:")
        for etype, seg in sorted(entry_types.items()):
            s = calc_stats(seg)
            seg_rpd = s.get('rpd', 0)
            fallback = " (降级)" if any(t.get('fallback') for t in seg) else ""
            print(f"    {etype}{fallback}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 日均{seg_rpd:>+.4f}%")

        # 出场原因
        print(f"\n  出场原因:")
        for reason in ['止盈', '追踪止损', '止损', '持仓到期']:
            seg = [t for t in trades if t.get('exit_reason') == reason]
            if seg:
                s = calc_stats(seg)
                print(f"    {reason:>6}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                      f"均收益{s['avg_return']:>+6.2f}% 均持仓{s['avg_hold']:.1f}天")

        # 月度统计
        print(f"\n  月度统计:")
        monthly = defaultdict(list)
        for t in trades:
            monthly[t['signal_date'][:7]].append(t)
        for month in sorted(monthly.keys()):
            seg = monthly[month]
            s = calc_stats(seg)
            total_r = sum(t['return_pct'] for t in seg)
            print(f"    {month}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 总收益{total_r:>+8.2f}%")

    # 交易明细
    if args.all_trades and trades:
        print(f"\n{'=' * 80}")
        print(f"📋 交易明细 ({len(trades)}笔)")
        print(f"{'=' * 80}")
        for t in sorted(trades, key=lambda x: x.get('entry_time', x.get('signal_date', ''))):
            emoji = '✅' if t['return_pct'] > 0 else '❌'
            d0f = t.get('d0_features', {})
            entry_info = f"{t.get('entry_type', '')} {t['entry_price']:>7.2f}"
            if 'intraday_pct' in t:
                entry_info += f" @{t.get('entry_time', '')[11:16]}"
            elif 'd1_gap' in t:
                entry_info += f" gap{t['d1_gap']:+.1f}%"
            print(f"  {emoji} {t['code']:>8} {t['board']:>6} "
                  f"{t['signal_date']} D0涨{d0f.get('d0_change', 0):.0f}% "
                  f"-> {entry_info} "
                  f"-> {t['exit_reason'][:6]} 持{t['exit_day']}天 "
                  f"收益{t['return_pct']:>+6.2f}%")

    # 导出
    if trades:
        outfile = args.export or "test_dragon_hot_v3_result.json"
        export = [{k: v for k, v in t.items() if k != 'daily_returns'} for t in trades]
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {outfile} ({len(trades)}笔)")


if __name__ == "__main__":
    main()
