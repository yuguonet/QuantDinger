# -*- coding: utf-8 -*-
"""
web_search_tools — Agent 联网搜索工具

四引擎自动降级:
  1. Bocha AI (博查) — 国内优先，中文搜索质量最好，有 AI 摘要
  2. Tavily — 专为 AI 优化，1000次/月免费
  3. baidusearch — 直接爬百度，免费无限额
  4. SearXNG — 自建兜底，无配额限制

工具函数由 ToolRegistry 自动发现，无需手动注册。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────
_BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"
_BOCHA_API_KEY = os.getenv("BOCHA_AI_API_KEY", "").strip()
_TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
_SEARXNG_URL = os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/")

_REQUEST_TIMEOUT = 12  # 秒
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB

# ── 工具层短时缓存: 同一 query 120s 内直接返回 ──
_search_cache: Dict[str, tuple] = {}
_CACHE_TTL = 120


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _search_cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL:
        _search_cache.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: Dict[str, Any]) -> None:
    if len(_search_cache) > 200:
        oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
        _search_cache.pop(oldest, None)
    _search_cache[key] = (time.time(), data)


def _clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _ok(results, provider, **extra) -> Dict[str, Any]:
    """构建成功响应。"""
    base = {"success": True, "results": results, "provider": provider,
            "total": len(results), "summary": "", "error": ""}
    base.update(extra)
    return base


def _fail(provider: str, error: str) -> Dict[str, Any]:
    """构建失败响应。"""
    return {"success": False, "results": [], "provider": provider,
            "total": 0, "summary": "", "error": error}


# ═══════════════════════════════════════════════════════════════
#  Engine 1: Bocha AI (博查)
# ═══════════════════════════════════════════════════════════════

def _bocha_search(query: str, count: int = 8, freshness: str = "") -> Dict[str, Any]:
    if not _BOCHA_API_KEY:
        return _fail("bocha", "BOCHA_AI_API_KEY 未配置")

    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {_BOCHA_API_KEY}"}
    payload = {"query": query, "count": min(max(count, 1), 10),
               "search_lang": "zh", "summary": True}
    if freshness:
        payload["freshness"] = freshness

    try:
        resp = requests.post(_BOCHA_API_URL, headers=headers, json=payload,
                             timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 403:
            return _fail("bocha", "博查额度不足")
        resp.raise_for_status()
        data = resp.json()

        web_pages = (data.get("data") or {}).get("webPages", {})
        items = web_pages.get("value", [])
        ai_summary = (data.get("data") or {}).get("summary", "") or ""

        results = []
        for item in items[:count]:
            results.append({
                "title": _clean(item.get("name", "")),
                "url": _clean(item.get("url", "")),
                "snippet": _clean(item.get("snippet", "")),
                "source": _clean(item.get("siteName", "")),
                "published": item.get("datePublished", ""),
            })

        if not results:
            return _fail("bocha", "博查搜索无结果")

        r = _ok(results, "bocha", total=web_pages.get("totalEstimatedMatches", 0))
        if ai_summary:
            r["summary"] = _clean(ai_summary)[:1500]
        return r

    except requests.exceptions.Timeout:
        return _fail("bocha", "博查超时")
    except requests.exceptions.HTTPError as e:
        return _fail("bocha", f"博查 HTTP {e.response.status_code}")
    except Exception as e:
        logger.warning("[WebSearch] Bocha 异常: %s", e)
        return _fail("bocha", str(e))


# ═══════════════════════════════════════════════════════════════
#  Engine 2: Tavily
# ═══════════════════════════════════════════════════════════════

def _tavily_search(query: str, count: int = 8, days: int = 7) -> Dict[str, Any]:
    if not _TAVILY_API_KEY:
        return _fail("tavily", "TAVILY_API_KEY 未配置")

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=_TAVILY_API_KEY)

        # days → Tavily topic + days 参数
        search_depth = "advanced" if count > 5 else "basic"
        response = client.search(
            query=query,
            max_results=min(count, 10),
            search_depth=search_depth,
            include_answer=True,
            topic="general",
        )

        results = []
        for item in response.get("results", []):
            results.append({
                "title": _clean(item.get("title", "")),
                "url": _clean(item.get("url", "")),
                "snippet": _clean(item.get("content", "")),
                "source": "",
                "published": "",
                "score": item.get("score", 0),
            })

        if not results:
            return _fail("tavily", "Tavily 搜索无结果")

        r = _ok(results, "tavily")
        answer = response.get("answer", "")
        if answer:
            r["summary"] = _clean(answer)[:1500]
        return r

    except ImportError:
        return _fail("tavily", "tavily-python 未安装")
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "invalid" in err_msg.lower():
            return _fail("tavily", "Tavily API Key 无效")
        if "429" in err_msg or "rate" in err_msg.lower():
            return _fail("tavily", "Tavily 频率限制")
        logger.warning("[WebSearch] Tavily 异常: %s", e)
        return _fail("tavily", err_msg)


# ═══════════════════════════════════════════════════════════════
#  Engine 3: baidusearch (免费无限额)
# ═══════════════════════════════════════════════════════════════

def _baidu_search(query: str, count: int = 8) -> Dict[str, Any]:
    try:
        from baidusearch.baidusearch import search

        raw = search(query, num_results=min(count, 10))

        results = []
        for item in raw:
            title = _clean(item.get("title", ""))
            # baidusearch 返回 url 或 href 字段，可能是相对路径
            url = _clean(item.get("url", "") or item.get("href", ""))
            snippet = _clean(item.get("abstract", "") or item.get("snippet", ""))
            if not title:
                continue
            # 补全相对路径
            if url and not url.startswith("http"):
                url = "https://www.baidu.com" + url
            if not url:
                continue
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": "百度",
                "published": "",
            })

        if not results:
            return _fail("baidu", "百度搜索无结果")
        return _ok(results[:count], "baidu")

    except ImportError:
        return _fail("baidu", "baidusearch 未安装 (pip install baidusearch)")
    except Exception as e:
        logger.warning("[WebSearch] Baidu 异常: %s", e)
        return _fail("baidu", str(e))


# ═══════════════════════════════════════════════════════════════
#  Engine 4: SearXNG (自建兜底)
# ═══════════════════════════════════════════════════════════════

def _searxng_search(query: str, count: int = 8, engines: str = "",
                    language: str = "zh") -> Dict[str, Any]:
    if not _SEARXNG_URL:
        return _fail("searxng", "SEARXNG_BASE_URL 未配置")

    params = {"q": query, "format": "json", "language": language, "pageno": 1}
    if engines:
        params["engines"] = engines

    try:
        resp = requests.get(f"{_SEARXNG_URL}/search", params=params,
                            timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            return _fail("searxng", "SearXNG 响应过大")

        items = resp.json().get("results", [])
        results = []
        for item in items[:count]:
            results.append({
                "title": _clean(item.get("title", "")),
                "url": _clean(item.get("url", "")),
                "snippet": _clean(item.get("content", "")),
                "source": _clean(item.get("engine", "")),
                "published": item.get("publishedDate", ""),
            })

        if not results:
            return _fail("searxng", "SearXNG 搜索无结果")
        return _ok(results, "searxng")

    except requests.exceptions.Timeout:
        return _fail("searxng", "SearXNG 超时")
    except Exception as e:
        logger.warning("[WebSearch] SearXNG 异常: %s", e)
        return _fail("searxng", str(e))


# ═══════════════════════════════════════════════════════════════
#  统一搜索入口（四引擎降级）
# ═══════════════════════════════════════════════════════════════

_ENGINES = [
    ("bocha",    lambda q, c, f: _bocha_search(q, c, f)),
    ("tavily",   lambda q, c, f: _tavily_search(q, c)),
    ("baidu",    lambda q, c, f: _baidu_search(q, c)),
    ("searxng",  lambda q, c, f: _searxng_search(q, c)),
]


def _unified_search(query: str, count: int = 8, freshness: str = "",
                    engines: str = "", language: str = "zh") -> Dict[str, Any]:
    cache_key = f"{query}|{count}|{freshness}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    errors = []
    for name, fn in _ENGINES:
        result = fn(query, count, freshness)
        if result["success"]:
            result["query"] = query
            _cache_set(cache_key, result)
            return result
        errors.append(f"{name}: {result.get('error', '?')}")
        logger.info("[WebSearch] %s 失败 → 下一个", name)

    return {
        "success": False, "query": query, "results": [],
        "summary": "", "provider": "none", "total": 0,
        "error": "所有引擎均失败: " + "; ".join(errors),
    }


# ═══════════════════════════════════════════════════════════════
#  工具函数 (ToolRegistry 自动发现)
# ═══════════════════════════════════════════════════════════════

def web_search(query: str, count: int = 8, freshness: str = "") -> dict:
    """
    联网搜索 — 获取实时互联网信息。用于查询最新新闻、政策、市场动态、公司公告等。

    Args:
        query: 搜索关键词，支持自然语言（如 "2025年央行降准最新消息"）
        count: 返回结果数量，1-10，默认 8
        freshness: 时效过滤（pd=当天, pw=本周, pm=本月, py=今年，空=不限）

    Returns:
        搜索结果列表 + AI 摘要（如有）
    """
    count = min(max(count, 1), 10)
    result = _unified_search(query, count=count, freshness=freshness)
    return _format_output(result)


# ═══════════════════════════════════════════════════════════════
#  输出格式化
# ═══════════════════════════════════════════════════════════════

def _format_output(result: Dict[str, Any]) -> dict:
    """统一输出格式。"""
    output = {
        "success": result["success"],
        "provider": result["provider"],
        "total": result.get("total", 0),
    }
    if result.get("summary"):
        output["ai_summary"] = result["summary"]

    output["results"] = []
    for i, r in enumerate(result.get("results", []), 1):
        entry = {
            "index": i,
            "title": r["title"],
            "url": r["url"],
            "snippet": r["snippet"][:300] if r.get("snippet") else "",
            "source": r.get("source", ""),
            "published": r.get("published", ""),
        }
        if r.get("score"):
            entry["relevance"] = round(r["score"], 2)
        output["results"].append(entry)

    if not result["success"]:
        output["error"] = result.get("error", "搜索失败")

    return output
