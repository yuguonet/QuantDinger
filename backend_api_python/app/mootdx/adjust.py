# -*- coding: utf-8 -*-
"""
mootdx 复权接口

功能              | 方法             | 说明
──────────────────────────────────────────────────
前复权           | qfq()            | 前复权 K 线
后复权           | hfq()            | 后复权 K 线
复权因子         | adjust_factor()  | 同花顺复权因子（按年）
原始复权计算     | reversion()      | 用 xdxr 数据手动复权
"""

from __future__ import annotations

from typing import Optional, Union

import pandas as pd

from .client import get_std_client
from . import kline as _kline
from . import finance as _finance
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 复权 K 线（基于同花顺复权因子）
# ================================================================

def qfq(
    symbol: str = "",
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    前复权 K 线

    :param symbol:    股票代码
    :param frequency: 周期
    :param start:     起始位置
    :param offset:    获取条数
    :return: DataFrame（复权后的 OHLCV）

    内部流程:
        1. 获取不复权 K 线
        2. 获取复权因子
        3. 应用复权计算
    """
    raw = _kline.bars(symbol=symbol, frequency=frequency, start=start, offset=offset, **kwargs)
    if raw is None or raw.empty:
        return raw
    return _apply_factor(symbol, raw, method="qfq")


def hfq(
    symbol: str = "",
    frequency: Union[str, int] = 9,
    start: int = 0,
    offset: int = 800,
    **kwargs,
) -> pd.DataFrame:
    """
    后复权 K 线

    :param symbol:    股票代码
    :param frequency: 周期
    :param start:     起始位置
    :param offset:    获取条数
    :return: DataFrame（复权后的 OHLCV）
    """
    raw = _kline.bars(symbol=symbol, frequency=frequency, start=start, offset=offset, **kwargs)
    if raw is None or raw.empty:
        return raw
    return _apply_factor(symbol, raw, method="hfq")


# ================================================================
# 复权因子（同花顺源）
# ================================================================

def adjust_factor(
    symbol: str = "",
    year: Optional[int] = None,
    factor: str = "01",
    **kwargs,
) -> pd.DataFrame:
    """
    获取同花顺复权因子

    :param symbol: 股票代码
    :param year:   年份（None=当年）
    :param factor: '01'=前复权因子, '02'=后复权因子, 'before'/'after' 也可以
    :return: DataFrame

    返回字段:
        date, open, high, low, close, volume, amount, adjust
    """
    from mootdx.contrib.adjust import get_adjust_year
    return get_adjust_year(symbol=symbol, year=year, factor=factor)


# ================================================================
# 基于 xdxr 的原始复权计算
# ================================================================

def reversion(
    symbol: str = "",
    stock_data: Optional[pd.DataFrame] = None,
    xdxr_data: Optional[pd.DataFrame] = None,
    type_: str = "qfq",
) -> pd.DataFrame:
    """
    使用除权除息数据进行复权计算

    :param symbol:     股票代码
    :param stock_data: 不复权 K 线 DataFrame（需含 open/high/low/close/volume）
    :param xdxr_data:  除权除息 DataFrame（xdxr() 返回）
    :param type_:      'qfq'=前复权, 'hfq'=后复权
    :return: DataFrame

    如果 stock_data 或 xdxr_data 为空，会自动获取。
    """
    if stock_data is None:
        stock_data = _kline.bars(symbol=symbol, frequency=9, offset=800)
    if xdxr_data is None:
        xdxr_data = _finance.xdxr(symbol=symbol)

    if stock_data is None or stock_data.empty:
        return stock_data
    if xdxr_data is None or xdxr_data.empty:
        return stock_data

    from mootdx.tools.reversion import reversion as _do_reversion
    return _do_reversion(symbol=symbol, stock_data=stock_data, xdxr=xdxr_data, type_=type_)


# ================================================================
# 内部辅助
# ================================================================

def _apply_factor(
    symbol: str,
    raw: pd.DataFrame,
    method: str = "qfq",
) -> pd.DataFrame:
    """应用复权因子到 K 线数据"""
    try:
        from mootdx.tools.reversion import factor_reversion
        return factor_reversion(symbol=symbol, method=method, raw=raw)
    except Exception as e:
        logger.warning("[Mootdx] 复权计算失败 %s: %s, 返回不复权数据", symbol, e)
        return raw
