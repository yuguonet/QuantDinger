# -*- coding: utf-8 -*-
"""
mootdx 本地通达信数据文件读取

适用于已下载到本地的通达信 vipdoc 数据，无需联网。

功能              | 方法             | 说明
──────────────────────────────────────────────────
日线数据         | daily()          | 读取 lday/*.day 文件
1 分钟线         | minute()         | 读取 minline/*.lc1 文件
5 分钟线         | fzline()         | 读取 fzline/*.lc5 文件
扩展市场日线     | ext_daily()      | 扩展市场日线
扩展市场分钟线   | ext_minute()     | 扩展市场分钟线
板块数据         | block()          | 板块 .dat 文件
自定义板块       | block_custom()   | 自定义板块

需要本地通达信安装目录 (tdxdir)。
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_reader(tdxdir: str = "C:/new_tdx", market: str = "std"):
    """获取 Reader 实例"""
    from mootdx.reader import Reader
    return Reader.factory(market=market, tdxdir=tdxdir)


# ================================================================
# 标准市场（A 股）
# ================================================================

def daily(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取本地日线数据

    :param symbol: 股票代码
    :param tdxdir: 通达信安装目录
    :return: DataFrame

    文件路径: {tdxdir}/vipdoc/{sh|sz}/lday/{sh|sz}{code}.day
    """
    reader = _get_reader(tdxdir)
    return reader.daily(symbol=symbol)


def minute(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取本地 1 分钟线数据

    :param symbol: 股票代码
    :param tdxdir: 通达信安装目录
    :return: DataFrame

    文件路径: {tdxdir}/vipdoc/{sh|sz}/minline/{sh|sz}{code}.lc1
    """
    reader = _get_reader(tdxdir)
    return reader.minute(symbol=symbol)


def fzline(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取本地 5 分钟线数据

    :param symbol: 股票代码
    :param tdxdir: 通达信安装目录
    :return: DataFrame

    文件路径: {tdxdir}/vipdoc/{sh|sz}/fzline/{sh|sz}{code}.lc5
    """
    reader = _get_reader(tdxdir)
    return reader.fzline(symbol=symbol)


# ================================================================
# 扩展市场
# ================================================================

def ext_daily(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取扩展市场本地日线

    :param symbol: 证券代码（含市场前缀，如 '47#IF2401'）
    :param tdxdir: 通达信安装目录
    :return: DataFrame
    """
    reader = _get_reader(tdxdir, market="ext")
    return reader.daily(symbol=symbol)


def ext_minute(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取扩展市场本地分钟线

    :param symbol: 证券代码
    :param tdxdir: 通达信安装目录
    :return: DataFrame
    """
    reader = _get_reader(tdxdir, market="ext")
    return reader.minute(symbol=symbol)


def ext_fzline(symbol: str = "", tdxdir: str = "C:/new_tdx") -> pd.DataFrame:
    """
    读取扩展市场本地 5 分钟线

    :param symbol: 证券代码
    :param tdxdir: 通达信安装目录
    :return: DataFrame
    """
    reader = _get_reader(tdxdir, market="ext")
    return reader.fzline(symbol=symbol)


# ================================================================
# 板块数据（本地文件）
# ================================================================

def block(
    symbol: str = "block.dat",
    tdxdir: str = "C:/new_tdx",
    group: bool = False,
) -> pd.DataFrame:
    """
    解析本地板块 .dat 文件

    :param symbol: 板块文件名
    :param tdxdir: 通达信安装目录
    :param group:  是否分组
    :return: DataFrame
    """
    reader = _get_reader(tdxdir)
    return reader.block(symbol=symbol, group=group)


def block_custom(
    name: Optional[str] = None,
    symbol: Optional[List[str]] = None,
    tdxdir: str = "C:/new_tdx",
    group: bool = False,
) -> pd.DataFrame:
    """
    自定义板块操作

    :param name:   板块名称（查询时用）
    :param symbol: 股票代码列表（创建时用）
    :param tdxdir: 通达信安装目录
    :param group:  是否分组
    :return: DataFrame 或 bool
    """
    reader = _get_reader(tdxdir)
    return reader.block_new(name=name, symbol=symbol, group=group)
