# -*- coding: utf-8 -*-
"""
Search tools — news search, comprehensive intelligence.

数据源统一走 app.data_providers.news → fetch_financial_news()
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _extract_news_items(resp: Dict) -> List[Dict[str, Any]]:
    """从 fetch_financial_news 返回的 {"cn":[...], "en":[...]} 中提取扁平列表。"""
    items: List[Dict[str, Any]] = []
    for lang_key in ("cn", "en"):
        for item in resp.get(lang_key) or []:
            items.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "source": item.get("source", ""),
                "published": item.get("published", ""),
                "sentiment": item.get("sentiment", "neutral"),
                "sentiment_score": item.get("sentiment_score", 0),
            })
    return items


def search_stock_news(stock_code: str, keyword: str = "") -> Dict[str, Any]:
    """搜索股票相关新闻、公告、研报。

    Args:
        stock_code: 股票代码
        keyword: 搜索关键词（可选，不填则用股票代码）
    """
    try:
        from app.services.news_service import fetch_financial_news

        resp = fetch_financial_news(
            lang="all",
            market="CNStock",
            symbol=stock_code,
            name=keyword or "",
        )
        items = _extract_news_items(resp)
        search_term = keyword or stock_code

        if items:
            return {
                "stock_code": stock_code,
                "keyword": search_term,
                "results": items[:5],
                "count": len(items),
            }
        return {
            "stock_code": stock_code,
            "keyword": search_term,
            "results": [],
            "count": 0,
            "message": "未找到相关新闻",
        }
    except Exception as e:
        logger.error("search_stock_news(%s) failed: %s", stock_code, e)
        return {
            "stock_code": stock_code,
            "results": [],
            "count": 0,
            "error": f"新闻搜索暂不可用: {e}",
            "retriable": True,
        }


def search_comprehensive_intel(stock_code: str) -> Dict[str, Any]:
    """综合情报搜索：最新消息 + 风险排查 + 业绩预期。
    一次调用获取多维度情报。"""
    try:
        from app.services.news_service import fetch_financial_news

        # 多维度：个股新闻 + 政策/宏观
        stock_resp = fetch_financial_news(lang="all", market="CNStock", symbol=stock_code)
        policy_resp = fetch_financial_news(lang="all", market="CNStock", symbol="POLICY")

        stock_items = _extract_news_items(stock_resp)
        policy_items = _extract_news_items(policy_resp)

        results = {
            "latest_news": stock_items[:8],
            "policy_news": policy_items[:5],
        }

        total = sum(len(v) for v in results.values())
        return {
            "stock_code": stock_code,
            "dimensions": {k: len(v) for k, v in results.items()},
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
