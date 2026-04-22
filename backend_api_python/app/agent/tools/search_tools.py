# -*- coding: utf-8 -*-
"""
Search tools — news search, comprehensive intelligence.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def search_stock_news(stock_code: str, keyword: str = "") -> Dict[str, Any]:
    """搜索股票相关新闻、公告、研报。
    
    Args:
        stock_code: 股票代码
        keyword: 搜索关键词（可选，不填则用股票代码）
    """
    try:
        from app.services.stock_news import get_stock_news_service
        svc = get_stock_news_service()
        search_term = keyword or stock_code
        results = svc.search(stock_code, search_term, max_results=5)
        if results:
            return {
                "stock_code": stock_code,
                "keyword": search_term,
                "results": results,
                "count": len(results),
            }
        return {"stock_code": stock_code, "keyword": search_term, "results": [], "count": 0,
                "message": "未找到相关新闻"}
    except Exception as e:
        logger.error("search_stock_news(%s) failed: %s", stock_code, e)
        # Graceful degradation — don't block the agent
        return {"stock_code": stock_code, "results": [], "count": 0,
                "error": f"新闻搜索暂不可用: {e}", "retriable": True}


def search_comprehensive_intel(stock_code: str) -> Dict[str, Any]:
    """综合情报搜索：最新消息 + 风险排查 + 业绩预期。
    一次调用获取多维度情报。"""
    try:
        from app.services.stock_news import get_stock_news_service
        svc = get_stock_news_service()
        # Try multi-dimension search if available
        if hasattr(svc, "search_comprehensive"):
            results = svc.search_comprehensive(stock_code, max_per_dim=3)
        else:
            # Fallback: just do a regular news search
            results = svc.search(stock_code, stock_code, max_results=8)
            results = {"latest_news": results or []}

        total = sum(len(v) if isinstance(v, list) else 0 for v in results.values())
        return {
            "stock_code": stock_code,
            "dimensions": {k: len(v) if isinstance(v, list) else 0 for k, v in results.items()},
            "total_results": total,
            "data": results,
        }
    except Exception as e:
        logger.error("search_comprehensive_intel(%s) failed: %s", stock_code, e)
        return {"stock_code": stock_code, "error": str(e), "retriable": True}


# ── OpenAI tool declarations ─────────────────────────────────

SEARCH_TOOLS = [
    {
        "fn": search_stock_news,
        "name": "search_stock_news",
        "description": "搜索股票相关新闻、公告、研报。用于获取最新消息面信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
                "keyword": {"type": "string", "description": "搜索关键词（可选）"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": search_comprehensive_intel,
        "name": "search_comprehensive_intel",
        "description": "综合情报搜索：最新消息 + 风险排查 + 业绩预期，多维度获取情报。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
]
