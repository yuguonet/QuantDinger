# -*- coding: utf-8 -*-
"""
通达信数据源 Provider

模块职责:
  通过通达信 HTTP 接口获取 A股的 K线和实时行情数据。
  通达信是国内老牌行情软件，其 HTTP 接口稳定且无需认证，作为A股数据源（priority=25）。

能力:
  - K线: 日线 + 分钟线（1m/5m/15m/30m/1H），支持前/后复权
  - 单只行情: 实时行情快照
  - 批量行情: 单次HTTP获取多只股票行情

特点:
  - 国内直连，无需API Key
  - 数据覆盖全面，含 Level-1 行情
  - 接口稳定，适合长期运行

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → TdxDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - app.data_sources.normalizer: 股票代码标准化
  - app.data_sources.rate_limiter: 限流器
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff, RateLimiter,
)
from app.data_sources.provider import register
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 限流器
# ================================================================

_tdx_limiter = RateLimiter(
    min_interval=1.0,
    jitter_min=0.5,
    jitter_max=1.5,
)

_tdx_quote_limiter = RateLimiter(
    min_interval=0.6,
    jitter_min=0.2,
    jitter_max=1.0,
)


# ================================================================
# 通达信市场代码映射
# ================================================================

# 通达信市场标识: 沪市=1, 深市=0, 北证=0
_TDX_MARKET = {"SH": 1, "SZ": 0, "BJ": 0}

# 通达信周期映射
_TDX_PERIOD = {
    "1m": 8, "5m": 0, "15m": 1, "30m": 2, "1H": 3, "1D": 4, "1W": 5,
}


def _to_tdx_params(code: str) -> Optional[tuple]:
    """
    将股票代码转换为通达信 (market, code) 元组。

    Args:
        code: 任意格式的股票代码

    Returns:
        (market_id, digits) 元组，无法识别返回 None
    """
    market, digits = detect_market(code)
    if not market or not digits:
        return None
    mkt = _TDX_MARKET.get(market)
    if mkt is None:
        return None
    return (mkt, digits)


@register(priority=25)
class TdxDataSource:
    """
    通达信数据源 — 老牌行情软件接口（priority=25）。

    能力:
      - K线: 日线 + 分钟线（1m/5m/15m/30m/1H/1W）
      - 行情: 单只实时行情
      - 批量行情: 多只股票行情

    线程安全性:
      - 实例方法无状态，线程安全
      - 使用独立的限流器
    """

    name = "tdx"
    priority = 20

    capabilities = {
        "kline": True,
        "kline_priority": 20,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "quote": True,
        "quote_priority": 25,
        "batch_quote": True,
        "batch_quote_priority": 30,
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

        通过通达信 HTTP K线接口获取数据，支持日线和分钟线。

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式
            timeout:   请求超时秒数

        Returns:
            K线数据列表
        """
        params = _to_tdx_params(code)
        if not params:
            return []
        mkt, digits = params
        period = _TDX_PERIOD.get(timeframe)
        if period is None:
            return []

        _tdx_limiter.wait()

        # 通达信 K线 HTTP 接口
        url = "https://d.10jqka.com.cn/v6/line/hs_{}/01/last{}.js".format(
            digits, min(int(count), 800)
        )
        if timeframe != "1D":
            # 分钟线使用不同的端点
            url = "https://d.10jqka.com.cn/v6/line/hs_{}/0{}/last{}.js".format(
                digits, period, min(int(count), 800)
            )

        try:
            resp = requests.get(
                url,
                headers=get_request_headers(referer="https://stockpage.10jqka.com/"),
                timeout=timeout,
            )
            resp.encoding = "utf-8"
            text = resp.text or ""
        except Exception as e:
            logger.warning("[通达信K线] 请求失败 %s: %s", code, e)
            return []

        # 解析通达信返回的 JavaScript 变量
        # 格式: quotebridge_v6_line_hs_600519_01_last300("600519","...", "...data...")
        m = re.search(r'"data"\s*:\s*"([^"]+)"', text)
        if not m:
            # 尝试另一种格式: 直接是分号分隔的数据
            m = re.search(r'"([^"]*\d{8}[^"]*)"', text)
        if not m:
            return []

        raw = m.group(1)
        out: List[Dict[str, Any]] = []

        # 通达信K线数据格式: 日期;开盘*100;最高*100;最低*100;收盘*100;成交量;成交额;...
        # 或分号分隔: 20240101;10000;10500;9800;10300;100000;1030000
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            parts = seg.split(";")
            if len(parts) < 5:
                # 可能是逗号分隔
                parts = seg.split(",")
            if len(parts) < 5:
                continue
            try:
                dt_str = parts[0].strip()
                if len(dt_str) == 8 and dt_str.isdigit():
                    ts = int(datetime.strptime(dt_str, "%Y%m%d").timestamp())
                elif len(dt_str) >= 10:
                    ts = int(datetime.strptime(dt_str[:10], "%Y-%m-%d").timestamp())
                else:
                    continue
                # 通达信部分接口价格*100，需要检测
                o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                v = float(parts[5]) if len(parts) > 5 else 0
                # 如果价格异常大（>10000且收盘<100），可能是*100的格式
                if o > 10000 and c < 100:
                    o, h, l, c = o / 100, h / 100, l / 100, c / 100
                if o == 0 and c == 0:
                    continue
                out.append({
                    "time": ts, "open": round(o, 4), "high": round(h, 4),
                    "low": round(l, 4), "close": round(c, 4), "volume": round(v, 2),
                })
            except (ValueError, TypeError, IndexError):
                continue

        out.sort(key=lambda x: x["time"])
        return out[-count:] if len(out) > count else out

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=8.0, exceptions=(
        requests.exceptions.RequestException, ConnectionError, TimeoutError,
    ))
    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        通过通达信行情快照接口获取。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        params = _to_tdx_params(code)
        if not params:
            return None
        mkt, digits = params

        _tdx_quote_limiter.wait()

        # 通达信实时行情 HTTP 接口 (同花顺)
        url = "https://d.10jqka.com.cn/v6/realtime/hs_{}/last.js".format(digits)
        try:
            resp = requests.get(
                url,
                headers=get_request_headers(referer="https://stockpage.10jqka.com/"),
                timeout=timeout,
            )
            resp.encoding = "utf-8"
            text = resp.text or ""
        except Exception as e:
            logger.warning("[通达信行情] 请求失败 %s: %s", code, e)
            return None

        # 解析 JSON 响应
        m = re.search(r'\{[^}]+\}', text)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return None

        last = float(data.get("399", data.get("last", 0)) or 0)
        prev = float(data.get("400", data.get("prev_close", 0)) or 0)
        if last == 0 and prev == 0:
            return None

        open_p = float(data.get("401", data.get("open", 0)) or 0)
        high = float(data.get("402", data.get("high", 0)) or 0)
        low = float(data.get("403", data.get("low", 0)) or 0)
        vol = float(data.get("404", data.get("volume", 0)) or 0)
        name = str(data.get("100", data.get("name", ""))).strip()

        chg = round(last - prev, 4) if prev else 0.0
        return {
            "symbol": f"{digits}", "name": name,
            "last": last, "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
            "open": open_p, "high": high, "low": low,
            "previousClose": prev, "volume": vol,
        }

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情。

        通过逐只调用 fetch_quote 实现（通达信无原生批量行情接口）。

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict}
        """
        if not codes:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for c in codes:
            if not c:
                continue
            q = self.fetch_quote(c, timeout=timeout)
            if q:
                result[c] = q
        return result
