# -*- coding: utf-8 -*-
"""
信号层工具 — 热点题材归因、北向资金、概念板块、限售解禁、行业排名、龙虎榜详情。

数据来源：同花顺 / 东财 push2 / 东财 datacenter / market_cn.index / market_cn.dragon_limit
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests

from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _stock_code_normalize(code: str) -> str:
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


def _em_datacenter(report_name: str, columns: str = "ALL",
                   filter_str: str = "", page_size: int = 50,
                   sort_columns: str = "", sort_types: str = "-1") -> list:
    """东财数据中心统一查询。"""
    import os, time
    import requests as _req

    _EM_MIN_INTERVAL = float(os.getenv("EM_MIN_INTERVAL", "1.0"))
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = _req.get(_DATACENTER_URL, params=params,
                 headers={"User-Agent": _UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ══════════════════════════════════════════════════════════════
# 热点题材归因
# ══════════════════════════════════════════════════════════════

def get_hot_stocks_with_reason(date: str = "") -> Dict[str, Any]:
    """获取同花顺当日强势股+题材归因。

    Args:
        date: 日期 YYYY-MM-DD，默认今天
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return {"error": f"同花顺热点错误: {data.get('errormsg', '')}"}

        rows = data.get("data") or []
        stocks = []
        for row in rows:
            stocks.append({
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "reason": row.get("reason", ""),
                "close": _safe_float(row.get("close")),
                "change_pct": _safe_float(row.get("zhangfu")),
                "turnover_pct": _safe_float(row.get("huanshou")),
                "amount": _safe_float(row.get("chengjiaoe")),
                "dde_net": _safe_float(row.get("ddejingliang")),
            })

        from collections import Counter
        tag_counter: Counter = Counter()
        for s in stocks:
            if s["reason"]:
                tags = [t.strip() for t in s["reason"].split("+") if t.strip()]
                tag_counter.update(tags)

        return {
            "date": date,
            "total": len(stocks),
            "stocks": stocks[:50],
            "hot_tags": tag_counter.most_common(15),
        }
    except Exception as e:
        logger.warning("get_hot_stocks_with_reason(%s) failed: %s", date, e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# 北向资金
# ══════════════════════════════════════════════════════════════

def get_northbound_flow() -> Dict[str, Any]:
    """获取北向资金实时分钟流向（同花顺 hsgtApi）。"""
    try:
        from app.market_cn.index import get_northbound_realtime
        return get_northbound_realtime()
    except Exception as e:
        logger.warning("get_northbound_flow failed: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# 概念板块归属
# ══════════════════════════════════════════════════════════════

def get_stock_concept_blocks(stock_code: str) -> Dict[str, Any]:
    """获取个股所属板块/概念归属（东财 slist）。

    Args:
        stock_code: 股票代码（如 600519）
    """
    from app.market_cn.eastmoney_search import _em_get

    code = _stock_code_normalize(stock_code)
    market_code = 1 if code.startswith("6") else 0
    params = {
        "fltt": "2", "invt": "2",
        "secid": f"{market_code}.{code}",
        "spt": "3", "pi": "0", "pz": "200", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = _em_get("https://push2.eastmoney.com/api/qt/slist/get",
                     params=params, headers=headers, timeout=15)
        d = r.json()
        diff = (d.get("data") or {}).get("diff") or {}
        items = diff.values() if isinstance(diff, dict) else diff
        boards = []
        for it in items:
            boards.append({
                "name": it.get("f14", ""),
                "code": it.get("f12", ""),
                "change_pct": it.get("f3", ""),
                "lead_stock": it.get("f128", ""),
            })
        return {
            "stock_code": code,
            "total": len(boards),
            "boards": boards,
            "concept_tags": [b["name"] for b in boards],
        }
    except Exception as e:
        logger.warning("get_stock_concept_blocks(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# 限售解禁
# ══════════════════════════════════════════════════════════════

def get_lockup_expiry(stock_code: str, forward_days: int = 90) -> Dict[str, Any]:
    """获取限售解禁日历。

    Args:
        stock_code: 股票代码（如 002475）
        forward_days: 向前看的天数，默认90天
    """
    code = _stock_code_normalize(stock_code)
    today = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=forward_days)).strftime("%Y-%m-%d")

    history_data = _em_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=15,
        sort_columns="FREE_DATE", sort_types="-1",
    )
    history = []
    for row in history_data:
        history.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": _safe_float(row.get("FREE_RATIO")),
        })

    upcoming_data = _em_datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{today}\')(FREE_DATE<=\'{end_date}\')',
        page_size=20,
        sort_columns="FREE_DATE", sort_types="1",
    )
    upcoming = []
    for row in upcoming_data:
        upcoming.append({
            "date": str(row.get("FREE_DATE", ""))[:10],
            "type": row.get("LIMITED_STOCK_TYPE", ""),
            "shares": row.get("FREE_SHARES_NUM", 0),
            "ratio": _safe_float(row.get("FREE_RATIO")),
        })

    return {
        "stock_code": code,
        "history": history,
        "upcoming": upcoming,
        "upcoming_count": len(upcoming),
    }


