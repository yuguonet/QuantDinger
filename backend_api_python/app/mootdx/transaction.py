# -*- coding: utf-8 -*-
"""
mootdx 逐笔成交接口

功能              | 方法                | 说明
──────────────────────────────────────────────────────
实时逐笔成交     | transaction()       | 当日分笔成交明细
历史逐笔成交     | transactions()      | 指定日期的分笔成交明细
扩展市场逐笔     | ext_transaction()   | 期货/外汇等
扩展市场历史逐笔 | ext_transactions()  | 期货/外汇历史分笔
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .client import get_std_client, get_ext_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# A 股逐笔成交
# ================================================================

def transaction(
    symbol: str = "",
    start: int = 0,
    offset: int = 2000,
    **kwargs,
) -> pd.DataFrame:
    """
    获取当日分笔成交（逐笔明细）

    :param symbol: 股票代码
    :param start:  起始位置 (0=最新)
    :param offset: 获取条数 (最大 2000)
    :return: DataFrame

    返回字段:
        time, price, vol, num(成交笔数), buyorsell(0=买, 1=卖, 2=中性)

    示例:
        df = transaction('600519')            # 最近 2000 笔
        df = transaction('600519', offset=100) # 最近 100 笔
    """
    client = get_std_client()
    return client.transaction(symbol=symbol, start=start, offset=offset, **kwargs)


def transactions(
    symbol: str = "",
    start: int = 0,
    offset: int = 2000,
    date: str = "20240101",
    **kwargs,
) -> pd.DataFrame:
    """
    获取历史分笔成交（指定日期）

    :param symbol: 股票代码
    :param start:  起始位置
    :param offset: 获取条数
    :param date:   日期 'YYYYMMDD'
    :return: DataFrame

    示例:
        df = transactions('600519', date='20240601')  # 2024-06-01 的逐笔
    """
    client = get_std_client()
    return client.transactions(symbol=symbol, start=start, offset=offset, date=date, **kwargs)


# ================================================================
# 扩展市场逐笔成交
# ================================================================

def ext_transaction(
    market: int,
    symbol: str,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    获取扩展市场分笔成交

    :param market: 市场 ID
    :param symbol: 证券代码
    :param start:  起始位置
    :param offset: 获取条数
    :return: DataFrame
    """
    client = get_ext_client()
    return client.transaction(market=market, symbol=symbol, start=start, offset=offset, **kwargs)


def ext_transactions(
    market: int,
    symbol: str,
    date: str = "",
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    获取扩展市场历史分笔成交

    :param market: 市场 ID
    :param symbol: 证券代码
    :param date:   日期 'YYYYMMDD'
    :param start:  起始位置
    :param offset: 获取条数
    :return: DataFrame
    """
    client = get_ext_client()
    return client.transactions(market=market, symbol=symbol, date=date, start=start, offset=offset, **kwargs)
