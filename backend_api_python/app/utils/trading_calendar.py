"""
交易日历模块
基于 akshare 获取沪深交易所交易日历，提供精确的交易日判断。

日历数据按年存 feather 文件，优先从文件加载；
文件不存在时调 akshare 获取整年数据并保存。
"""

import os
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# feather 文件存放目录，默认在模块同级 calendar/ 子目录
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar")


def _file_path(year: int) -> str:
    return os.path.join(_DIR, f"{year}.feather")


def _load_year(year: int) -> Set[str]:
    """从 feather 文件加载某年交易日集合，文件不存在返回空 set"""
    path = _file_path(year)
    if not os.path.isfile(path):
        return set()
    try:
        import pandas as pd
        df = pd.read_feather(path)
        return set(df["trade_date"].astype(str).tolist())
    except Exception as e:
        logger.error(f"读取 {path} 失败: {e}")
        return set()


def _fetch_and_save(year: int) -> Set[str]:
    """从 akshare 获取整年交易日，保存 feather 文件，返回日期集合"""
    import akshare as ak
    import pandas as pd

    logger.info(f"从 akshare 获取 {year} 年交易日历...")
    df = ak.tool_trade_date_hist_sina()

    prefix = str(year)
    dates = []
    for val in df["trade_date"]:
        s = str(val).strip()
        if len(s) == 8 and s.isdigit():
            s = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if s.startswith(prefix):
            dates.append(s)
    dates.sort()

    if dates:
        os.makedirs(_DIR, exist_ok=True)
        out = pd.DataFrame({"trade_date": dates})
        out.to_feather(_file_path(year))
        logger.info(f"{year} 年共 {len(dates)} 个交易日，已保存到 {_file_path(year)}")

    return set(dates)


def _ensure_year(year: int) -> Set[str]:
    """确保某年数据可用，优先文件，文件不存在则请求 akshare"""
    dates = _load_year(year)
    if not dates:
        dates = _fetch_and_save(year)
    return dates


# ─── 公共 API ───────────────────────────────────────────────


def is_trading_day(date: str) -> bool:
    """判断是否为交易日 (YYYY-MM-DD 或 YYYYMMDD)"""
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return date in _ensure_year(int(date[:4]))


def is_trading_day_today() -> bool:
    return is_trading_day(datetime.now().strftime("%Y-%m-%d"))


def prev_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """前 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    year = int(date[:4])
    # 可能跨年，合并当前年和前一年的交易日
    dates = sorted(_ensure_year(year) | _ensure_year(year - 1))

    result = []
    for d in reversed(dates):
        if d < date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    # 数据不够（极端情况），逐天回退
    dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _ensure_year(dt.year):
            result.append(s)
        dt -= timedelta(days=1)
    return result[-1]


def next_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """后 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    year = int(date[:4])
    dates = sorted(_ensure_year(year) | _ensure_year(year + 1))

    result = []
    for d in dates:
        if d > date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _ensure_year(dt.year):
            result.append(s)
        dt += timedelta(days=1)
    return result[-1]


def trade_date_range(start_date: str, end_date: str) -> List[str]:
    """范围内的交易日列表"""
    y1, y2 = int(start_date[:4]), int(end_date[:4])
    result = []
    for y in range(y1, y2 + 1):
        for d in _ensure_year(y):
            if start_date <= d <= end_date:
                result.append(d)
    result.sort()
    return result


def trading_days_count(start_date: str, end_date: str) -> int:
    return len(trade_date_range(start_date, end_date))


def is_business_day(date: str) -> bool:
    return is_trading_day(date)
