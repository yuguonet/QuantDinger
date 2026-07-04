# -*- coding: utf-8 -*-
"""
Fund Flow Tools — 资金流向（个股/板块/大盘）。

数据源优先级：腾讯 > 新浪 > 同花顺 > 东财
（market_cn.tape / market_cn.index 已内置多源容灾）
"""
from __future__ import annotations
import json

from app.agent.log import logger
from typing import Any, Dict, List
from app.agent.utils.md_format import _batch_execute, _format_output, _to_md
def get_fund_flow(stock_codes: str = "", _output: str = "markdown") -> str:
    """个股资金流向：返回主力/散户/净流入金额、资金流向趋势。

    Args:
        stock_codes: 股票代码，如 "000001" 或 "000001,600519"
        _output: "markdown"(默认) | "json"
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

    _r = {"count": len(results), "data": results}
    return _format_output(_r, _output)
def get_sector_fund_flow(indicator: str = "今日", _output: str = "markdown") -> str:
    """行业资金流向：返回各行业板块主力资金净流入排名。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
        _output: "markdown"(默认) | "json"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator)
        _r = {"indicator": indicator, "count": len(data), "sectors": data}
        return _format_output(_r, _output)
    except Exception as e:
        logger.warning("get_sector_fund_flow failed: %s", e)
        return {"error": str(e)}
def get_concept_fund_flow(indicator: str = "今日", _output: str = "markdown") -> str:
    """概念资金流向：返回各概念板块主力资金净流入排名。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
        _output: "markdown"(默认) | "json"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator, board_type="concept")
        _r = {"indicator": indicator, "count": len(data), "concepts": data}
        return _format_output(_r, _output)
    except Exception as e:
        logger.warning("get_concept_fund_flow failed: %s", e)
        return {"error": str(e)}
def get_fund_flow_daily(codes: str, days: int = 120, _output: str = "markdown") -> str:
    """个股历史资金流向：返回近N天每日主力/散户净流入金额。

    Args:
        codes: 多股用逗号分隔"
        days: 回溯天数，默认120
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str, _output: str = "markdown") -> str:
        from app.market_cn.tape import get_fund_flow_daily as _get
        try:
            return _get(stock_code, days=days)
        except Exception as e:
            logger.warning("get_fund_flow_daily(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    return _format_output(_batch_execute(_one, code_list), output)
def get_market_fund_flow( _output: str = "markdown") -> str:
    """大盘资金流向：返回全市场主力/散户实时净流入金额。"""
    from app.market_cn.index import get_market_fund_flow_realtime as _get
    try:
        return _get()
    except Exception as e:
        logger.warning("get_market_fund_flow failed: %s", e)
        return {"error": str(e)}
def get_northbound_flow( _output: str = "markdown") -> str:
    """北向资金：返回沪股通/深股通当日实时净买入金额。"""
    from app.market_cn.index import get_northbound_realtime as _get
    try:
        return _get()
    except Exception as e:
        logger.warning("get_northbound_flow failed: %s", e)
        return {"error": str(e)}
