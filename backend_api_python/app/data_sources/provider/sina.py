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
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import to_sina_code
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff, RateLimiter,
)
from app.data_sources.provider import register
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
    """
    解析新浪行情响应文本。

    新浪行情格式: var hq_str_sh600519="贵州茅台,开盘价,昨收,最新价,最高,最低,..."
    字段以逗号分隔，至少32个字段，关键字段索引:
      parts[0] = 名称
      parts[1] = 开盘价
      parts[2] = 昨收价
      parts[3] = 最新价
      parts[4] = 最高价
      parts[5] = 最低价
      parts[8] = 成交量（股）
      parts[9] = 成交额（元）

    Args:
        text: 新浪行情API的原始响应文本

    Returns:
        行情字典，解析失败返回 None
    """
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
        # 全零视为无效数据
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
    """
    将新浪K线JSON数据转换为标准化字典列表。

    新浪K线JSON格式: [{"day": "2024-01-01", "open": 100, "high": 105, "low": 98, "close": 103, "volume": 1000}, ...]

    Args:
        data:  新浪K线API返回的JSON数组
        count: 请求的数据条数（用于截取最后N条）

    Returns:
        标准化K线字典列表，按时间升序排列
    """
    out: List[Dict[str, Any]] = []
    for item in data:
        try:
            dt_str = str(item.get("day", "")).strip()
            if not dt_str:
                continue
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    ts = int(datetime.strptime(dt_str, fmt).timestamp())
                    break
                except ValueError:
                    continue
            if ts is None:
                continue
            o = float(item.get("open", 0))
            h = float(item.get("high", 0))
            low = float(item.get("low", 0))
            c = float(item.get("close", 0))
            v = float(item.get("volume", 0))
            if o == 0 and c == 0:
                continue
            out.append({
                "time": ts, "open": round(o, 4), "high": round(h, 4),
                "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
            })
        except (ValueError, TypeError, KeyError):
            continue
    out.sort(key=lambda x: x["time"])
    return out[-count:] if len(out) > count else out


def _fetch_sina_kline_hisdata(sc: str, count: int, timeout: int) -> List[Dict[str, Any]]:
    """
    通过新浪 hisdata 页面获取日线K线（兜底机制）。

    当主API（vip.stock.finance.sina.com.cn）返回异常时，通过 hisdata 页面
    的 JavaScript 变量获取历史K线数据。数据格式为:
    "2024-01-01, 100.00, 103.00, 105.00, 98.00, 1000.00"
    即: 日期, 开盘, 收盘, 最高, 最低, 成交量

    注意: 这里的字段顺序是 日期,开盘,收盘,最高,最低,成交量
    （与标准 OHLC 顺序不同，收盘在第2位）

    Args:
        sc:    新浪格式股票代码（如 "sh600519"）
        count: 请求数据条数
        timeout: 请求超时秒数

    Returns:
        K线数据列表
    """
    url = f"https://finance.sina.com.cn/realstock/company/{sc}/hisdata/klc_kl.js"
    _sina_limiter.wait()
    resp = requests.get(
        url,
        headers=get_request_headers(referer=_sina_kline_referers.next()),
        timeout=timeout,
    )
    resp.encoding = "gbk"
    text = resp.text or ""

    # 正则匹配: "2024-01-01, 100.00, 103.00, 105.00, 98.00, 1000.00"
    # 字段: 日期, 开盘, 收盘, 最高, 最低, 成交量
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
                "time": ts, "open": round(o, 4), "high": round(h, 4),
                "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
            })
        except (ValueError, TypeError):
            continue
    if len(out) > count:
        out = out[-count:]
    out.sort(key=lambda x: x["time"])
    return out


