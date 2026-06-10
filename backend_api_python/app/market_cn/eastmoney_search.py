"""
东财智能选股搜索 — 正本 (single source of truth)

所有东财 search-code API 调用统一经此模块。
被以下模块引用:
  - app.market_cn.dragon_limit (龙虎榜/涨跌停池)
  - app.market_cn.cards.* (前端卡片)
  - app.agent.tools.market_data_tools (Agent 工具)
  - app.agent.tools.screener_tools (选股工具)

前端调用: GET /api/shichang/search?keyword=xxx&page_size=200
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

# 东财智能选股接口
_EM_SEARCH_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"


def _gen_id(length: int = 32) -> str:
    import random, string
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def search_stocks(
    keyword: str,
    page_size: int = 200,
    page_no: int = 1,
    timeout: int = 15,
) -> Dict[str, Any]:
    """
    调东财智能选股接口，返回标准化结果。

    Args:
        keyword:   搜索关键词（自然语言，如 "市盈率低于20的科技股"）
        page_size: 每页条数，最大 200
        page_no:   页码
        timeout:   请求超时秒数

    Returns:
        {
            "code": 1,
            "keyword": str,
            "total": int,
            "page_no": int,
            "page_size": int,
            "stocks": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "industry": "",
                    "concept": "",
                    "new_price": 1800.0,
                    "change_rate": 1.25,
                    "high_price": 1810.0,
                    "low_price": 1790.0,
                    "pre_close_price": 1780.0,
                    "volume": 123456.0,
                    "deal_amount": "223456789",
                    "volume_ratio": 1.2,
                    "turnoverrate": 0.35,
                    "amplitude": 1.12,
                    "pe9": 33.5,
                    "pbnewmrq": 10.2,
                    "total_market_cap": "2260000000000",
                    "free_cap": "2260000000000",
                },
                ...
            ]
        }
    """
    if not keyword or not keyword.strip():
        return {"code": 0, "msg": "keyword 不能为空", "stocks": []}

    body = {
        "needAmbiguousSuggest": True,
        "pageSize": min(page_size, 200),
        "pageNo": page_no,
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
        "keyWordNew": keyword.strip(),
        "customDataNew": json.dumps([{"type": "text", "value": keyword.strip(), "extra": ""}]),
    }

    try:
        resp = requests.post(
            _EM_SEARCH_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("[东财搜索] 请求失败: %s", e)
        return {"code": 0, "msg": f"请求失败: {e}", "stocks": []}
    except ValueError:
        logger.warning("[东财搜索] 响应非 JSON")
        return {"code": 0, "msg": "响应格式错误", "stocks": []}

    if str(data.get("code")) != "100":
        return {"code": 0, "msg": data.get("msg", "搜索失败"), "stocks": []}

    res = (data.get("data") or {}).get("result") or {}
    raw_list = res.get("dataList") or []

    stocks: List[Dict[str, Any]] = []
    for s in raw_list:
        if not isinstance(s, dict):
            continue
        stocks.append({
            "code": str(s.get("SECURITY_CODE", "")),
            "name": str(s.get("SECURITY_SHORT_NAME", "")),
            "industry": str(s.get("INDUSTRY", "")),
            "concept": str(s.get("CONCEPT", "")),
            "new_price": _safe_float(s.get("NEWEST_PRICE")),
            "change_rate": _safe_float(s.get("CHG")),
            "high_price": _safe_float(s.get("HIGH_PRICE")),
            "low_price": _safe_float(s.get("LOW_PRICE")),
            "pre_close_price": _safe_float(s.get("PRE_CLOSE_PRICE")),
            "volume": _safe_float(s.get("TRADE_VOLUME")),
            "deal_amount": s.get("TRADING_VOLUMES") or s.get("TRADE_AMOUNT"),
            "volume_ratio": s.get("QRR"),
            "turnoverrate": _safe_float(s.get("TURNOVER_RATE")),
            "amplitude": _safe_float(s.get("AMPLITUDE")),
            "pe9": s.get("PE_DYNAMIC") or s.get("PE9"),
            "pbnewmrq": s.get("PB_NEW_MRQ"),
            "total_market_cap": s.get("TOEAL_MARKET_VALUE") or s.get("TOTAL_MARKET_CAP"),
            "free_cap": s.get("FREE_CAP"),
        })

    return {
        "code": 1,
        "keyword": keyword.strip(),
        "total": res.get("total") or len(stocks),
        "page_no": page_no,
        "page_size": page_size,
        "stocks": stocks,
    }


def _em_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> requests.Response:
    """东财通用 GET 请求封装。

    被 research_tools / signal_tools 等模块调用。
    返回原始 Response 对象，由调用方自行 .json() 解析。

    Args:
        url:     请求地址
        params:  URL 参数
        headers: 请求头
        timeout: 超时秒数

    Returns:
        requests.Response

    Raises:
        requests.RequestException: 网络/HTTP 错误
    """
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if headers:
        default_headers.update(headers)

    resp = requests.get(url, params=params, headers=default_headers, timeout=timeout)
    resp.raise_for_status()
    return resp
