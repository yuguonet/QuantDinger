"""
kline_clean.py — K 线数据连贯性补齐（纯数据处理)

输入: list[dict]（含 time/open/high/low/close/volume）
输出: 补齐中间缺失 bar 的 list[dict]

规则:
  - 只补中间缺失，不补首尾之外
  - 前向填充: 价=上一根收盘，量=0
  - 自动检测时间间隔（从数据推断）
  - 跳过非交易日（A 股）

用法:
    from data_sources.kline_clean import clean_klines

    cleaned = clean_klines(bars, "15m")
    cleaned = clean_klines(bars, "1D")
"""

from datetime import datetime, timedelta, time as dt_time
from typing import Dict, List, Any, Optional
from zoneinfo import ZoneInfo

from app.utils.trading_calendar import is_trading_day as _is_trading_day_str
from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_SH = ZoneInfo("Asia/Shanghai")

# ── 交易时段（分钟级补齐用）──

_TRADING_SESSIONS = [
    ((9, 30), (11, 30)),
    ((13, 0), (15, 0)),
]

# ── 时间框架 → 秒数 ──

_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900,
    "30m": 1800, "60m": 3600, "2H": 7200, "4H": 14400,
    "1D": 86400, "1W": 604800,
}

_TF_ALIASES = {
    "D": "1D", "day": "1D", "daily": "1D",
    "W": "1W", "week": "1W", "weekly": "1W",
    "M": "1m", "min": "1m",
    "H": "60m", "h": "60m",
    "2h": "2H", "4h": "4H",
}


# ── 工具函数 ──

def _normalize_tf(timeframe: str) -> str:
    return _TF_ALIASES.get(timeframe, timeframe)


def _ensure_aware(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=TZ_SH)
    return t


def _bar_to_dt(bar: dict) -> Optional[datetime]:
    t = bar.get("time")
    if t is None:
        return None
    if isinstance(t, datetime):
        return _ensure_aware(t)
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=TZ_SH)
    return None


def _dt_to_ts(t: datetime) -> int:
    return int(t.timestamp())


def _is_trading_day(d: str) -> bool:
    return _is_trading_day_str(d)


# ── 交易日缓存 ──

_sorted_trading_days: Optional[List[str]] = None
_trading_day_ref: Optional[frozenset] = None


def _get_sorted_trading_days() -> List[str]:
    global _sorted_trading_days, _trading_day_ref
    from app.utils.trading_calendar import trade_date_range
    end_year = datetime.now().year + 1
    current = frozenset(trade_date_range("2015-01-01", f"{end_year}-12-31"))
    if current is not _trading_day_ref:
        _sorted_trading_days = sorted(current)
        _trading_day_ref = current
    return _sorted_trading_days


# ── 期望时间点生成 ──

def _expected_times_between(
    start: datetime, end: datetime, interval_sec: int, timeframe: str
) -> List[datetime]:
    """生成 (start, end) 开区间内所有期望时间点（不含首尾）"""
    tf = _normalize_tf(timeframe)

    if tf == "1D":
        return _expected_daily_between(start, end)
    if tf == "1W":
        return _expected_weekly_between(start, end)

    # 分钟级
    return _expected_intraday_between(start, end, interval_sec)


def _expected_daily_between(start: datetime, end: datetime) -> List[datetime]:
    """生成两个日期之间的交易日（不含 start 当天，不含 end 当天）"""
    start_d = _ensure_aware(start).strftime("%Y-%m-%d")
    end_d = _ensure_aware(end).strftime("%Y-%m-%d")

    result = []
    for d in _get_sorted_trading_days():
        if d <= start_d or d >= end_d:
            continue
        result.append(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH))
    return result


