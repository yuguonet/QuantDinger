# -*- coding: utf-8 -*-
"""
研报情报工具 — 研报评级、一致预期、个股新闻、全球资讯、个股公告。

数据来源：东财 reportapi / 同花顺 / 东财搜索API / 巨潮 cninfo
"""
from __future__ import annotations
def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

import json
from app.agent.log import logger
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

import requests
from app.agent.utils.md_format import _batch_execute, _format_output, _to_md
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ══════════════════════════════════════════════════════════════
# 研报评级
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 一致预期
# ══════════════════════════════════════════════════════════════

def get_consensus_eps(codes: str, _output: str = "markdown") -> str:
    """机构一致预期EPS：返回同花顺数据源的机构预测每股收益均值。

    Args:
        codes: 逗号分隔的股票代码，如 "688017" 或 "688017,600519"
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str, _output: str = "markdown") -> str:
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
                _r = {"stock_code": code, "consensus": records, "source": "同花顺"}
                return _format_output(_r, _output)
            return {"stock_code": code, "consensus": [], "message": "未找到一致预期数据"}
        except Exception as e:
            logger.warning("get_consensus_eps(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    return _batch_execute(_one, code_list)
# ══════════════════════════════════════════════════════════════
# 个股新闻
# ══════════════════════════════════════════════════════════════

def get_eastmoney_stock_news(codes: str, page_size: int = 20, _output: str = "markdown") -> str:
    """个股新闻：返回东财数据源的个股相关新闻标题和摘要。

    Args:
        codes: 逗号分隔的股票代码，如 "688017" 或 "688017,600519"
        page_size: 返回条数，默认20
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str, _output: str = "markdown") -> str:
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
            _r = {"stock_code": code, "total": len(rows), "news": rows}
            return _format_output(_r, _output)
        except Exception as e:
            logger.warning("get_eastmoney_stock_news(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    return _batch_execute(_one, code_list)
# ══════════════════════════════════════════════════════════════
# 全球财经资讯
# ══════════════════════════════════════════════════════════════

def get_global_finance_news(page_size: int = 30, _output: str = "markdown") -> str:
    """全球资讯：返回东财数据源的全球财经快讯标题和摘要。

    Args:
        page_size: 返回条数，默认30
        _output: "markdown"(默认) | "json"
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
        _r = {"total": len(rows), "news": rows}
        return _format_output(_r, _output)
    except Exception as e:
        logger.warning("get_global_finance_news failed: %s", e)
        return {"error": str(e)}

