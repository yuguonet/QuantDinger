# -*- coding: utf-8 -*-
"""
mootdx 股票列表 / 市场统计接口

功能              | 方法             | 说明
──────────────────────────────────────────────────
股票列表         | stocks()         | 沪/深单市场股票列表
全部股票         | stock_all()      | 沪深全部股票列表
市场股票数量     | stock_count()    | 沪/深市场股票数量
交易流量统计     | traffic()        | 服务器流量统计
板块数据         | block()          | 通达信板块文件 (block.dat)
"""

from __future__ import annotations

import pandas as pd

from mootdx.consts import MARKET_SH, MARKET_SZ, MARKET_BJ

from .client import get_std_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# 市场常量导出
MARKET_MAP = {
    "sh": MARKET_SH, "SH": MARKET_SH, "沪": MARKET_SH, "1": MARKET_SH,
    "sz": MARKET_SZ, "SZ": MARKET_SZ, "深": MARKET_SZ, "0": MARKET_SZ,
    "bj": MARKET_BJ, "BJ": MARKET_BJ, "北": MARKET_BJ, "2": MARKET_BJ,
}


def stock_count(market=MARKET_SH) -> int:
    """
    获取市场股票数量

    :param market: 市场代码 (0=深, 1=沪, 2=北)
    :return: int
    """
    client = get_std_client()
    return client.stock_count(market=market)


def stocks(market=MARKET_SH) -> pd.DataFrame:
    """
    获取单市场股票列表

    :param market: 市场代码 (0=深, 1=沪)
    :return: DataFrame

    返回字段:
        code, volunit, decimal_point, name, pre_close
    """
    client = get_std_client()
    return client.stocks(market=market)


def stock_all() -> pd.DataFrame:
    """
    获取沪深全部股票列表（自动合并沪+深）

    :return: DataFrame
    """
    client = get_std_client()
    return client.stock_all()


def traffic() -> dict:
    """
    获取服务器流量统计

    :return: dict
    """
    client = get_std_client()
    return client.traffic()


def block(tofile: str = "block.dat", **kwargs) -> pd.DataFrame:
    """
    获取通达信板块数据

    :param tofile: 板块文件名
        - block.dat   默认板块
        - block_gn.dat 概念板块
        - block_fg.dat 风格板块
        - block_zs.dat 指数板块
    :return: DataFrame
    """
    client = get_std_client()
    return client.block(tofile=tofile, **kwargs)


# ================================================================
# 便捷函数
# ================================================================

def all_stock_codes() -> list:
    """
    获取全部沪深股票代码列表（纯 code list）

    :return: list[str] 如 ['600000', '600001', ...]
    """
    df = stock_all()
    if df is not None and not df.empty and "code" in df.columns:
        return df["code"].tolist()
    return []


def sh_stocks() -> pd.DataFrame:
    """沪市股票列表"""
    return stocks(market=MARKET_SH)


def sz_stocks() -> pd.DataFrame:
    """深市股票列表"""
    return stocks(market=MARKET_SZ)