@register(priority=20)
class SinaDataSource:
    """
    新浪财经数据源 — A股第二选择（priority=20）。

    能力:
      - K线: 日线（JSON API + hisdata 兜底）+ 分钟线（JSONP API）
      - 行情: 单只实时行情（hq.sinajs.cn）
      - 批量行情: 单次HTTP获取多只（最多500只/批）

    特点:
      - 国内直连，无需API Key
      - 行情响应速度快
      - K线数据通过正则解析 hisdata 页面作为兜底

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
        "quote": True,
        "quote_priority": 15,
        "batch_quote": True,
        "batch_quote_priority": 15,
        "hk": False,
        "markets": {"CNStock"},
    }

    @retry_with_backoff(max_attempts=3, base_delay=1.5, max_delay=10.0, exceptions=(
        requests.exceptions.RequestException, ConnectionError, TimeoutError,
    ))
    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据。

        根据 timeframe 分流:
          - 日线: _fetch_raw_daily_kline（JSON API + hisdata 兜底）
          - 分钟线: _fetch_minute_kline（JSONP API）

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式（新浪日线通过东财除权数据计算复权因子）
            timeout:   请求超时秒数

        Returns:
            K线数据列表
        """
        sc = to_sina_code(code)
        if not sc:
            return []
        scale = _SINA_TF_TO_SCALE.get(timeframe)
        if scale is None:
            return []
        _sina_limiter.wait()
        if timeframe != "1D":
            return self._fetch_minute_kline(sc, scale, count, timeout)
        return self._fetch_raw_daily_kline(sc, count, timeout)

    def _fetch_raw_daily_kline(self, sc: str, count: int, timeout: int) -> List[Dict[str, Any]]:
        """
        获取日线K线 — 优先JSON API，失败则用 hisdata 页面兜底。

        Args:
            sc:    新浪格式股票代码
            count: 请求数据条数
            timeout: 请求超时秒数

        Returns:
            K线数据列表
        """
        url = "https://vip.stock.finance.sina.com.cn/cn/api/json.php/CN_MarketDataService.getKLineData"
        params = {"symbol": sc, "scale": 240, "ma": "no", "datalen": min(int(count), 2000)}
        resp = requests.get(
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
        # JSON API 失败，用 hisdata 页面兜底
        return _fetch_sina_kline_hisdata(sc, count, timeout)

    def _fetch_minute_kline(self, sc: str, scale: int, count: int, timeout: int) -> List[Dict[str, Any]]:
        """
        获取分钟级K线 — 通过新浪 JSONP API。

        新浪分钟线API返回JSONP格式: var xxx=[{...}, {...}]
        需要用正则提取JSON数组部分。

        Args:
            sc:    新浪格式股票代码
            scale: 分钟数（1/5/15/30/60）
            count: 请求数据条数
            timeout: 请求超时秒数

        Returns:
            K线数据列表
        """
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var/CN_MarketDataService.getKLineData"
        params = {"symbol": sc, "scale": scale, "ma": "no", "datalen": min(int(count), 2000)}
        resp = requests.get(
            url,
            headers=get_request_headers(referer=_sina_kline_referers.next()),
            params=params, timeout=timeout,
        )
        text = (resp.text or "").strip()
        # 从JSONP响应中提取JSON数组
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
    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        通过 hq.sinajs.cn 接口获取行情，响应格式:
        var hq_str_sh600519="贵州茅台,开盘价,昨收,最新价,最高,最低,..."

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        sc = to_sina_code(code)
        if not sc:
            return None
        _sina_quote_limiter.wait()
        resp = requests.get(
            f"https://hq.sinajs.cn/list={sc}",
            headers=get_request_headers(referer=_sina_quote_referers.next()),
            timeout=timeout,
        )
        resp.encoding = "gbk"
        quote = _parse_sina_quote(resp.text)
        if not quote:
            return None
        quote["symbol"] = sc
        last = quote["last"]
        prev = quote["prev_close"]
        quote["change"] = round(last - prev, 4) if prev else 0.0
        quote["changePercent"] = round(quote["change"] / prev * 100, 2) if prev else 0.0
        quote["open"] = quote.get("open", last) or last
        quote["previousClose"] = prev
        return quote

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情 — 单次HTTP请求。

        通过逗号拼接多个新浪代码，一次请求获取所有行情。
        每批最多500只。响应中每行一只股票，格式:
        var hq_str_sh600519="贵州茅台,开盘价,昨收,..."

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict} — 仅包含成功获取的代码
        """
        if not codes:
            return {}
        sina_codes = [to_sina_code(c) for c in codes if c]
        if not sina_codes:
            return {}
        batch_size = 500
        result: Dict[str, Dict[str, Any]] = {}

        # 分批请求（每批500只）
        for i in range(0, len(sina_codes), batch_size):
            batch = sina_codes[i:i + batch_size]
            query = ",".join(batch)
            _sina_quote_limiter.wait()
            try:
                resp = requests.get(
                    f"https://hq.sinajs.cn/list={query}",
                    headers=get_request_headers(referer=_sina_quote_referers.next()),
                    timeout=timeout,
                )
                resp.encoding = "gbk"
            except Exception as e:
                logger.warning("[新浪批量行情] 请求失败: %s", e)
                continue

            # 逐行解析响应（每行一只股票）
            for line in (resp.text or "").strip().split("\n"):
                line = line.strip().rstrip(";")
                # 提取: var hq_str_sh600519="贵州茅台,开盘价,..."
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
                    # 全零视为无效数据
                    if last == 0 and prev_close == 0 and open_p == 0:
                        continue
                    chg = round(last - prev_close, 4) if prev_close else 0.0
                    result[code_str] = {
                        "name": name, "last": last, "change": chg,
                        "changePercent": round(chg / prev_close * 100, 2) if prev_close else 0.0,
                        "open": open_p, "high": high, "low": low,
                        "previousClose": prev_close, "volume": vol, "symbol": code_str,
                    }
                except (ValueError, IndexError):
                    continue
        return result
