# -*- coding: utf-8 -*-
"""
mootdx 板块数据接口

功能                | 方法                | 说明
──────────────────────────────────────────────────────
行业板块           | block_industry()    | 申万/通达信行业分类
概念板块           | block_concept()     | 概念题材分类
地域板块           | block_region()      | 省市地域分类
指数板块           | block_index()       | 指数成分板块
风格板块           | block_style()       | 风格分类
全部板块           | block_all()         | 全部板块合并
自定义板块         | block_custom()      | 通达信自定义板块
板块文件解析       | parse_block()       | 解析任意 .dat 板块文件
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from .client import get_std_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 标准板块（在线获取）
# ================================================================

def block_industry(**kwargs) -> pd.DataFrame:
    """
    获取行业板块数据

    :return: DataFrame
    """
    return _get_block("block.dat", **kwargs)


def block_concept(**kwargs) -> pd.DataFrame:
    """
    获取概念板块数据

    :return: DataFrame
    """
    return _get_block("block_gn.dat", **kwargs)


def block_region(**kwargs) -> pd.DataFrame:
    """
    获取地域板块数据

    :return: DataFrame
    """
    return _get_block("block_fg.dat", **kwargs)


def block_index(**kwargs) -> pd.DataFrame:
    """
    获取指数板块数据

    :return: DataFrame
    """
    return _get_block("block_zs.dat", **kwargs)


def block_style(**kwargs) -> pd.DataFrame:
    """
    获取风格板块数据

    :return: DataFrame
    """
    return _get_block("block_fg.dat", **kwargs)


def block_all(**kwargs) -> pd.DataFrame:
    """
    获取全部板块数据（合并行业+概念+地域+指数）

    :return: DataFrame
    """
    frames = []
    for name in ["block.dat", "block_gn.dat", "block_fg.dat", "block_zs.dat"]:
        try:
            df = _get_block(name, **kwargs)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception as e:
            logger.debug("[Mootdx] 获取板块 %s 失败: %s", name, e)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ================================================================
# 自定义板块（本地通达信文件）
# ================================================================

def block_custom(
    tdxdir: str = "C:/new_tdx",
    name: Optional[str] = None,
    group: bool = False,
) -> pd.DataFrame:
    """
    获取通达信自定义板块数据

    :param tdxdir: 通达信安装目录
    :param name:   板块名称（None=全部）
    :param group:  是否分组解析
    :return: DataFrame 或 list
    """
    from mootdx.reader import Reader
    reader = Reader.factory(market="std", tdxdir=tdxdir)
    return reader.block_new(name=name, group=group)


def create_custom_block(
    tdxdir: str = "C:/new_tdx",
    name: str = "",
    symbol: Optional[List[str]] = None,
) -> bool:
    """
    创建自定义板块

    :param tdxdir: 通达信安装目录
    :param name:   板块名称
    :param symbol: 股票代码列表
    :return: bool
    """
    from mootdx.reader import Reader
    reader = Reader.factory(market="std", tdxdir=tdxdir)
    return reader.block_new(name=name, symbol=symbol)


# ================================================================
# 板块文件解析（本地 .dat 文件）
# ================================================================

def parse_block(
    tdxdir: str = "C:/new_tdx",
    symbol: str = "block.dat",
    group: bool = False,
) -> pd.DataFrame:
    """
    解析通达信板块 .dat 文件

    :param tdxdir: 通达信安装目录
    :param symbol: 板块文件名
    :param group:  是否分组
    :return: DataFrame
    """
    from mootdx.reader import Reader
    reader = Reader.factory(market="std", tdxdir=tdxdir)
    return reader.block(symbol=symbol, group=group)


# ================================================================
# 内部辅助
# ================================================================

def _get_block(tofile: str = "block.dat", **kwargs) -> pd.DataFrame:
    """通过在线接口获取板块数据"""
    client = get_std_client()
    return client.block(tofile=tofile, **kwargs)
