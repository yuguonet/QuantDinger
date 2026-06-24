# -*- coding: utf-8 -*-
"""
news_search_tools — Agent 新闻情报工具 (薄壳)

实际逻辑在 services/news_search.py + news_analysis.py
返回: 总分 + 方向 + 摘要(一票否决置顶, 合计≤20条)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List


logger = logging.getLogger(__name__)

# 工具层短时缓存: 同一 symbol 60s 内直接返回
_search_cache: Dict[str, tuple] = {}  # key → (timestamp, result)
_CACHE_TTL = 60


def _get_policy_from_cache() -> List[Dict[str, Any]]:
    """政策新闻: 只读 DB 缓存 (scheduler 每日写入)"""
    try:
        from app.services.news_search import get_news_cache_manager
        cached = get_news_cache_manager().get_items("POLICY", "CNStock")
        if not cached:
            return []
        return [
            {"title": r["title"], "link": r.get("url", ""),
             "snippet": r.get("snippet", ""), "source": r.get("source", ""),
             "published": r.get("published_date", ""),
             "sentiment": r.get("sentiment", "neutral"),
             "sentiment_score": r.get("sentiment_score")}
            for r in cached
        ]
    except Exception as e:
        logger.warning("读取 POLICY 缓存失败: %s", e)
        return []


def _get_news(symbol: str, market: str = "CNStock", name: str = "") -> List[Dict[str, Any]]:
    """个股/板块新闻: 走 fetch_financial_news (缓存→搜索→写入)"""
    try:
        from app.services.news_search import fetch_financial_news
        resp = fetch_financial_news(lang="all", market=market, symbol=symbol, name=name)
        items = []
        for lang_key in ("cn", "en"):
            for it in resp.get(lang_key) or []:
                items.append({
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "snippet": it.get("snippet", ""),
                    "source": it.get("source", ""),
                    "published": it.get("published", ""),
                    "sentiment": it.get("sentiment", "neutral"),
                    "sentiment_score": it.get("sentiment_score"),
                })
        return items
    except Exception as e:
        logger.warning("获取新闻失败 %s(%s): %s", symbol, market, e)
        return []


def _build_result(items: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """评分 + 排序: 一票否决置顶, 合计≤20条"""
    from app.services.news_analysis import composite_score

    articles = [
        {"score": it.get("sentiment_score") or 0.0,
         "published_date": it.get("published", "")}
        for it in items
    ]
    score_info = composite_score(articles) if articles else {}

    veto = score_info.get("veto", False)
    veto_article = score_info.get("veto_article")

    # 分离一票否决 vs 正常
    veto_items, normal_items = [], []
    for it in items:
        sc = it.get("sentiment_score")
        if sc == -999:
            veto_items.append({**it, "_veto": True})
        else:
            normal_items.append(it)

    # 正常按时间倒序
    normal_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    # 合并: 一票否决置顶, 合计≤20
    merged = veto_items + normal_items
    merged = merged[:20]

    return {
        "label": label,
        "composite_score": score_info.get("composite_score", 0),
        "direction": score_info.get("direction", "中性"),
        "veto": veto,
        "veto_article": veto_article,
        "count": len(merged),
        "news": merged,
    }


def search_stock_intel(codes: str, name: str = "") -> Dict[str, Any]:
    """个股情报搜索：返回指定股票的新闻、公告、研报列表及摘要。

    Args:
        codes: 多股用逗号分隔"
        name: 股票名称，如 "贵州茅台"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        items = _get_news(stock_code, "CNStock", name)
        return _build_result(items, f"个股:{stock_code}")

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}


def search_sector_intel(market: str = "CNStock") -> Dict[str, Any]:
    """板块情报搜索：返回指定板块的相关新闻和政策动态。

    Args:
        market: 板块名称或关键词
    """
    items = _get_news(market, market)
    return _build_result(items, f"板块:{market}")


def search_policy_intel(market: str = "CNStock") -> Dict[str, Any]:
    """政策情报搜索：返回最新财经政策、监管动态。

    Args:
        market: 市场或政策关键词
    """
    items = _get_policy_from_cache()
    return _build_result(items, f"政策:{market}")


def search_comprehensive_intel(codes: str, name: str = "") -> Dict[str, Any]:
    """综合情报：同时搜索个股新闻+板块动态+政策面，返回合并结果。

    Args:
        codes: 多股用逗号分隔"
        name: 股票名称，如 "贵州茅台"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        stock_items = _get_news(stock_code, "CNStock", name)
        policy_items = _get_policy_from_cache()

        seen = set()
        merged = []
        for it in stock_items + policy_items:
            t = it.get("title", "")
            if t and t not in seen:
                seen.add(t)
                merged.append(it)

        return _build_result(merged, f"综合:{stock_code}")

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
