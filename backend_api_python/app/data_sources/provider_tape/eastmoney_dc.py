# -*- coding: utf-8 -*-
"""provider_tape / eastmoney_dc —— 分笔/盘口族东财数据中心源。

datacenter-web.eastmoney.com 可达（非 push2），提供历史分笔/成交明细。
⚠️ 实时五档盘口需 push2.eastmoney.com，本部署环境被出口拦截（curl 56/52），
   orderbook 显式返回 NotSupported。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
_EM_MIN_INTERVAL = 1.0
_last_call = 0.0


def _em_datacenter(report_name: str, columns: str = "ALL",
                   filter_str: str = "", page_size: int = 50,
                   sort_columns: str = "", sort_types: str = "-1") -> list:
    """东财数据中心 API 封装（内部限流 1s/次）。"""
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _EM_MIN_INTERVAL:
        time.sleep(_EM_MIN_INTERVAL - elapsed)

    import requests
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = requests.get(_DATACENTER_URL, params=params,
                         headers=dict(_HTTP_HEADERS), timeout=15)
        _last_call = time.time()
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
        return []
    except Exception as e:
        logger.warning("[em_dc] %s failed: %s", report_name, e)
        return []


def _orderbook(code: str) -> Dict[str, Any]:
    """五档盘口（东财 push2 被拦截 → 降级为 sohu 快照，无 bid/ask 但有价格数据）。

    push2 本环境不可用（curl 56/52），改用 sohu 心跳接口获取基本行情。
    bid/ask 置空 dict（比 None 强：下游可读 last/open/high/low）。
    """
    import urllib.request
    import re

    c = (code or "").strip().zfill(6)
    # sohu 心跳接口: hq.stock.sohu.com/cn/{code_last3}/{biz_code}-1.html
    code_last3 = c[3:]
    biz_code = f"cn_{c}"
    url = f"https://hq.stock.sohu.com/cn/{code_last3}/{biz_code}-1.html"

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[em_dc] orderbook %s sohu fallback 失败: %s", code, e)
        return {"object": None}

    # 解析 sohu 心跳: fortune_hq({'price_A1':['cn_600519','贵州茅台','1349.98','+14.98','1.12%',...],
    #                            'price_A2':['1340.00',...,'41599',...],
    #                            'price_A3':['1349.98',...]})
    # price_A1: [code, name, last, change, change_pct, ...]
    # price_A2: [prev_close, ?, ?, open, ?, high, pct, low, vol(手), amount(万), ...]
    m1 = re.search(r"'price_A1':\[([^\]]+)\]", raw)
    m2 = re.search(r"'price_A2':\[([^\]]+)\]", raw)
    if not m1:
        return {"object": None}

    p1 = re.findall(r"'([^']*)'", m1.group(1))
    p2 = re.findall(r"'([^']*)'", m2.group(1)) if m2 else []

    if len(p1) < 5:
        return {"object": None}

    try:
        name = p1[1]
        last = float(p1[2])
        chg = float(p1[3].replace("+", ""))
        chg_pct = float(p1[4].rstrip("%"))
    except (ValueError, IndexError):
        return {"object": None}

    # price_A2 提取 prev_close/open/high/low/vol
    prev, open_p, high, low, vol = 0, 0, 0, 0, 0
    if len(p2) >= 9:
        try:
            prev = float(p2[0])
            open_p = float(p2[3]) if p2[3] not in ("--", "") else last
            high = float(p2[5]) if p2[5] not in ("--", "") else last
            low = float(p2[7]) if p2[7] not in ("--", "") else last
            vol = float(p2[8]) * 100  # sohu vol 为"手"→×100 转"股"
        except (ValueError, IndexError):
            pass

    from app.data_sources.provider import normalize_quote
    result = {
        "last": last,
        "change": chg,
        "changePercent": chg_pct,
        "high": high or last,
        "low": low or last,
        "open": open_p or last,
        "previousClose": prev or round(last - chg, 4),
        "volume": vol,
        "time": "",
        "name": name,
        "symbol": code,
        "bid": {},
        "ask": {},
    }
    normalize_quote(result)
    return {"object": result}


def fetch_ticks_today(code: str, limit: int = 2000) -> Dict[str, Any]:
    """当日分笔（东财 datacenter 无此接口）。"""
    return {"rows": [], "error": "eastmoney_dc 无当日分笔 API"}


def fetch_ticks_history(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """历史分笔成交明细（东财 datacenter RPT_TRANSACTION_DET）。

    返回: {"rows": [{time, price, vol, amount, buy_sell}, ...]}
    """
    # 东财成交明细: RPT_TRANSACTION_DET (DATA_CODE + TRADE_DATE)
    c = (code or "").strip().zfill(6)
    date_fmt = date.replace("-", "")

    data = _em_datacenter(
        "RPT_TRANSACTION_DET",
        columns="ALL",
        filter_str=f'(DATA_CODE="{c}")(TRADE_DATE=\'{date_fmt}\')',
        page_size=min(limit, 500),
        sort_columns="TRADE_TIME",
        sort_types="1",
    )

    if not data:
        return {"rows": [], "error": "empty"}

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            t = str(item.get("TRADE_TIME", ""))
            p = float(item.get("PRICE", 0))
            v = float(item.get("VOLUME", 0))
            a = float(item.get("AMOUNT", 0))
            # 东财 BUY_SELL: "B"=买 "S"=卖 "M"=中性
            bs_raw = str(item.get("BUY_SELL", "") or item.get("DIRECTION", "")).upper()
            if bs_raw in ("B", "BUY", "1"):
                buy_sell = "0"
            elif bs_raw in ("S", "SELL", "2"):
                buy_sell = "1"
            else:
                buy_sell = "2"

            rows.append({
                "time": t,
                "price": p,
                "vol": v,
                "amount": a,
                "buy_sell": buy_sell,
            })
        except (ValueError, TypeError):
            continue

    return {"rows": rows[:limit]}


from app.data_sources.provider_tape import register  # noqa: E402


@register
class EastmoneyDcTapeProvider:
    name = "eastmoney_dc"
    priority = 25
    markets = {"CNStock"}
    capabilities = {
        "orderbook": _orderbook,
        "ticks_today": fetch_ticks_today,
        "ticks_history": fetch_ticks_history,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
