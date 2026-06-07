# -*- coding: utf-8 -*-
"""
研报情报工具 — 研报评级、一致预期、个股新闻、全球资讯、个股公告。

数据来源：东财 reportapi / 同花顺 / 东财搜索API / 巨潮 cninfo
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

import requests

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _stock_code_normalize(code: str) -> str:
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


# ══════════════════════════════════════════════════════════════
# 研报评级
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 研报评级+EPS预测。机构对个股的评级(买入/增持)和未来三年EPS预测。中线持仓必查：机构覆盖密度高=共识强，评级上调=预期改善。配合一致预期EPS做估值。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_stock_reports(stock_code: str, max_pages: int = 3) -> Dict[str, Any]:
    """获取个股研报列表（东财 reportapi）。

    Args:
        stock_code: 股票代码（如 600519）
        max_pages: 最大页数，默认3
    """
    from app.market_cn.eastmoney_search import _em_get

    code = _stock_code_normalize(stock_code)
    report_api = "https://reportapi.eastmoney.com/report/list"
    all_records = []

    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        try:
            r = _em_get(report_api, params=params,
                        headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        except Exception as e:
            logger.warning("get_stock_reports(%s) page %d failed: %s", code, page, e)
            break

    reports = []
    for row in all_records[:50]:
        reports.append({
            "title": row.get("title", ""),
            "date": (row.get("publishDate") or "")[:10],
            "org": row.get("orgSName", ""),
            "rating": row.get("emRatingName", ""),
            "industry": row.get("indvInduName", ""),
            "eps_this_year": row.get("predictThisYearEps"),
            "eps_next_year": row.get("predictNextYearEps"),
            "eps_next2_year": row.get("predictNextTwoYearEps"),
            "info_code": row.get("infoCode", ""),
        })

    return {"stock_code": code, "total": len(all_records), "reports": reports}


# ══════════════════════════════════════════════════════════════
# 一致预期
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 机构一致预期EPS。预测机构数、均值、最大最小值。算前向PE/PEG的核心输入：当前价÷一致预期EPS=前向PE。机构数<3的要谨慎。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_consensus_eps(stock_code: str) -> Dict[str, Any]:
    """获取同花顺机构一致预期EPS。

    Args:
        stock_code: 股票代码（如 688017）
    """
    code = _stock_code_normalize(stock_code)
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


# ══════════════════════════════════════════════════════════════
# 个股新闻
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线+中线] 个股新闻（补充 search_stock_news：本工具直连东财JSONP，作为 news_search_service 的备用数据源）。东财个股相关新闻流。突发利好/利空消息第一时间获取，配合热点题材判断消息面催化。新闻+概念板块交叉验证。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_eastmoney_stock_news(stock_code: str, page_size: int = 20) -> Dict[str, Any]:
    """获取东财个股新闻。

    Args:
        stock_code: 股票代码（如 688017）
        page_size: 返回条数，默认20
    """
    from app.market_cn.eastmoney_search import _em_get

    code = _stock_code_normalize(stock_code)
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


# ══════════════════════════════════════════════════════════════
# 全球财经资讯
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线] 全球财经资讯。东财7×24滚动快讯。盘中突发事件监控：政策利好、外围异动、行业突发新闻。短线事件驱动策略必看。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
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
        items = d.get("data", {}).get("listData", []) or d.get("data", []) or []
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


# ══════════════════════════════════════════════════════════════
# 个股公告
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线+中线] 个股公告。巨潮全量公告（沪深北交所）。利好公告（业绩预增/回购/增持）=短线催化，定期报告=中线基本面更新。解禁/减持公告=风险预警。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_stock_filings(stock_code: str, page_size: int = 20) -> Dict[str, Any]:
    """获取个股公告列表（巨潮 cninfo）。

    Args:
        stock_code: 股票代码（如 600519）
        page_size: 返回条数，默认20
    """
    code = _stock_code_normalize(stock_code)
    org_id = _cninfo_orgid(code)

    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    data = {
        "stock": f"{code},{org_id}" if org_id else code,
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "sse" if code.startswith("6") else "szse",
        "category": "", "plate": "", "seDate": "",
        "searchkey": "", "secid": "",
        "sortName": "", "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": _UA,
        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        r = requests.post(url, data=data, headers=headers, timeout=15)
        d = r.json()
        announcements = d.get("announcements") or []
        rows = []
        for ann in announcements:
            rows.append({
                "title": ann.get("announcementTitle", ""),
                "date": ann.get("announcementTime", ""),
                "type": ann.get("announcementTypeName", ""),
                "url": f"http://static.cninfo.com.cn/{ann['adjunctUrl']}" if ann.get("adjunctUrl") else "",
            })
        return {"stock_code": code, "total": d.get("totalAnnouncement", 0), "filings": rows}
    except Exception as e:
        logger.warning("get_stock_filings(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


def _cninfo_orgid(code: str) -> str:
    """动态获取巨潮公告 orgId。"""
    try:
        url = "http://www.cninfo.com.cn/new/data/szse_stock.json"
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
        data = r.json()
        for stock in data.get("stockList", []):
            if stock.get("code") == code:
                return stock.get("orgId", "")
    except Exception:
        pass
    if code.startswith("6"):
        return f"gssh0{code}"
    return f"gssz0{code}"
