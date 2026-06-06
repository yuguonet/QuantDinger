# -*- coding: utf-8 -*-
"""
mootdx 实时行情接口

功能           | 方法              | 说明
─────────────────────────────────────────────────
实时日行情     | quotes()          | 最新价/涨跌幅/成交量/买卖五档
批量行情       | quotes()          | 传入列表即可批量获取
扩展市场行情   | ext_quote()       | 期货/外汇/港股等扩展市场

返回 pandas DataFrame，可直接 .to_dict('records') 转字典。
"""

from __future__ import annotations

from typing import List, Optional, Union

import pandas as pd

from .client import get_std_client, get_ext_client
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# A 股实时行情
# ================================================================

def quotes(symbol: Union[str, List[str]], **kwargs) -> pd.DataFrame:
    """
    获取 A 股实时日行情（含买卖五档）

    :param symbol: 股票代码，支持单只 '600519' 或列表 ['600519', '000001']
    :return: DataFrame

    返回字段:
        code, open, high, low, close, last_close, vol, amount,
        bid1~5, ask1~5, bid1_vol~5_vol, ask1_vol~5_vol, ...
    """
    client = get_std_client()
    return client.quotes(symbol=symbol, **kwargs)


def batch_quotes(symbols: List[str], batch_size: int = 80) -> pd.DataFrame:
    """
    批量获取实时行情（自动分批，每批最多 80 只）

    :param symbols: 股票代码列表
    :param batch_size: 每批大小（默认 80，pytdx 协议硬限）
    :return: DataFrame
    """
    if not symbols:
        return pd.DataFrame()

    client = get_std_client()
    frames = []
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        df = client.quotes(symbol=batch)
        if df is not None and not df.empty:
            frames.append(df)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ================================================================
# 扩展市场行情（期货/外汇/港股等）
# ================================================================

def ext_quote(market: int, symbol: str, **kwargs) -> pd.DataFrame:
    """
    获取扩展市场五档行情

    :param market: 市场 ID（如 28=港股, 33=深证扩展, 47=期货 等）
    :param symbol: 证券代码
    :return: DataFrame
    """
    client = get_ext_client()
    return client.quote(market=market, symbol=symbol, **kwargs)


def ext_markets(**kwargs) -> pd.DataFrame:
    """获取扩展市场列表"""
    client = get_ext_client()
    return client.markets(**kwargs)


def ext_instruments(start: int = 0, offset: int = 800, **kwargs) -> pd.DataFrame:
    """
    获取扩展市场代码列表

    :param start: 起始位置
    :param offset: 获取数量
    :return: DataFrame
    """
    client = get_ext_client()
    return client.instrument(start=start, offset=offset, **kwargs)


def ext_instrument_count() -> int:
    """获取扩展市场商品数量"""
    client = get_ext_client()
    return client.instrument_count()


def ext_all_instruments(**kwargs) -> pd.DataFrame:
    """获取扩展市场全部代码列表（自动翻页）"""
    client = get_ext_client()
    return client.instruments(**kwargs)
