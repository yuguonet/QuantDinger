# -*- coding: utf-8 -*-
"""
mootdx 财务 / 公告 / F10 接口

功能              | 方法                    | 说明
──────────────────────────────────────────────────────────
财务指标         | finance()               | 每股收益/净资产/营收等
除权除息         | xdxr()                  | 分红/配股/送转信息
公司信息目录     | f10_category()          | F10 公告标题列表
公司信息详情     | f10_content()           | F10 公告正文内容
一键获取全部 F10 | f10_all()               | 目录+内容一次全拿
财务文件下载     | download_finance_file() | 下载通达信财务文件
财务文件列表     | finance_files()         | 可下载的财务文件列表
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from .client import get_std_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 财务指标
# ================================================================

def finance(symbol: str = "000001", **kwargs) -> pd.DataFrame:
    """
    获取个股财务指标

    :param symbol: 股票代码
    :return: DataFrame

    返回字段 (部分):
        code, 流通股本, 总股本, 资产负债率, 每股收益, 每股净资产,
        营业收入, 净利润, 市盈率, 市净率, ...
    """
    client = get_std_client()
    return client.finance(symbol=symbol, **kwargs)


def xdxr(symbol: str = "", **kwargs) -> pd.DataFrame:
    """
    获取除权除息信息

    :param symbol: 股票代码
    :return: DataFrame

    返回字段:
        year, month, day, category(1=除权除息), fenhong(分红),
        peigu(配股), peigujia(配股价), songzhuangu(送转股),
        suogu(缩股), xingquanjia(行权价), fenshu, ...

    category 含义:
        1  = 除权除息
        11 = ETF 缩股/扩股
    """
    client = get_std_client()
    return client.xdxr(symbol=symbol, **kwargs)


# ================================================================
# F10 公司信息
# ================================================================

def f10_category(symbol: str = "", **kwargs) -> list:
    """
    获取公司信息目录（F10 公告标题列表）

    :param symbol: 股票代码
    :return: list[dict]，每个元素含 name/filename/start/length

    示例:
        categories = f10_category('600519')
        # [{'name': '公司概况', 'filename': '...', 'start': 0, 'length': 1234}, ...]
    """
    client = get_std_client()
    return client.F10C(symbol=symbol)


def f10_content(
    symbol: str = "",
    name: str = "",
    **kwargs,
) -> Optional[Dict[str, str]]:
    """
    获取公司信息详情（F10 公告正文）

    :param symbol: 股票代码
    :param name:   公告标题（为空则获取全部）
    :return: dict[name] = content，或单条内容

    示例:
        # 获取全部 F10
        all_info = f10_content('600519')

        # 获取指定标题
        overview = f10_content('600519', name='公司概况')
    """
    client = get_std_client()
    return client.F10(symbol=symbol, name=name)


def f10_all(symbol: str = "") -> Dict[str, object]:
    """
    一键获取 F10 全部信息（目录 + 内容）

    :param symbol: 股票代码
    :return: dict { 'categories': [...], 'content': {name: text} }
    """
    categories = f10_category(symbol)
    content = f10_content(symbol)
    return {
        "categories": categories,
        "content": content,
    }


# ================================================================
# 财务数据文件下载（Affair）
# ================================================================

def finance_files() -> list:
    """
    获取可下载的财务文件列表

    :return: list[dict]，每个元素含 filename/hash/filesize
    """
    from mootdx.affair import Affair
    return Affair.files()


def download_finance_file(
    downdir: str = ".",
    filename: str = "",
    **kwargs,
) -> Optional[pd.DataFrame]:
    """
    下载并解析财务数据文件

    :param downdir:   下载目录
    :param filename:  文件名（如 'gpcw20240630.zip'）
    :return: DataFrame 或 None

    文件名格式: gpcw{YYYYMMDD}.zip
    可先用 finance_files() 查看可用文件列表。
    """
    from mootdx.affair import Affair
    return Affair.parse(downdir=downdir, filename=filename, **kwargs)


def fetch_finance_file(downdir: str = ".", filename: str = "") -> bool:
    """
    仅下载财务文件（不解析）

    :param downdir:  下载目录
    :param filename: 文件名
    :return: True/False
    """
    from mootdx.affair import Affair
    return Affair.fetch(downdir=downdir, filename=filename)