# ══════════════════════════════════════════════════════════════
# 行业排名
# ══════════════════════════════════════════════════════════════

def get_industry_ranking(top_n: int = 20) -> Dict[str, Any]:
    """获取行业板块涨跌幅排名（东财行业板块）。

    Args:
        top_n: 返回前N个行业，默认20
    """
    from app.market_cn.eastmoney_search import _em_get

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": _UA}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return {"top": [], "bottom": [], "total": 0}

        rows = []
        for i, item in enumerate(items):
            rows.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": _safe_float(item.get("f3")),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
                "leader_change": _safe_float(item.get("f136")),
            })

        n = min(top_n, len(rows))
        return {"top": rows[:n], "bottom": rows[-n:], "total": len(rows)}
    except Exception as e:
        logger.warning("get_industry_ranking failed: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# 龙虎榜详情
# ══════════════════════════════════════════════════════════════

def get_dragon_tiger_detail(stock_code: str, look_back_days: int = 30) -> Dict[str, Any]:
    """获取个股龙虎榜详情（席位+机构）。

    Args:
        stock_code: 股票代码（如 002475）
        look_back_days: 回看天数，默认30
    """
    code = _stock_code_normalize(stock_code)
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    data = _em_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{today}')(SECURITY_CODE=\"{code}\")",
        page_size=50,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    records = []
    for row in data:
        records.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "reason": row.get("EXPLANATION", ""),
            "net_buy_wan": round(_safe_float(row.get("BILLBOARD_NET_AMT")) / 10000, 1),
            "turnover_pct": round(_safe_float(row.get("TURNOVERRATE")), 2),
        })

    seats = {"buy": [], "sell": []}
    buy_data = []
    sell_data = []
    if records:
        latest_date = records[0]["date"]
        buy_data = _em_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="BUY", sort_types="-1",
        )
        for row in buy_data[:5]:
            seats["buy"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_wan": round(_safe_float(row.get("BUY")) / 10000, 1),
                "sell_wan": round(_safe_float(row.get("SELL")) / 10000, 1),
                "net_wan": round(_safe_float(row.get("NET")) / 10000, 1),
            })

        sell_data = _em_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="SELL", sort_types="-1",
        )
        for row in sell_data[:5]:
            seats["sell"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_wan": round(_safe_float(row.get("BUY")) / 10000, 1),
                "sell_wan": round(_safe_float(row.get("SELL")) / 10000, 1),
                "net_wan": round(_safe_float(row.get("NET")) / 10000, 1),
            })

    institution = {"buy_wan": 0, "sell_wan": 0, "net_wan": 0}
    for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail_data:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = _safe_float(row.get("BUY") if side == "buy" else row.get("SELL"))
                if side == "buy":
                    institution["buy_wan"] += amt
                else:
                    institution["sell_wan"] += amt
    institution["buy_wan"] = round(institution["buy_wan"] / 10000, 1)
    institution["sell_wan"] = round(institution["sell_wan"] / 10000, 1)
    institution["net_wan"] = round(institution["buy_wan"] - institution["sell_wan"], 1)

    return {
        "stock_code": code,
        "records": records,
        "seats": seats,
        "institution": institution,
    }
