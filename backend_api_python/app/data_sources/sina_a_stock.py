"""
新浪 A股数据源 — 市场快照 + 涨跌筛选

数据接口:
- 全市场行情: vip.stock.finance.sina.com.cn (Market_Center.getHQNodeData)
- 从行情中筛选涨停/跌停股票

特点:
- 免费、无需 API Key、国内直连快
- 只有行情数据，没有龙虎榜/炸板池等结构化数据
- 涨停池缺少连板天数、封板资金、涨停时间等细节
- 适合作为市场快照的 fallback 源
"""

from __future__ import annotations

import json
import json
import time
from typing import Any, Dict, List

import requests

from app.data_sources.rate_limiter import get_request_headers
from app.data_sources.normalizer import (
    normalize_market_snapshot,
    normalize_zt_pool,
    normalize_dt_pool,
    _sf, _si, _ss,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SINA_REFERER = "https://finance.sina.com.cn/"

# 涨跌停判断阈值 (含容差)
_ZT_THRESHOLDS = {10: (9.5, 10.5), 20: (19.5, 20.5), 30: (29.5, 30.5), 5: (4.5, 5.5)}
_DT_THRESHOLDS = {10: (-10.5, -9.5), 20: (-20.5, -19.5), 30: (-30.5, -29.5), 5: (-5.5, -4.5)}

# 全市场行情缓存 (避免重复分页请求)
_stocks_cache: Dict[str, Any] = {"data": None, "ts": 0}
_CACHE_TTL = 120  # 2分钟


def _fetch_all_stocks(max_pages: int = 70, page_size: int = 80) -> List[Dict[str, Any]]:
    """
    从新浪获取全部A股行情数据 (分页)。带 2 分钟缓存，避免多次请求。

    Returns:
        [{"symbol":"sz301513", "code":"301513", "name":"...", "changepercent":10.02, ...}, ...]
    """
    now = time.time()
    if _stocks_cache["data"] is not None and (now - _stocks_cache["ts"]) < _CACHE_TTL:
        return _stocks_cache["data"]

    all_stocks = []
    page = 1

    while page <= max_pages:
        url = (
            f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeData?page={page}&num={page_size}"
            f"&sort=changepercent&asc=0&node=hs_a"
        )
        try:
            resp = requests.get(url, headers=get_request_headers(referer=_SINA_REFERER), timeout=10)
            data = json.loads(resp.text)
            if not data or not isinstance(data, list):
                break
            all_stocks.extend(data)
            if len(data) < page_size:
                break
            page += 1
        except Exception as e:
            logger.debug(f"[Sina] fetch page {page} failed: {e}")
            break

    logger.debug(f"[Sina] fetched {len(all_stocks)} stocks across {page} pages")
    _stocks_cache["data"] = all_stocks
    _stocks_cache["ts"] = now
    return all_stocks


def _is_zt(pct: float) -> bool:
    """判断是否涨停"""
    for low, high in _ZT_THRESHOLDS.values():
        if low <= pct <= high:
            return True
    return False


def _is_dt(pct: float) -> bool:
    """判断是否跌停"""
    for low, high in _DT_THRESHOLDS.values():
        if low <= pct <= high:
            return True
    return False


# ---------- 市场快照 ----------

def fetch_sina_market_snapshot() -> Dict[str, Any]:
    """
    从新浪获取市场涨跌快照。

    Returns:
        标准化市场快照 dict (见 normalizer.MARKET_SNAPSHOT_SCHEMA)
    """
    stocks = _fetch_all_stocks()
    if not stocks:
        return {}

    up = down = flat = limit_up = limit_down = 0
    total_amount = 0.0

    for s in stocks:
        pct = _sf(s.get("changepercent", 0))
        amount = _sf(s.get("amount", 0))
        total_amount += amount

        if pct > 0.01:
            up += 1
            if _is_zt(pct):
                limit_up += 1
        elif pct < -0.01:
            down += 1
            if _is_dt(pct):
                limit_down += 1
        else:
            flat += 1

    raw = {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "total_amount": round(total_amount / 1e8, 2),
        "emotion": 50,  # auto-calculated in normalize
        "north_net_flow": 0.0,
    }

    return normalize_market_snapshot(raw)


# ---------- 涨停池 (简易版) ----------

def fetch_sina_zt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """
    从新浪筛选涨停股。

    注意: 只有基础行情数据，缺少连板天数、封板资金、涨停时间等东财专有字段。
    """
    stocks = _fetch_all_stocks()
    if not stocks:
        return []

    result = []
    for s in stocks:
        pct = _sf(s.get("changepercent", 0))
        if not _is_zt(pct):
            continue

        code = _ss(s.get("code", ""))
        if not code:
            continue

        raw = {
            "stock_code": code,
            "stock_name": _ss(s.get("name", "")),
            "trade_date": trade_date,
            "price": _sf(s.get("trade")),
            "change_percent": pct,
            "continuous_zt_days": 0,  # 新浪无法获取
            "zt_time": "",
            "seal_amount": 0,         # 新浪无法获取
            "turnover_rate": _sf(s.get("turnoverratio")),
            "volume": _sf(s.get("amount")),
            "amount": _sf(s.get("amount")),
            "sector": "",
            "reason": "",
            "open_count": 0,
        }
        result.append(normalize_zt_pool(raw, source="sina", trade_date=trade_date))

    logger.info(f"[Sina] zt_pool: {len(result)} stocks")
    return result


# ---------- 跌停池 (简易版) ----------

def fetch_sina_dt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """
    从新浪筛选跌停股。
    """
    stocks = _fetch_all_stocks()
    if not stocks:
        return []

    result = []
    for s in stocks:
        pct = _sf(s.get("changepercent", 0))
        if not _is_dt(pct):
            continue

        code = _ss(s.get("code", ""))
        if not code:
            continue

        raw = {
            "stock_code": code,
            "stock_name": _ss(s.get("name", "")),
            "trade_date": trade_date,
            "price": _sf(s.get("trade")),
            "change_percent": pct,
            "seal_amount": 0,
            "turnover_rate": _sf(s.get("turnoverratio")),
            "amount": _sf(s.get("amount")),
        }
        result.append(normalize_dt_pool(raw, source="sina", trade_date=trade_date))

    logger.info(f"[Sina] dt_pool: {len(result)} stocks")
    return result
