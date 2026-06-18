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
    try:
        from app.market_cn.index import get_index_realtime
        data = get_index_realtime(["000001", "399001", "399006"])
        indices = []
        for item in data:
            indices.append({
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "price": item.get("price", 0),
                "change_pct": item.get("change_percent", 0),
                "amount_wan": round(item.get("amount", 0) / 10000, 2),
            })
        return {"indices": indices} if indices else {"error": "未获取到指数数据"}
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
    try:
        from app.market_cn.index import get_sector_fund_flow
        data = get_sector_fund_flow("今日")
        if data:
            sectors = []
            for i, item in enumerate(data[:20]):
                sectors.append({
                    "rank": i + 1,
                    "name": item.get("name", ""),
                    "change_pct": item.get("change_pct", 0),
                    "code": item.get("code", ""),
                    "main_net": item.get("main_net", 0),
                    "main_pct": item.get("main_pct", 0),
                    "lead_stock": item.get("lead_stock", ""),
                })
            return {"sectors": sectors}
    except Exception as e:
        logger.warning("get_sector_rankings via index failed: %s", e)

    # fallback: 东财直连
    try:
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "20", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f14,f104,f105,f128,f136",
        }
        r = requests.get(url, params=params, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        sectors = []
        for i, item in enumerate(items):
            sectors.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": item.get("f3", 0),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "lead_stock": item.get("f128", ""),
            })
        return {"sectors": sectors}
    except Exception as e2:
        logger.error("get_sector_rankings fallback also failed: %s", e2)
        return {"error": str(e2)}