def _expected_weekly_between(start: datetime, end: datetime) -> List[datetime]:
    """生成两个时间之间的周线时间点（每周最后一个交易日）"""
    start_d = _ensure_aware(start).strftime("%Y-%m-%d")
    end_d = _ensure_aware(end).strftime("%Y-%m-%d")

    # 收集范围内的所有交易日（含边界），按周分组
    weeks: Dict[tuple, str] = {}
    for d in _get_sorted_trading_days():
        if d < start_d or d > end_d:
            continue
        iso = datetime.strptime(d, "%Y-%m-%d").isocalendar()
        weeks[(iso[0], iso[1])] = d  # 排序后最后一个覆盖

    # 排除 start 和 end 所在周（只补中间）
    start_iso = _ensure_aware(start).isocalendar()
    end_iso = _ensure_aware(end).isocalendar()
    start_key = (start_iso[0], start_iso[1])
    end_key = (end_iso[0], end_iso[1])

    return [
        datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH)
        for key, d in sorted(weeks.items())
        if key != start_key and key != end_key
    ]


def _expected_intraday_between(
    start: datetime, end: datetime, interval_sec: int
) -> List[datetime]:
    """生成两个时间之间的分钟级时间点（只在交易时段内）"""
    start_d = _ensure_aware(start)
    end_d = _ensure_aware(end)
    interval_min = interval_sec // 60

    start_str = start_d.strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    result = []
    for d in _get_sorted_trading_days():
        if d < start_str or d > end_str:
            continue
        dt_base = datetime.strptime(d, "%Y-%m-%d")
        for (sh, sm), (eh, em) in _TRADING_SESSIONS:
            t = sh * 60 + sm
            t_end = eh * 60 + em
            while t <= t_end:
                h, m = divmod(t, 60)
                bar_dt = dt_base.replace(hour=h, minute=m, tzinfo=TZ_SH)
                if start_d < bar_dt < end_d:
                    result.append(bar_dt)
                t += interval_min
    return result


# ═══════════════════════════════════════════════════════════════════
#  核心 API
# ═══════════════════════════════════════════════════════════════════

def clean_klines(
    bars: List[dict],
    timeframe: str,
) -> List[dict]:
    """补齐 K 线中间缺失部分（前向填充）

    规则:
      - 只补中间缺失，不补首尾之外
      - 缺失 bar: time=期望时间, open/high/low/close=上一根收盘, volume=0
      - 自动从数据推断时间间隔
      - 跳过非交易日

    Args:
        bars:      K 线列表，每条需含 time/open/high/low/close/volume
        timeframe: 时间框架（"1m"/"5m"/"15m"/"1D"/"1W" 等）

    Returns:
        补齐后的 K 线列表（时间连续，无中间缺失）
    """
    if len(bars) < 2:
        return list(bars)

    tf = _normalize_tf(timeframe)

    # 按时间排序
    sorted_bars = sorted(bars, key=lambda b: _bar_to_dt(b) or datetime.min)

    # 转为 (datetime, bar) 对
    dt_bars = []
    for b in sorted_bars:
        dt = _bar_to_dt(b)
        if dt is not None:
            dt_bars.append((dt, b))

    if len(dt_bars) < 2:
        return sorted_bars

    # 获取时间间隔
    interval_sec = _TF_SECONDS.get(tf)
    if interval_sec is None:
        # 从数据推断间隔
        diffs = [
            (dt_bars[i + 1][0] - dt_bars[i][0]).total_seconds()
            for i in range(len(dt_bars) - 1)
        ]
        if not diffs:
            return sorted_bars
        interval_sec = max(60, min(diffs))  # 取最小间隔，至少 60 秒

    # 逐对检查中间缺失
    result = []
    last_close = None

    for i in range(len(dt_bars)):
        dt, bar = dt_bars[i]

        # 和前一根之间检查缺失
        if i > 0:
            prev_dt, prev_bar = dt_bars[i - 1]
            prev_close = prev_bar.get("close", 0)

            # 生成中间期望时间点
            gaps = _expected_times_between(prev_dt, dt, interval_sec, tf)

            for gap_dt in gaps:
                result.append({
                    "time": _dt_to_ts(gap_dt),
                    "open": prev_close,
                    "high": prev_close,
                    "low": prev_close,
                    "close": prev_close,
                    "volume": 0,
                })

        result.append(bar)

    return result
