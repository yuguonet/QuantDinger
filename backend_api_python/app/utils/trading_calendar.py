"""
交易日历模块
基于 akshare 获取沪深交易所交易日历，提供精确的交易日判断。

日历数据以 pickle 单文件存储（加载快、体积小），过滤 2000 年之前的数据。
文件不存在时调 akshare 获取并保存。
"""

import os
import pickle
import logging
from datetime import datetime, timedelta, time as dt_time
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# 缓存目录：app/data/trading_calendar/
_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trading_calendar")
_CACHE_FILE = os.path.join(_DIR, "trading_days.pkl")

# 内存缓存：避免重复反序列化
_cached_dates: Set[str] = set()
_loaded = False


def _load() -> Set[str]:
    """加载交易日集合（内存缓存 + pickle 文件）"""
    global _cached_dates, _loaded
    if _loaded:
        return _cached_dates

    if os.path.isfile(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "rb") as f:
                _cached_dates = pickle.load(f)
            _loaded = True
            logger.info(f"交易日历已加载，共 {len(_cached_dates)} 个交易日")
            return _cached_dates
        except Exception as e:
            logger.error(f"读取 {_CACHE_FILE} 失败: {e}")

    # 文件不存在或损坏 → 拉取
    _fetch_and_save()
    return _cached_dates


def _fetch_and_save():
    """从 akshare 获取交易日，过滤 2000 年前，保存 pickle"""
    global _cached_dates, _loaded
    import akshare as ak

    logger.info("从 akshare 获取交易日历...")
    df = ak.tool_trade_date_hist_sina()

    dates: Set[str] = set()
    for val in df["trade_date"]:
        s = str(val).strip()
        if len(s) == 8 and s.isdigit():
            s = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        # 过滤 2000 年之前
        if s >= "2000-01-01":
            dates.add(s)

    if dates:
        os.makedirs(_DIR, exist_ok=True)
        with open(_CACHE_FILE, "wb") as f:
            pickle.dump(dates, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info(f"共 {len(dates)} 个交易日（2000-至今），已保存到 {_CACHE_FILE}")

    _cached_dates = dates
    _loaded = True


# ─── 公共 API ───────────────────────────────────────────────


def is_trading_day(date: str) -> bool:
    """判断是否为交易日 (YYYY-MM-DD 或 YYYYMMDD)"""
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return date in _load()


def is_trading_day_today() -> bool:
    return is_trading_day(datetime.now().strftime("%Y-%m-%d"))


def prev_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """前 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    all_dates = sorted(_load())
    result = []
    for d in reversed(all_dates):
        if d < date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    # 数据不够（极端情况），逐天回退
    dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _load():
            result.append(s)
        dt -= timedelta(days=1)
    return result[-1]


def next_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """后 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    all_dates = sorted(_load())
    result = []
    for d in all_dates:
        if d > date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _load():
            result.append(s)
        dt += timedelta(days=1)
    return result[-1]

def last_finish_trading_day(ref_dt: Optional[str] = None) -> str:
    """返回已经结束的最近一个交易日。

    以 ref_dt 为参考点（默认当前时间）：
      - ref_dt 所在日期是交易日，且时间 >= 15:00:00 → 返回该日期
      - 否则（非交易日，或交易日但 < 15:00:00）→ 返回上一个交易日

    Args:
        ref_dt: 参考时刻字符串，支持以下格式：
                - "YYYY-MM-DD HH:MM:SS"
                - "YYYY-MM-DD HH:MM"
                - "YYYY-MM-DD"（时间视为 00:00:00）
                若为 None 则使用当前时间。

    Returns:
        日期字符串（YYYY-MM-DD）。
    """
    if ref_dt is None:
        dt = datetime.now()
    else:
        # 尝试完整日期时间格式，失败则按纯日期解析（时间默认 00:00:00）
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ref_dt, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析日期字符串: {ref_dt}")

    ref_date = dt.strftime("%Y-%m-%d")
    cutoff = dt.replace(hour=15, minute=0, second=0, microsecond=0)

    if is_trading_day(ref_date) and dt >= cutoff:
        return ref_date
    return prev_trading_day(ref_date)

def trade_date_range(start_date: str, end_date: str) -> List[str]:
    """范围内的交易日列表"""
    return sorted(d for d in _load() if start_date <= d <= end_date)


def trading_days_count(start_date: str, end_date: str) -> int:
    return len(trade_date_range(start_date, end_date))


def is_business_day(date: str) -> bool:
    return is_trading_day(date)
