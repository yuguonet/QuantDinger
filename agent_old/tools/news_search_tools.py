# -*- coding: utf-8 -*-
"""
news Search tools — news search, comprehensive intelligence.

数据源统一走 app.services.news_search.fetch_financial_news()（带 DB 缓存）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 转接适配层 — 对齐原 fetch_financial_news() 接口
# ═══════════════════════════════════════════════════════════════

def fetch_financial_news(
    lang: str = "all",
    market: str = "all",
    symbol: str = "",
    name: str = "",
    keywords: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    转接适配函数 — 调用 news_search.fetch_financial_news()（带缓存）

    内部流程:
      1. 先查 DB 缓存 (24h 内命中直接返回, 零网络请求)
      2. 缓存未命中 → search_news_dispatch() → 评分 → 写 DB
      3. 格式化返回
    """
    from app.services.news_search import fetch_financial_news as _fetch
    return _fetch(lang=lang, market=market, symbol=symbol, name=name, keywords=keywords)

# ═══════════════════════════════════════════════════════════════
# 工具函数 — 无需改动
# ═══════════════════════════════════════════════════════════════

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

@tool(
    description="综合情报搜索：个股新闻 + 政策新闻 + 风险排查，多维度获取情报。可选 keyword 聚焦搜索关键词。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def search_comprehensive_intel(stock_code: str, keyword: str = "") -> Dict[str, Any]:
    """综合情报搜索：个股新闻 + 政策新闻。可选 keyword 聚焦关键词。

    Args:
        stock_code: 股票代码
        keyword: 可选，搜索关键词（如 "解禁"、"减持"）
    """
    try:
        stock_resp = fetch_financial_news(lang="all", market="CNStock", symbol=stock_code, name=keyword or "")
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
            "keyword": keyword or stock_code,
            "dimensions": {k: len(v) for k, v in results.items()},
            "total_results": total,
            "results": stock_items[:8],  # 兼容旧接口的扁平列表
            "data": results,
        }
    except Exception as e:
        logger.error("search_comprehensive_intel(%s) failed: %s", stock_code, e)
        return {"stock_code": stock_code, "error": str(e), "retriable": True}
