# -*- coding: utf-8 -*-
"""
Sector Analysis Tools — 桥接 market_cn.china_market 到 agent 工具系统。

所有板块/概念分析统一走 china_market.py（带缓存+自动刷新）。
不直接 import hot_sectors/sector_history 底层模块。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List


logger = logging.getLogger(__name__)


def get_hot_sectors(industry_limit: int = 15, concept_limit: int = 15) -> Dict[str, Any]:
    """获取实时热门板块（行业+概念），含涨停数/领涨股/强度标签/情绪判断。

    Args:
        industry_limit: 行业板块数量，默认15
        concept_limit: 概念板块数量，默认15
    """
    try:
        from app.market_cn.china_market import get_hot_sectors as _get
        result = _get(industry_limit=industry_limit, concept_limit=concept_limit)
        return result
    except Exception as e:
        logger.warning("get_hot_sectors failed: %s", e)
        return {"error": str(e)}


def get_sector_trend_analysis(board_type: str = "industry") -> Dict[str, Any]:
    """获取板块趋势分析（1月趋势+6月周期+今日预测）。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
    """
    try:
        from app.market_cn.china_market import get_sector_trend as _get
        return _get(board_type=board_type)
    except Exception as e:
        logger.warning("get_sector_trend_analysis failed: %s", e)
        return {"error": str(e)}


def get_sector_history_data(board_type: str = "industry", days: int = 30) -> Dict[str, Any]:
    """获取板块历史排名数据。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
        days: 获取天数，默认30
    """
    try:
        from app.market_cn.china_market import get_sector_history as _get
        return _get(board_type=board_type, days=days)
    except Exception as e:
        logger.warning("get_sector_history_data failed: %s", e)
        return {"error": str(e)}


def get_sector_prediction() -> Dict[str, Any]:
    """获取今日热门板块预测。"""
    try:
        from app.market_cn.china_market import get_sector_prediction as _get
        return _get()
    except Exception as e:
        logger.warning("get_sector_prediction failed: %s", e)
        return {"error": str(e)}


def get_sector_cycle(board_type: str = "industry") -> Dict[str, Any]:
    """获取板块6个月周期分析。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
    """
    try:
        from app.market_cn.china_market import get_sector_cycle as _get
        return _get(board_type=board_type)
    except Exception as e:
        logger.warning("get_sector_cycle failed: %s", e)
        return {"error": str(e)}


def get_stock_sector_info(codes: str) -> Dict[str, Any]:
    """从本地数据库查询股票所属行业和概念，支持多股批量获取。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            from app.data_sources.normalizer import strip_market_prefix

            db = get_stock_basic_db()
            sym = strip_market_prefix(stock_code)
            stock = db.get_stock(sym)

            if not stock:
                return {"stock_code": stock_code, "error": "未找到该股票信息"}

            result = {"stock_code": sym}
            if stock.get("name"):
                result["name"] = stock["name"]
            if stock.get("industry"):
                result["industry"] = stock["industry"]
            concepts_str = stock.get("concepts", "")
            if concepts_str:
                result["concepts"] = [c.strip() for c in concepts_str.split(",") if c.strip()]
            if stock.get("market_cn"):
                result["market_cn"] = stock["market_cn"]
            if stock.get("list_date"):
                result["list_date"] = stock["list_date"]
            return result
        except Exception as e:
            logger.warning("get_stock_sector_info(%s) failed: %s", stock_code, e)
            return {"stock_code": stock_code, "error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}


def get_sector_stocks(board_code: str, limit: int = 10) -> Dict[str, Any]:
    """获取板块内强势个股。

    Args:
        board_code: 板块代码（如 BK0475 行业板块、BK0815 概念板块）
        limit: 返回数量，默认10
    """
    try:
        from app.market_cn.china_market import get_sector_stocks as _get
        return _get(board_code=board_code, limit=limit)
    except Exception as e:
        logger.warning("get_sector_stocks(%s) failed: %s", board_code, e)
        return {"board_code": board_code, "error": str(e)}
