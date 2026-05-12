# -*- coding: utf-8 -*-
"""
新浪财经数据源 Provider

模块职责:
  通过新浪财经 API 获取 A股的 K线和实时行情数据。
  新浪是国内直连、无需API Key的数据源，速度较快，作为A股第二选择（priority=20）。

能力:
  - K线: 日线 + 分钟线（1m/5m/15m/30m/1H），支持前/后复权
  - 单只行情: 实时行情快照（hq.sinajs.cn）
  - 批量行情: 单次HTTP获取多只股票行情（每批最多500只）

特点:
  - 国内直连，无需API Key
  - 行情响应速度快（hq.sinajs.cn 是经典接口）
  - K线数据通过正则解析 hisdata 页面（兜底机制）

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → SinaDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - re: 正则表达式（解析 hisdata 页面）
  - app.data_sources.normalizer: 股票代码标准化（to_sina_code）
  - app.data_sources.rate_limiter: 限流器
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_TZ_CN = timezone(timedelta(hours=8))

import requests

from app.data_sources.normalizer import normalize_cn_code as to_sina_code
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff, RateLimiter, get_shared_session,
)
from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# Referer 轮换池 — 提高访问成功率
# ================================================================

class _RefererPool:
    """线程安全的 Referer 轮换池"""

    def __init__(self, referers: List[str]):
        self._referers = referers
        self._cycle = itertools.cycle(referers)
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            return next(self._cycle)


# 新浪 K线接口 Referer 池
_sina_kline_referers = _RefererPool([
    "https://finance.sina.com.cn/",
    "https://stock.finance.sina.com.cn/",
    "https://vip.stock.finance.sina.com.cn/",
    "https://money.finance.sina.com.cn/",
])

# 新浪行情接口 Referer 池
_sina_quote_referers = _RefererPool([
    "https://finance.sina.com.cn/",
    "https://hq.sinajs.cn/",
    "https://stock.finance.sina.com.cn/",
    "https://money.finance.sina.com.cn/",
])


# ================================================================
# 限流器
# ================================================================

# K线请求限流器: 最小间隔1.5秒，抖动0.8-2.5秒
_sina_limiter = RateLimiter(
    min_interval=1.5,
    jitter_min=0.8,
    jitter_max=2.5,
)

# 行情请求限流器: 最小间隔0.8秒，抖动0.3-1.2秒（行情可以更快）
_sina_quote_limiter = RateLimiter(
    min_interval=0.8,
    jitter_min=0.3,
    jitter_max=1.2,
)
# ================================================================

# 新浪周期 → scale 参数映射
# scale 表示每根K线的分钟数（日线固定为240分钟）
_SINA_TF_TO_SCALE = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "1D": 240,
}


def _parse_sina_quote(text: str) -> Optional[Dict[str, Any]]:
    """解析新浪行情响应文本"""
    m = re.search(r'\"(.+?)\"', text)
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
        if last == 0 and prev_close == 0 and open_p == 0:
            return None
        return {
            "name": name, "open": open_p, "prev_close": prev_close,
            "last": last, "high": high, "low": low,
            "volume": volume, "amount": amount,
        }
    except (ValueError, IndexError):
        return None


def _sina_kline_to_dicts(data: list, count: int) -> List[Dict[str, Any]]:
    """将新浪K线JSON数据转换为标准化字典列表"""
    out: List[Dict[str, Any]] = []
    for item in data:
        try:
            dt_str = str(item.get("day", "")).strip()
            if not dt_str:
                continue
            o = float(item.get("open", 0))
            h = float(item.get("high", 0))
            low = float(item.get("low", 0))
            c = float(item.get("close", 0))
            v = float(item.get("volume", 0))
            if o == 0 and c == 0:
                continue
            out.append({
                "time": dt_str, "open": round(o, 4), "high": round(h, 4),
                "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
            })
        except (ValueError, TypeError, KeyError):
            continue
    out.sort(key=lambda x: x["time"])
    return out[-count:] if len(out) > count else out


def _fetch_sina_kline_hisdata(sc: str, count: int, timeout: int) -> List[Dict[str, Any]]:
    """通过新浪 hisdata 页面获取日线K线（兜底机制）"""
    url = f"https://finance.sina.com.cn/realstock/company/{sc}/hisdata/klc_kl.js"
    _sina_limiter.wait()
    resp = get_shared_session().get(
        url,
        headers=get_request_headers(referer=_sina_kline_referers.next()),
        timeout=timeout,
    )
    resp.encoding = "gbk"
    text = resp.text or ""

    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}),\s*"
        r"([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*"
        r"([\d.]+)"
    )
    out: List[Dict[str, Any]] = []
    for m in pattern.finditer(text):
        try:
            dt_str, o, c, h, low, v = m.groups()
            o, c, h, low, v = float(o), float(c), float(h), float(low), float(v)
            if o == 0 and c == 0:
                continue
            out.append({
                "time": dt_str, "open": round(o, 4), "high": round(h, 4),
                "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
            })
        except (ValueError, TypeError):
            continue
    if len(out) > count:
        out = out[-count:]
    out.sort(key=lambda x: x["time"])
    return out


# ═══════════════ 前复权（共享模块）═══════════════
from app.data_sources.provider.adjustment import apply_fwd_adjust as _apply_fwd_adjust


@register(priority=20)
class SinaDataSource:
    """
    新浪财经数据源 — A股第二选择（priority=20）。

    能力:
      - K线: 日线（JSON API + hisdata 兜底）+ 分钟线（JSONP API）
      - 行情: 单只实时行情（hq.sinajs.cn）
      - 批量行情: 单次HTTP获取多只（最多500只/批）
      - 全市场行情: 多批次拼接（每批500只，通过东财获取代码列表）

    线程安全性:
      - 实例方法无状态，线程安全
      - 使用独立的限流器（_sina_limiter / _sina_quote_limiter）
    """

    name = "sina"
    priority = 15

    capabilities = {
        "kline": True,
        "kline_priority": 10,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D"},
        "kline_batch": True,
        "quote": True,
        "quote_priority": 15,
        "batch_quote": True,
        "batch_quote_priority": 15,
        "hk": False,
        "markets": {"CNStock"},
    }

    def fetch_market_kline(
        self, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 15,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """全市场批量K线 — 并发 fetch_kline，支持历史数据"""
        from app.data_sources.provider import _batch_fetch_kline_by_codes
        return _batch_fetch_kline_by_codes(
            self, timeframe=timeframe, count=count, adj=adj, timeout=timeout,
            start_date=start_date, end_date=end_date, batch_size=500,
            symbols=symbols,
        )

    @retry_with_backoff(max_attempts=3, base_delay=1.5, max_delay=10.0, exceptions=(
        requests.exceptions.RequestException, ConnectionError, TimeoutError,
    ))
    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        if start_date:
            from app.data_sources.provider import calc_kline_count
            count = calc_kline_count(timeframe, start_date, end_date)

        sc = to_sina_code(code)
        if not sc:
            return []
        scale = _SINA_TF_TO_SCALE.get(timeframe)
        if scale is None:
            return []
        _sina_limiter.wait()
        if timeframe != "1D":
            bars = self._fetch_minute_kline(sc, scale, count, timeout)
        else:
            bars = self._fetch_raw_daily_kline(sc, count, timeout)
        if bars and adj in ("qfq", "hfq"):
            bars = _apply_fwd_adjust(bars, code)
        return bars

    def _fetch_raw_daily_kline(self, sc: str, count: int, timeout: int) -> List[Dict[str, Any]]:
        # 主接口: money.finance（返回纯JSON，稳定）
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": sc, "scale": 240, "ma": "no", "datalen": min(int(count), 2000)}
        _sina_limiter.wait()
        resp = get_shared_session().get(
            url,
            headers=get_request_headers(referer=_sina_kline_referers.next()),
            params=params, timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, list) and data:
            return _sina_kline_to_dicts(data, count)

        # 备选: vip.stock（URL已变更，保留作为备用）
        url2 = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        try:
            resp2 = get_shared_session().get(
                url2,
                headers=get_request_headers(referer=_sina_kline_referers.next()),
                params=params, timeout=timeout,
            )
            data2 = resp2.json()
        except Exception:
            data2 = None
        if isinstance(data2, list) and data2:
            return _sina_kline_to_dicts(data2, count)

        # 兜底: hisdata 页面解析
        return _fetch_sina_kline_hisdata(sc, count, timeout)

    def _fetch_minute_kline(self, sc: str, scale: int, count: int, timeout: int) -> List[Dict[str, Any]]:
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var/CN_MarketDataService.getKLineData"
        params = {"symbol": sc, "scale": scale, "ma": "no", "datalen": min(int(count), 2000)}
        resp = get_shared_session().get(
            url,
            headers=get_request_headers(referer=_sina_kline_referers.next()),
            params=params, timeout=timeout,
        )
        text = (resp.text or "").strip()
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group())
        except Exception:
            return []
        return _sina_kline_to_dicts(data, count) if isinstance(data, list) else []

    @retry_with_backoff(max_attempts=3, base_delay=1.5, max_delay=10.0, exceptions=(
        requests.exceptions.RequestException, ConnectionError, TimeoutError,
    ))
    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        sc = to_sina_code(code)
        if not sc:
            return None
        _sina_quote_limiter.wait()
        headers = get_request_headers(referer=_sina_quote_referers.next())
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        resp = get_shared_session().get(
            f"https://hq.sinajs.cn/list={sc}",
            headers=headers,
            timeout=timeout,
        )
        resp.encoding = "gbk"
        quote = _parse_sina_quote(resp.text)
        if not quote:
            return None
        last = quote["last"]
        prev = quote["prev_close"]
        chg = round(last - prev, 4) if prev else 0.0
        vol = quote.get("volume", 0)
        time_str = ""
        parts_raw = re.search(r'\"(.+?)\"', resp.text)
        if parts_raw:
            p = parts_raw.group(1).split(",")
            if len(p) > 31 and p[30] and p[31]:
                time_str = f"{p[30].strip()} {p[31].strip()}"
        return {
            "last": last,
            "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
            "high": quote.get("high", last),
            "low": quote.get("low", last),
            "open": quote.get("open", last) or last,
            "previousClose": prev,
            "volume": vol, "time": time_str,
            "name": quote.get("name", ""),
            "symbol": sc,
        }

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        if not codes:
            return {}
        sina_codes = [to_sina_code(c) for c in codes if c]
        if not sina_codes:
            return {}

        batch_size = 500
        batches = [sina_codes[i:i + batch_size] for i in range(0, len(sina_codes), batch_size)]

        if len(batches) <= 1:
            # 只有 1 批，直接串行，没必要开线程池
            result: Dict[str, Dict[str, Any]] = {}
            self._fetch_single_quote_batch(batches[0], result, timeout)
            return result

        # 多批并发
        import concurrent.futures
        result: Dict[str, Dict[str, Any]] = {}
        lock = threading.Lock()
        max_workers = min(len(batches), 5)

        def _fetch_batch(batch):
            local: Dict[str, Dict[str, Any]] = {}
            self._fetch_single_quote_batch(batch, local, timeout)
            if local:
                with lock:
                    result.update(local)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_batch, b) for b in batches]
            concurrent.futures.wait(futures, timeout=timeout + 5)

        return result

    def _fetch_single_quote_batch(
        self, batch: List[str], result: Dict[str, Dict[str, Any]], timeout: int
    ):
        """单批次行情请求（内部辅助，供并发调用）"""
        query = ",".join(batch)
        _sina_quote_limiter.wait()
        headers = get_request_headers(referer=_sina_quote_referers.next())
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        try:
            resp = get_shared_session().get(
                f"https://hq.sinajs.cn/list={query}",
                headers=headers,
                timeout=timeout,
            )
            resp.encoding = "gbk"
        except Exception as e:
            logger.warning("[新浪批量行情] 请求失败: %s", e)
            return

        for line in (resp.text or "").strip().split("\n"):
            line = line.strip().rstrip(";")
            m = re.search(r'hq_str_(\w+)="(.+?)"', line)
            if not m:
                continue
            code_str = m.group(1)
            data = m.group(2)
            parts = data.split(",")
            if len(parts) < 6:
                continue
            try:
                name = parts[0].strip()
                if not name:
                    continue
                open_p = float(parts[1]) if parts[1] else 0.0
                prev_close = float(parts[2]) if parts[2] else 0.0
                last = float(parts[3]) if parts[3] else 0.0
                high = float(parts[4]) if parts[4] else 0.0
                low = float(parts[5]) if parts[5] else 0.0
                vol = float(parts[8]) if len(parts) > 8 and parts[8] else 0.0
                if last == 0 and prev_close == 0 and open_p == 0:
                    continue
                chg = round(last - prev_close, 4) if prev_close else 0.0
                time_str = ""
                if len(parts) > 31 and parts[30] and parts[31]:
                    time_str = f"{parts[30].strip()} {parts[31].strip()}"
                result[code_str] = {
                    "name": name, "last": last, "change": chg,
                    "changePercent": round(chg / prev_close * 100, 2) if prev_close else 0.0,
                    "open": open_p, "high": high, "low": low,
                    "previousClose": prev_close, "volume": vol, "time": time_str,
                    "symbol": code_str,
                }
            except (ValueError, IndexError):
                continue


