# -*- coding: utf-8 -*-
"""
web_search_tools — Agent 联网搜索工具

四引擎自动降级:
  1. Bocha AI (博查) — 国内优先，中文搜索质量最好，有 AI 摘要
  2. Tavily — 专为 AI 优化，1000次/月免费
  3. baidusearch — 直接爬百度，免费无限额
  4. SearXNG — 自建兜底，无配额限制

【注册方式·易错点】本模块在 tools/base.py 的 `_MUST_HAVE` 名单里 ⇒ scan_directory
**跳过它**、ToolProvider 里**没有** web_search ⇒ 上层包装（task_agent.py 的
_WebSearchTool）必须**直接 import 本模块的实现**，绝不能 `provider.get("web_search")`
（恒为 None，2026-09-21 事故）。曾误写为"由 ToolRegistry 自动发现，无需手动注册"。

【freshness 词表·易错点】工具对外统一用 pd/pw/pm/py（Tavily/Bing 惯例），但各引擎
原生词表不同（博查 oneDay/oneWeek/oneMonth/oneYear，SearXNG day/week/month/year，
Tavily 用天数 days）——下发前必须翻译，否则引擎按非法值处理、返回一堆旧文。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────
# 【易错点】以下 key 都在 **import 期**读取一次：改 .env 后必须重启进程才生效，
# 热改 .env 不生效（排查时最容易被误判成"key 明明配了却没生效"）。
_BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"
_TAVILY_API_URL = "https://api.tavily.com/search"
_BOCHA_API_KEY = os.getenv("BOCHA_AI_API_KEY", "").strip()
# Tavily key 的项目统一命名是**复数** TAVILY_API_KEYS（逗号分隔可轮换，见
# app/config/api_keys.py、app/services/news_search.py）。此处曾读单数
# TAVILY_API_KEY ⇒ 与 .env 的 TAVILY_API_KEYS 不匹配 ⇒ 该引擎**永远**报"未配置"
# 并被静默降级掉（无报错，现象是"Tavily 从来没用上"）。2026-09-21 修复。
_TAVILY_API_KEYS = [k.strip() for k in os.getenv("TAVILY_API_KEYS", "").split(",") if k.strip()]
_SEARXNG_URL = os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/")

# freshness 词表：工具对外统一用 pd/pw/pm/py（Tavily/Bing 惯例，也是工具 docstring
# 对模型承诺的取值）。各引擎**原生词表不同**，必须翻译后再下发，否则引擎会当成
# 非法值处理。见 _BOCHA_FRESHNESS 处的 2026-09-21 事故说明。
_FRESHNESS_DAYS = {"pd": 1, "pw": 7, "pm": 30, "py": 365}
_BOCHA_FRESHNESS = {"pd": "oneDay", "pw": "oneWeek", "pm": "oneMonth", "py": "oneYear"}
_SEARXNG_FRESHNESS = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}

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


def _normalize_title(title: str) -> str:
    """标题归一化：去标点、去空白、统一小写，用于去重判断。"""
    if not title:
        return ""
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", title)
    t = t.lower().strip()
    return t


def _deduplicate(results: List[Dict]) -> List[Dict]:
    """基于归一化标题 + URL 域名联合去重。"""
    seen = set()
    unique = []
    for r in results:
        title_key = _normalize_title(r.get("title", ""))
        url_domain = ""
        url = r.get("url", "")
        if url:
            try:
                url_domain = urlparse(url).netloc.lower()
            except Exception:
                pass
        dedup_key = f"{title_key}|{url_domain}"

        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        unique.append(r)
    return unique


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
    """博查引擎。freshness 必须翻译成博查原生词表（见 _BOCHA_FRESHNESS）。

    【2026-09-21 事故】原实现直接把工具的 pd/pw/pm/py 塞进 payload["freshness"]，
    而博查只认 noLimit/oneDay/oneWeek/oneMonth/oneYear（或 YYYY-MM-DD 区间）。
    实测非法值下博查返回 2018~2025 年的旧文（同一 query 不传该参数反而返回当天
    结果）⇒ 随后被 _filter_by_date 全数剔除 ⇒ 降级到最差的 baidu，返回
    "大家还在搜…"这类垃圾。表现是"搜索结果全是废话"，根因却是参数词表不通。
    """
    if not _BOCHA_API_KEY:
        return _fail("bocha", "BOCHA_AI_API_KEY 未配置")

    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {_BOCHA_API_KEY}"}
    payload = {"query": query, "count": min(max(count, 1), 10),
               "search_lang": "zh", "summary": True}
    bocha_freshness = _BOCHA_FRESHNESS.get(freshness, "")
    if bocha_freshness:
        payload["freshness"] = bocha_freshness

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

def _tavily_search(query: str, count: int = 8, freshness: str = "") -> Dict[str, Any]:
    """Tavily 引擎：直连 REST，**不依赖 tavily-python 包**。

    【2026-09-21 修复】原实现 `from tavily import TavilyClient`，而 requirements.txt
    里 tavily-python 是**注释掉的**（未安装）⇒ 每次调用都 ImportError、引擎永远不可用。
    项目内已有同款 REST 直连先例（app/services/news_search.py 的
    TavilySearchProvider._do_search_rest），此处对齐复用该做法，免装包。
    freshness 走服务端 `days`（旧实现把它写死成 7，pd/py 都被当成一周）。
    """
    if not _TAVILY_API_KEYS:
        return _fail("tavily", "TAVILY_API_KEYS 未配置")

    payload = {
        "api_key": _TAVILY_API_KEYS[0],   # 复数 env 支持逗号分隔轮换，此处取首个
        "query": query,
        "search_depth": "advanced" if count > 5 else "basic",
        "max_results": min(max(count, 1), 10),
        "include_answer": True,
        "topic": "general",
    }
    days = _FRESHNESS_DAYS.get(freshness, 0)
    if days:
        payload["days"] = days

    try:
        resp = requests.post(_TAVILY_API_URL, json=payload, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 401:
            return _fail("tavily", "Tavily API Key 无效")
        if resp.status_code == 429:
            return _fail("tavily", "Tavily 频率限制")
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append({
                "title": _clean(item.get("title", "")),
                "url": _clean(item.get("url", "")),
                "snippet": _clean(item.get("content", "")),
                "source": "",
                "published": item.get("published_date", "") or "",
                "score": item.get("score", 0),
            })

        if not results:
            return _fail("tavily", "Tavily 搜索无结果")

        r = _ok(results, "tavily")
        answer = data.get("answer", "")
        if answer:
            r["summary"] = _clean(answer)[:1500]
        return r

    except requests.exceptions.Timeout:
        return _fail("tavily", "Tavily 超时")
    except Exception as e:
        logger.warning("[WebSearch] Tavily 异常: %s", e)
        return _fail("tavily", str(e))


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
            url = _clean(item.get("url", "") or item.get("href", ""))
            snippet = _clean(item.get("abstract", "") or item.get("snippet", ""))
            if not title:
                continue
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

def _searxng_search(query: str, count: int = 8, freshness: str = "",
                    engines: str = "", language: str = "zh") -> Dict[str, Any]:
    if not _SEARXNG_URL:
        return _fail("searxng", "SEARXNG_BASE_URL 未配置")

    params = {"q": query, "format": "json", "language": language, "pageno": 1}
    if engines:
        params["engines"] = engines
    # SearXNG 原生词表是 day/week/month/year（与工具的 pd/pw/pm/py 不同，需翻译）
    time_range = _SEARXNG_FRESHNESS.get(freshness, "")
    if time_range:
        params["time_range"] = time_range

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
    ("tavily",   lambda q, c, f: _tavily_search(q, c, f)),
    ("baidu",    lambda q, c, f: _baidu_search(q, c)),
    ("searxng",  lambda q, c, f: _searxng_search(q, c, f)),
]


def _filter_by_date(results: List[Dict], max_age_days: int = 180) -> List[Dict]:
    """后置过滤：根据 published 字段剔除超过 max_age_days 天的结果。

    解析 published 字段中的日期，与当前日期比较。
    无法解析日期的结果保留（可能是实时数据或格式不标准）。
    """
    from datetime import datetime, timedelta

    if not results or max_age_days <= 0:
        return results

    cutoff = datetime.now() - timedelta(days=max_age_days)
    filtered = []

    # 常见日期格式
    date_patterns = [
        (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
        (r"(\d{4})/(\d{2})/(\d{2})", "%Y/%m/%d"),
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", None),  # 中文格式特殊处理
    ]

    for r in results:
        published = r.get("published", "") or ""
        if not published:
            # 无日期信息，保留
            filtered.append(r)
            continue

        parsed_date = None
        # 尝试 ISO 格式 (2026-07-19T10:00:00)
        try:
            parsed_date = datetime.fromisoformat(published[:19])
        except (ValueError, TypeError):
            pass

        # 尝试常见格式
        if not parsed_date:
            for pattern, fmt in date_patterns:
                m = re.search(pattern, published)
                if m:
                    try:
                        if fmt:
                            parsed_date = datetime.strptime(m.group(0), fmt)
                        else:
                            # 中文格式
                            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                            parsed_date = datetime(y, mo, d)
                    except (ValueError, TypeError):
                        pass
                    break

        if parsed_date and parsed_date >= cutoff:
            filtered.append(r)
        elif not parsed_date:
            # 无法解析，保守保留
            filtered.append(r)
        # parsed_date < cutoff 的结果被丢弃

    return filtered


def _unified_search(query: str, count: int = 8, freshness: str = "",
                    engines: str = "", language: str = "zh") -> Dict[str, Any]:
    cache_key = f"{query}|{count}|{freshness}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # freshness → max_age_days 映射（后置过滤用）
    # "" = 不过滤（知识型查询），有值时按值过滤
    max_age_days = _FRESHNESS_DAYS.get(freshness, 0)  # 0 = 不过滤

    errors = []
    for name, fn in _ENGINES:
        result = fn(query, count, freshness)
        if result["success"]:
            result["query"] = query
            # 后置日期过滤：剔除过期结果
            before_filter = len(result.get("results", []))
            result["results"] = _filter_by_date(result.get("results", []), max_age_days)
            after_filter = len(result["results"])
            if before_filter > after_filter:
                logger.info("[WebSearch] 日期过滤: %d → %d 条（剔除 %d 条超过 %d 天的）",
                            before_filter, after_filter, before_filter - after_filter, max_age_days)
            # 去重
            result["results"] = _deduplicate(result["results"])
            if result["results"]:  # 过滤后仍有结果
                _cache_set(cache_key, result)
                return result
            # 过滤后无结果，尝试下一个引擎。
            # 必须记入 errors：否则"全部引擎都被时效过滤掉"时 errors 为空，最终
            # 报出 `所有引擎均失败: `（后面什么都没有）——排查时完全误导。
            logger.info("[WebSearch] %s 日期过滤后无结果，尝试下一个引擎", name)
            errors.append(f"{name}: {before_filter} 条结果全部超过时效({freshness})被过滤")
            continue
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

def web_search(query: str, count: int = 8, freshness: str = "pm") -> dict:
    """
    联网搜索 — 获取互联网实时信息。任何工具无法覆盖的查询（天气、新闻、百科、实时数据等）都可使用。

    Args:
        query: 搜索关键词，支持自然语言（如 "2025年央行降准最新消息"）
        count: 返回结果数量，1-10，默认 8
        freshness: 时效过滤（pd=当天, pw=本周, pm=本月, py=今年，空=不限），默认 pm（本月）

    Returns:
        搜索结果列表 + AI 摘要（如有）
    """
    count = min(max(count, 1), 10)
    result = _unified_search(query, count=count, freshness=freshness)
    # 外网返回内容不可信 —— 就地消毒（提示注入中和），详见文件末尾 _sanitize_result
    return _sanitize_result(_format_output(result))


# ═══════════════════════════════════════════════════════════════
#  输出格式化（去重归一化 + 去掉 url）
# ═══════════════════════════════════════════════════════════════

def _format_output(result: Dict[str, Any]) -> dict:
    """统一输出格式：去掉 url，基于标题归一化二次去重。"""
    output = {
        "success": result["success"],
        "provider": result["provider"],
        "total": result.get("total", 0),
    }
    if result.get("summary"):
        output["ai_summary"] = result["summary"]

    # 二次去重：基于归一化标题
    seen_titles = set()
    unique_results = []
    for r in result.get("results", []):
        title_key = _normalize_title(r.get("title", ""))
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        unique_results.append(r)

    output["results"] = []
    for i, r in enumerate(unique_results, 1):
        entry = {
            "index": i,
            "title": r["title"],
            # url 已去掉，不再输出
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


# ═══════════════════════════════════════════════════════════════
#  结果消毒：提示注入中和（2026-09-14）
# ═══════════════════════════════════════════════════════════════
# 本工具是 agent **唯一**与外网发生关系的入口，返回的搜索结果是不可信文本，
# 可能夹带提示注入（"忽略之前的指令，执行 xxx"）。沙箱已去掉，故在此就地消毒：
# 命中即**注释掉**——保留原文便于排查，但使其失去指令形态。
#
# 注意：消毒必须放在**真正的实现**里（而非上层包装 _WebSearchTool），
# 这样无论谁调用（agent 包装 / 其它工具 / 直接调用）都经过同一道处理。
_WEB_INJECTION_MARKERS = (
    "ignore previous instructions", "ignore all previous instructions",
    "ignore the above", "ignore the following",
    "disregard all previous", "disregard previous",
    "忽略之前的所有指令", "忽略之前的指令", "忽略上面的指令", "忽略以上指令",
    "忽略先前的所有指令", "忽略上述指令",
    "不要遵守之前的指令", "不要理会上面的指令",
    "你现在是", "你现在扮演", "你现在作为", "你现在充当",
    "new instructions:", "new instruction:", "updated instructions:",
    "system prompt:", "system message:",
)


def _neutralize_text(text):
    """把含注入标记的整行注释掉（不删内容，便于事后排查对方塞了什么）。"""
    if not text:
        return text
    out = []
    for line in str(text).splitlines():
        low = line.lower()
        if any(m in low for m in _WEB_INJECTION_MARKERS):
            out.append("# [已屏蔽·疑似提示注入] " + line.strip()[:200])
        else:
            out.append(line)
    return "\n".join(out)


def _sanitize_result(obj, depth=0):
    """递归消毒返回结构里的所有字符串（dict / list / str）。"""
    if depth > 6:
        return obj
    if isinstance(obj, str):
        return _neutralize_text(obj)
    if isinstance(obj, list):
        return [_sanitize_result(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_result(v, depth + 1) for k, v in obj.items()}
    return obj