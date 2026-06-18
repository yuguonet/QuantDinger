# -*- coding: utf-8 -*-
"""
Sector Analysis Tools — 桥接 market_cn.china_market 到 agent 工具系统。

所有板块/概念分析统一走 china_market.py（带缓存+自动刷新）。
不直接 import hot_sectors/sector_history 底层模块。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)


@tool(
    description="[短线核心] 实时热门板块（行业+概念）。包含涨停家数、领涨股、成交额、强度标签（强势领涨/稳步上行/弱势调整）、市场情绪判断。比 get_sector_rankings 更详细，短线看板块主线必用。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
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


@tool(
    description="[短线+中线] 板块趋势分析（1个月趋势+6个月周期+今日预测）。看哪些板块持续走强/走弱、季节性规律、今日可能的热门板块。中线行业轮动核心工具。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
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


@tool(
    description="[中线] 板块历史排名走势（最近N天每日排名数据）。用于绘制板块排名走势图、分析板块轮动节奏。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
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


@tool(
    description="[中线] 今日热门板块预测（基于趋势+季节性+最新排名综合评分）。中线布局参考。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_sector_prediction() -> Dict[str, Any]:
    """获取今日热门板块预测。"""
    try:
        from app.market_cn.china_market import get_sector_prediction as _get
        return _get()
    except Exception as e:
        logger.warning("get_sector_prediction failed: %s", e)
        return {"error": str(e)}


@tool(
    description="[中线] 板块6个月周期分析（季节性规律、轮动模式）。判断当前月份历史表现最佳的板块。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
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


@tool(
    description="[短线+中线] 个股所属行业和概念（从本地 stock_basic_info 表查询）。快速获取一只股票属于哪些行业和概念板块，用于题材关联判断。",
    category="名称查询",
    layer="数据层",
    domain=[],
)
def get_stock_sector_info(stock_code: str) -> Dict[str, Any]:
    """从本地数据库查询股票所属行业和概念。

    Args:
        stock_code: 股票代码（如 600519）
    """
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


@tool(
    description="[短线+中线] 板块内强势个股（指定板块代码，返回板块内领涨股详情）。用于板块确认后找龙头和补涨标的。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
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
