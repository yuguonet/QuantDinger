# -*- coding: utf-8 -*-
"""
news Search tools — news search, comprehensive intelligence.

数据源统一走 app.services.news_search → SearchService.search_news_dispatch()
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 转接适配层 — 对齐原 fetch_financial_news() 接口
# ═══════════════════════════════════════════════════════════════

def _detect_lang_from_text(text: str, default: str = "cn") -> str:
    """根据文本推断语言：中文字符占比 >15% 视为中文"""
    if not text:
        return default
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    return "cn" if cn_chars / max(len(text), 1) > 0.15 else "en"

def fetch_financial_news(
    lang: str = "all",
    market: str = "all",
    symbol: str = "",
    name: str = "",
    keywords: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    转接适配函数 — 调用 news_search.SearchService.search_news_dispatch()
    返回格式与原 fetch_financial_news() 完全一致:
    {"cn": [...], "en": [...]}
    """
    from app.services.news_search import get_search_service

    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    try:
        effective_market = market if market != "all" else "CNStock"

        svc = get_search_service()
        resp = svc.search_news_dispatch(
            symbol=symbol,
            market=effective_market,
            lang=lang,
            days=3,
            max_web_results=5,
            name=name,
            keywords=keywords,
        )

        if not resp.results:
            return result

        for r in resp.results:
            title = r.title or ""
            detected = _detect_lang_from_text(title, "cn" if effective_market == "CNStock" else "en")
            target = lang if lang != "all" else detected

            result[target].append({
                "title": title,
                "link": r.url or "",
                "snippet": r.snippet or "",
                "source": r.source or "",
                "published": r.published_date or "",
                "sentiment": r.sentiment,
                "sentiment_score": r.sentiment_score,
            })

    except Exception as e:
        logger.error("转接 fetch_financial_news 失败: %s", e)

    return result

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
    description="搜索股票相关新闻、公告、研报。用于获取最新消息面信息。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def search_stock_news(stock_code: str, keyword: str = "") -> Dict[str, Any]:
    """搜索股票相关新闻、公告、研报。"""
    try:
        # 直接调用本文件内的转接函数，不再依赖 news_service
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

@tool(
    description="综合情报搜索：最新消息 + 风险排查 + 业绩预期，多维度获取情报。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def search_comprehensive_intel(stock_code: str) -> Dict[str, Any]:
    """综合情报搜索：最新消息 + 风险排查 + 业绩预期。"""
    try:
        # 直接调用本文件内的转接函数，不再依赖 news_service
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
