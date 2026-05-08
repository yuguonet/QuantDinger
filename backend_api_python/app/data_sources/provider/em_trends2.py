# -*- coding: utf-8 -*-
"""
东方财富 trends2 极速数据源 Provider

模块职责:
  通过 push2.eastmoney.com trends2 API 获取 A股实时1分钟数据，
  聚合为15分钟K线。这是目前已知最快的免费A股数据源。

能力:
  - K线: 仅15m（1min聚合），今天的数据
  - 全市场批量: 并发获取全市场15min K线
  - 不支持行情/批量行情接口

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
# 前复权计算（从 TDX 获取除权除息数据）
# ================================================================

_xdxr_cache: Dict[str, list] = {}
_xdxr_lock = threading.Lock()

# TDX 候选服务器
TDX_CANDIDATE_SERVERS = [
    ("180.153.18.170", 7709), ("60.191.117.167", 7709), ("60.12.136.251", 7709),
    ("60.12.136.250", 7709), ("115.238.90.165", 7709), ("218.75.126.9", 7709),
    ("115.238.56.198", 7709), ("119.147.212.81", 7709), ("112.74.214.43", 7709),
    ("221.231.141.60", 7709), ("101.227.73.20", 7709), ("101.227.77.254", 7709),
]

_tdx_live_servers: list = []
_tdx_server_lock = threading.Lock()
_tdx_server_idx = [0]
HAS_TDX = False

try:
    from pytdx.hq import TdxHq_API
    HAS_TDX = True
except ImportError:
    pass


def _tdx_discover():
    """并行探测 TDX 服务器，按延迟排序"""
    global _tdx_live_servers
    import socket
    results = []

    def _probe(host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            t0 = time.time()
            s.connect((host, port))
            lat = time.time() - t0
            s.close()
            try:
                api = TdxHq_API()
                api.connect(host, port, time_out=3)
                api.get_security_bars(1, 0, '000001', 0, 1)
                api.disconnect()
                results.append((host, port, lat))
            except Exception:
                pass
        except Exception:
            pass

    threads = [threading.Thread(target=_probe, args=(h, p), daemon=True)
               for h, p in TDX_CANDIDATE_SERVERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    results.sort(key=lambda x: x[2])
    _tdx_live_servers = [(h, p) for h, p, _ in results]
    return _tdx_live_servers


# TDX 连接池
_tdx_conn_pool = threading.local()


def _tdx_get_conn():
    """获取当前线程的 TDX 连接"""
    conn = getattr(_tdx_conn_pool, 'conn', None)
    if conn:
        try:
            conn.get_security_count(0)
            return conn
        except Exception:
            try:
                conn.disconnect()
            except Exception:
                pass
            _tdx_conn_pool.conn = None

    if not _tdx_live_servers:
        return None

    n = len(_tdx_live_servers)
    for _ in range(n):
        with _tdx_server_lock:
            idx = _tdx_server_idx[0] % n
            _tdx_server_idx[0] += 1
        host, port = _tdx_live_servers[idx]
        try:
            api = TdxHq_API()
            api.connect(host, port, time_out=3)
            _tdx_conn_pool.conn = api
            return api
        except Exception:
            continue
    return None


def _fetch_xdxr(code: str) -> list:
    """从 TDX 获取除权除息数据"""
    if not HAS_TDX or not _tdx_live_servers:
        return []
    nc = _normalize(code)
    market = 1 if nc.startswith("sh") else 0
    symbol = nc[2:]
    for host, port in _tdx_live_servers[:3]:
        try:
            api = TdxHq_API()
            api.connect(host, port, time_out=3)
            xdxr = api.get_xdxr_info(market, symbol)
            api.disconnect()
            if xdxr:
                return xdxr
            return []
        except Exception:
            continue
    return []


def _build_fwd_factor(code: str) -> list:
    """构建前复权因子: 返回 [(date_str, cum_factor), ...] 按日期升序"""
    with _xdxr_lock:
        if code in _xdxr_cache:
            return _xdxr_cache[code]

    xdxr = _fetch_xdxr(code)
    if not xdxr:
        with _xdxr_lock:
            _xdxr_cache[code] = []
        return []

    events = []
    for r in xdxr:
        try:
            if int(r.get('category', 0)) != 1:
                continue
            y = int(r.get('year', 0))
            m = int(r.get('month', 0))
            d = int(r.get('day', 0))
            if y < 2000:
                continue
            date_str = f"{y:04d}-{m:02d}-{d:02d}"
            fenhong = float(r.get('fenhong', 0) or 0)
            songzhuangu = float(r.get('songzhuangu', 0) or 0)
            peigujia = float(r.get('peigujia', 0) or 0)
            peigu = float(r.get('peigu', 0) or 0)
            if fenhong == 0 and songzhuangu == 0 and peigu == 0:
                continue
            events.append((date_str, fenhong, songzhuangu / 10.0, peigujia, peigu / 10.0))
        except Exception:
            continue

    if not events:
        with _xdxr_lock:
            _xdxr_cache[code] = []
        return []

    events.sort(key=lambda x: x[0])

    cum = 1.0
    result = []
    for date_str, fenhong, sg_ratio, pgj, pg_ratio in events:
        divisor = 1.0 + sg_ratio + pg_ratio
        if divisor > 0:
            cum *= (1.0 / divisor)
        if fenhong > 0:
            cum *= (10.0 - fenhong) / 10.0
        result.append((date_str, cum))

    with _xdxr_lock:
        _xdxr_cache[code] = result
    return result


def _apply_fwd_adjust(klines: list, code: str) -> list:
    """对不复权K线数据施加前复权"""
    if not klines:
        return klines
    factors = _build_fwd_factor(code)
    if not factors:
        return klines

    adjusted = []
    factor_idx = 0
    current_factor = 1.0

    for bar in klines:
        bar_date = str(bar.get("time", ""))[:10]
        while factor_idx < len(factors) and factors[factor_idx][0] <= bar_date:
            current_factor = factors[factor_idx][1]
            factor_idx += 1

        if current_factor < 1.0:
            adjusted.append(_k(
                bar["time"],
                bar["open"] * current_factor,
                bar["high"] * current_factor,
                bar["low"] * current_factor,
                bar["close"] * current_factor,
                bar["volume"],
                bar.get("amount", 0),
            ))
        else:
            adjusted.append(bar)
    return adjusted


# ================================================================
# 核心数据获取
# ================================================================

def _em_trends2_15m(code: str, limit: int = 200) -> Optional[list]:
    """push2.eastmoney.com trends2: 今天1分钟数据 → 聚合为15min"""
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
        if len(trends) < 15:
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

        # 15根1min → 1根15min
        result = []
        for i in range(0, len(bars) - 14, 15):
            c = bars[i:i + 15]
            result.append(_k(
                c[0]["time"], c[0]["open"],
                max(b["high"] for b in c),
                min(b["low"] for b in c),
                c[-1]["close"],
                sum(b["volume"] for b in c),
                sum(b["amount"] for b in c),
            ))
        return result if result else None
    except Exception:
        return None


# ================================================================
# Provider 注册
# ================================================================

@register(priority=5)
class EmTrends2DataSource:
    """
    东方财富 trends2 极速数据源 — 最快的A股免费源（priority=5）。

    能力:
      - K线: 仅15m（1min聚合为15min），今天的数据
      - 全市场批量: 并发获取全市场15min K线（30线程）
      - 不支持行情/批量行情接口

    线程安全性:
      - 使用域名限流器控制并发
      - TDX 连接池线程本地
    """

    name = "em_trends2"
    priority = 5

    capabilities = {
        "kline": True,
        "kline_priority": 5,
        "kline_tf": {"15m"},
        "kline_batch": True,
        "kline_batch_priority": 5,
        "quote": False,
        "batch_quote": False,
        "hk": False,
        "markets": {"CNStock"},
    }

    def __init__(self):
        """初始化: 探测 TDX 服务器（用于前复权）"""
        if HAS_TDX and not _tdx_live_servers:
            try:
                _tdx_discover()
                logger.info("[EmTrends2] TDX 服务器探测完成: %d 个可用", len(_tdx_live_servers))
            except Exception as e:
                logger.warning("[EmTrends2] TDX 探测失败: %s", e)

    def fetch_kline(
        self, code: str, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票15分钟K线。

        仅支持15m周期，返回今天的数据（1min聚合为15min）。
        不复权数据通过 TDX 除权除息数据转前复权。
        """
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_kline", f"不支持 {timeframe} 周期")

        data = _em_trends2_15m(code, count)
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
            result = _apply_fwd_adjust(result, code)
            # 转换回标准格式（_apply_fwd_adjust 返回 _k 格式）
            converted = []
            for bar in result:
                try:
                    ts_str = str(bar.get("time", ""))
                    if "-" in ts_str and ":" in ts_str:
                        ts = int(datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M").timestamp())
                    else:
                        ts = int(float(ts_str))
                    converted.append({
                        "time": ts,
                        "open": round(float(bar["open"]), 4),
                        "high": round(float(bar["high"]), 4),
                        "low": round(float(bar["low"]), 4),
                        "close": round(float(bar["close"]), 4),
                        "volume": round(float(bar["volume"]), 2),
                    })
                except (ValueError, TypeError, KeyError):
                    continue
            result = converted

        return result[-count:] if len(result) > count else result

    def fetch_market_kline(
        self, timeframe: str = "15m", count: int = 200,
        adj: str = "qfq", timeout: int = 30,
        start_date: str = "", end_date: str = "",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量15分钟K线 — 30线程并发获取。

        线程结构与 akline_market.py 保持一致:
        - 每组50只，从队列中领取
        - 30线程并发
        - 先完成的接着领下一组
        """
        if timeframe != "15m":
            return NotSupportedResult(self.name, "fetch_market_kline", f"不支持 {timeframe} 周期")

        from queue import Queue, Empty

        # 获取股票列表
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
                data = _em_trends2_15m(code, count)
                if data:
                    # 标准化
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
                        bars = _apply_fwd_adjust(bars, code)
                        converted = []
                        for bar in bars:
                            try:
                                ts_str = str(bar.get("time", ""))
                                if "-" in ts_str and ":" in ts_str:
                                    ts = int(datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M").timestamp())
                                else:
                                    ts = int(float(ts_str))
                                converted.append({
                                    "time": ts,
                                    "open": round(float(bar["open"]), 4),
                                    "high": round(float(bar["high"]), 4),
                                    "low": round(float(bar["low"]), 4),
                                    "close": round(float(bar["close"]), 4),
                                    "volume": round(float(bar["volume"]), 2),
                                })
                            except (ValueError, TypeError, KeyError):
                                continue
                        bars = converted

                    if bars:
                        with lock:
                            result[code] = bars
                            stats_ok[0] += 1
                        return
            except Exception:
                pass
            with lock:
                stats_fail[0] += 1

        def _worker():
            while True:
                try:
                    _, stocks_group = q.get(timeout=5)
                except Empty:
                    break
                futs = []
                with ThreadPoolExecutor(max_workers=min(len(stocks_group), THREADS_PER_SOURCE)) as pool:
                    for s in stocks_group:
                        futs.append(pool.submit(_fetch_one, s))
                    for f in futs:
                        try:
                            f.result()
                        except Exception:
                            pass
                q.task_done()

        # 启动 worker 线程
        workers = []
        for _ in range(min(THREADS_PER_SOURCE, len(groups))):
            t = threading.Thread(target=_worker, daemon=True)
            workers.append(t)
            t.start()

        # 等待完成
        for t in workers:
            t.join(timeout=timeout)

        logger.info("[EmTrends2] 全市场完成: %d成功 %d失败", stats_ok[0], stats_fail[0])
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """不支持实时行情"""
        return NotSupportedResult(self.name, "fetch_ticker")

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """不支持批量行情"""
        return NotSupportedResult(self.name, "fetch_batch_quotes")

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
