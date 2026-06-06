# -*- coding: utf-8 -*-
"""
mootdx 分时数据接口

功能              | 方法              | 说明
──────────────────────────────────────────────────
当日分时         | minute()          | 当日实时分时线
历史分时         | minutes()         | 指定日期的分时线
扩展市场当日分时 | ext_minute()      | 期货/外汇等
扩展市场历史分时 | ext_minutes()     | 期货/外汇历史分时
"""

from __future__ import annotations

import pandas as pd

from .client import get_std_client, get_ext_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# A 股分时数据
# ================================================================

def minute(symbol: str = None, **kwargs) -> pd.DataFrame:
    """
    获取当日实时分时数据

    :param symbol: 股票代码
    :return: DataFrame

    返回字段:
        datetime, price, vol(成交量), amount(成交额)
    """
    client = get_std_client()
    return client.minute(symbol=symbol, **kwargs)


def minutes(symbol: str = None, date: str = "20240101", **kwargs) -> pd.DataFrame:
    """
    获取历史分时数据

    :param symbol: 股票代码
    :param date:   日期 'YYYYMMDD'
    :return: DataFrame

    示例:
        df = minutes('600519', date='20240601')
    """
    client = get_std_client()
    return client.minutes(symbol=symbol, date=date, **kwargs)


# ================================================================
# 扩展市场分时数据
# ================================================================

def ext_minute(market: int, symbol: str, **kwargs) -> pd.DataFrame:
    """
    获取扩展市场当日分时

    :param market: 市场 ID
    :param symbol: 证券代码
    :return: DataFrame
    """
    client = get_ext_client()
    return client.minute(market=market, symbol=symbol, **kwargs)


def ext_minutes(market: int, symbol: str, date: str = "", **kwargs) -> pd.DataFrame:
    """
    获取扩展市场历史分时

    :param market: 市场 ID
    :param symbol: 证券代码
    :param date:   日期 'YYYYMMDD'
    :return: DataFrame
    """
    client = get_ext_client()
    return client.minutes(market=market, symbol=symbol, date=date, **kwargs)
