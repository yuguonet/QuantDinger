"""
新浪财经数据源 — A股日K线 + 实时行情（免费、无需API Key）

数据接口:
- 日K线: vip.stock.finance.sina.com.cn/cn/api/json.php/CN_MarketDataService.getKLineData
  备用: finance.sina.com.cn/realstock/company/<code>/hisdata/klc_kl.js
- 实时行情: hq.sinajs.cn/list=<code>

特点:
- 国内直连速度快，无需 API Key / Token
- 有频率限制，需要限流 + 随机 UA
- 日K线无复权，返回原始价格
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.rate_limiter import (
    get_request_headers,
    retry_with_backoff,
    RateLimiter,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---------- 限流器 ----------

_sina_limiter = RateLimiter(
    min_interval=1.5,
    jitter_min=0.8,
    jitter_max=2.5,
)

_sina_quote_limiter = RateLimiter(
    min_interval=0.8,
    jitter_min=0.3,
    jitter_max=1.2,
)


# ---------- 代码转换 ----------

def _sina_code_from_cn(symbol: str) -> str:
    """
    Convert A-share symbol to Sina format: sh600519 / sz000001.
    Accepts: 600519, 600519.SH, sh600519, SZ000001, etc.
    """
    s = (symbol or "").strip().upper()
    if s.startswith(("SH", "SZ")) and not s.startswith(("SH.", "SZ.")):
        return s.lower()
    if s.endswith((".SH", ".SS")):
        return "sh" + s[:s.rfind(".")]
    if s.endswith(".SZ"):
        return "sz" + s[:s.rfind(".")]
    if s.isdigit() and len(s) == 6:
        return ("sh" + s) if s.startswith("6") else ("sz" + s)
    # 已经是 sh600519 格式
    if len(s) >= 3 and s[:2].lower() in ("sh", "sz") and s[2:].isdigit():
        return s.lower()
    return s.lower()


# ---------- 实时行情 ----------

def _parse_sina_quote(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse Sina hq response line:
    var hq_str_sh600519="贵州茅台,1750.00,1745.00,...";
    Fields (32+): name(0), open(1), prev_close(2), price(3), high(4), low(5),
                  buy1(6), sell1(7), volume(8), amount(9), ...
    """
    m = re.search(r'"(.+?)"', text)
    if not m:
        return None
    parts = m.group(1).split(",")
    if len(parts) < 32:
        return None
    try:
        name = parts[0].strip()
        if not name:
            return None
        open_p = float(parts[1]) if parts[1] else 0.0
        prev_close = float(parts[2]) if parts[2] else 0.0
        last = float(parts[3]) if parts[3] else 0.0
        high = float(parts[4]) if parts[4] else 0.0
        low = float(parts[5]) if parts[5] else 0.0
        volume = float(parts[8]) if parts[8] else 0.0
        amount = float(parts[9]) if parts[9] else 0.0

        # 基础校验：如果所有价格都为0，可能是停牌/无效数据
        if last == 0 and prev_close == 0 and open_p == 0:
            return None

        return {
            "name": name,
            "open": open_p,
            "prev_close": prev_close,
            "last": last,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
        }
    except (ValueError, IndexError):
        return None


@retry_with_backoff(max_attempts=3, base_delay=1.5, max_delay=10.0, exceptions=(
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
))
def fetch_sina_quote(code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """Fetch real-time quote from Sina hq.sinajs.cn."""
    sc = _sina_code_from_cn(code)
    if not sc:
        return None

    _sina_quote_limiter.wait()
    url = f"https://hq.sinajs.cn/list={sc}"
    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://finance.sina.com.cn/"),
        timeout=timeout,
    )
    resp.encoding = "gbk"
    quote = _parse_sina_quote(resp.text)
    if quote:
        quote["symbol"] = sc
        last = quote["last"]
        prev = quote["prev_close"]
        quote["change"] = round(last - prev, 4) if prev else 0.0
        quote["changePercent"] = round(quote["change"] / prev * 100, 2) if prev else 0.0
        quote["open"] = quote.get("open", last) or last
        quote["previousClose"] = prev
    return quote


