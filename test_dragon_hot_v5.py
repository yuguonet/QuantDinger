#!/usr/bin/env python3
"""龙虎榜游资D0策略 v5 — 去掉涨停+净买入，保留突破前高+量比

基于龙虎榜数据筛选D0标的，通过1分钟K线VWAP状态分析实现盘中精准入场。
默认模式(纯VWAP时间入场): 全天逐1m bar扫描，第一根"VWAP健康"即入场。
对照模式(--pattern): 使用旧统一信号形态(早盘强势/回踩企稳/冲板动能)+VWAP过滤。

===========================
入场规则
===========================

一、D0候选门槛 (check_d0_conditions)
  v5: 去掉涨停、净买入、量比条件，只保留:
    1) D-1突破前高: D-1收盘价 > D-1之前5日最高价

  d0_mode 变体 (--d0-mode):
    none          默认: 不设前高条件，只满足上述3条即可
    breakout      收盘突破前5日最高价
    below10       低位涨停: 收盘价×1.1 < 前5日最高价(距前高>10%空间)
    ma10_pullback D0盘中回踩10日线不破后涨停(low触及MA10±2%且未有效跌破)

二、VWAP日内均线状态分析 (analyze_vwap_strength)
  基于1分钟K线计算日内累积VWAP，判断弱转强状态:

  健康状态(可入场):
    strong_up      强势沿VWAP: 近3根close全>VWAP 且 VWAP斜率>0.1%
    strong_reclaim 回踩快速收复: low刺破VWAP(1-2根内), close收回>VWAP, VWAP持平或向上
    strong_pierce  弱转强刺破: 前根close在VWAP下方, 当前close向上刺破站上VWAP,
                   贴近VWAP(距离<=3.5%)且VWAP持平或向上
    hold_above     价格在VWAP上方但无强势特征(上方走平)

  不健康状态(不入场，继续扫描或全天无→降级D1):
    danger_far     现价离VWAP>5%且未贴近涨停(距涨停>1.5%)——高位远离均线易回调
    oscillation    近3根bar在VWAP上下反复穿越>=2次(震荡)
    weak_slope     VWAP斜率<-0.1%(均线走弱)
    weak_below     close在VWAP下方(未收复)

  附加过滤规则:
    - 距VWAP > 2%: hold_above 降级为不健康(胜率从76%骤降到52%)
    - 距VWAP > 3%: 所有状态均拒绝
    - VWAP斜率 1.2~1.7%: 斜率陷阱区，胜率仅40%，全部拒绝

三、纯VWAP时间入场 (默认模式, vwap_time=True)
  全天逐1m bar扫描，连续confirm根"VWAP健康"bar即入场。
  入场附加约束:
    - max_entry_pct (默认8%): 当日涨幅上限
    - max_dist_vwap (默认2%): 价格距VWAP上限
    - confirm (默认1): 连续N根健康bar才入场
    - 拉升效率: (D0累积涨跌幅/D0累积量) / (D-1同时间涨跌幅/D-1累积量) > 1.2

  --skip-su: 强排除 strong_up 状态的实验开关，实测延迟买入更贵(120d仅+1086)，默认关。

四、统一信号形态 (对照模式, --pattern)
  在当日1m扫描中按优先级取最早命中的盘中形态:
    1) 早盘强势: 开盘前15根1m bar全阳 + 累计涨幅>3% + 第15根量>第1根×1.2
    2) 回踩企稳: 曾触及涨停(high>=涨停价×0.998) + 回踩<=5% + 量不放大
                 + 收盘站上当日VWAP + D0量比<1.3 + 日内涨幅<=11% + 时间<14:45
    3) 冲板动能: bar涨5~12% + high距涨停<=5% + 突破前bar高点
                 + 量不极度萎缩 + 时间<11:30
  盘中信号经VWAP状态过滤后入场。


===========================
出场规则
===========================
  D0未涨停: D1开盘出场（快速止损）
  止损:       -12%
  追踪止损:   -10%
  止盈:       +25%
  持仓上限:   10天

  出场优先级: D0未涨停 > 止盈 > 追踪止损 > 止损 > 持仓到期

===========================
数据源
===========================
  龙虎榜: cnd_dragon_tiger_list (PostgreSQL)
  日线:   kline_1D_YYYY (前复权, PostgreSQL)
  1分钟:  kline_1m_YYYY (表名按年分, bar时间为区间结束点, 首根09:31)
  实时快照: realtime_snapshot_YYYY (--today模式, 转为增量1m bar)

===========================
用法
===========================
  # 基础回测 (20天窗口, 持有7天)
  python test_dragon_hot_v5.py --days 20 --hold-days 7

  # 长周期回测 + 详细输出
  python test_dragon_hot_v5.py --days 120 --debug-vwap --all-trades

  # 对照: 旧统一信号形态
  python test_dragon_hot_v5.py --days 120 --pattern

  # 对照: 排除strong_up状态
  python test_dragon_hot_v5.py --days 120 --skip-su

  # 连续确认扫描 (N={1,2,3,5,8,10}对比)
  python test_dragon_hot_v5.py --days 120 --confirm-sweep

  # 盘中实时扫描模式 (读取今日实时快照)
  python test_dragon_hot_v5.py --today

  # 自定义出场参数
  python test_dragon_hot_v5.py --days 20 --stop-loss -10 --trailing-stop -8 --take-profit 20

  # 导出结果到指定文件
  python test_dragon_hot_v5.py --days 120 --export result.json

===========================
关键命令行参数
===========================
  --days N          D0搜索窗口，交易日 (默认20)
  --hold-days N     持仓天数上限 (默认10)
  --stop-loss N     止损百分比 (默认-12)
  --trailing-stop N 追踪止损百分比 (默认-10)
  --take-profit N   止盈百分比 (默认25, <=0不设止盈)
  --min-net N       最小龙虎榜净买入额，万元 (默认3000)
  --d0-mode MODE    D0门槛变体: none|breakout|below10|ma10_pullback
  --max-gap N       D1入场最大高开幅度% (默认5, 仅用于show_detail对比)
  --confirm N       连续N根VWAP健康bar才入场 (默认1)
  --max-entry-pct N 入场时当日涨幅上限% (默认8, 0=禁用)
  --max-dist-vwap N 入场时距VWAP上限% (默认2, 0=禁用)
  --pattern         使用旧统一信号形态替代纯VWAP入场
  --skip-su         实验: strong_up不入场 (默认关)
  --confirm-sweep   连续确认N={1,2,3,5,8,10}扫描对比
  --debug-vwap      输出VWAP状态×信号假设收益判别力表
  --all-trades      输出交易明细
  --detail          输出详细匹配信息
  --export PATH     导出JSON文件路径
  --today           盘中实时扫描模式
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# 临时评估: VWAP状态×信号假设收益 (仅 --debug-vwap 时填充)
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


def fetch_kline_1m(code: str, start_date: str, end_date: str) -> List[Dict]:
    """从 kline_1m_YYYY 表加载1分钟K线

    表名按年分: kline_1m_2025, kline_1m_2026, ...
    跨年时合并多张表。

    Returns:
        按时间升序的1m bar列表, 每条: {time, open, high, low, close, volume}
    """
    pool = _get_cnstock_pool()
    # 计算涉及的年份
    try:
        y_start = int(start_date[:4])
        y_end = int(end_date[:4])
    except (ValueError, IndexError):
        print(f"  [1m] {code} 日期解析失败: {start_date}~{end_date}", file=sys.stderr)
        return []

    all_bars = []
    for year in range(y_start, y_end + 1):
        table = f"kline_1m_{year}"
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
                    print(f"  [1m] {code} {table}: {len(rows)}条", file=sys.stderr)
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
            print(f"  [1m] {code} {table} 查询失败: {e}", file=sys.stderr)
            continue

    if not all_bars:
        print(f"  [1m] {code} {start_date}~{end_date} 无数据", file=sys.stderr)
    all_bars.sort(key=lambda b: b["time"])
    return all_bars


def fetch_realtime_snapshot(code: str, date: str) -> List[Dict]:
    """从 realtime_snapshot_YYYY 表加载实时快照并转为1m bar格式

    snapshot表字段: symbol, time, "last", open, high, low, "previousClose", volume
    其中 open/high/low 是日内累计值，volume 是累计成交量（股）。
    需要转换为增量1m bar: open=上一根last, high/low=区间极点, volume=增量。
    """
    pool = _get_cnstock_pool()
    table = f"realtime_snapshot_{date[:4]}"
    try:
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f'SELECT time, "last", open, high, low, "previousClose", volume '
                f'FROM "{table}" '
                f'WHERE symbol = %s AND time >= %s AND time <= %s '
                f'ORDER BY time',
                (code, date + " 09:00:00", date + " 16:00:00")
            )
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        print(f"  [snapshot] {code} {table} 查询失败: {e}", file=sys.stderr)
        return []

    if not rows:
        return []

    # 转为增量1m bar
    bars = []
    prev_last = None
    prev_cum_vol = 0
    for r in rows:
        t = r[0]
        last = float(r[1]) if r[1] else 0
        day_open = float(r[2]) if r[2] else 0
        day_high = float(r[3]) if r[3] else 0
        day_low = float(r[4]) if r[4] else 0
        prev_close = float(r[5]) if r[5] else 0
        cum_vol = float(r[6]) if r[6] else 0

        if last <= 0:
            continue

        time_str = t.strftime("%Y-%m-%d %H:%M:%S") if isinstance(t, datetime) else str(t)

        # 增量volume
        vol_delta = cum_vol - prev_cum_vol if prev_cum_vol > 0 else 0

        # 用snapshot的day open/high/low作为bar的OHLC
        # 但更精确的做法: 用last作为close，day high/low作为bar的high/low
        bar = {
            "time": time_str,
            "open": prev_last if prev_last else last,
            "high": day_high if day_high > 0 else last,
            "low": day_low if day_low > 0 else last,
            "close": last,
            "volume": vol_delta if vol_delta > 0 else cum_vol,
            "prev_close": prev_close,
        }
        bars.append(bar)
        prev_last = last
        prev_cum_vol = cum_vol

    # 补充prev_close到第一根bar
    if bars and bars[0].get('prev_close', 0) <= 0:
        # 从day_open推算（第一根bar的open通常是前一日收盘价）
        pass

    return bars


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
    min_net_wan: float = 3000,
    d0_mode: str = "breakout",
) -> Optional[Dict]:
    """检查D0是否满足门槛条件

    v5: 去掉涨停和净买入条件，只保留突破前高 + 量比。

    d0_mode 可选:
      breakout      默认: 收盘突破前5日最高价
    """
    if d0_idx < 5 or d0_idx >= len(bars):
        return None

    d0 = bars[d0_idx]
    prev_close = bars[d0_idx - 1]['close']  # D-1收盘价

    # D-1突破前高: D-1收盘价 > D-1之前5日最高价
    pre5_bars = bars[max(0, d0_idx - 6):d0_idx - 1]
    pre5_high = max(b['high'] for b in pre5_bars) if pre5_bars else 0
    if d0_mode == "breakout":
        if prev_close <= pre5_high:
            return None
    elif d0_mode == "none":
        pass
    elif d0_mode == "below10":
        if pre5_high <= 0 or d0['close'] * 1.1 >= pre5_high:
            return None
    elif d0_mode == "ma10_pullback":
        if d0_idx < 9:
            return None
        ma10 = sum(bars[j]['close'] for j in range(d0_idx - 9, d0_idx + 1)) / 10
        if not (ma10 * 0.98 <= d0['low'] <= ma10 * 1.02 and d0['close'] > ma10):
            return None
    else:
        raise ValueError(f"unknown d0_mode: {d0_mode}")

    d1_bar = bars[d0_idx - 1]  # D-1
    pre5_vols = [b['volume'] for b in pre5_bars if b['volume'] > 0]
    avg_pre_vol = sum(pre5_vols) / len(pre5_vols) if pre5_vols else 1
    vol_ratio = d1_bar['volume'] / avg_pre_vol if avg_pre_vol > 0 else 999

    net_wan = net_amount / 10000  # 仅记录，不做门槛检查
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
# 1分钟入场信号检测
# ================================================================

def _split_1m_by_date(bars_1m: List[Dict]) -> Dict[str, List[Dict]]:
    """将1m bar按日期分组"""
    by_date = defaultdict(list)
    for b in bars_1m:
        date_str = b["time"][:10]
        by_date[date_str].append(b)
    return dict(by_date)


def analyze_vwap_strength(
    bars_1m_day: List[Dict],
    bar_idx: int,
    buy_price: float,
    prev_close: float,
    board_type: str,
    allow_early: bool = False,
) -> Optional[Dict]:
    """日内VWAP强弱状态分类（弱转强核心）

    args:
        allow_early=False: 允许早盘不足3根bar时也计算(用2根bar估算斜率)。
        用于纯VWAP入场模式, 数据最早bar为09:31结束点,
        用户视角"9:30算一根", 故第1根可算bar对应09:31结束点。

    强信号(保留1m入场):
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
    if allow_early:
        if bar_idx < 1 or bar_idx >= len(bars_1m_day):
            return None
    elif bar_idx < 2 or bar_idx >= len(bars_1m_day):
        return None

    # 计算到当前bar为止的累积VWAP
    bars_so_far = bars_1m_day[:bar_idx + 1]
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
    elif allow_early and len(vwap_series) >= 2:
        vwap_slope = (vwap_series[-1] - vwap_series[-2]) / vwap_series[-2] * 100
    else:
        vwap_slope = 0.0

    bar = bars_1m_day[bar_idx]
    prev_bar = bars_1m_day[bar_idx - 1] if bar_idx > 0 else None
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
            b = bars_1m_day[bar_idx + i]
            prev_v = vwap_series[bar_idx + i]
            if (b['close'] > prev_v) != (bars_1m_day[bar_idx + i + 1]['close'] > vwap_series[bar_idx + i + 1]):
                crosses += 1

    # 近3根close是否全部在VWAP上方
    recent_above_vwap = True
    if len(vwap_series) >= 3:
        for i in range(-3, 0):
            if bars_1m_day[bar_idx + i]['close'] < vwap_series[bar_idx + i]:
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

    # ---- 过滤优化 ----
    # VWAP距离>2%: 胜率从76%骤降到52%, 降级hold_above
    if dist_vwap > 2.0:
        pass_states -= {"hold_above"}
        if dist_vwap > 3.0:
            pass_states = set()

    # 斜率陷阱区1.2~1.7%: 胜率仅40%, 全部拒绝
    if 1.2 <= vwap_slope < 1.7:
        pass_states = set()
        reasons.append(f"斜率陷阱区({vwap_slope:.1f}%)")

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


