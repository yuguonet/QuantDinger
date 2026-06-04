# -*- coding: utf-8 -*-
"""
Market tools — index quotes, sector rankings.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)

@tool(
    description="获取大盘指数实时行情（上证指数、深证成指、创业板指）。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_market_indices() -> Dict[str, Any]:
    """获取大盘指数行情（上证指数、深证成指、创业板指）。"""
    ds = _get_ds("CNStock")
    try:
        if hasattr(ds, "get_index_quotes"):
            quotes = ds.get_index_quotes(["000001", "399001", "399006"])
            return {"indices": quotes}
        return {"error": "当前数据源不支持指数查询", "retriable": False}
    except NotImplementedError:
        return {"error": "当前数据源不支持指数查询", "retriable": False}
    except Exception as e:
        logger.error("get_market_indices failed: %s", e)
        return {"error": str(e)}

@tool(
    description="获取行业板块涨跌排名和资金流向。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_sector_rankings() -> Dict[str, Any]:
    """获取行业板块涨跌排名和资金流向。"""
    ds = _get_ds("CNStock")
    try:
        if hasattr(ds, "get_sector_fund_flow"):
            flows = ds.get_sector_fund_flow()
            return {"sectors": flows[:20] if isinstance(flows, list) else flows}
        if hasattr(ds, "get_fund_flow"):
            flows = ds.get_fund_flow()
            return {"sectors": flows[:20] if isinstance(flows, list) else flows}
        return {"error": "当前数据源不支持板块排名", "retriable": False}
    except NotImplementedError:
        return {"error": "当前数据源不支持板块排名", "retriable": False}
    except Exception as e:
        logger.error("get_sector_rankings failed: %s", e)
        return {"error": str(e)}

# Legacy list — kept for backward compat during migration; safe to remove later.