# ---------- 日K线 ----------

@retry_with_backoff(max_attempts=3, base_delay=2.0, max_delay=12.0, exceptions=(
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
))
def fetch_sina_kline(
    code: str,
    count: int = 300,
    adj: str = "qfq",
    timeout: int = 10,
) -> List[Dict[str, Any]]:
    """
    Fetch daily K-line from Sina finance.

    优先使用 JSON API，失败回退到 hisdata/klc_kl.js。
    注意：Sina 日K线不支持复权，返回原始价格。
    """
    sc = _sina_code_from_cn(code)
    if not sc:
        return []

    _sina_limiter.wait()

    # 优先 JSON API
    url = "https://vip.stock.finance.sina.com.cn/cn/api/json.php/CN_MarketDataService.getKLineData"
    params = {
        "symbol": sc,
        "scale": 240,  # 240 = daily
        "ma": "no",
        "datalen": min(int(count), 2000),
    }

    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://finance.sina.com.cn/"),
        params=params,
        timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        data = None

    if isinstance(data, list) and data:
        return _parse_sina_json_kline(data, count)

    # 备用: hisdata/klc_kl
    return _fetch_sina_kline_hisdata(sc, count, timeout)


def _parse_sina_json_kline(data: list, count: int) -> List[Dict[str, Any]]:
    """Parse Sina JSON API kline response."""
    out: List[Dict[str, Any]] = []
    for item in data:
        try:
            dt_str = item.get("day", "")
            if not dt_str:
                continue
            ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
            o = float(item.get("open", 0))
            h = float(item.get("high", 0))
            low = float(item.get("low", 0))
            c = float(item.get("close", 0))
            v = float(item.get("volume", 0))
            if o == 0 and c == 0:
                continue
            out.append({
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(v, 2),
            })
        except (ValueError, TypeError, KeyError):
            continue

    out.sort(key=lambda x: x["time"])
    if len(out) > count:
        out = out[-count:]
    logger.debug("Sina kline(JSON): returned %d bars", len(out))
    return out


def _fetch_sina_kline_hisdata(
    sc: str,
    count: int,
    timeout: int,
) -> List[Dict[str, Any]]:
    """
    备用: 从 hisdata/klc_kl.js 获取日K线。
    返回的是 JS 文本，用正则解析。
    """
    url = f"https://finance.sina.com.cn/realstock/company/{sc}/hisdata/klc_kl.js"
    _sina_limiter.wait()
    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://finance.sina.com.cn/"),
        timeout=timeout,
    )
    resp.encoding = "gbk"
    text = resp.text or ""

    # 匹配: '2024-01-15,1700.00,1720.50,1695.00,1715.00,25000'
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}),\s*"
        r"([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*"
        r"([\d.]+)"
    )

    out: List[Dict[str, Any]] = []
    for m in pattern.finditer(text):
        try:
            dt_str, o, c, h, low, v = m.groups()
            ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
            o, c, h, low, v = float(o), float(c), float(h), float(low), float(v)
            if o == 0 and c == 0:
                continue
            out.append({
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(v, 2),
            })
        except (ValueError, TypeError):
            continue

    if len(out) > count:
        out = out[-count:]
    out.sort(key=lambda x: x["time"])
    logger.debug("Sina kline(hisdata): returned %d bars for %s", len(out), sc)
    return out


# ---------- 便捷接口 ----------

def sina_kline_to_ticker(code: str) -> Optional[Dict[str, Any]]:
    """Fetch Sina quote and return in CNStockDataSource.get_ticker() format."""
    q = fetch_sina_quote(code)
    if not q:
        return None
    return {
        "last": q.get("last", 0),
        "change": q.get("change", 0),
        "changePercent": q.get("changePercent", 0),
        "high": q.get("high", 0),
        "low": q.get("low", 0),
        "open": q.get("open", 0),
        "previousClose": q.get("previousClose", 0),
        "name": q.get("name", ""),
        "symbol": q.get("symbol", code),
    }
