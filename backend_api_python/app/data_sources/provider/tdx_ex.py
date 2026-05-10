# -*- coding: utf-8 -*-
"""
通达信扩展行情数据源 Provider (ExHQ)

模块职责:
  通过 pytdx TdxExHq_API 获取 A股 K线数据。
  使用 ExHQ 协议（端口 7727），与 tdx provider（d.10jqka.com.cn HTTP）完全独立。

能力:
  - K线: 1m/5m/15m/30m/1H/1D/1W
  - 行情: 单只/批量实时行情（get_instrument_quotes）
  - 全市场批量: 并发获取全市场K线

特点:
  - 纯 pytdx 二进制协议，速度快
  - 需要安装 pytdx: pip install pytdx
  - ExHQ 服务器通常在 7727 端口
  - 自动探测可用服务器，按延迟排序

与 tdx provider 的区别:
  - tdx:       HTTP API (d.10jqka.com.cn)，同花顺 Web 接口
  - tdx_ex:    pytdx ExHQ 二进制协议 (TdxExHq_API)，通达信原生接口

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → TdxExDataSource（本模块）
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_TZ_CN = timezone(timedelta(hours=8))

from app.data_sources.provider import register, NotSupportedResult
from app.data_sources.normalizer import normalize_cn_code
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ================================================================
# pytdx 可用性检查
# ================================================================

HAS_TDX = False
try:
    from pytdx.exhq import TdxExHq_API
    HAS_TDX = True
except ImportError:
    pass


# ================================================================
# ExHQ Category 映射
# ================================================================
# get_instrument_bars(category, market, symbol, start, count)
# 注意: ExHQ 的 category 与 HQ (get_security_bars) 不同
# 这里是基于 akline_market.py 中的实测验证

_TDX_EX_TF_CATEGORY = {
    "1m":  [7, 8],       # 1分钟: 尝试 cat=7, 回退 cat=8
    "5m":  [0],          # 5分钟: cat=0
    "15m": [8, 1, 9],    # 15分钟: cat=8 优先, 回退 1, 9
    "30m": [2],          # 30分钟: cat=2
    "1H":  [3],          # 1小时:  cat=3
    "1D":  [4, 9],       # 日线:   cat=4, 回退 9
    "1W":  [5],          # 周线:   cat=5
}

# 支持的周期集合（从映射表自动生成）
_TDX_EX_SUPPORTED_TF = set(_TDX_EX_TF_CATEGORY.keys())


# ================================================================
# 候选服务器
# ================================================================

TDX_EX_CANDIDATE_SERVERS = [
    ("112.74.214.43", 7727), ("180.153.18.170", 7727), ("180.153.18.171", 7727),
    ("60.191.117.167", 7727), ("115.238.56.198", 7727), ("115.238.90.165", 7727),
    ("218.75.126.9", 7727), ("60.12.136.251", 7727), ("60.12.136.250", 7727),
    ("119.147.212.81", 7727), ("124.160.88.183", 7727), ("101.227.73.20", 7727),
    ("101.227.77.254", 7727), ("14.215.128.18", 7727), ("59.173.18.140", 7727),
    ("60.28.23.80", 7727), ("221.231.141.60", 7727), ("113.105.142.162", 7727),
    ("218.108.98.244", 7727), ("61.152.107.171", 7727), ("61.153.144.66", 7727),
    ("218.108.47.69", 7727), ("180.153.39.51", 7727), ("118.114.77.13", 7727),
    ("61.135.142.88", 7727), ("218.85.139.19", 7727), ("202.108.253.130", 7727),
    ("202.108.253.131", 7727),
    # 也试 7709 端口（少数服务器支持 ExHQ 握手）
    ("180.153.18.170", 7709), ("60.12.136.251", 7709), ("60.12.136.250", 7709),
    ("115.238.90.165", 7709), ("218.75.126.9", 7709), ("115.238.56.198", 7709),
]

# ================================================================
# 服务器探测 & 连接池
# ================================================================

_live_servers: List[tuple] = []
_server_lock = threading.Lock()
_server_idx = [0]
_discovered = False
_discover_lock = threading.Lock()


def _discover_servers(force: bool = False):
    """并行探测 ExHQ 服务器，按延迟排序。force=True 强制重新探测"""
    global _live_servers, _discovered
    with _discover_lock:
        if _discovered and not force:
            return
        _discovered = True
        _live_servers = []

    results = []

    def _probe(host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            t0 = time.time()
            s.connect((host, port))
            lat = time.time() - t0
            s.close()
            # 验证 ExHQ 握手 + 能拉数据
            try:
                api = TdxExHq_API()
                api.connect(host, port, time_out=3)
                data = None
                for mkt in [28, 33, 0, 1]:
                    try:
                        data = api.get_instrument_bars(9, mkt, '000001', 0, 1)
                        if data:
                            break
                    except Exception:
                        continue
                api.disconnect()
                if data:
                    results.append((host, port, lat))
            except Exception:
                pass
        except Exception:
            pass

    threads = [
        threading.Thread(target=_probe, args=(h, p), daemon=True)
        for h, p in TDX_EX_CANDIDATE_SERVERS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)
    results.sort(key=lambda x: x[2])
    _live_servers = [(h, p) for h, p, _ in results]
    logger.info("[TDX ExHQ] 服务器探测完成: %d 个可用", len(_live_servers))


# 线程本地连接池
_conn_pool = threading.local()


def _get_conn():
    """获取当前线程的 ExHQ 连接，断了自动重连"""
    conn = getattr(_conn_pool, 'conn', None)
    if conn:
        try:
            conn.get_instrument_count(0)
            return conn
        except Exception:
            try:
                conn.disconnect()
            except Exception:
                pass
            _conn_pool.conn = None

    if not _live_servers:
        _discover_servers(force=True)
    if not _live_servers:
        return None

    n = len(_live_servers)
    for _ in range(n):
        with _server_lock:
            idx = _server_idx[0] % n
            _server_idx[0] += 1
        host, port = _live_servers[idx]
        try:
            api = TdxExHq_API()
            api.connect(host, port, time_out=3)
            _conn_pool.conn = api
            return api
        except Exception:
            continue
    return None


# ================================================================
# 核心数据获取
# ================================================================

def _fetch_tdx_ex_kline(
    code: str,
    timeframe: str = "15m",
    limit: int = 200,
) -> Optional[List[Dict[str, Any]]]:
    """
    获取单只股票K线数据，支持 1m/5m/15m/30m/1H/1D/1W。

    通过 ExHQ 协议获取，不同服务器可能对 category 支持不同，
    所以每个周期尝试多个 category 值。
    """
    if not HAS_TDX or not _live_servers:
        return None

    categories = _TDX_EX_TF_CATEGORY.get(timeframe)
    if not categories:
        return None  # 不支持的周期

    nc = normalize_cn_code(code)
    if nc.startswith("sh"):
        market = 28  # 沪A
    elif nc.startswith("sz"):
        market = 33  # 深A
    else:
        market = 33  # 北交所归深A
    symbol = nc[2:]

    api = _get_conn()
    if not api:
        return None

    # 尝试多个 category
    data = None
    for cat in categories:
        try:
            data = api.get_instrument_bars(cat, market, symbol, 0, limit)
            if data:
                break
        except Exception:
            continue

    if not data:
        return None

    result = []
    for bar in data:
        dt = str(bar.get("datetime", ""))
        if not dt:
            continue
        try:
            # pytdx 返回 "YYYY-MM-DD HH:MM" 格式
            if "-" in dt and ":" in dt:
                ts = int(datetime.strptime(dt[:16], "%Y-%m-%d %H:%M").replace(tzinfo=_TZ_CN).timestamp())
            elif len(dt) == 8 and dt.isdigit():
                ts = int(datetime.strptime(dt, "%Y%m%d").replace(tzinfo=_TZ_CN).timestamp())
            else:
                ts = int(float(dt))
            result.append({
                "time": ts,
                "open": round(float(bar.get("open", 0)), 4),
                "high": round(float(bar.get("high", 0)), 4),
                "low": round(float(bar.get("low", 0)), 4),
                "close": round(float(bar.get("close", 0)), 4),
                "volume": round(float(bar.get("vol", 0)), 2),
            })
        except (ValueError, TypeError, KeyError):
            continue

    if not result:
        return None

    result.sort(key=lambda x: x["time"])
    return result[-limit:] if len(result) > limit else result


# ================================================================
# 前复权（共享模块）
# ================================================================
from app.data_sources.provider.adjustment import apply_fwd_adjust as _apply_fwd_adjust


# ================================================================
# Provider 注册
# ================================================================

class TdxExDataSource:
    """
    通达信扩展行情数据源 — ExHQ 协议（priority=22）。

    与 tdx provider（d.10jqka.com.cn HTTP）完全独立。
    使用 pytdx TdxExHq_API，端口 7727。

    能力:
      - K线: 1m/5m/15m/30m/1H/1D/1W
      - 行情: 单只/批量实时行情（get_instrument_quotes）
      - 全市场批量: 并发获取

    线程安全性:
      - 线程本地连接池
      - 自动探测可用服务器

    依赖:
      - pytdx 未安装时不注册（避免 capabilities 与实际能力不匹配）
    """

    name = "tdx_ex"
    priority = 22

    capabilities = {
        "kline": True,
        "kline_priority": 22,
        "kline_tf": _TDX_EX_SUPPORTED_TF,
        "kline_batch": True,
        "kline_batch_priority": 22,
        "quote": True,
        "quote_priority": 22,
        "batch_quote": True,
        "batch_quote_priority": 22,
        "hk": False,
        "markets": {"CNStock"},
    }

    def __init__(self):
        """启动时探测 ExHQ 服务器"""
        _discover_servers()

    def prepare(self) -> bool:
        """下载前准备: 确保有可用的 ExHQ 服务器"""
        if not HAS_TDX:
            return False
        if not _live_servers:
            _discover_servers(force=True)
        return bool(_live_servers)

    def fetch_kline(
        self, code: str, timeframe: str = "15m", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """获取单只股票K线，支持 1m/5m/15m/30m/1H/1D/1W"""
        if timeframe not in _TDX_EX_TF_CATEGORY:
            return NotSupportedResult(self.name, "fetch_kline", f"不支持 {timeframe} 周期")

        if not HAS_TDX:
            return NotSupportedResult(self.name, "fetch_kline", "未安装 pytdx")

        if not _live_servers:
            return NotSupportedResult(self.name, "fetch_kline", "无可用 ExHQ 服务器")

        if start_date:
            from app.data_sources.provider import calc_kline_count
            count = calc_kline_count(timeframe, start_date, end_date)

        data = _fetch_tdx_ex_kline(code, timeframe, count)
        if not data:
            return []

        # 前复权
        if adj == "qfq":
            data = _apply_fwd_adjust(data, code)

        return data

    def fetch_market_kline(
        self, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 15,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """全市场批量K线 — 并发获取"""
        if timeframe not in _TDX_EX_TF_CATEGORY:
            return NotSupportedResult(self.name, "fetch_market_kline", f"不支持 {timeframe} 周期")

        if not HAS_TDX or not _live_servers:
            return NotSupportedResult(self.name, "fetch_market_kline", "未安装 pytdx 或无可用服务器")

        from queue import Queue, Empty

        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            return {}

        if start_date:
            from app.data_sources.provider import calc_kline_count
            count = calc_kline_count(timeframe, start_date, end_date)

        group_size = 50
        groups = [symbols[i:i + group_size] for i in range(0, len(symbols), group_size)]
        q: Queue = Queue()
        for idx, g in enumerate(groups):
            q.put((idx, g))

        result: Dict[str, List[Dict[str, Any]]] = {}
        lock = threading.Lock()

        def _fetch_one(code):
            try:
                data = _fetch_tdx_ex_kline(code, timeframe, count)
                if data:
                    if adj == "qfq":
                        data = _apply_fwd_adjust(data, code)
                    nc = normalize_cn_code(code)
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
                with ThreadPoolExecutor(max_workers=min(len(stocks), 30)) as pool:
                    futs = [pool.submit(_fetch_one, s) for s in stocks]
                    for f in futs:
                        try:
                            f.result()
                        except Exception:
                            pass
                q.task_done()

        workers = []
        for _ in range(min(30, len(groups))):
            t = threading.Thread(target=_worker, daemon=True)
            workers.append(t)
            t.start()

        for t in workers:
            t.join(timeout=timeout)

        logger.info("[TDX ExHQ] 全市场完成: %d只", len(result))
        return result

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """获取单只股票实时行情 — 通过 ExHQ get_instrument_quotes"""
        if not HAS_TDX or not _live_servers:
            return NotSupportedResult(self.name, "fetch_ticker", "未安装 pytdx 或无可用服务器")

        nc = normalize_cn_code(code)
        if nc.startswith("sh"):
            market = 28
        elif nc.startswith("sz"):
            market = 33
        else:
            market = 33
        symbol = nc[2:]

        api = _get_conn()
        if not api:
            return None

        try:
            # get_instrument_quotes: [(market, code), ...] → list of dicts
            data = api.get_instrument_quotes([(market, symbol)])
            if not data or len(data) == 0:
                return None

            q = data[0] if isinstance(data, list) else data
            last = float(q.get("price", 0) or 0)
            if last <= 0:
                return None

            prev = float(q.get("last_close", 0) or 0)
            open_p = float(q.get("open", 0) or last)
            high = float(q.get("high", 0) or last)
            low = float(q.get("low", 0) or last)
            chg = round(last - prev, 4) if prev else 0

            return {
                "last": last,
                "change": chg,
                "changePercent": round(chg / prev * 100, 2) if prev else 0,
                "high": high,
                "low": low,
                "open": open_p,
                "previousClose": prev,
                "name": "",
                "symbol": symbol,
            }
        except Exception as e:
            logger.debug("[TDX ExHQ] fetch_ticker %s 失败: %s", code, e)
            try:
                _conn_pool.conn.disconnect()
            except Exception:
                pass
            _conn_pool.conn = None
            return None

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """批量实时行情 — 通过 ExHQ get_instrument_quotes 一次拿多只"""
        if not HAS_TDX or not _live_servers:
            return NotSupportedResult(self.name, "fetch_batch_quotes", "未安装 pytdx 或无可用服务器")

        api = _get_conn()
        if not api:
            return NotSupportedResult(self.name, "fetch_batch_quotes", "无可用连接")

        # 构建 [(market, code), ...]
        pairs = []
        code_map = {}  # index → normalized code
        for raw_code in codes:
            nc = normalize_cn_code(raw_code)
            if nc.startswith("sh"):
                market = 28
            elif nc.startswith("sz"):
                market = 33
            else:
                market = 33
            pairs.append((market, nc[2:]))
            code_map[len(pairs) - 1] = nc

        result: Dict[str, Dict[str, Any]] = {}

        try:
            # get_instrument_quotes 一次可拿多只
            data = api.get_instrument_quotes(pairs)
            if not data:
                return {}

            for i, q in enumerate(data):
                if not isinstance(q, dict):
                    continue
                nc = code_map.get(i)
                if not nc:
                    continue
                last = float(q.get("price", 0) or 0)
                if last <= 0:
                    continue
                prev = float(q.get("last_close", 0) or 0)
                chg = round(last - prev, 4) if prev else 0
                result[nc] = {
                    "last": last,
                    "change": chg,
                    "changePercent": round(chg / prev * 100, 2) if prev else 0,
                    "high": float(q.get("high", 0) or last),
                    "low": float(q.get("low", 0) or last),
                    "open": float(q.get("open", 0) or last),
                    "previousClose": prev,
                    "name": "",
                    "symbol": nc[2:],
                }
        except Exception as e:
            logger.debug("[TDX ExHQ] fetch_batch_quotes 失败: %s", e)
            try:
                _conn_pool.conn.disconnect()
            except Exception:
                pass
            _conn_pool.conn = None

        return result


# 仅在 pytdx 可用时注册，避免 capabilities 声明支持但实际全部 NotSupported
if HAS_TDX:
    register(priority=22)(TdxExDataSource)
