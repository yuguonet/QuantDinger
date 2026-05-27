"""
stock_concept_industry — 股票所属概念 & 行业扩展参数

自动注入 df 的列：
    industry     所属行业（如 "白酒"）
    concepts     所属概念（逗号分隔，如 "锂电池,新能源"）
    concept_list 所属概念列表（Python list）

脚本可用变量：
    stock_industry   当前股票所属行业
    stock_concepts   当前股票所属概念列表
"""
import logging

from . import provider

logger = logging.getLogger(__name__)

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
        logger.debug("query_stock_concept(%s) 失败: %s", symbol, e)
        info = {}
    _cache[symbol] = info
    return info


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if symbol and df is not None:
        info = _query_stock_info(symbol)
        if info:
            industry = str(info.get('industry', '') or '').strip()
            concepts_raw = str(info.get('concepts', '') or '').strip()
            concept_list = [c.strip() for c in concepts_raw.split(',') if c.strip()]

            # 注入 df 列（每行相同值，方便策略中按列筛选）
            df['industry'] = industry
            df['concepts'] = concepts_raw
            df['concept_list'] = [concept_list] * len(df) if len(df) > 0 else []

            # 暴露为独立变量
            extras['stock_industry'] = industry
            extras['stock_concepts'] = concept_list

    return extras
