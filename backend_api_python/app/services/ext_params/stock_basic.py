"""
stock_basic — 股票基本面扩展参数

自动注入 df 的列：
    turnover_rate  换手率 (volume / circ_shares * 100)
    total_shares   总股本 (股)
    circ_shares    流通股本 (股)
    pe_ratio       市盈率 (动态)
    pb_ratio       市净率

脚本可用函数：
    query_stock(sym=None)  查询任意股票基本面信息
"""
import logging

from . import provider

logger = logging.getLogger(__name__)

# 单次回测内的缓存
_cache = {}


def _query_stock_info(symbol: str) -> dict:
    """从 basicinfo_db 查询股票基本信息（带缓存）。"""
    if not symbol:
        return {}
    if symbol in _cache:
        return _cache[symbol]
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        info = get_stock_basic_db().get_stock(symbol) or {}
    except Exception as e:
        logger.debug("query_stock(%s) 失败: %s", symbol, e)
        info = {}
    _cache[symbol] = info
    return info


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    # 1. 暴露 query_stock 函数
    extras['query_stock'] = _query_stock_info

    # 2. 自动往 df 注入常用衍生列
    if symbol and df is not None:
        info = _query_stock_info(symbol)
        if info:
            circ = float(info.get('circ_shares', 0) or 0)
            total = float(info.get('total_shares', 0) or 0)
            df['total_shares'] = total
            df['circ_shares'] = circ
            df['pe_ratio'] = float(info.get('pe_ratio', 0) or 0)
            df['pb_ratio'] = float(info.get('pb_ratio', 0) or 0)
            if circ > 0:
                df['turnover_rate'] = (df['volume'] / circ * 100).round(4)
            else:
                df['turnover_rate'] = 0.0

    return extras