def detect_signal(
    bars_1m_day: List[Dict],
    prev_close: float,
    board_type: str,
    vol_ratio: Optional[float] = None,
) -> Optional[Dict]:
    """统一1m盘中信号检测（v4 合并 A/B/C，不再区分入场模式）

    在当日1m扫描中按以下优先级取最早命中的盘中形态:
      1) 早盘强势: 开盘前15根1m bar全阳 + 累计涨幅>3% + 第15根量>第1根×1.2
      2) 回踩企稳: 曾触及涨停(high>=涨停价×0.998) + 回踩<=5% + 量不放大
                   + 收盘站上当日VWAP + D0量比<1.3 + 日内涨幅<=11% + 时间<14:45
      3) 冲板动能: bar涨5~12% + high距涨停<=5% + 突破前bar高点
                   + 量不极度萎缩 + 时间<11:30

    Returns:
        {bar_idx, bar_time, buy_price, intraday_pct, signal_type, reason, ...}
    """
    n = len(bars_1m_day)
    if n < 15 or prev_close <= 0:
        return None

    limit_price = get_limit_price(prev_close, board_type)

    # 累计VWAP序列（供回踩形态的收盘站上VWAP判断）
    vwap_series = []
    total_pv = 0.0
    total_vol = 0.0
    for b in bars_1m_day:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        total_pv += typical * b["volume"]
        total_vol += b["volume"]
        vwap_series.append(total_pv / total_vol if total_vol > 0 else 0)

    # --- 1) 早盘强势（前15根1m bar，覆盖09:30~09:45） ---
    # 检查前15根bar是否全部阳线
    first_15_idx = min(14, n - 1)
    if bars_1m_day[0]["time"][11:16] <= "09:45":
        all_bullish = all(bars_1m_day[i]["close"] > bars_1m_day[i]["open"] for i in range(first_15_idx + 1))
        if all_bullish:
            b_first = bars_1m_day[0]
            b_last = bars_1m_day[first_15_idx]
            intraday_pct = (b_last["close"] / prev_close - 1) * 100
            if intraday_pct > 3.0 and (b_first["volume"] <= 0 or b_last["volume"] > b_first["volume"] * 1.2):
                return {
                    "bar_idx": first_15_idx,
                    "bar_time": b_last["time"],
                    "buy_price": b_last["close"],
                    "intraday_pct": round(intraday_pct, 2),
                    "signal_type": "盘中信号",
                    "reason": "早盘强势",
                }

    # --- 2) 回踩企稳（首次触及涨停后） ---
    first_touch_idx = None
    for i, bar in enumerate(bars_1m_day):
        if bar["high"] >= limit_price * 0.998:
            first_touch_idx = i
            break

    if first_touch_idx is not None:
        for i in range(first_touch_idx + 1, n):
            bar = bars_1m_day[i]
            if bar["time"][11:16] >= "14:45":
                break
            pullback_pct = (limit_price - bar["low"]) / limit_price * 100
            prev_bar = bars_1m_day[i - 1]

            # 回撤不超过5%
            if pullback_pct > 5.0:
                continue
            # 量能不明显放大（拒绝放量出货）
            if prev_bar["volume"] > 0 and bar["volume"] >= prev_bar["volume"] * 1.3:
                continue
            # 收盘站上当日VWAP
            if bar["close"] < vwap_series[i]:
                continue

            intraday_pct = (bar["close"] / prev_close - 1) * 100
            # D0量比<1.3（放量回踩多为出货，放弃）
            if vol_ratio is not None and vol_ratio >= 1.3:
                break
            # 买入时日内涨幅<=11%（过滤20cm追涨）
            if intraday_pct > 11.0:
                continue

            return {
                "bar_idx": i,
                "bar_time": bar["time"],
                "buy_price": bar["close"],
                "intraday_pct": round(intraday_pct, 2),
                "signal_type": "盘中信号",
                "reason": "回踩企稳",
                "pullback_pct": round(pullback_pct, 2),
            }

    # --- 3) 冲板动能 ---
    for i in range(1, n):
        bar = bars_1m_day[i]
        if bar["time"][11:16] >= "11:30":
            break
        intraday_pct = (bar["close"] / prev_close - 1) * 100
        high_dist = (limit_price - bar["high"]) / limit_price * 100
        prev_bar = bars_1m_day[i - 1]

        # 涨幅 5~12%（太低没动能，太高追涨被套）
        if intraday_pct < 5.0 or intraday_pct > 12.0:
            continue
        # high距涨停 <= 5%
        if high_dist > 5.0:
            continue
        # 突破前bar高点
        if bar["close"] <= prev_bar["high"]:
            continue
        # 量能不极度萎缩（<30%）
        if prev_bar["volume"] > 0 and bar["volume"] < prev_bar["volume"] * 0.3:
            continue

        return {
            "bar_idx": i,
            "bar_time": bar["time"],
            "buy_price": bar["close"],
            "intraday_pct": round(intraday_pct, 2),
            "signal_type": "盘中信号",
            "reason": "冲板动能",
            "dist_to_limit": round(high_dist, 2),
        }

    return None



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
    hold_days: int = 10,
    stop_loss: float = -12.0,
    trailing_stop: float = -10.0,
    take_profit: float = 25.0,
) -> Optional[Dict]:
    """回测: 从buy_idx开始持有

    take_profit<=0 表示不设止盈, 仅靠止损/追踪/持仓到期出场。
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

        if take_profit > 0 and b['high'] >= buy_price * (1 + take_profit / 100):
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

def strategy_v5(
    dragon_data: List[Dict],
    kline_cache: Dict[str, List[Dict]],
    window_days: int = 20,
    min_net_wan: float = 3000,
    hold_days: int = 10,
    stop_loss: float = -12.0,
    trailing_stop: float = -10.0,
    take_profit: float = 25.0,
    max_gap: float = 5.0,
    show_detail: bool = False,
    vwap_debug: bool = False,
    vwap_time: bool = True,
    skip_strong_up: bool = False,
    confirm: int = 1,
    max_entry_pct: float = 8.0,
    max_dist_vwap: float = 2.0,
    d0_mode: str = "none",
    _1m_shared: Optional[Dict] = None,
) -> List[Dict]:
    """v4策略: 纯VWAP时间入场

    confirm: 连续N根"VWAP健康"bar才入场(单根刺破不做数)。
             confirm=1等价旧行为(第一根健康即入场)。期间有一根不健康则计数清零重新累积。

    max_entry_pct: 入场时该股当日涨幅上限(%)。<=0禁用。
                   实测>8%入场WR仅56.7%(n67), <=8%则83.0%; 故默认8。
    max_dist_vwap: 入场时价格距VWAP上限(%)。<=0禁用。
                   实测dist 2~3% WR仅48.0%, <2%则76~78%; 故默认2。
                   两者合并(<=8&dist<2)验证集WR87.0%。

    d0_mode: D0候选门槛变体: none(默认, 去掉前高)/breakout(突破前高)
             /below10(收盘×1.1<前高, 低位涨停)/ma10_pullback(回踩10日线不破涨停)。
             实验开关, 均不影响其他门槛。

    vwap_time=True(默认): 去掉盘中信号形态, 全天逐bar扫描,
    第一根VWAP健康(allow_early)即入场。实测120d 237笔 58.2% 总+1163.9。

    skip_strong_up(默认关, --skip-su实验): strong_up状态不作入场信号, 继续等更强状态。
    实测反而更差(延迟买入价更高: 120d 236笔 57.6% +1086.3), 故默认关闭。

    vwap_time=False(--pattern): 旧统一信号(早盘强势/回踩企稳/冲板动能)+VWAP过滤。
    D0盘中信号经VWAP状态过滤后入场；降级D1开盘入场(高开<=max_gap)。
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
    _1m_cache = _1m_shared if _1m_shared is not None else {}  # code -> 1m bars

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
        d0_features = check_d0_conditions(bars, d0_idx, net_amount, board_type, min_net_wan, d0_mode)
        if d0_features is None:
            continue

        tech = analyze_tech(bars, d0_idx)
        prev_close = bars[d0_idx - 1]['close'] if d0_idx > 0 else 0

        # === 统一D0盘中入场 ===
        # 加载1m数据
        if code not in _1m_cache:
            d0_dt = datetime.strptime(d0_date, "%Y-%m-%d")
            start_1m = (d0_dt - timedelta(days=2)).strftime("%Y-%m-%d")
            end_1m = (d0_dt + timedelta(days=2)).strftime("%Y-%m-%d")
            _1m_cache[code] = fetch_kline_1m(code, start_1m, end_1m)

        bars_1m_all = _1m_cache.get(code, [])
        if not bars_1m_all:
            continue
            continue

        # 按日期分组1m bar
        bars_1m_by_date = _split_1m_by_date(bars_1m_all)
        bars_1m_day = bars_1m_by_date.get(d0_date, [])

        if not bars_1m_day:
            print(f"  [信号] {code} {d0_date} 无1m bar (1m_by_date keys: {list(bars_1m_by_date.keys())[:3]})", file=sys.stderr)
            continue

        # D-1的1m数据 (用于动量对比)
        d1_date_in_bars = bars[d0_idx - 1]['time'] if d0_idx > 0 else ''
        bars_1m_prev = bars_1m_by_date.get(d1_date_in_bars, []) if d1_date_in_bars else []

        print(f"  [信号] {code} {d0_date} 1m有{len(bars_1m_day)}根bar, 检测盘中信号...", file=sys.stderr)

        # 统一信号检测
        if vwap_time:
            # 纯VWAP入场: 全天逐bar, 连续confirm根"健康"状态即入场
            signal = None
            run_len = 0
            for idx in range(1, len(bars_1m_day)):
                bar = bars_1m_day[idx]
                vw = analyze_vwap_strength(bars_1m_day, idx, bar["close"],
                                           prev_close, board_type, allow_early=True)
                if vw is None:
                    run_len = 0
                    continue
                if vw["pass"] and not (skip_strong_up and vw["state"] == "strong_up"):
                    run_len += 1
                else:
                    run_len = 0
                if run_len >= confirm:
                    ipct = (bar["close"] / prev_close - 1) * 100
                    pct_ok = max_entry_pct <= 0 or ipct <= max_entry_pct
                    dist_ok = max_dist_vwap <= 0 or vw["dist_vwap"] < max_dist_vwap
                    # 累积成交量对比: D0当前时间累积量 / D-1同时间累积量 > 1.2
                    mom_ok = True
                    if bars_1m_prev:
                        cur_time = bar['time'][11:16]  # HH:MM
                        # D0累积涨跌幅和累积成交量
                        d0_cum_vol = sum(bars_1m_day[j]['volume'] for j in range(idx + 1))
                        d0_open = bars_1m_day[0]['open']
                        d0_chg = (bar['close'] - d0_open) / d0_open if d0_open > 0 else 0
                        # D-1累积涨跌幅和累积成交量
                        d1_cum_vol = 0
                        d1_chg = 0
                        d1_open = bars_1m_prev[0]['open'] if bars_1m_prev else 0
                        for pb in bars_1m_prev:
                            d1_cum_vol += pb['volume']
                            if pb['time'][11:16] >= cur_time:
                                d1_chg = (pb['close'] - d1_open) / d1_open if d1_open > 0 else 0
                                break
                        # 效率 = 涨跌幅 / 累积量, 比值 > 1.2 说明D0拉升效率更高
                        if d0_cum_vol > 0 and d1_cum_vol > 0 and d1_chg > 0:
                            d0_eff = d0_chg / d0_cum_vol
                            d1_eff = d1_chg / d1_cum_vol
                            if d1_eff > 0 and d0_eff / d1_eff < 1.2:
                                mom_ok = False
                    if pct_ok and dist_ok and mom_ok:
                        signal = {
                            "bar_idx": idx,
                            "bar_time": bar["time"],
                            "buy_price": bar["close"],
                            "intraday_pct": round(ipct, 2),
                            "signal_type": "盘中信号",
                            "reason": f"VWAP:{vw['state']}x{run_len}",
                            "_vwap": vw,
                            "run_len": run_len,
                        }
                        break

            # 盘后失效检测: 入场后D0当日是否随后跌破VWAP(模拟"信号已失效")
            if signal is not None:
                for j in range(signal["bar_idx"] + 1, len(bars_1m_day)):
                    vwj = analyze_vwap_strength(bars_1m_day, j, bars_1m_day[j]["close"],
                                                prev_close, board_type, allow_early=True)
                    if vwj is None:
                        continue
                    if not vwj["pass"]:
                        signal["_broke_after"] = {
                            "idx": j,
                            "time": bars_1m_day[j]["time"],
                            "state": vwj["state"],
                            "bars_later": j - signal["bar_idx"],
                        }
                        break
        else:
            signal = detect_signal(bars_1m_day, prev_close, board_type,
                                   vol_ratio=d0_features.get('vol_ratio'))

        if signal is None:
            if bars_1m_day:
                print(f"  [信号] {code} {d0_date} 1m有{len(bars_1m_day)}根bar但盘中信号未触发", file=sys.stderr)
            continue

        # === VWAP日内均线过滤 ===
        vwap_check = signal.get('_vwap')
        if vwap_check is None:
            vwap_check = analyze_vwap_strength(
                bars_1m_day, signal['bar_idx'], signal['buy_price'], prev_close, board_type)
        if vwap_debug and vwap_check is not None and VWAP_DEBUG is not None:
            hr = run_backtest(bars, d0_idx, signal['buy_price'],
                              hold_days, stop_loss, trailing_stop, take_profit)
            VWAP_DEBUG.setdefault("盘中", []).append({
                'code': code, 'signal_date': d0_date, 'signal_type': signal['signal_type'],
                'reason': signal.get('reason', ''),
                'state': vwap_check['state'],
                'accepted': vwap_check['pass'],
                'vwap_slope': vwap_check['vwap_slope'],
                'dist_vwap': vwap_check['dist_vwap'],
                'hypothetical_return': hr['return_pct'] if hr else None,
            })
        if vwap_check is not None and not vwap_check['pass']:
            if show_detail:
                print(f"    {code} {d0_date} {signal['signal_type']}({signal.get('reason', '')}) "
                      f"VWAP[{vwap_check['state']}]: {', '.join(vwap_check['reasons'])}")
            continue

        # 有盘中信号且VWAP健康 → 用信号价格作为买入价
        buy_price = signal['buy_price']
        signal_time = signal['bar_time']

        # 入场时点特征(供胜率判别分析)
        first_vol = bars_1m_day[0]['volume'] if bars_1m_day else 0
        entry_bar = bars_1m_day[signal['bar_idx']] if 'bar_idx' in signal else None
        d0_last_bar = bars_1m_day[-1]
        entry_min = None
        if entry_bar is not None:
            try:
                hh, mm = signal_time[11:16].split(':')
                entry_min = int(hh) * 60 + int(mm) - 570  # 距09:30分钟数
            except (ValueError, IndexError):
                entry_min = None
        vol_ratio_1m = round(entry_bar['volume'] / first_vol, 2) if (entry_bar is not None and first_vol > 0) else None
        d0_close_ret = round((d0_last_bar['close'] / buy_price - 1) * 100, 2) if buy_price > 0 else None

        # D0未涨停 → D1开盘出场
        d0_close_price = bars[d0_idx]['close']
        d0_prev_close = bars[d0_idx - 1]['close'] if d0_idx > 0 else 0
        d0_is_limit_up = is_limit_up(d0_close_price, d0_prev_close, board_type)
        if not d0_is_limit_up:
            d1_idx = d0_idx + 1
            if d1_idx < len(bars):
                d1_open = bars[d1_idx]['open']
                return_pct = (d1_open / buy_price - 1) * 100
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
                    'entry_min': entry_min,
                    'vol_ratio_1m': vol_ratio_1m,
                    'd0_close_ret': d0_close_ret,
                    'vwap_info': vwap_check,
                    'signal_detail': {k: v for k, v in signal.items()
                                      if k not in ('bar_idx', 'buy_price', 'bar_time', '_vwap')},
                    'exit_price': round(d1_open, 3),
                    'exit_day': 1,
                    'exit_reason': 'D0未涨停',
                    'return_pct': round(return_pct, 2),
                    'peak_return_pct': round(return_pct, 2),
                    'drawdown': 0,
                    'daily_returns': [round(return_pct, 2)],
                })
            continue

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
            'entry_min': entry_min,
            'vol_ratio_1m': vol_ratio_1m,
            'd0_close_ret': d0_close_ret,
            'vwap_info': vwap_check,
            'signal_detail': {k: v for k, v in signal.items()
                              if k not in ('bar_idx', 'buy_price', 'bar_time', '_vwap')},
            **result,
        })

        if show_detail:
            d1_entry = get_d1_entry(bars, d0_idx, max_gap)
            d1_price = d1_entry['buy_price'] if d1_entry else buy_price
            savings = (d1_price - buy_price) / d1_price * 100 if d1_price > 0 else 0
            print(f"    {code} {d0_date} {signal['signal_type']}({signal.get('reason', '')}) "
                  f"买{buy_price:.2f}@{signal_time[11:16]} "
                  f"(日涨{signal['intraday_pct']:.1f}% 比D1省{savings:+.1f}%) "
                  f"-> {result['exit_reason']} {result['return_pct']:+.1f}%")

    return trades


