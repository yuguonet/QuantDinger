# -*- coding: utf-8 -*-
"""
东方财富 trends2 极速数据源 Provider

模块职责:
  通过 push2.eastmoney.com trends2 API 获取 A股实时1分钟数据，
  聚合为15分钟K线。这是目前已知最快的免费A股数据源。

能力:
  - K线: 1m/5m/15m/30m/1H（1min数据聚合），今天的数据
  - 不支持 1D（API只返回当天数据）
  - 行情: 用当天1min数据最新bar作为实时行情
  - 全市场批量: 并发获取全市场K线
  - 不支持批量行情接口

特点:
  - 极速源: push2 trends2, 每秒可处理50+只
  - 纯标准库实现，无第三方依赖
  - 域名限流保护

数据标准化:
  - time: Unix 时间戳
  - open/high/low/close: OHLC 四价
  - volume: 成交量
  - 复权: 不复权 → 通过 TDX 除权除息数据转前复权

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → EmTrends2DataSource（本模块）
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
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
PER_DOMAIN_CONCURRENT = 50
PER_DOMAIN_INTERVAL = 0.01

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 极速源用持久 opener（连接复用）
_fast_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX))


# ================================================================
# 域名限流
# ================================================================

class _DomainThrottler:
    """线程安全的域名级限流器"""

    def __init__(self, max_c: int = 50, interval: float = 0.01):
        self._sems: Dict[str, threading.Semaphore] = {}
        self._last: Dict[str, float] = {}
        self._max = max_c
        self._interval = interval
        self._lock = threading.Lock()

    def _domain(self, url: str) -> str:
        m = re.search(r'https?://([^/]+)', url)
        return m.group(1) if m else url

    def _sem(self, d: str) -> threading.Semaphore:
        with self._lock:
            if d not in self._sems:
                self._sems[d] = threading.Semaphore(self._max)
            return self._sems[d]

    def acquire(self, url: str):
        d = self._domain(url)
        self._sem(d).acquire()
        wait = 0.0
        with self._lock:
            wait = max(0, self._interval - (time.time() - self._last.get(d, 0)))
            self._last[d] = time.time() + wait
        if wait > 0:
            time.sleep(wait)

    def release(self, url: str):
        self._sem(self._domain(url)).release()


_throttler = _DomainThrottler(PER_DOMAIN_CONCURRENT, PER_DOMAIN_INTERVAL)


# ================================================================
# HTTP 工具
# ================================================================

def _http_get(url: str, headers: dict = None, timeout: int = TIMEOUT) -> Optional[str]:
    h = {**HEADERS, **(headers or {})}
    _throttler.acquire(url)
    try:
        req = urllib.request.Request(url, headers=h)
        with _fast_opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
    except Exception:
        return None
    finally:
        _throttler.release(url)


def _http_get_json(url: str, headers: dict = None, timeout: int = TIMEOUT) -> Optional[dict]:
    t = _http_get(url, headers, timeout)
    if not t:
        return None
    try:
        m = re.search(r'[=(]\s*(\{[\s\S]*\})\s*[);]*$', t)
        if m:
            return json.loads(m.group(1))
        return json.loads(t)
    except Exception:
        return None


# ================================================================
# 代码工具
# ================================================================

def _normalize(code: str) -> str:
    """标准化为 sh/sz/bj 前缀 + 6位数字"""
    c = code.strip().upper().replace(".", "").replace("SH", "").replace("SZ", "").replace("BJ", "")
    if c.startswith("6"):
        return f"sh{c}"
    elif c.startswith(("0", "3")):
        return f"sz{c}"
    elif c.startswith(("8", "4")):
        return f"bj{c}"
    return c


def _to_em(code: str) -> str:
    """转东财 secid 格式: 1.600519 / 0.000001"""
    nc = _normalize(code)
    return f"1.{nc[2:]}" if nc.startswith("sh") else f"0.{nc[2:]}"


def _cn(code: str) -> str:
    """提取纯数字代码"""
    return _normalize(code)[2:]


def _k(t, o, h, l, c, v, a=0) -> Dict[str, Any]:
    """构建标准化K线字典"""
    return {
        "time": str(t), "open": float(o), "high": float(h),
        "low": float(l), "close": float(c),
        "volume": float(v), "amount": float(a),
    }


BAR_LIMIT = 64


def _last_n_bars(klines: list, n: int = BAR_LIMIT) -> Optional[list]:
    return klines[-n:] if klines and len(klines) > 0 else None


# ================================================================
# 前复权（共享模块）
# ================================================================
from app.data_sources.provider.adjustment import apply_fwd_adjust


# ================================================================
# 核心数据获取
# ================================================================

def _em_trends2_raw(code: str) -> Optional[list]:
    """push2.eastmoney.com trends2: 获取今天1分钟原始数据"""
    secid = _to_em(code)
    try:
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/trends2/get?"
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1"
        )
        req = urllib.request.Request(url, headers=HEADERS)
        with _fast_opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        d = json.loads(raw)
        trends = (d.get("data") or {}).get("trends") or []
        if not trends:
            return None

        bars = []
        for t in trends:
            p = t.split(",")
            if len(p) < 7:
                continue
            bars.append({
                "time": p[0], "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
            })
        return bars if bars else None
    except Exception:
        return None


# 聚合周期映射: timeframe → 每根bar包含的1min bar数
_EM_AGG_STEPS = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
}


def _aggregate_bars(raw_bars: list, timeframe: str) -> Optional[list]:
    """将1分钟原始数据聚合为指定周期的K线。

    支持: 1m(不聚合), 5m, 15m, 30m, 1H。
    1D 不支持（API只返回当天数据，不够聚合出日线）。
    """
    step = _EM_AGG_STEPS.get(timeframe)
    if step is None:
        return None  # 不支持的周期（如 1D）

    if step == 1:
        return raw_bars  # 1m 直接返回

    result = []
    for i in range(0, len(raw_bars) - step + 1, step):
        chunk = raw_bars[i:i + step]
        result.append(_k(
            chunk[0]["time"],
            chunk[0]["open"],
            max(b["high"] for b in chunk),
            min(b["low"] for b in chunk),
            chunk[-1]["close"],
            sum(b["volume"] for b in chunk),
            sum(b["amount"] for b in chunk),
        ))
    return result if result else None


def _em_trends2_kline(code: str, timeframe: str = "15m", limit: int = 200) -> Optional[list]:
    """获取单只股票K线数据，支持 1m/5m/15m/30m/1H。

    流程: 获取全天1min数据 → 聚合为目标周期 → 截取 limit 条。
    """
    raw = _em_trends2_raw(code)
    if not raw:
        return None
    return _aggregate_bars(raw, timeframe)


# ================================================================
# 实时行情 — 用当天1min数据最新bar的close作为当前价
# ================================================================

def _fetch_em_trends2_quote(code: str) -> Optional[Dict[str, Any]]:
    """获取单只股票实时行情 — 从全天1min数据提取最新bar"""
    raw = _em_trends2_raw(code)
    if not raw:
        return None

    last_bar = raw[-1]
    last = float(last_bar.get("close", 0) or 0)
    if last <= 0:
        return None

    highs = [float(b.get("high", 0)) for b in raw if float(b.get("high", 0)) > 0]
    lows = [float(b.get("low", 0)) for b in raw if float(b.get("low", 0)) > 0]
    open_p = float(raw[0].get("open", 0) or last)

    return {
        "last": last,
        "change": 0,
        "changePercent": 0,
        "high": max(highs) if highs else last,
        "low": min(lows) if lows else last,
        "open": open_p,
        "previousClose": 0,
        "name": "",
        "symbol": code,
    }


# ================================================================
# Provider 注册
# ================================================================

@register(priority=5)
class EmTrends2DataSource:
    """
    东方财富 trends2 极速数据源 — 最快的A股免费源（priority=5）。

    能力:
      - K线: 1m/5m/15m/30m/1H（1min数据聚合），今天的数据
      - 不支持 1D（API只返回当天数据）
      - 行情: 用当天1min数据最新bar作为实时行情
      - 全市场批量: 并发获取全市场K线（30线程）
      - 不支持批量行情接口

    线程安全性:
      - 使用域名限流器控制并发
      - TDX 连接池线程本地
    """

    name = "em_trends2"
    priority = 5

    capabilities = {
        "kline": True,
        "kline_priority": 5,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H"},
        "kline_batch": True,
        "kline_batch_priority": 5,
        "quote": True,
        "quote_priority": 5,
        "batch_quote": True,
        "batch_quote_priority": 5,
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
        """
        获取单只股票K线，支持 1m/5m/15m/30m/1H。
        数据来源: 全天1min数据聚合。
        不支持 1D（API只返回当天数据）。
        不复权数据通过 TDX 除权除息数据转前复权。
        """
        if timeframe not in _EM_AGG_STEPS:
            return NotSupportedResult(self.name, "fetch_kline", f"不支持 {timeframe} 周期")

        data = _em_trends2_kline(code, timeframe, count)
        if not data:
            return []

        # 标准化时间格式: "2026-05-08 09:45" → Unix timestamp
        result = []
        for bar in data:
            try:
                ts_str = str(bar.get("time", ""))
                if "-" in ts_str and ":" in ts_str:
                    # 字符串时间 → Unix timestamp
                    ts = int(datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M").timestamp())
                else:
                    ts = int(float(ts_str))
                result.append({
                    "time": ts,
                    "open": round(float(bar["open"]), 4),
                    "high": round(float(bar["high"]), 4),
                    "low": round(float(bar["low"]), 4),
                    "close": round(float(bar["close"]), 4),
                    "volume": round(float(bar["volume"]), 2),
                })
            except (ValueError, TypeError, KeyError):
                continue

        # 前复权处理
        if adj == "qfq" and result:
            result = apply_fwd_adjust(result, code)

        return result[-count:] if len(result) > count else result

    def fetch_market_kline(
        self, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 30,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量K线 — 30线程并发获取。
        支持 1m/5m/15m/30m/1H（不支持 1D）。

        线程结构: 持久线程池跨组复用，先完成先领下一组。
        """
        if timeframe not in _EM_AGG_STEPS:
            return NotSupportedResult(self.name, "fetch_market_kline", f"不支持 {timeframe} 周期")

        from queue import Queue, Empty

        # 获取股票列表
        if symbols:
            stocks = [{"code": c, "name": ""} for c in symbols]
        else:
            stocks = self._get_stock_list()
        if not stocks:
            logger.warning("[EmTrends2] 获取股票列表失败")
            return {}

        group_size = 50
        groups = [stocks[i:i + group_size] for i in range(0, len(stocks), group_size)]
        q: Queue = Queue()
        for idx, g in enumerate(groups):
            q.put((idx, g))

        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()
        stats_ok = [0]
        stats_fail = [0]

        def _fetch_one(stock):
            code = stock.get("code", "")
            if not code:
                return
            try:
                data = _em_trends2_kline(code, timeframe, count)
                if data:
                    # 标准化时间格式
                    bars = []
                    for bar in data:
                        try:
                            ts_str = str(bar.get("time", ""))
                            if "-" in ts_str and ":" in ts_str:
                                ts = int(datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M").timestamp())
                            else:
                                ts = int(float(ts_str))
                            bars.append({
                                "time": ts,
                                "open": round(float(bar["open"]), 4),
                                "high": round(float(bar["high"]), 4),
                                "low": round(float(bar["low"]), 4),
                                "close": round(float(bar["close"]), 4),
                                "volume": round(float(bar["volume"]), 2),
                            })
                        except (ValueError, TypeError, KeyError):
                            continue

                    # 前复权
                    if adj == "qfq" and bars:
                        bars = apply_fwd_adjust(bars, code)

                    if bars:
                        with lock:
                            result[code] = bars
                            stats_ok[0] += 1
                        return
            except Exception:
                pass
            with lock:
                stats_fail[0] += 1

        # 持久线程池 — 跨组复用，避免每组重建开销
        # 两层并发: group-level workers 领组, 共享 stock_pool 处理个股
        stock_pool = ThreadPoolExecutor(max_workers=THREADS_PER_SOURCE)

        def _worker():
            while True:
                try:
                    _, stocks_group = q.get(timeout=5)
                except Empty:
                    break
                futs = [stock_pool.submit(_fetch_one, s) for s in stocks_group]
                for f in futs:
                    try:
                        f.result()
                    except Exception:
                        pass
                q.task_done()

        num_workers = min(THREADS_PER_SOURCE, len(groups))
        workers = []
        for _ in range(num_workers):
            t = threading.Thread(target=_worker, daemon=True)
            workers.append(t)
            t.start()

        try:
            for t in workers:
                t.join(timeout=timeout)
        finally:
            stock_pool.shutdown(wait=False)

        logger.info("[EmTrends2] 全市场完成: %d成功 %d失败", stats_ok[0], stats_fail[0])
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """获取单只股票实时行情 — 用当天1min数据最新bar的close作为当前价"""
        return _fetch_em_trends2_quote(code)

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """批量实时行情 — 并发直接调 _fetch_em_trends2_quote"""
        result: Dict[str, Dict[str, Any]] = {}
        lock = threading.Lock()

        def _fetch(code):
            q = _fetch_em_trends2_quote(code)
            if q:
                nc = code.strip().upper()
                if nc.startswith("6"):
                    nc = "sh" + nc
                elif nc.startswith(("0", "3")):
                    nc = "sz" + nc
                with lock:
                    result[nc] = q

        max_workers = min(len(codes), 30)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_fetch, c) for c in codes]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass

        return result

    def _get_stock_list(self) -> list:
        """获取A股股票列表（通过东财 clist API）"""
        try:
            stocks, page = [], 1
            while True:
                data = _http_get_json(
                    f"https://82.push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=5000&po=1&np=1"
                    f"&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3"
                    f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f14,f13"
                )
                if not data:
                    break
                items = (data.get("data") or {}).get("diff") or []
                if not items:
                    break
                for i in items:
                    c, n, m = i.get("f12", ""), i.get("f14", ""), i.get("f13", 0)
                    if c:
                        stocks.append({"code": f"{'sh' if m == 1 else 'sz'}{c}", "name": n})
                if len(stocks) >= ((data.get("data") or {}).get("total", 0)):
                    break
                page += 1
            return stocks
        except Exception as e:
            logger.error("[EmTrends2] 获取股票列表失败: %s", e)
            return []
