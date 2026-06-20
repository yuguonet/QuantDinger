# -*- coding: utf-8 -*-
"""
研报情报工具 — 研报评级、一致预期、个股新闻、全球资讯、个股公告。

数据来源：东财 reportapi / 同花顺 / 东财搜索API / 巨潮 cninfo
"""
from __future__ import annotations
from app.data_sources.normalizer import strip_market_prefix as _strip_prefix

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

import requests


logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"



# ══════════════════════════════════════════════════════════════
# 研报评级
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 一致预期
# ══════════════════════════════════════════════════════════════

def get_consensus_eps(codes: str) -> Dict[str, Any]:
    """获取同花顺机构一致预期EPS，支持多股批量获取。

    Args:
        codes: 逗号分隔的股票代码，如 "688017" 或 "688017,600519"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        code = _strip_prefix(stock_code)
        try:
            import pandas as pd
            from io import StringIO

            url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
            headers = {
                "User-Agent": _UA,
                "Referer": "https://basic.10jqka.com.cn/",
            }
            r = requests.get(url, headers=headers, timeout=15)
            r.encoding = "gbk"
            dfs = pd.read_html(StringIO(r.text))

            target_df = None
            for df in dfs:
                cols = [str(c) for c in df.columns]
                if any("每股收益" in c or "均值" in c for c in cols):
                    target_df = df
                    break
            if target_df is None and dfs:
                target_df = dfs[0]

            if target_df is not None:
                records = target_df.to_dict(orient="records")
                return {"stock_code": code, "consensus": records, "source": "同花顺"}
            return {"stock_code": code, "consensus": [], "message": "未找到一致预期数据"}
        except Exception as e:
            logger.warning("get_consensus_eps(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}


# ══════════════════════════════════════════════════════════════
# 个股新闻
# ══════════════════════════════════════════════════════════════

def get_eastmoney_stock_news(codes: str, page_size: int = 20) -> Dict[str, Any]:
    """获取东财个股新闻，支持多股批量获取。

    Args:
        codes: 逗号分隔的股票代码，如 "688017" 或 "688017,600519"
        page_size: 返回条数，默认20
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        from app.market_cn.eastmoney_search import _em_get

        code = _strip_prefix(stock_code)
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = json.dumps({
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "default",
                "pageIndex": 1, "pageSize": page_size,
                "preTag": "", "postTag": "",
            }},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": _UA, "Referer": "https://so.eastmoney.com/"}
        try:
            r = _em_get(url, params=params, headers=headers, timeout=15)
            text = r.text
            json_str = text[text.index("(") + 1: text.rindex(")")]
            d = json.loads(json_str)

            articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
            rows = []
            for a in articles:
                rows.append({
                    "title": re.sub(r'<[^>]+>', '', a.get("title", "")),
                    "content": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                    "time": a.get("date", ""),
                    "source": a.get("mediaName", ""),
                    "url": a.get("url", ""),
                })
            return {"stock_code": code, "total": len(rows), "news": rows}
        except Exception as e:
            logger.warning("get_eastmoney_stock_news(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}


# ══════════════════════════════════════════════════════════════
# 全球财经资讯
# ══════════════════════════════════════════════════════════════

def get_global_finance_news(page_size: int = 30) -> Dict[str, Any]:
    """获取东财全球财经资讯。

    Args:
        page_size: 返回条数，默认30
    """
    from app.market_cn.eastmoney_search import _em_get

    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    params = {
        "client": "web", "biz": "web_724",
        "fastColumn": "102", "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        data = d.get("data", {})
        if isinstance(data, str):
            data = json.loads(data) if data else {}
        items = data.get("fastNewsList", []) or data.get("listData", []) or []
        rows = []
        for item in items:
            rows.append({
                "title": item.get("title", ""),
                "summary": item.get("digest", "") or item.get("content", "")[:200],
                "time": item.get("showTime", "") or item.get("date", ""),
            })
        return {"total": len(rows), "news": rows}
    except Exception as e:
        logger.warning("get_global_finance_news failed: %s", e)
        return {"error": str(e)}



