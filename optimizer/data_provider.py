"""
data_provider.py — 回测数据接口：读取 K 线，缺失段自动补 bar

在 db_market 之上封装一层，回测引擎只调这个模块。
缺失段用前向填充（last close, volume=0），不落库。

支持时间框架: 1D, 1W, 1m, 5m, 15m, 30m, 60m
"""

from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional

TZ_SH = ZoneInfo("Asia/Shanghai")

# ── A 股交易时段定义 ──

# 15m 标准 bar 时间点（与 tdx_download.py 一致）
_BAR_TIMES_15M = [
    (9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0),
]

# 交易时段：(开始时, 分, 结束时, 分)
_TRADING_SESSIONS = [
    ((9, 30), (11, 30)),   # 上午
    ((13, 0), (15, 0)),    # 下午
]

# 各时间框架的分钟间隔
_TF_INTERVALS = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
}


# 常用时间框架别名
_TF_ALIASES = {
    "D": "1D", "day": "1D", "daily": "1D",
    "W": "1W", "week": "1W", "weekly": "1W",
    "M": "1m", "min": "1m",
}


def _is_trading_day(dt: datetime) -> bool:
    """判断是否为交易日（仅跳过周末，不含节假日）"""
    return dt.weekday() < 5  # 0=周一 ... 4=周五


def _generate_expected_times(start: datetime, end: datetime, timeframe: str) -> list[datetime]:
    """生成 [start, end] 范围内该时间框架的所有期望时间戳（带时区）"""

    # 别名标准化
    timeframe = _TF_ALIASES.get(timeframe, timeframe)

    if timeframe == "1D":
        return _gen_daily(start, end)
    if timeframe == "1W":
        return _gen_weekly(start, end)

    interval = _TF_INTERVALS.get(timeframe)
    if interval is None:
        raise ValueError(f"不支持的时间框架: {timeframe}")

    return _gen_intraday(start, end, interval)


def _gen_daily(start: datetime, end: datetime) -> list[datetime]:
    """生成日线期望时间戳：每个交易日 00:00"""
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    end_naive = end.replace(tzinfo=None) if end.tzinfo else end

    result = []
    current = start_naive.date()
    end_date = end_naive.date()

    while current <= end_date:
        dt = datetime(current.year, current.month, current.day, tzinfo=TZ_SH)
        if _is_trading_day(dt):
            result.append(dt)
        current += timedelta(days=1)

    return result


def _gen_weekly(start: datetime, end: datetime) -> list[datetime]:
    """生成周线期望时间戳：每周最后一个交易日（通常周五）00:00

    规则：以自然周（周一~周日）为单位，取该周内最后一个交易日。
    如果整周都是节假日则跳过。
    """
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    end_naive = end.replace(tzinfo=None) if end.tzinfo else end

    result = []
    # 找到 start 所在周的周一
    current_date = start_naive.date()
    monday = current_date - timedelta(days=current_date.weekday())

    while monday <= end_naive.date():
        # 从该周周五往回找，找到第一个交易日
        for offset in range(4, -1, -1):  # 周五=4, 周四=3, ..., 周一=0
            day = monday + timedelta(days=offset)
            if day > end_naive.date():
                continue
            if day < start_naive.date():
                continue
            dt = datetime(day.year, day.month, day.day, tzinfo=TZ_SH)
            if _is_trading_day(dt):
                result.append(dt)
                break
        monday += timedelta(days=7)

    return result


def _gen_intraday(start: datetime, end: datetime, interval_min: int) -> list[datetime]:
    """生成分钟级期望时间戳（遍历交易时段内的标准 bar 时间点）"""
    start_d = start.astimezone(TZ_SH) if start.tzinfo else start.replace(tzinfo=TZ_SH)
    end_d = end.astimezone(TZ_SH) if end.tzinfo else end.replace(tzinfo=TZ_SH)

    result = []
    current_date = start_d.date()
    end_date = end_d.date()

    while current_date <= end_date:
        if _is_trading_day(datetime(current_date.year, current_date.month,
                                     current_date.day, tzinfo=TZ_SH)):
            for (sh, sm), (eh, em) in _TRADING_SESSIONS:
                # 遍历该时段内所有 bar 时间点
                t = sh * 60 + sm
                t_end = eh * 60 + em
                while t <= t_end:
                    h, m = divmod(t, 60)
                    bar_dt = datetime(current_date.year, current_date.month,
                                      current_date.day, h, m, tzinfo=TZ_SH)
                    if start_d <= bar_dt <= end_d:
                        result.append(bar_dt)
                    t += interval_min
        current_date += timedelta(days=1)

    return result


def _bars_to_dict(bars: list[dict]) -> dict[datetime, dict]:
    """把 bar 列表按 time 建索引（统一转为 aware datetime）"""
    index = {}
    for b in bars:
        t = b["time"]
        if isinstance(t, datetime):
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_SH)
            index[t] = b
    return index


class MarketDataProvider:
    """回测数据接口：读取 K 线，缺失段自动补 bar

    用法:
        provider = MarketDataProvider(writer, market="CNStock")
        bars = provider.get_bars("000001", "15m", start, end)
        # 返回的 bars 时间连续，缺失段已自动填充
    """

    def __init__(self, writer, market: str = "CNStock"):
        """
        Args:
            writer: db_market KlineWriter 实例（需有 query 方法）
            market: 市场名
        """
        self.writer = writer
        self.market = market

    def get_bars(self, code: str, timeframe: str,
                 start: datetime, end: datetime,
                 limit: int = 0) -> list[dict]:
        """读取 K 线，缺失段自动填充

        Args:
            code: 股票代码
            timeframe: "1D", "1W", "15m", "1m", "5m", "30m", "60m"
                       也支持别名: "daily", "weekly", "day", "week"
            start: 起始时间（datetime，带不带时区都行）
            end: 结束时间
            limit: 0=不限制（默认）

        Returns:
            list[dict]: 时间连续的 K 线数据，缺失段已前向填充
        """
        # 从 db 读原始数据
        raw = self._query_raw(code, timeframe, start, end, limit)
        if not raw:
            return []

        # 生成期望时间序列
        expected = _generate_expected_times(start, end, timeframe)
        if not expected:
            return raw

        # 建索引
        raw_index = _bars_to_dict(raw)

        # 填充
        result = []
        last_close = None

        for t in expected:
            if t in raw_index:
                bar = raw_index[t]
                last_close = bar["close"]
                result.append(bar)
            else:
                # 缺失 → 前向填充
                if last_close is None:
                    # 连第一根都没有，跳过
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

    def _query_raw(self, code: str, timeframe: str,
                   start: datetime, end: datetime,
                   limit: int) -> list[dict]:
        """从 db 读原始 K 线"""
        try:
            # 优先用带范围的查询
            if hasattr(self.writer, 'query_range'):
                return self.writer.query_range(
                    self.market, code, timeframe,
                    start=start, end=end
                )
            # fallback: query + 手动过滤
            rows = self.writer.query(
                self.market, code, timeframe, limit=limit or 0
            )
            if not rows:
                return []
            # 按时间范围过滤
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
        except Exception:
            return []
