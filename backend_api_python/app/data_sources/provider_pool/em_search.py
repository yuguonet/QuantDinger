# -*- coding: utf-8 -*-
"""provider_pool / em_search —— 榜单族东财智能选股源（em_stock_search）。

接口: POST https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code
自然语言关键词 → 选股结果（东财"智能选股"，全仓唯一实现）。

⚠ 字段名带动态后缀（2026-09-14 实证）：同一字段会以 `PEAK_PRICE<140>`（字段 ID 后缀）
  或 `PETTM{2026-09-14}`（日期后缀）出现，后缀随请求变化 ⇒ 固定键名 `s.get(name)`
  会**静默取空**（下游拿到一堆 0）。故解析层用「精确 → 前缀」两级匹配（见 `_pick`）。

⚠ 数值格式不统一：VOLUME 常是 "460.85万" 这类格式化字符串，`safe_float` 吃不下
  ⇒ 用 `_to_num` 解析 亿/万 后缀（见 `_to_num`）。
"""
from __future__ import annotations

import json
import random
import string
from datetime import datetime
from typing import Any, Dict, List

from app.data_sources.normalizer import safe_float as _safe_float
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SEARCH_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"


def _gen_id(length: int = 32) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _pick(s: Dict[str, Any], name: str) -> Any:
    """字段取值，容忍东财的动态键名后缀（精确 → 前缀两级匹配）。"""
    if name in s:
        return s[name]
    for suffix in ("<", "{"):
        for k, v in s.items():
            if isinstance(k, str) and k.startswith(name + suffix):
                return v
    return None


def _to_num(v: Any) -> float:
    """把东财的「460.85万 / 191.43亿」格式转成数值；纯数值原样，解析不动返回 0.0。"""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return 0.0
    t = v.strip()
    try:
        if t.endswith("亿"):
            return float(t[:-1]) * 1e8
        if t.endswith("万"):
            return float(t[:-1]) * 1e4
        return float(t)
    except ValueError:
        return 0.0


def fetch_em_stock_search(keyword: str = "", page_size: int = 200,
                          page_no: int = 1) -> Dict[str, Any]:
    """东财智能选股搜索。

    返回 {"rows": [{code, name, industry, concept, new_price, change_rate, ...
                    pe9, pbnewmrq, total_market_cap, free_cap}, ...],
          "total": 上游命中总数}。
    空关键词 ⇒ 直接空信封（不是失败）；请求/协议异常 ⇒ raise（单源 kind）。
    """
    import requests

    kw = str(keyword or "").strip()
    if not kw:
        return {"rows": [], "total": 0}

    body = {
        "needAmbiguousSuggest": True,
        "pageSize": min(int(page_size or 200), 200),
        "pageNo": int(page_no or 1),
        "fingerprint": _gen_id(32),
        "matchWord": "",
        "shareToGuba": False,
        "timestamp": str(int(datetime.now().timestamp() * 1000)),
        "requestId": _gen_id(32) + str(int(datetime.now().timestamp() * 1000)),
        "removedConditionIdList": [],
        "ownSelectAll": False,
        "needCorrect": True,
        "client": "WEB",
        "product": "",
        "needShowStockNum": False,
        "biz": "web_ai_select_stocks",
        "xcId": "",
        "gids": [],
        "dxInfoNew": [],
        "keyWordNew": kw,
        "customDataNew": json.dumps([{"type": "text", "value": kw, "extra": ""}]),
    }

    try:
        resp = requests.post(_SEARCH_URL, json=body,
                             headers={"Content-Type": "application/json"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("[em_search] 请求失败: %s", e)
        raise

    if str(data.get("code")) != "100":
        raise RuntimeError("东财搜索返回 code=%r msg=%r"
                           % (data.get("code"), data.get("msg")))

    res = (data.get("data") or {}).get("result") or {}
    raw_list = res.get("dataList") or []

    rows: List[Dict[str, Any]] = []
    for s in raw_list:
        if not isinstance(s, dict):
            continue
        rows.append({
            "code": str(_pick(s, "SECURITY_CODE") or ""),
            "name": str(_pick(s, "SECURITY_SHORT_NAME") or ""),
            "industry": str(_pick(s, "INDUSTRY") or ""),
            "concept": str(_pick(s, "CONCEPT") or ""),
            "new_price": _safe_float(_pick(s, "NEWEST_PRICE")),
            "change_rate": _safe_float(_pick(s, "CHG")),
            "high_price": _safe_float(_pick(s, "PEAK_PRICE")),
            "low_price": _safe_float(_pick(s, "BOTTOM_PRICE")),
            "pre_close_price": _safe_float(_pick(s, "PRE_CLOSE_PRICE")),
            "volume": _to_num(_pick(s, "VOLUME")),
            "deal_amount": _pick(s, "TRADING_VOLUMES") or _pick(s, "TRADE_AMOUNT"),
            "volume_ratio": _pick(s, "QRR"),
            "turnoverrate": _safe_float(_pick(s, "TURNOVER_RATE")),
            "amplitude": _safe_float(_pick(s, "AMPLITUDE")),
            "pe9": _pick(s, "PE_DYNAMIC") or _pick(s, "PE9"),
            "pbnewmrq": _pick(s, "PB_NEW_MRQ") or _pick(s, "PB"),
            "total_market_cap": _pick(s, "TOAL_MARKET_VALUE") or _pick(s, "TOTAL_MARKET_CAP"),
            "free_cap": _pick(s, "FREE_CAP"),
        })

    return {"rows": rows, "total": res.get("total") or len(rows)}


from app.data_sources.provider_pool import register  # noqa: E402


@register
class EastmoneySearchProvider:
    name = "eastmoney_search"
    priority = 20
    markets = {"CNStock"}
    capabilities = {
        "em_stock_search": fetch_em_stock_search,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
