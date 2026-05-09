# -*- coding: utf-8 -*-
"""
雪球数据源 Provider

模块职责:
  通过雪球 API 获取 A股的 K线和实时行情数据。
  雪球是国内知名投资社区，数据接口稳定。

能力:
  - K线: 1m/5m/15m/30m/1H/1D/1W（前复权），通过 chart/kline.json API
  - 单只行情: 实时行情快照
  - 全市场批量: 并发获取全市场K线

特点:
  - 需要先访问 xueqiu.com 获取 cookie
  - 数据为前复权
  - 15m 周期数据较全

数据标准化:
  - time: Unix 时间戳
  - open/high/low/close: OHLC 四价
  - volume: 成交量
  - 复权: 原生前复权

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → XueqiuDataSource（本模块）
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import normalize_cn_code
from app.data_sources.rate_limiter import get_request_headers, RateLimiter
from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 限流器
# ================================================================

_xueqiu_limiter = RateLimiter(
    min_interval=0.5,
    jitter_min=0.2,
    jitter_max=0.8,
)

# ================================================================
# Cookie 管理
# ================================================================

_cookie_lock = threading.Lock()
_cookie: Optional[str] = None
_cookie_ts: float = 0
COOKIE_TTL = 3600  # 1小时刷新


def _refresh_cookie() -> Optional[str]:
    """访问雪球首页获取 cookie"""
    global _cookie, _cookie_ts
    with _cookie_lock:
        if _cookie and time.time() - _cookie_ts < COOKIE_TTL:
            return _cookie
    try:
        resp = requests.get("https://xueqiu.com/", timeout=5, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        cookies = resp.cookies.get_dict()
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        with _cookie_lock:
            _cookie = cookie_str
            _cookie_ts = time.time()
        return cookie_str
    except Exception as e:
        logger.warning("[雪球] 获取 cookie 失败: %s", e)
        return None


def _invalidate_cookie():
    """清除缓存的 cookie，下次请求强制重新获取"""
    global _cookie, _cookie_ts
    with _cookie_lock:
        _cookie = None
        _cookie_ts = 0


def _get_headers() -> dict:
    """获取带 cookie 的请求头，cookie 为空时自动重试一次"""
    cookie = _refresh_cookie()
    if not cookie:
        _invalidate_cookie()
        cookie = _refresh_cookie()
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://xueqiu.com/",
        "Cookie": cookie or "",
    }


# ================================================================
# 代码转换
# ================================================================

def _to_xueqiu_symbol(code: str) -> str:
    """股票代码 → 雪球格式: SH600519 / SZ000001"""
    nc = normalize_cn_code(code)
    if not nc:
        return ""
    # normalize_cn_code 返回 sh600519 格式
    prefix = nc[:2].upper()
    digits = nc[2:]
    return f"{prefix}{digits}"


# ================================================================
# 数据获取
# ================================================================

# 雪球 API period 参数映射
# 分钟级: "1"=1m, "5"=5m, "15"=15m, "30"=30m, "60"=1H
# 日线级: "day"=1D, "week"=1W, "month"=1M
_XQ_TF_TO_PERIOD = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1H": "60",
    "1D": "day",
    "1W": "week",
}


def _fetch_xueqiu_kline(code: str, timeframe: str = "15m", limit: int = 200) -> Optional[List[Dict[str, Any]]]:
    """获取单只股票K线数据（前复权），支持多周期。

    支持的周期: 1m, 5m, 15m, 30m, 1H, 1D, 1W
    雪球 API 原生支持这些周期，无需额外聚合。
    """
    symbol = _to_xueqiu_symbol(code)
    if not symbol:
        return None

    period = _XQ_TF_TO_PERIOD.get(timeframe)
    if not period:
        return None  # 不支持的周期

    _xueqiu_limiter.wait()
    try:
        url = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
        params = {
            "symbol": symbol,
            "begin": int(time.time() * 1000),
            "period": period,
            "type": "before",  # 前复权
            "count": f"-{limit}",
            "indicator": "kline",
        }
        resp = requests.get(url, params=params, headers=_get_headers(), timeout=10)
        data = resp.json()

        items = (data.get("data") or {}).get("item") or []
        if not items:
            return None

        # 雪球返回: [timestamp, volume, open, high, low, close, ...]
        result = []
        for r in items:
            if len(r) < 6:
                continue
            try:
                ts = int(r[0]) / 1000  # 毫秒 → 秒
                result.append({
                    "time": int(ts),
                    "open": round(float(r[2]), 4),
                    "high": round(float(r[3]), 4),
                    "low": round(float(r[4]), 4),
                    "close": round(float(r[5]), 4),
                    "volume": round(float(r[1]), 2),
                })
            except (ValueError, TypeError, IndexError):
                continue

        return result if result else None
    except Exception as e:
        logger.debug("[雪球] fetch_kline %s %s 失败: %s", code, timeframe, e)
        return None


def _fetch_ticker(code: str) -> Optional[Dict[str, Any]]:
    """获取单只股票实时行情"""
    symbol = _to_xueqiu_symbol(code)
    if not symbol:
        return None

    _xueqiu_limiter.wait()
    try:
        url = "https://stock.xueqiu.com/v5/stock/quote.json"
        params = {"symbol": symbol, "extend": "detail"}
        resp = requests.get(url, params=params, headers=_get_headers(), timeout=8)
        data = resp.json()

        quote = (data.get("data") or {}).get("quote") or {}
        if not quote:
            return None

        last = float(quote.get("current", 0) or 0)
        prev = float(quote.get("last_close", 0) or 0)
        chg = round(last - prev, 4) if prev else 0

        return {
            "last": last,
            "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0,
            "high": float(quote.get("high", 0) or last),
            "low": float(quote.get("low", 0) or last),
            "open": float(quote.get("open", 0) or last),
            "previousClose": prev,
            "name": quote.get("name", ""),
            "symbol": symbol,
        }
    except Exception as e:
        logger.debug("[雪球] fetch_ticker %s 失败: %s", code, e)
        return None


# ================================================================
# Provider 注册
# ================================================================

@register(priority=40)
class XueqiuDataSource:
    """
    雪球数据源 — A股数据源（priority=40）。

    能力:
      - K线: 15m（前复权），通过 chart/kline.json API
      - 行情: 单只实时行情（quote.json）
      - 全市场批量: 并发获取全市场K线

    线程安全性:
      - 使用限流器控制并发
      - Cookie 线程安全刷新
    """

    name = "xueqiu"
    priority = 40

    capabilities = {
        "kline": True,
        "kline_priority": 40,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "kline_batch": True,
        "kline_batch_priority": 40,
        "quote": True,
        "quote_priority": 40,
        "batch_quote": True,
        "batch_quote_priority": 40,
        "hk": False,
        "markets": {"CNStock"},
    }

    def __init__(self):
        """初始化: 预热 cookie"""
        try:
            _refresh_cookie()
        except Exception:
            pass

    def fetch_kline(
        self, code: str, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """获取单只股票K线（前复权），支持 1m/5m/15m/30m/1H/1D/1W"""
        if timeframe not in _XQ_TF_TO_PERIOD:
            return NotSupportedResult(self.name, "fetch_kline", f"不支持 {timeframe} 周期")

        data = _fetch_xueqiu_kline(code, timeframe, count)
        return data if data else []

    def fetch_market_kline(
        self, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 30,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量K线 — 并发获取。
        支持 1m/5m/15m/30m/1H/1D/1W。
        线程结构与 akline_market.py 保持一致: 每组50只，30线程并发。
        """
        if timeframe not in _XQ_TF_TO_PERIOD:
            return NotSupportedResult(self.name, "fetch_market_kline", f"不支持 {timeframe} 周期")

        from queue import Queue, Empty

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning("[雪球] 获取股票列表失败")
            return {}

        group_size = 50
        groups = [symbols[i:i + group_size] for i in range(0, len(symbols), group_size)]
        q: Queue = Queue()
        for idx, g in enumerate(groups):
            q.put((idx, g))

        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()
        threads_per_source = 30

        def _fetch_one(code):
            try:
                data = _fetch_xueqiu_kline(code, timeframe, count)
                if data:
                    with lock:
                        result[normalize_cn_code(code)] = data
            except Exception:
                pass

        def _worker():
            while True:
                try:
                    _, stocks = q.get(timeout=5)
                except Empty:
                    break
                with ThreadPoolExecutor(max_workers=min(len(stocks), threads_per_source)) as pool:
                    futs = [pool.submit(_fetch_one, s) for s in stocks]
                    for f in futs:
                        try:
                            f.result()
                        except Exception:
                            pass
                q.task_done()

        workers = []
        for _ in range(min(threads_per_source, len(groups))):
            t = threading.Thread(target=_worker, daemon=True)
            workers.append(t)
            t.start()

        for t in workers:
            t.join(timeout=timeout)

        logger.info("[雪球] 全市场完成: %d只", len(result))
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """获取单只股票实时行情"""
        return _fetch_ticker(code)

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """不支持批量行情（逐个获取）"""
        result: Dict[str, Dict[str, Any]] = {}
        lock = threading.Lock()

        def _fetch(code):
            q = _fetch_ticker(code)
            if q:
                with lock:
                    result[normalize_cn_code(code)] = q

        max_workers = min(len(codes), 5)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_fetch, c) for c in codes]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass

        return result
