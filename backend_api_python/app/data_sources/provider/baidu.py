# -*- coding: utf-8 -*-
"""
百度股市通数据源 Provider

模块职责:
  通过百度财经 API 获取 A股的 K线数据。

能力:
  - K线: 15m（百度API原生），其他周期返回NotSupported
  - 行情: 单只/批量实时行情
  - 全市场批量: 并发获取全市场K线

特点:
  - 国内直连，无需 API Key
  - 数据较全
  - 15m 周期数据

数据标准化:
  - time: Unix 时间戳
  - open/high/low/close: OHLC 四价
  - volume: 成交量

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → BaiduDataSource（本模块）
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 基础配置
# ================================================================

TIMEOUT = 10
THREADS_PER_SOURCE = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ================================================================
# HTTP 工具
# ================================================================

def _http_get_json(url: str, timeout: int = TIMEOUT) -> Optional[Any]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw = resp.read()
            for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
                try:
                    text = raw.decode(enc)
                    return json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            return None
    except Exception:
        return None


# ================================================================
# 代码转换
# ================================================================

def _cn(code: str) -> str:
    """提取纯数字代码"""
    c = code.strip().upper().replace(".", "").replace("SH", "").replace("SZ", "").replace("BJ", "")
    return c


# ================================================================
# 数据获取
# ================================================================

def _fetch_baidu_15m(code: str, limit: int = 200) -> Optional[List[Dict[str, Any]]]:
    """获取单只股票K线数据"""
    cn_code = _cn(code)
    url = (
        f"https://finance.pae.baidu.com/selfselect/getstockquotation?"
        f"all=1&code={cn_code}&is498=1&isBk=false&isBlock=false"
        f"&isFutures=false&isStock=true&isIndex=false"
        f"&market_type=ab&newFormat=1&group=quotation_kline_ab&finClientType=pc"
    )
    data = _http_get_json(url)
    if not data:
        return None

    r = data.get("Result") or []
    if not r:
        return None

    sd = r[0] if isinstance(r, list) else r
    k = sd.get("kline") or sd.get("dayLine") or []
    if not k:
        return None

    result = []
    for i in k:
        if not isinstance(i, dict):
            continue
        try:
            dt_str = str(i.get("date", i.get("time", "")))
            if not dt_str:
                continue
            # 百度返回的时间格式可能是 "2026-05-08 09:45" 或时间戳
            if "-" in dt_str and ":" in dt_str:
                ts = int(datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M").timestamp())
            elif "-" in dt_str:
                ts = int(datetime.strptime(dt_str[:10], "%Y-%m-%d").timestamp())
            else:
                ts = int(float(dt_str))

            o = float(i.get("open", 0) or 0)
            h = float(i.get("high", 0) or 0)
            low = float(i.get("low", 0) or 0)
            c = float(i.get("close", 0) or 0)
            v = float(i.get("volume", 0) or 0)

            if o == 0 and c == 0:
                continue

            result.append({
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(v, 2),
            })
        except (ValueError, TypeError, KeyError):
            continue

    return result[-limit:] if len(result) > limit else result


def _fetch_baidu_quote(code: str) -> Optional[Dict[str, Any]]:
    """获取单只股票实时行情 — 百度 getstockquotation 同一接口"""
    cn_code = _cn(code)
    url = (
        f"https://finance.pae.baidu.com/selfselect/getstockquotation?"
        f"all=1&code={cn_code}&is498=1&isBk=false&isBlock=false"
        f"&isFutures=false&isStock=true&isIndex=false"
        f"&market_type=ab&newFormat=1&group=quotation_kline_ab&finClientType=pc"
    )
    data = _http_get_json(url)
    if not data:
        return None

    r = data.get("Result") or []
    if not r:
        return None

    sd = r[0] if isinstance(r, list) else r
    last = float(sd.get("last", sd.get("price", 0)) or 0)
    if last <= 0:
        return None

    prev = float(sd.get("prevClose", sd.get("preClose", 0)) or 0)
    chg = round(last - prev, 4) if prev else 0

    return {
        "last": last,
        "change": chg,
        "changePercent": round(chg / prev * 100, 2) if prev else 0,
        "high": float(sd.get("high", 0) or last),
        "low": float(sd.get("low", 0) or last),
        "open": float(sd.get("open", 0) or last),
        "previousClose": prev,
        "name": str(sd.get("name", sd.get("stockName", ""))),
        "symbol": cn_code,
    }


# ================================================================
# Provider 注册
# ================================================================

@register(priority=50)
class BaiduDataSource:
    """
    百度股市通数据源 — A股数据源（priority=50）。

    能力:
      - K线: 15m
      - 全市场批量: 并发获取全市场K线

    线程安全性:
      - 纯标准库 HTTP，线程安全
    """

    name = "baidu"
    priority = 50

    capabilities = {
        "kline": True,
        "kline_priority": 50,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D"},
        "kline_batch": True,
        "kline_batch_priority": 50,
        "quote": True,
        "quote_priority": 50,
        "batch_quote": True,
        "batch_quote_priority": 50,
        "hk": False,
        "markets": {"CNStock"},
    }

    def fetch_kline(
        self, code: str, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """获取单只股票K线。百度API仅支持15m周期，其他周期返回NotSupported。"""
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_kline", f"百度API仅支持15m周期，不支持 {timeframe}")

        data = _fetch_baidu_15m(code, count)
        return data if data else []

    def fetch_market_kline(
        self, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 30,
        start_date: str = "", end_date: str = "",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """全市场批量K线。百度API仅支持15m周期。"""
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_market_kline", f"百度API仅支持15m周期，不支持 {timeframe}")

        from app.data_sources.provider import _fetch_all_cn_codes
        from queue import Queue, Empty

        codes = _fetch_all_cn_codes()
        if not codes:
            return {}

        group_size = 50
        groups = [codes[i:i + group_size] for i in range(0, len(codes), group_size)]
        q: Queue = Queue()
        for idx, g in enumerate(groups):
            q.put((idx, g))

        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()

        def _fetch_one(code):
            try:
                data = _fetch_baidu_15m(code, count)
                if data:
                    nc = code.strip().upper()
                    if nc.startswith("6"):
                        nc = "sh" + nc
                    elif nc.startswith(("0", "3")):
                        nc = "sz" + nc
                    with lock:
                        result[nc] = data
            except Exception:
                pass

        def _worker():
            while True:
                try:
                    _, stocks = q.get(timeout=5)
                except Empty:
                    break
                with ThreadPoolExecutor(max_workers=min(len(stocks), THREADS_PER_SOURCE)) as pool:
                    futs = [pool.submit(_fetch_one, s) for s in stocks]
                    for f in futs:
                        try:
                            f.result()
                        except Exception:
                            pass
                q.task_done()

        workers = []
        for _ in range(min(THREADS_PER_SOURCE, len(groups))):
            t = threading.Thread(target=_worker, daemon=True)
            workers.append(t)
            t.start()

        for t in workers:
            t.join(timeout=timeout)

        logger.info("[百度] 全市场完成: %d只", len(result))
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """获取单只股票实时行情"""
        return _fetch_baidu_quote(code)

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """批量实时行情 — 并发逐只获取"""
        result: Dict[str, Dict[str, Any]] = {}
        lock = threading.Lock()

        def _fetch(code):
            q = _fetch_baidu_quote(code)
            if q:
                with lock:
                    result[_cn(code)] = q

        max_workers = min(len(codes), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_fetch, c) for c in codes]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass

        return result