def run_realtime_scan(args):
    """盘中实时扫描模式: 从realtime_quote_snapshot读取今日数据, 扫描龙虎榜D0标的入场信号"""
    from datetime import timezone, timedelta as td
    tz_cn = timezone(td(hours=8))
    today = datetime.now(tz_cn).strftime("%Y-%m-%d")
    live_date = today  # 实时快照查询日期 = 当前交易日，不受龙虎榜回退影响
    now_time = datetime.now(tz_cn).strftime("%H:%M:%S")

    print(f"\n{'=' * 80}")
    print(f"📡 盘中实时扫描模式 | {today} {now_time}")
    print(f"{'=' * 80}")

    # 1. 加载龙虎榜数据, 筛选今日D0标的
    print(f"\n📊 加载龙虎榜数据...")
    dragon_data = fetch_dragon_tiger_from_db(limit=50000)
    print(f"  龙虎榜: {len(dragon_data)}条")

    if not dragon_data:
        print("\n❌ 无龙虎榜数据")
        return

    # 筛选今日龙虎榜标的
    today_dragons = [r for r in dragon_data if r.get('trade_date', '') == today]
    if not today_dragons:
        # 也看最近的龙虎榜日期
        all_dates = sorted(set(r.get('trade_date', '') for r in dragon_data), reverse=True)
        print(f"\n  今日({today})无龙虎榜数据")
        print(f"  最近龙虎榜日期: {all_dates[:5]}")
        if all_dates:
            print(f"\n  使用最近日期 {all_dates[0]} 的龙虎榜数据...")
            today = all_dates[0]
            today_dragons = [r for r in dragon_data if r.get('trade_date', '') == today]
        if not today_dragons:
            print("\n❌ 无可用龙虎榜数据")
            return

    # 按code聚合
    by_code: Dict[str, List[Dict]] = defaultdict(list)
    for r in today_dragons:
        code = r.get('stock_code', '')
        if code:
            by_code[code].append(r)

    print(f"  龙虎榜标的: {len(by_code)}只")

    # 2. 对每只标的进行实时扫描
    kline_cache = {}
    signals = []
    no_data = []
    no_signal = []
    expired = []

    for code, rows in by_code.items():
        rows.sort(key=lambda x: x.get('trade_date', ''))
        d0_row = rows[0]
        d0_date = d0_row.get('trade_date', '')
        board_type = get_board_type(code)
        net_amount = float(d0_row.get('net_amount', 0) or 0)

        # 加载日线检查D0条件
        if code not in kline_cache:
            bars = fetch_kline_db(code, 300)
            if bars:
                kline_cache[code] = bars
        bars = kline_cache.get(code)
        if not bars:
            continue

        d0_idx = None
        for j, b in enumerate(bars):
            if b['time'] == d0_date:
                d0_idx = j
                break
        if d0_idx is None:
            continue

        d0_features = check_d0_conditions(bars, d0_idx, net_amount, board_type, args.min_net, args.d0_mode)
        if d0_features is None:
            continue

        tech = analyze_tech(bars, d0_idx)
        prev_close = bars[d0_idx - 1]['close'] if d0_idx > 0 else 0

        # 加载实时快照数据 (始终查当前交易日的快照, 不是龙虎榜交易日)
        snapshot_bars = fetch_realtime_snapshot(code, live_date)
        if not snapshot_bars:
            no_data.append(code)
            continue

        # 获取prev_close和D-1的1m数据 (用于拉升效率对比)
        if snapshot_bars and snapshot_bars[0].get('prev_close', 0) > 0:
            prev_close = snapshot_bars[0]['prev_close']

        # 加载D-1的1m数据
        bars_1m_prev = []
        if d0_idx > 0:
            d1_date_in_bars = bars[d0_idx - 1]['time']
            d1_dt = datetime.strptime(d1_date_in_bars, "%Y-%m-%d")
            start_1m = (d1_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            end_1m = (d1_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            all_1m = fetch_kline_1m(code, start_1m, end_1m)
            bars_1m_by_date = _split_1m_by_date(all_1m)
            bars_1m_prev = bars_1m_by_date.get(d1_date_in_bars, [])

        # 运行VWAP分析: 记录最早触发点 + 当前(最新)bar状态
        trigger = None
        for idx in range(1, len(snapshot_bars)):
            bar = snapshot_bars[idx]
            vw = analyze_vwap_strength(snapshot_bars, idx, bar["close"],
                                       prev_close, board_type, allow_early=True)
            if vw is None:
                continue
            if vw["pass"]:
                trigger = (idx, bar, vw)
                break

        # 当前(最新)bar是否仍满足VWAP健康 — 决定信号是否仍然有效
        last_i = len(snapshot_bars) - 1
        last_vw = analyze_vwap_strength(snapshot_bars, last_i, snapshot_bars[last_i]["close"],
                                        prev_close, board_type, allow_early=True)
        cur_ok = bool(last_vw and last_vw["pass"])

        if cur_ok:
            # 当前VWAP仍健康 → 实时可入场 (以现价为准)
            bar = snapshot_bars[last_i]
            ipct = (bar["close"] / prev_close - 1) * 100 if prev_close > 0 else 0
            pct_ok = args.max_entry_pct <= 0 or ipct <= args.max_entry_pct
            dist_ok = args.max_dist_vwap <= 0 or last_vw["dist_vwap"] < args.max_dist_vwap
            # 拉升效率对比
            eff_ok = True
            if bars_1m_prev:
                cur_time = bar['time'][11:16]
                d0_open = snapshot_bars[0]['open']
                d0_cum_vol = sum(b['volume'] for b in snapshot_bars[:last_i + 1])
                d0_chg = (bar['close'] - d0_open) / d0_open if d0_open > 0 else 0
                d1_cum_vol = 0
                d1_chg = 0
                d1_open = bars_1m_prev[0]['open'] if bars_1m_prev else 0
                for pb in bars_1m_prev:
                    d1_cum_vol += pb['volume']
                    if pb['time'][11:16] >= cur_time:
                        d1_chg = (pb['close'] - d1_open) / d1_open if d1_open > 0 else 0
                        break
                if d0_cum_vol > 0 and d1_cum_vol > 0 and d0_chg > 0 and d1_chg > 0:
                    d0_eff = d0_chg / d0_cum_vol
                    d1_eff = d1_chg / d1_cum_vol
                    if d1_eff > 0 and d0_eff / d1_eff < 1.2:
                        eff_ok = False
            if pct_ok and dist_ok and eff_ok:
                signal = {
                    "bar_idx": last_i,
                    "bar_time": bar["time"],
                    "buy_price": bar["close"],
                    "intraday_pct": round(ipct, 2),
                    "signal_type": "盘中信号",
                    "reason": "VWAP:" + last_vw["state"],
                    "_vwap": last_vw,
                }
                vwap_info = signal.get('_vwap', {})
                signals.append({
                    'code': code,
                    'name': d0_row.get('stock_name', ''),
                    'board': get_board_name(code),
                    'signal': signal,
                    'vwap_info': vwap_info,
                    'd0_features': d0_features,
                    'tech': tech,
                    'prev_close': prev_close,
                    'bars_count': len(snapshot_bars),
                    'last_price': bar["close"],
                })
            else:
                # VWAP健康但涨幅/距VWAP超限 → 等回落再触发 (与回测max_entry_pct/max_dist_vwap同规则)
                no_signal.append({
                    'code': code,
                    'name': d0_row.get('stock_name', ''),
                    'bars_count': len(snapshot_bars),
                    'last_price': bar["close"],
                    'intraday_pct': round(ipct, 2),
                    'note': 'VWAP健康但涨幅/距VWAP超限, 等回落',
                })
        elif trigger is not None:
            # 盘中触发过，但当前已不满足VWAP健康 → 信号已失效
            # 细分: 跌破并不可怕, 怕的是不会快速回到均线之上(距离越走越远=危险, 回升接近=尚可)
            brk = None
            for k in range(trigger[0] + 1, last_i + 1):
                vwk = analyze_vwap_strength(snapshot_bars, k, snapshot_bars[k]["close"],
                                            prev_close, board_type, allow_early=True)
                if vwk is not None and not vwk["pass"]:
                    brk = (k, snapshot_bars[k], vwk)
                    break
            last_dist = last_vw["dist_vwap"] if last_vw else None
            brk_dist = brk[2]["dist_vwap"] if brk else last_dist
            unrecovered = (last_i - brk[0]) if brk else 0
            drifting = (last_dist is not None and brk_dist is not None
                        and last_dist < brk_dist - 1e-9)
            expired.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'trigger_time': trigger[1]["time"],
                'trigger_price': trigger[1]["close"],
                'last_price': snapshot_bars[-1]['close'],
                'state': last_vw['state'] if last_vw else 'N/A',
                'intraday_pct': round((snapshot_bars[-1]['close'] / prev_close - 1) * 100, 2)
                                if prev_close > 0 else 0,
                'drifting': drifting,
                'last_dist': last_dist,
                'brk_dist': brk_dist,
                'brk_time': brk[1]["time"] if brk else '',
                'unrecovered_bars': unrecovered,
            })
        else:
            no_signal.append({
                'code': code,
                'name': d0_row.get('stock_name', ''),
                'bars_count': len(snapshot_bars),
                'last_price': snapshot_bars[-1]['close'] if snapshot_bars else 0,
                'intraday_pct': round((snapshot_bars[-1]['close'] / prev_close - 1) * 100, 2)
                                   if snapshot_bars and prev_close > 0 else 0,
            })

    # 3. 输出结果
    print(f"\n{'=' * 80}")
    print(f"📡 实时扫描结果 | 标的D0 {today} | 实时快照 {live_date}")
    print(f"{'=' * 80}")

    if signals:
        print(f"\n✅ 入场信号: {len(signals)}只")
        print(f"{'=' * 80}")
        for s in signals:
            sig = s['signal']
            vw = s['vwap_info']
            d0f = s['d0_features']
            buy_p = sig['buy_price']
            last_p = s.get('last_price', buy_p)
            vwap_val = vw.get('vwap', 0)
            max_buy = round(vwap_val * 1.02, 2) if vwap_val > 0 else buy_p
            print(f"\n  🔔 {s['code']} {s['name']} ({s['board']})")
            print(f"     建议买入价: {buy_p:.2f} @ {sig['bar_time'][11:16]}  (现价{last_p:.2f} | 上限{max_buy:.2f})")
            print(f"     信号: {sig['reason']}")
            print(f"     日涨: {sig['intraday_pct']:+.1f}% | 前收: {s['prev_close']:.2f}")
            print(f"     VWAP: {vw.get('vwap', 0):.3f} | 距离: {vw.get('dist_vwap', 0):+.1f}% | 斜率: {vw.get('vwap_slope', 0):+.3f}%")
            print(f"     D-1突破: {d0f.get('breakout_pct', 0):.1f}% | 量比: {d0f.get('vol_ratio', 0):.1f}x")
    else:
        print(f"\n❌ 无入场信号")

    if no_signal:
        print(f"\n⏳ 等待信号: {len(no_signal)}只")
        for ns in no_signal:
            note = f" | {ns['note']}" if ns.get('note') else ""
            print(f"  ⏳ {ns['code']} {ns['name']} | {ns['bars_count']}根bar | "
                  f"价格{ns['last_price']:.2f} | 日涨{ns['intraday_pct']:+.1f}%{note}")

    if expired:
        print(f"\n⚠️ 信号已失效: {len(expired)}只 (盘中曾触发, 现价已回落VWAP下方)")
        for ex in expired:
            md = "🔻 远离中" if ex.get('drifting') else "🟡 回升中"
            dstr = ""
            if ex.get('last_dist') is not None:
                dstr = f" | 距VWAP {ex['last_dist']:+.1f}% (破位{ex.get('brk_dist') or 0:+.1f}%)"
            print(f"  ⚠️ {ex['code']} {ex['name']} {md} | 曾触发 {ex['trigger_time'][11:16]} @ {ex['trigger_price']:.2f} | "
                  f"现价{ex['last_price']:.2f} | 跌破{ex.get('unrecovered_bars', 0)}根未收复 | "
                  f"状态:{ex['state']} | 日涨{ex['intraday_pct']:+.1f}%{dstr}")

    if no_data:
        print(f"\n⚠️ 无实时数据: {len(no_data)}只 ({', '.join(no_data[:10])}{'...' if len(no_data) > 10 else ''})")

    print(f"\n{'=' * 80}")
    print(f"扫描完成 | 信号{len(signals)} | 等待{len(no_signal)} | 失效{len(expired)} | 无数据{len(no_data)}")
    print(f"{'=' * 80}")


