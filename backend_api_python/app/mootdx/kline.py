# -*- coding: utf-8 -*-
"""
mootdx K 线数据接口

功能              | 方法           | 说明
──────────────────────────────────────────────────
个股 K 线         | bars()         | 1m/5m/15m/30m/1H/日/周/月/季/年
指数 K 线         | index_bars()   | 上证/深证/创业板等指数
按日期范围取 K 线 | k()            | 传入起止日期，自动拼接
扩展市场 K 线     | ext_bars()     | 期货/外汇等
"""

from __future__ import annotations

from typing import Optional, Union

import pandas as pd

from .client import get_std_client, get_ext_client
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 周期映射: 用户友好名 → mootdx frequency 编号
FREQUENCY_MAP = {
    "1m": 8, "5m": 0, "15m": 1, "30m": 2,
    "1h": 3, "1H": 3,
    "day": 9, "1d": 9, "1D": 9, "daily": 9,
    "week": 5, "1w": 5, "1W": 5, "weekly": 5,
    "mon": 6, "month": 6, "monthly": 6,
    "3mon": 10, "quarter": 10,
    "year": 11, "yearly": 11,
    # 数字直通
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "10": 10, "11": 11,
}


def _freq(frequency) -> int:
    """将周期名转为 mootdx frequency 编号"""
    if isinstance(frequency, int):
        return frequency
    key = str(frequency).strip().lower()
    if key in FREQUENCY_MAP:
        return FREQUENCY_MAP[key]
    raise ValueError(f"不支持的周期: {frequency}, 可选: {list(FREQUENCY_MAP.keys())}")


# ================================================================
# 个股 K 线
# ================================================================

def bars(
    symbol: str = "000001",
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    获取个股实时 K 线

    :param symbol:    股票代码 (如 '600519')
    :param frequency: 周期 ('1m','5m','15m','30m','1h','day','week','mon','3mon','year')
    :param start:     起始位置 (0=最新, 越大越早)
    :param offset:    获取条数 (最大 800)
    :return: DataFrame (datetime, open, high, low, close, vol, amount)

    示例:
        df = bars('600519', 'day', offset=100)       # 最近 100 根日 K
        df = bars('000001', '5m', offset=200)        # 最近 200 根 5 分钟 K
    """
    client = get_std_client()
    return client.bars(symbol=symbol, frequency=_freq(frequency), start=start, offset=offset, **kwargs)


def k(
    symbol: str = "",
    begin: Optional[str] = None,
    end: Optional[str] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    按日期范围获取日 K 线（自动拼接多页）

    :param symbol: 股票代码
    :param begin:  开始日期 'YYYY-MM-DD'
    :param end:    结束日期 'YYYY-MM-DD'
    :return: DataFrame

    示例:
        df = k('600519', begin='2024-01-01', end='2024-06-30')
    """
    client = get_std_client()
    return client.k(symbol=symbol, begin=begin, end=end, **kwargs)


# ================================================================
# 指数 K 线
# ================================================================

def index_bars(
    symbol: str = "000001",
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    获取指数 K 线

    :param symbol:    指数代码 (如 '000001'=上证指数, '399001'=深证成指)
    :param frequency: 周期
    :param start:     起始位置
    :param offset:    获取条数
    :return: DataFrame

    常用指数代码:
        000001  上证指数
        399001  深证成指
        399006  创业板指
        000300  沪深300
        000016  上证50
        000905  中证500
    """
    client = get_std_client()
    return client.index_bars(symbol=symbol, frequency=_freq(frequency), start=start, offset=offset, **kwargs)


def index(
    symbol: str = "000001",
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """index_bars 的别名，与 mootdx 原生方法名一致"""
    return index_bars(symbol=symbol, frequency=frequency, start=start, offset=offset, **kwargs)


# ================================================================
# 扩展市场 K 线
# ================================================================

def ext_bars(
    market: int,
    symbol: str,
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    获取扩展市场 K 线（期货/外汇/港股等）

    :param market:    市场 ID
    :param symbol:    证券代码
    :param frequency: 周期
    :param start:     起始位置
    :param offset:    获取条数
    :return: DataFrame
    """
    client = get_ext_client()
    return client.bars(frequency=_freq(frequency), market=market, symbol=symbol, start=start, offset=offset, **kwargs)
