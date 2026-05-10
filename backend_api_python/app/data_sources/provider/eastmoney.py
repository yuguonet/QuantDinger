# -*- coding: utf-8 -*-
"""
东方财富数据源 Provider

模块职责:
  通过东方财富 API 获取 A股的 K线、实时行情以及市场数据（龙虎榜/热度/涨停池/跌停池/炸板池）。
  东财是国内最稳定的免费数据源之一，作为A股第三选择（priority=30）。

能力:
  - K线: 全周期（1m/5m/15m/30m/1H/1D/1W），通过 kline/get API
  - 单只行情: 实时行情快照（stock/get API）
  - 批量行情: 单次HTTP获取全市场行情（clist/get API，最多6000只）
  - 市场数据（龙虎榜/涨停池等）已迁移至 maket_cn/eastmoney_market.py

特点:
  - 国内最稳定的免费数据源
  - 批量行情支持全市场（一次HTTP获取所有A股行情）
  - K线API是 per-symbol 的，不支持原生批量

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → EastMoneyDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - app.data_sources.normalizer: 股票代码标准化（to_eastmoney_secid, to_raw_digits）
  - app.data_sources.rate_limiter: 限流器
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff,
)
from app.data_sources.provider import register
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _to_eastmoney_secid(symbol: str) -> str:
    """股票代码 → 东财 secid（沪1.xxx / 深北0.xxx）"""
    market, digits = detect_market(symbol)
    if not market or not digits:
        return ""
    return f"1.{digits}" if market == "SH" else f"0.{digits}"


# ================================================================
# 请求头 — 随机 UA + Referer 轮换池
# ================================================================
# 不做 CDN 节点探测（太慢），靠 Referer 轮换 + 随机 UA 规避反爬。
# 失败交给 Coordinator 熔断器和多源 fallback 处理。

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 OPR/103.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _make_headers(referer: str = "https://quote.eastmoney.com/") -> dict:
    """随机 UA + 指定 Referer + 完整浏览器指纹"""
    return {
        "User-Agent": _random_ua(), "Referer": referer,
        "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br", "Connection": "keep-alive",
    }


class _RefererPool:
    """线程安全的 Referer 轮换池"""
    def __init__(self, referers: List[str]):
        self._cycle = itertools.cycle(referers)
        self._lock = threading.Lock()
    def next(self) -> str:
        with self._lock: return next(self._cycle)


_em_referers = _RefererPool([
    # 东财主站
    "https://quote.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://stock.eastmoney.com/",
    "https://data.eastmoney.com/",
    "https://push2.eastmoney.com/",
    # 东财子站 / 频道
    "https://futures.eastmoney.com/",
    "https://fund.eastmoney.com/",
    "https://bond.eastmoney.com/",
    "https://forex.eastmoney.com/",
    "https://hk.eastmoney.com/",
    "https://guba.eastmoney.com/",
    "https://so.eastmoney.com/",
    "https://newsapi.eastmoney.com/",
    "https://emweb.eastmoney.com/",
    "https://pdf.eastmoney.com/",
    "https://jspdf.eastmoney.com/",
    "https://cx.eastmoney.com/",
    "https://appshare.eastmoney.com/",
    "https://zlcndc.eastmoney.com/",
    "https://choice.eastmoney.com/",
    # 东财行情 push 域名
    "https://push2.eastmoney.com/",
    "https://push2his.eastmoney.com/",
    # 东财其他产品
    "https://eastmoney.com/",
    "https://caifuhao.eastmoney.com/",
    "https://mp.eastmoney.com/",
    "https://search-api-web.eastmoney.com/",
    # 行情页 Referer（模拟从股票详情页发起请求）
    "https://quote.eastmoney.com/concept/sh600519.html",
    "https://quote.eastmoney.com/concept/sz000001.html",
    "https://quote.eastmoney.com/center/gridlist.html",
    "https://quote.eastmoney.com/center/boardlist.html",
])


# 东财固定域名（不做 CDN 探测，直接用默认域名）
_EM_KLINE_HOST = "push2his.eastmoney.com"
_EM_QUOTE_HOST = "push2.eastmoney.com"



# 东财K线周期映射: 内部周期 → 东财 klt 参数
# klt (K Line Type): 1=1分钟, 5=5分钟, ..., 101=日线, 102=周线
_EM_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "1D": 101, "1W": 102}

# 东财复权类型映射: 内部复权方式 → 东财 fqt 参数
# fqt (Forward/Backward Adjust): 0=不复权, 1=前复权, 2=后复权
_EM_FQT = {"": 0, "qfq": 1, "hfq": 2}


@register(priority=30)
class EastMoneyDataSource:
    """
    东方财富数据源 — 国内最稳定的免费数据源之一（priority=30）。

    能力:
      - K线: 全周期（分钟/日/周），通过 kline/get API
      - 行情: 单只实时行情（stock/get API）
      - 批量行情: 全市场行情（clist/get API，一次HTTP最多6000只）
      - 市场数据: 龙虎榜/热度/涨停池/跌停池/炸板池（独立函数）

    线程安全性:
      - 实例方法无状态，线程安全

    API参数说明:
      - secid: 证券ID，格式为 "市场代码.股票代码"（如 "1.600519"）
      - ut: 用户令牌（固定值，东财API要求）
      - fields1: 基础字段（f1=代码, f2=名称, f3=最新价）
      - fields2: K线字段（f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低, f56=成交量...）
      - klt: K线周期类型
      - fqt: 复权类型
    """

    name = "eastmoney"
    priority = 25

    capabilities = {
        "kline": True,
        "kline_priority": 25,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "kline_batch": True,
        "quote": True,
        "quote_priority": 20,
        "batch_quote": True,
        "batch_quote_priority": 5,
        "hk": False,
        "markets": {"CNStock"},
    }

    def fetch_market_kline(
        self, timeframe: str = "1D", count: int = None,
        adj: str = "qfq", timeout: int = 15,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """全市场批量K线 — count=None 走批量行情（1 HTTP），count 有值走并发 K 线"""
        from app.data_sources.provider import _resolve_market_kline_count
        count = _resolve_market_kline_count(timeframe, count, start_date, end_date)
        if count is None:
            from app.data_sources.provider import _all_market_kline_via_quotes
            return _all_market_kline_via_quotes(self, timeframe=timeframe, timeout=timeout, symbols=symbols)

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            return {}
        import concurrent.futures
        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()

        def _fetch_one(code: str):
            bars = self.fetch_kline(code, timeframe, count, adj=adj, timeout=timeout,
                                    start_date=start_date, end_date=end_date)
            if bars:
                with lock:
                    result[code] = bars

        max_workers = min(len(symbols), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one, c) for c in symbols]
            concurrent.futures.wait(futures, timeout=timeout + 5)
        return result

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=8.0, exceptions=(
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
        from datetime import date as _date
        em_end = end_date.replace("-", "") if end_date else _date.today().strftime("%Y%m%d")

        secid = _to_eastmoney_secid(code)
        if not secid:
            return []
        klt = _EM_KLT.get(timeframe)
        if klt is None:
            return []

        url = f"https://{_EM_KLINE_HOST}/api/qt/stock/kline/get"

        resp = requests.get(
            url,
            headers=_make_headers(referer=_em_referers.next()),
            params={
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbbd1835",
                "fields1": "f1,f2,f3",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt,
                "fqt": _EM_FQT.get(adj, 1),
                "end": em_end,
                "lmt": min(int(count), 5000),
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        klines_data = (data.get("data") or {}).get("klines")
        if not isinstance(klines_data, list):
            return []

        out = []
        for line in klines_data:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                dt_str = parts[0].strip()
                ts = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        ts = int(datetime.strptime(dt_str, fmt).timestamp())
                        break
                    except ValueError:
                        continue
                if ts is None:
                    continue
                o, c, h, low, v = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])
                if o == 0 and c == 0:
                    continue
                if h > 0 and low > 0 and h < low:
                    h, low = low, h
                out.append({
                    "time": ts, "open": round(o, 4), "high": round(h, 4),
                    "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
                })
            except (ValueError, TypeError, IndexError):
                continue
        out.sort(key=lambda x: x["time"])
        return out[-count:] if len(out) > count else out

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        secid = _to_eastmoney_secid(code)
        if not secid:
            return None

        url = f"https://{_EM_QUOTE_HOST}/api/qt/stock/get"

        resp = requests.get(
            url,
            headers=_make_headers(referer=_em_referers.next()),
            params={
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f171",
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        d = data.get("data")
        if not isinstance(d, dict):
            return None

        def _f(key: str, default: float = 0.0) -> float:
            v = d.get(key)
            if v is None or v == "-" or v == "":
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        last = _f("f43") / 100
        prev = _f("f60") / 100
        if last == 0 and prev == 0:
            return None
        chg = round(last - prev, 4) if prev else 0.0
        return {
            "last": last,
            "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
            "high": _f("f44") / 100,
            "low": _f("f45") / 100,
            "open": _f("f46") / 100,
            "previousClose": prev,
            "name": str(d.get("f58", "")).strip(),
            "symbol": secid,
        }

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 15) -> Dict[str, Dict[str, Any]]:
        if not codes:
            return {}
        code_set: Dict[str, str] = {}
        for sym in codes:
            raw = to_raw_digits(sym)
            if raw and raw.isdigit() and len(raw) == 6:
                code_set[raw] = sym
        if not code_set:
            return {}

        url = f"https://{_EM_QUOTE_HOST}/api/qt/clist/get"

        try:
            resp = requests.get(
                url,
                headers=_make_headers(referer=_em_referers.next()),
                params={
                    "pn": 1, "pz": 6000, "po": 1, "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2, "invt": 2, "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                    "fields": "f2,f5,f6,f12,f15,f16,f17,f18",
                },
                timeout=timeout,
            )
            data = resp.json()
            diff = ((data.get("data") or {}).get("diff")) or []
        except Exception as e:
            logger.warning("[东财批量行情] clist 请求失败: %s", e)
            return {}

        now = datetime.now(timezone(timedelta(hours=8)))
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        result: Dict[str, Dict[str, Any]] = {}
        for item in diff:
            code = str(item.get("f12", "")).strip()
            sym = code_set.get(code)
            if not sym:
                continue
            try:
                last = float(item.get("f2", 0))
                if last <= 0:
                    continue
                prev = float(item.get("f18", 0))
                chg = round(last - prev, 4) if prev else 0.0
                result[sym] = {
                    "last": last,
                    "change": chg,
                    "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                    "high": round(float(item.get("f15", 0)), 4),
                    "low": round(float(item.get("f16", 0)), 4),
                    "open": round(float(item.get("f17", 0)), 4),
                    "previousClose": prev,
                    "name": "",
                    "symbol": sym,
                    "time": today_ts,
                }
            except (ValueError, TypeError):
                continue
        return result