def main():
    parser = argparse.ArgumentParser(description="龙虎榜游资D0策略 v5(默认)")
    parser.add_argument("--days", type=int, default=20, help="D0搜索窗口(交易日)")
    parser.add_argument("--hold-days", type=int, default=10, help="持仓天数")
    parser.add_argument("--stop-loss", type=float, default=-12.0, help="止损%%")
    parser.add_argument("--trailing-stop", type=float, default=-10.0, help="追踪止损%%")
    parser.add_argument("--take-profit", type=float, default=25.0, help="止盈%%")
    parser.add_argument("--min-net", type=float, default=3000, help="最小净买入额(万)")
    parser.add_argument("--d0-mode", type=str, default="breakout",
                        choices=["breakout", "none", "below10", "ma10_pullback"],
                        help="D0候选门槛变体: none(默认, 去前高)/breakout(突破前高)/below10(收盘×1.1<前高)/ma10_pullback(回踩10日线)")
    parser.add_argument("--max-gap", type=float, default=5.0, help="最大D1高开幅度%%(超过则放弃)")
    parser.add_argument("--debug-vwap", action="store_true", help="输出VWAP状态×信号假设收益判别力表")
    parser.add_argument("--pattern", action="store_true",
                        help="用旧统一信号(早盘强势/回踩企稳/冲板动能)替代纯VWAP入场")
    parser.add_argument("--skip-su", action="store_true",
                        help="实验: strong_up不入场等更强状态(实测更差, 默认关)")
    parser.add_argument("--confirm", type=int, default=1,
                        help="连续N根VWAP健康bar才入场(默认1=第一根健康即入场)")
    parser.add_argument("--confirm-sweep", action="store_true",
                        help="实验: 连续确认N={1,2,3,5,8,10}扫描对比, 共享1m缓存只拉一次")
    parser.add_argument("--max-entry-pct", type=float, default=8.0,
                        help="入场时该股当日涨幅上限%%(默认8, 0=禁用), 实测>8%% WR仅56.7%%")
    parser.add_argument("--max-dist-vwap", type=float, default=2.0,
                        help="入场时价格距VWAP上限%%(默认2, 0=禁用), 实测2~3%% WR仅48.0%%")
    parser.add_argument("--all-trades", action="store_true", help="输出交易明细")
    parser.add_argument("--detail", action="store_true", help="输出详细匹配信息")
    parser.add_argument("--export", type=str, default="", help="导出JSON文件路径")
    parser.add_argument("--today", action="store_true",
                        help="盘中实时扫描模式: 从realtime_quote_snapshot读取今日数据, 扫描龙虎榜D0标的入场信号")
    args = parser.parse_args()

    print("=" * 80)
    print("龙虎榜游资D0策略 v5")
    mode_tag = {"breakout": " + 突破前高", "none": " (去掉前高)",
                "below10": " + 收盘×1.1<前高", "ma10_pullback": " + 回踩10日线不破"}.get(args.d0_mode, "")
    print(f"D0: D-1突破前高{mode_tag}")
    print(f"止损{args.stop_loss}% | 追踪{args.trailing_stop}% | 止盈{args.take_profit}% | 持仓{args.hold_days}天")
    print("=" * 80)

    if args.today:
        run_realtime_scan(args)
        return

    # 加载数据
    print(f"\n📊 加载龙虎榜数据 (窗口={args.days}天)...")
    dragon_data = fetch_dragon_tiger_from_db()
    print(f"  龙虎榜: {len(dragon_data)}条")

    if not dragon_data:
        print("\n❌ 无数据")
        return

    kline_cache = {}

    global VWAP_DEBUG
    VWAP_DEBUG = {} if args.debug_vwap else None

    if args.confirm_sweep:
        print(f"\n🧪 连续确认扫描 N={{1,2,3,5,8,10}} (共享1m缓存, 只拉一次)...")
        sweep_1m = {}
        sweep_rows = []
        for c in [1, 2, 3, 5, 8, 10]:
            t0 = time.time()
            tr = strategy_v5(
                dragon_data, kline_cache,
                window_days=args.days, min_net_wan=args.min_net,
                hold_days=args.hold_days, stop_loss=args.stop_loss,
                trailing_stop=args.trailing_stop, take_profit=args.take_profit,
                max_gap=args.max_gap,
                show_detail=False,
                vwap_debug=False,
                vwap_time=True,
                skip_strong_up=False,
                confirm=c,
                max_entry_pct=args.max_entry_pct,
                max_dist_vwap=args.max_dist_vwap,
                d0_mode=args.d0_mode,
                _1m_shared=sweep_1m,
            )
            s = calc_stats(tr)
            intra = [t for t in tr if t.get('vwap_info')]
            si = calc_stats(intra)
            avg_min = (sum(t['entry_min'] for t in intra if t.get('entry_min') is not None) / len(intra)
                       if intra else 0)
            expd = sum(1 for t in intra if (t.get('signal_detail') or {}).get('_broke_after'))
            total_ret = sum(t['return_pct'] for t in tr)
            sweep_rows.append((c, tr, intra, s, si, avg_min, expd, total_ret, time.time() - t0))
            print(f"  N={c:>2}: 总{len(tr):>3}笔(盘中{len(intra):>3}) 胜率{s['win_rate']:>5.1f}% "
                  f"均收益{s['avg_return']:>+6.2f}% 总{total_ret:>+9.2f}% | "
                  f"盘中胜率{si['win_rate']:>5.1f}% 均入{avg_min:>5.1f}min 盘后失效{expd}")

        print(f"\n{'=' * 80}")
        print(f"📊 连续确认扫描汇总 (窗口{args.days}天, 持有{args.hold_days}天)")
        print(f"{'=' * 80}")
        print(f"  {'N':>4} | {'笔数':>5} | {'胜率':>7} | {'均收益':>8} | {'总收益':>10} | "
              f"{'盘中笔数':>7} | {'盘中胜率':>7} | {'均入min':>7} | {'盘后失效':>7}")
        for c, tr, intra, s, si, avg_min, expd, total_ret, dt in sweep_rows:
            print(f"  {c:>4} | {len(tr):>5} | {s['win_rate']:>6.1f}% | {s['avg_return']:>+7.2f}% | "
                  f"{total_ret:>+9.2f}% | {len(intra):>7} | {si['win_rate']:>6.1f}% | "
                  f"{avg_min:>7.1f} | {expd:>7}")
        return

    print(f"\n🔄 运行策略 (窗口={args.days}天)...")
    t0 = time.time()
    trades = strategy_v5(
        dragon_data, kline_cache,
        window_days=args.days, min_net_wan=args.min_net,
        hold_days=args.hold_days, stop_loss=args.stop_loss,
        trailing_stop=args.trailing_stop, take_profit=args.take_profit,
        max_gap=args.max_gap,
        show_detail=args.detail,
        vwap_debug=args.debug_vwap,
        vwap_time=not args.pattern,
        skip_strong_up=args.skip_su,
        confirm=args.confirm,
        max_entry_pct=args.max_entry_pct,
        max_dist_vwap=args.max_dist_vwap,
        d0_mode=args.d0_mode,
    )
    elapsed = time.time() - t0

    print(f"\n{'=' * 80}")
    print(f"📊 回测结果 (耗时{elapsed:.1f}秒)")
    print(f"{'=' * 80}")

    stats = calc_stats(trades)
    print_stats(stats, "v5纯VWAP时间入场" if not args.pattern else "v5统一信号[旧]")

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

        # 盘中信号触发原因统计
        reasons = defaultdict(list)
        for t in trades:
            reason = (t.get('signal_detail') or {}).get('reason', '')
            if reason:
                reasons[reason].append(t)
        if reasons:
            print(f"\n  盘中信号原因:")
            for rn, seg in sorted(reasons.items()):
                s = calc_stats(seg)
                print(f"    {rn}: {s['total']:>3}笔 胜率{s['win_rate']:>5.1f}% "
                      f"均收益{s['avg_return']:>+6.2f}% 总收益{sum(x['return_pct'] for x in seg):+8.2f}%")

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
                  f"{t['signal_date']} D-1突{d0f.get('breakout_pct', 0):.0f}% "
                  f"-> {entry_info} "
                  f"-> {t['exit_reason'][:6]} 持{t['exit_day']}天 "
                  f"收益{t['return_pct']:>+6.2f}%")

    # 导出
    if trades:
        outfile = args.export or "test_dragon_hot_v5_result.json"
        export = [{k: v for k, v in t.items() if k != 'daily_returns'} for t in trades]
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        print(f"\n💾 {outfile} ({len(trades)}笔)")

    # VWAP状态×假设收益判别力表
    if VWAP_DEBUG:
        print(f"\n{'=' * 80}")
        print(f"📐 VWAP状态判别力 (盘中信号假设收益, 是否被VWAP拒绝)")
        print(f"{'=' * 80}")
        rows = [r for r in VWAP_DEBUG.get("盘中", []) if r.get('hypothetical_return') is not None]
        if rows:
            states = sorted(set(r['state'] for r in rows))

            def seg_line(tag, g):
                if not g:
                    return f"{tag}: -"
                w = sum(1 for r in g if r['hypothetical_return'] > 0)
                return (f"{tag}: {len(g)}笔 WR{w/len(g)*100:.0f}% "
                        f"avg{sum(r['hypothetical_return'] for r in g)/len(g):+.2f}%")

            # 基础表: state × 保留/拒绝
            for st in states:
                grp = [r for r in rows if r['state'] == st]
                acc = [r for r in grp if r['accepted']]
                rej = [r for r in grp if not r['accepted']]
                print(f"    {st:<16} {seg_line('保留', acc)} | {seg_line('拒绝', rej)}")

            acc_rows = [r for r in rows if r['accepted']]
            if acc_rows:
                # 月份 × state (保留) — 监控VWAP状态与行情月份的关系
                print(f"\n  月份×状态 (保留信号):")
                by_month = defaultdict(list)
                for r in acc_rows:
                    by_month[r['signal_date'][:7]].append(r)
                for mo in sorted(by_month.keys()):
                    g = by_month[mo]
                    st_cnt = defaultdict(list)
                    for r in g:
                        st_cnt[r['state']].append(r)
                    parts = []
                    for st in states:
                        sg = st_cnt.get(st, [])
                        if len(sg) >= 2:
                            w = sum(1 for x in sg if x['hypothetical_return'] > 0)
                            parts.append(f"{st[:6]}{len(sg)}笔{w/len(sg)*100:.0f}%")
                    w = sum(1 for r in g if r['hypothetical_return'] > 0)
                    print(f"    {mo}: {len(g)}笔 WR{w/len(g)*100:.0f}% "
                          f"总{sum(r['hypothetical_return'] for r in g):+.1f}%  "
                          f"[{', '.join(parts)}]")

                # state × 信号原因 (保留)
                print(f"\n  状态×信号原因 (保留):")
                for st in states:
                    sg = [r for r in acc_rows if r['state'] == st]
                    if not sg:
                        continue
                    by_r = defaultdict(list)
                    for r in sg:
                        by_r[r.get('reason', '')].append(r)
                    print(f"    {st:<16} {' | '.join(seg_line(rn, g2) for rn, g2 in sorted(by_r.items()))}")

                # state × 距VWAP分桶 (保留) — 监控远离均线毒性段
                print(f"\n  状态×距VWAP (保留):")
                b_list = [('dist<3.5', lambda r: r['dist_vwap'] < 3.5),
                          ('3.5-5', lambda r: 3.5 <= r['dist_vwap'] < 5),
                          ('>=5', lambda r: r['dist_vwap'] >= 5)]
                for st in states:
                    sg = [r for r in acc_rows if r['state'] == st]
                    if not sg:
                        continue
                    parts = []
                    for bname, cond in b_list:
                        bg = [r for r in sg if cond(r)]
                        if bg:
                            parts.append(seg_line(bname, bg))
                    print(f"    {st:<16} {' | '.join(parts)}")
        print()


if __name__ == "__main__":
    main()

