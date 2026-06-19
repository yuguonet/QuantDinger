# -*- coding: utf-8 -*-
"""
Fund Flow Tools — 资金流向（个股/板块/大盘）。

数据源优先级：腾讯 > 新浪 > 同花顺 > 东财
（market_cn.tape / market_cn.index 已内置多源容灾）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def get_fund_flow(stock_codes: str = "") -> Dict[str, Any]:
    """获取个股资金流向。支持单只或批量（逗号分隔），单次最多20只。

    Args:
        stock_codes: 股票代码，如 "000001" 或 "000001,600519"
    """
    if not stock_codes or not stock_codes.strip():
        return {"error": "stock_codes 不能为空", "retriable": False}

    codes = [c.strip() for c in stock_codes.split(",") if c.strip()][:20]
    from app.market_cn.tape import get_fund_flow_realtime

    results = {}
    for code in codes:
        try:
            results[code] = get_fund_flow_realtime(code)
        except Exception as e:
            results[code] = {"error": str(e)}

    return {"count": len(results), "data": results}


def get_sector_fund_flow(indicator: str = "今日") -> Dict[str, Any]:
    """获取行业板块资金流向。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator)
        return {"indicator": indicator, "count": len(data), "sectors": data}
    except Exception as e:
        logger.warning("get_sector_fund_flow failed: %s", e)
        return {"error": str(e)}


def get_concept_fund_flow(indicator: str = "今日") -> Dict[str, Any]:
    """获取概念板块资金流向。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator, board_type="concept")
        return {"indicator": indicator, "count": len(data), "concepts": data}
    except Exception as e:
        logger.warning("get_concept_fund_flow failed: %s", e)
        return {"error": str(e)}


def get_fund_flow_daily(stock_code: str, days: int = 120) -> Dict[str, Any]:
    """获取个股历史资金流向（日线级别）。

    Args:
        stock_code: 股票代码
        days: 回溯天数，默认120
    """
    from app.market_cn.tape import get_fund_flow_daily as _get
    try:
        return _get(stock_code, days=days)
    except Exception as e:
        logger.warning("get_fund_flow_daily(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def get_market_fund_flow() -> Dict[str, Any]:
    """获取大盘实时资金流向（主力/散户）。"""
    from app.market_cn.index import get_market_fund_flow_realtime as _get
    try:
        return _get()
    except Exception as e:
        logger.warning("get_market_fund_flow failed: %s", e)
        return {"error": str(e)}


def get_northbound_flow() -> Dict[str, Any]:
    """获取北向资金实时流向。"""
    from app.market_cn.index import get_northbound_realtime as _get
    try:
        return _get()
    except Exception as e:
        logger.warning("get_northbound_flow failed: %s", e)
        return {"error": str(e)}
