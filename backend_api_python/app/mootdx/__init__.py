# -*- coding: utf-8 -*-
"""
mootdx 全功能封装层

基于 mootdx 库（pytdx 二次封装），提供通达信全部数据接口。
自动选择最优服务器，返回 pandas DataFrame。

模块结构:
    client.py       连接管理（自动选最优服务器）
    quotes.py       实时行情（单只/批量/五档/扩展市场）
    kline.py        K 线（个股/指数/全周期/扩展市场）
    transaction.py  逐笔成交（实时/历史/扩展市场）
    minute.py       分时数据（当日/历史/扩展市场）
    finance.py      财务指标/除权除息/F10/财务文件下载
    stock_list.py   股票列表/数量/流量统计
    adjust.py       复权（前复权/后复权/复权因子）
    sector.py       板块（行业/概念/地域/指数/自定义）
    reader.py       本地通达信文件读取（离线分析）

快速使用:
    from app.data_sources.mootdx import quotes, kline, finance

    # 实时行情
    df = quotes.quotes('600519')

    # K 线
    df = kline.bars('600519', 'day', offset=100)
    df = kline.index_bars('000001', 'day')  # 上证指数

    # 逐笔成交
    df = transaction.transaction('600519')

    # 财务
    df = finance.finance('600519')
    df = finance.xdxr('600519')

    # 复权
    df = adjust.qfq('600519', 'day')

    # 板块
    df = sector.block_concept()

    # 本地文件
    df = reader.daily('600519', tdxdir='C:/new_tdx')
"""

from . import (
    adjust,
    client,
    finance,
    kline,
    minute,
    quotes,
    reader,
    sector,
    stock_list,
    transaction,
)

__all__ = [
    "client",
    "quotes",
    "kline",
    "transaction",
    "minute",
    "finance",
    "stock_list",
    "adjust",
    "sector",
    "reader",
]
