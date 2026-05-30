# -*- coding: utf-8 -*-
"""
optimizer 共享工具 — 日线前复权转换

DB 存储不复权数据，optimizer 回测/分析需要前复权数据。
此模块提供 DataFrame 级别的前复权转换。
"""

import pandas as pd
from app.data_sources.provider.adjustment import fetch_qfq_factors, _to_sina_code, _build_factor_lookup, _find_factor, _extract_date


def adjust_daily_df(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """将不复权日线 DataFrame 转为前复权。

    Args:
        df: 含 open/high/low/close/volume 列，index 为 DatetimeIndex 或含 time 列
        code: 股票代码（纯数字如 "000001" 或带前缀如 "SZ000001"）

    Returns:
        前复权后的 DataFrame（新对象），无因子时返回原 DataFrame
    """
    if df is None or df.empty:
        return df

    factors = fetch_qfq_factors(code)
    factor_map, sorted_dates, latest_ex = _build_factor_lookup(factors)
    if not factor_map:
        return df

    result = df.copy()

    # 统一获取日期序列
    if isinstance(result.index, pd.DatetimeIndex):
        dates = result.index.strftime("%Y-%m-%d")
    elif "time" in result.columns:
        dates = pd.to_datetime(result["time"]).dt.strftime("%Y-%m-%d")
    else:
        dates = result.index.strftime("%Y-%m-%d") if hasattr(result.index, 'strftime') else None
        if dates is None:
            return df

    # 逐行查找因子并调整 OHLC
    for col in ("open", "high", "low", "close"):
        if col not in result.columns:
            continue
        factors_series = dates.apply(
            lambda d: _find_factor(sorted_dates, factor_map, d, latest_ex)
        )
        mask = factors_series != 1.0
        if mask.any():
            result.loc[mask, col] = (result.loc[mask, col] / factors_series[mask]).round(4)

    return result
