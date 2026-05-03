"""
data_provider.py — 回测/显示通用 K 线数据接口

在 db_market 之上封装一层，回测引擎和图表显示都调这个模块。
缺失段用前向填充（last close, volume=0），不落库。

支持时间框架:
  基础（直接查 DB）:    1m, 5m, 15m
  聚合（从 15m 聚合）:  30m, 60m, 2H, 4H
  日级:                 1D, 1W

用法:
    from data_provider import MarketDataProvider

    provider = MarketDataProvider(writer)
    bars = provider.get_clean_klines("CNStock", "000001", start, end, "15m")
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import os, sys

_co_dir = os.path.dirname(os.path.abspath(__file__))
if _co_dir not in sys.path:
    sys.path.insert(0, _co_dir)

from trading_day import is_trading_day as _is_trading_day_str

TZ_SH = ZoneInfo("Asia/Shanghai")

# ── A 股交易时段定义 ──

# 15m 标准 bar 时间点（结束时间标注，与 tdx_download.py 一致）
# 9:30 为开盘竞价 bar（暂不使用），9:45 为第一根常规 bar，15:00 收盘
_BAR_TIMES_15M = [
    (9, 30),
    (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0),
]

# 交易时段：(开始时, 分, 结束时, 分)
_TRADING_SESSIONS = [
    ((9, 30), (11, 30)),   # 上午
    ((13, 0), (15, 0)),    # 下午
]

# ── 时间框架配置 ──

# 基础分钟间隔（直接查 DB）
_TF_INTERVALS = {
    "1m": 1, "5m": 5, "15m": 15,
    "30m": 30, "60m": 60, "2H": 120, "4H": 240,
}

# 聚合时间框架（从 15m 聚合，必须是 15 的整数倍）
_AGGREGATE_TFS = {"30m", "60m", "2H", "4H"}

# 常用别名
_TF_ALIASES = {
    "D": "1D", "day": "1D", "daily": "1D",
    "W": "1W", "week": "1W", "weekly": "1W",
    "M": "1m", "min": "1m",
    "H": "60m", "h": "60m",
    "2h": "2H", "4h": "4H",
}


# ── 工具函数 ──

_sorted_trading_days: list[str] | None = None
_trading_day_ref: frozenset[str] | None = None  # 用于检测缓存是否过期


def _get_sorted_trading_days() -> list[str]:
    """获取排序后的交易日列表（模块级缓存，避免重复排序）

    自动跟踪 trading_day 模块的缓存刷新：
    如果 frozenset 引用变了（说明 refresh 过），重新排序。
    """
    global _sorted_trading_days, _trading_day_ref
    from trading_day import get_trading_day_set
    current = get_trading_day_set()
    if current is not _trading_day_ref:
        _sorted_trading_days = sorted(current)
        _trading_day_ref = current
    return _sorted_trading_days


def _normalize_tf(timeframe: str) -> str:
    """时间框架别名标准化"""
    return _TF_ALIASES.get(timeframe, timeframe)


def _generate_expected_times(start: datetime, end: datetime, timeframe: str) -> list[datetime]:
    """生成 [start, end] 范围内该时间框架的所有期望时间戳（带时区）"""
    timeframe = _normalize_tf(timeframe)

    if timeframe == "1D":
        return _gen_daily(start, end)
    if timeframe == "1W":
        return _gen_weekly(start, end)

    interval = _TF_INTERVALS.get(timeframe)
    if interval is None:
        raise ValueError(f"不支持的时间框架: {timeframe}")

    return _gen_intraday(start, end, interval)


def _filter_trading_days(bars: list[dict]) -> list[dict]:
    """过滤非交易日数据（按日期分组判断，避免逐 bar 做 strftime）"""
    date_cache: dict[str, bool] = {}
    result = []
    for b in bars:
        t = b["time"]
        d = t.astimezone(TZ_SH).strftime("%Y-%m-%d") if t.tzinfo else t.strftime("%Y-%m-%d")
        if d not in date_cache:
            date_cache[d] = _is_trading_day_str(d)
        if date_cache[d]:
            result.append(b)
    return result


def _gen_daily(start: datetime, end: datetime) -> list[datetime]:
    """生成日线期望时间戳：每个交易日 00:00（直接遍历交易日集合）"""
    start_d = start.astimezone(TZ_SH) if start.tzinfo else start.replace(tzinfo=TZ_SH)
    end_d = end.astimezone(TZ_SH) if end.tzinfo else end.replace(tzinfo=TZ_SH)
    start_str = start_d.strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    result = []
    for d in _get_sorted_trading_days():
        if d < start_str or d > end_str:
            continue
        result.append(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH))
    return result


def _gen_weekly(start: datetime, end: datetime) -> list[datetime]:
    """生成周线期望时间戳：每周最后一个交易日（直接遍历交易日集合）"""
    start_d = start.astimezone(TZ_SH) if start.tzinfo else start.replace(tzinfo=TZ_SH)
    end_d = end.astimezone(TZ_SH) if end.tzinfo else end.replace(tzinfo=TZ_SH)
    start_str = start_d.strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    # 按自然周分组，取每周最后一个交易日
    weeks: dict[tuple[int, int], str] = {}  # (iso_year, iso_week) -> last_date
    for d in _get_sorted_trading_days():
        if d < start_str or d > end_str:
            continue
        iso = datetime.strptime(d, "%Y-%m-%d").isocalendar()
        weeks[(iso[0], iso[1])] = d  # 排序后最后一个覆盖

    result = []
    for d in sorted(weeks.values()):
        result.append(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH))
    return result


def _gen_intraday(start: datetime, end: datetime, interval_min: int) -> list[datetime]:
    """生成分钟级期望时间戳（直接遍历交易日集合）"""
    start_d = start.astimezone(TZ_SH) if start.tzinfo else start.replace(tzinfo=TZ_SH)
    end_d = end.astimezone(TZ_SH) if end.tzinfo else end.replace(tzinfo=TZ_SH)
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
                if start_d <= bar_dt <= end_d:
                    result.append(bar_dt)
                t += interval_min
    return result


def _bars_to_dict(bars: list[dict]) -> dict[datetime, dict]:
    """把 bar 列表按 time 建索引（统一转为 aware datetime）"""
    import logging
    logger = logging.getLogger(__name__)
    skipped = 0
    index = {}
    for b in bars:
        t = b["time"]
        if isinstance(t, datetime):
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_SH)
            index[t] = b
        else:
            skipped += 1
    if skipped:
        logger.warning(f"_bars_to_dict: 跳过 {skipped} 条 time 非 datetime 的记录")
    return index


def _aggregate_bars(bars: list[dict], target_time: datetime) -> dict:
    """将多根小周期 bar 聚合成一根大周期 bar

    open  = 第一根的 open
    high  = 所有 high 的最大值
    low   = 所有 low 的最小值
    close = 最后一根的 close
    volume = 所有 volume 之和
    """
    sorted_bars = sorted(bars, key=lambda b: b["time"])
    return {
        "time": target_time,
        "open": sorted_bars[0]["open"],
        "high": max(b["high"] for b in sorted_bars),
        "low": min(b["low"] for b in sorted_bars),
        "close": sorted_bars[-1]["close"],
        "volume": sum(b.get("volume", 0) for b in sorted_bars),
    }


# ── 数据接口 ──

class MarketDataProvider:
    """回测/显示通用 K 线数据接口

    用法:
        provider = MarketDataProvider(writer)

        # 基础周期，直接查 DB
        bars = provider.get_clean_klines("CNStock", "000001", start, end, "15m")

        # 聚合周期，从 15m 自动聚合
        bars = provider.get_clean_klines("CNStock", "000001", start, end, "60m")
        bars = provider.get_clean_klines("CNStock", "000001", start, end, "4H")

        # 日线/周线
        bars = provider.get_clean_klines("CNStock", "000001", start, end, "1D")
    """

    def __init__(self, writer):
        """
        Args:
            writer: db_market KlineWriter 实例（需有 query 方法）
        """
        self.writer = writer

    def get_clean_klines(self, market: str, symbol: str,
                            start: datetime, end: datetime,
                            timeframe: str) -> list[dict]:
        """获取清洗后的 K 线数据

        清洗规则：
          - 过滤非交易日数据（节假日、调休日的脏数据）
          - 缺失 bar 用前向填充补齐（价=上一根收盘，量=0）
          - 30m/60m/2H/4H 从 15m 数据聚合

        注意：不保证数据"完整"——最新日期可能尚未入库。

        Args:
            market:    市场标识，如 "CNStock"
            symbol:    股票代码，如 "000001"
            start:     起始时间（datetime，带不带时区都行）
            end:       结束时间
            timeframe: 时间框架
                       基础（直接查DB）: "1m", "5m", "15m"
                       聚合（从15m聚合）: "30m", "60m", "2H", "4H"
                       日级: "1D", "1W"
                       别名: "daily"→"1D", "H"→"60m", "2h"→"2H" 等

        Returns:
            list[dict]: 时间连续的 K 线数据
        """
        timeframe = _normalize_tf(timeframe)

        # 聚合周期：从 15m 聚合
        if timeframe in _AGGREGATE_TFS:
            return self._get_aggregated(market, symbol, timeframe, start, end)

        # 基础周期 / 日级：直接查 DB + 前向填充
        raw = self._query_raw(market, symbol, timeframe, start, end)
        if not raw:
            return []

        # 过滤非交易日的脏数据
        raw = _filter_trading_days(raw)

        expected = _generate_expected_times(start, end, timeframe)
        if not expected:
            return raw

        return self._fill_gaps(raw, expected)

    # ── 聚合逻辑 ──

    def _get_aggregated(self, market: str, symbol: str, timeframe: str,
                        start: datetime, end: datetime) -> list[dict]:
        """从 15m 数据聚合成大周期 bar，缺失段前向填充"""
        interval = _TF_INTERVALS[timeframe]

        # 查 15m 原始数据，过滤非交易日
        raw_15m = self._query_raw(market, symbol, "15m", start, end)
        raw_15m = _filter_trading_days(raw_15m) if raw_15m else []
        raw_index = _bars_to_dict(raw_15m) if raw_15m else {}

        # 生成期望的大周期时间序列
        expected = _generate_expected_times(start, end, timeframe)
        if not expected:
            return raw_15m or []

        result = []
        last_close = None

        for t in expected:
            # 找出这个 bar 时间窗口内的所有 15m bar
            window_bars = []
            for offset_min in range(0, interval, 15):
                sub_t = t - timedelta(minutes=(interval - 15 - offset_min))
                if sub_t in raw_index:
                    window_bars.append(raw_index[sub_t])

            if window_bars:
                agg = _aggregate_bars(window_bars, t)
                last_close = agg["close"]
                result.append(agg)
            else:
                if last_close is None:
                    continue
                result.append({
                    "time": t,
                    "open": last_close,
                    "high": last_close,
                    "low": last_close,
                    "close": last_close,
                    "volume": 0,
                })

        return result

    # ── 前向填充 ──

    @staticmethod
    def _fill_gaps(raw: list[dict], expected: list[datetime]) -> list[dict]:
        """用前向填充补齐缺失 bar"""
        raw_index = _bars_to_dict(raw)
        result = []
        last_close = None

        for t in expected:
            if t in raw_index:
                bar = raw_index[t]
                last_close = bar["close"]
                result.append(bar)
            else:
                if last_close is None:
                    continue
                result.append({
                    "time": t,
                    "open": last_close,
                    "high": last_close,
                    "low": last_close,
                    "close": last_close,
                    "volume": 0,
                })

        return result

    # ── DB 查询 ──

    def _query_raw(self, market: str, symbol: str, timeframe: str,
                   start: datetime, end: datetime) -> list[dict]:
        """从 db 读原始 K 线"""
        import logging
        logger = logging.getLogger(__name__)
        try:
            if hasattr(self.writer, 'query_range'):
                return self.writer.query_range(
                    market, symbol, timeframe,
                    start=start, end=end
                )
            rows = self.writer.query(market, symbol, timeframe, limit=0)
            if not rows:
                return []
            start_d = start.astimezone(TZ_SH) if start.tzinfo else start.replace(tzinfo=TZ_SH)
            end_d = end.astimezone(TZ_SH) if end.tzinfo else end.replace(tzinfo=TZ_SH)
            filtered = []
            for r in rows:
                t = r["time"]
                if isinstance(t, datetime):
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=TZ_SH)
                    if start_d <= t <= end_d:
                        filtered.append(r)
            return filtered
        except Exception as e:
            logger.warning(f"查询 {market}/{symbol}/{timeframe} 失败: {e}")
            return []
