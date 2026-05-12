# -*- coding: utf-8 -*-
"""
搜狐财经数据源 Provider

模块职责:
  通过搜狐财经 API 获取 A股的 K线数据。

能力:
  - K线: 15m（不复权，需转前复权），其他周期返回NotSupported
  - 全市场批量: 并发获取全市场K线

特点:
  - 国内直连，无需 API Key
  - 不复权数据，需自行转换前复权
  - 15m 周期返回历史数据

数据标准化:
  - time: Unix 时间戳
  - open/high/low/close: OHLC 四价
  - volume: 成交量
  - 复权: 不复权 → 通过 TDX 除权除息数据转前复权

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → SohuDataSource（本模块）
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_TZ_CN = timezone(timedelta(hours=8))

from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 基础配置
# ================================================================

TIMEOUT = 10
THREADS_PER_SOURCE = 8

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
# 前复权（共享模块）
# ================================================================
from app.data_sources.provider.adjustment import apply_fwd_adjust as _apply_fwd_adjust


# ================================================================
# 数据获取
# ================================================================

def _fetch_sohu_15m(code: str, limit: int = 200) -> Optional[List[Dict[str, Any]]]:
    """获取单只股票15分钟K线（不复权）"""
    cn_code = _cn(code)
    url = f"https://q.stock.sohu.com/hisHq?code=cn_{cn_code}&start=20260101&end=20261231&period=15"
    data = _http_get_json(url)
    if not data or not isinstance(data, list):
        return None

    hq = data[0].get("hq") or []
    if not hq:
        return None

    # 搜狐返回: [日期, 开盘, 收盘, 最高, 最低, 成交量, ...]
    result = []
    for r in hq:
        if len(r) < 6:
            continue
        try:
            dt_str = str(r[0])
            # 解析时间 "2026-05-08 09:45"
            ts = dt_str[:16]  # "YYYY-MM-DD HH:MM"
            result.append({
                "time": ts,
                "open": round(float(r[1]), 4),
                "high": round(float(r[3]), 4),
                "low": round(float(r[4]), 4),
                "close": round(float(r[2]), 4),
                "volume": round(float(r[5]), 2),
            })
        except (ValueError, TypeError, IndexError):
            continue

    return result[-limit:] if len(result) > limit else result


# ================================================================
# Provider 注册
# ================================================================

@register(priority=45)
class SohuDataSource:
    """
    搜狐财经数据源 — A股数据源（priority=45）。

    能力:
      - K线: 15m（不复权，需转前复权）
      - 全市场批量: 并发获取全市场K线

    线程安全性:
      - 纯标准库 HTTP，线程安全
    """

    name = "sohu"
    priority = 45

    capabilities = {
        "kline": True,
        "kline_priority": 45,
        "kline_tf": {"15m"},
        "kline_batch": True,
        "kline_batch_priority": 45,
        "quote": False,
        "batch_quote": False,
        "hk": False,
        "markets": {"CNStock"},
    }

    def __init__(self):
        pass

    def fetch_kline(
        self, code: str, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """获取单只股票K线。搜狐API仅支持15m周期，其他周期返回NotSupported。"""
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_kline", f"搜狐API仅支持15m周期，不支持 {timeframe}")

        data = _fetch_sohu_15m(code, count)
        if not data:
            return []

        # 前复权处理
        if adj == "qfq":
            data = _apply_fwd_adjust(data, code)

        return data

    def fetch_market_kline(
        self, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 30,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """全市场批量K线。搜狐API仅支持15m周期。"""
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_market_kline", f"搜狐API仅支持15m周期，不支持 {timeframe}")

        from queue import Queue, Empty

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            return {}

        group_size = 50
        groups = [symbols[i:i + group_size] for i in range(0, len(symbols), group_size)]
        q: Queue = Queue()
        for idx, g in enumerate(groups):
            q.put((idx, g))

        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()

        def _fetch_one(code):
            try:
                data = _fetch_sohu_15m(code, count)
                if data:
                    if adj == "qfq":
                        data = _apply_fwd_adjust(data, code)
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

        logger.info("[搜狐] 全市场完成: %d只", len(result))
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """不支持实时行情"""
        return NotSupportedResult(self.name, "fetch_ticker")

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """不支持批量行情"""
        return NotSupportedResult(self.name, "fetch_batch_quotes")
