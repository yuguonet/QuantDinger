# -*- coding: utf-8 -*-
"""
BaoStock 数据源 Provider

模块职责:
  通过 BaoStock 公开 API 获取 A股的 K线和实时行情数据。
  BaoStock 是证券宝提供的免费数据接口，数据来源可靠，作为A股数据源（priority=40）。

能力:
  - K线: 日线 + 周线 + 月线，支持前/后复权
  - 单只行情: 实时行情快照
  - 批量行情: 多只股票行情

特点:
  - 国内直连，无需API Key
  - 数据来源可靠（证券宝）
  - 历史数据丰富，适合回测

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → BaoStockDataSource（本模块）

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

_baostock_limiter = RateLimiter(
    min_interval=1.2,
    jitter_min=0.5,
    jitter_max=2.0,
)

_baostock_quote_limiter = RateLimiter(
    min_interval=0.8,
    jitter_min=0.3,
    jitter_max=1.2,
)


# BaoStock 代码格式: sh.600519 / sz.000001
def _to_baostock_code(code: str) -> str:
    """
    将股票代码转换为 BaoStock 格式: sh.600519 / sz.000001。

    Args:
        code: 任意格式的股票代码

    Returns:
        BaoStock 格式代码，无法识别返回空字符串
    """
    market, digits = detect_market(code)
    if not market or not digits:
        return ""
    return f"{market.lower()}.{digits}"


# BaoStock 周期映射
_BS_PERIOD = {
    "1D": "d", "1W": "w", "1M": "m",
}

# BaoStock 复权映射
_BS_ADJ = {"": "0", "qfq": "1", "hfq": "2"}


@register(priority=40)
class BaoStockDataSource:
    """
    BaoStock 数据源 — 证券宝免费数据（priority=40）。

    能力:
      - K线: 日线/周线/月线，支持复权
      - 行情: 单只实时行情
      - 批量行情: 多只股票行情

    线程安全性:
      - 实例方法无状态，线程安全
      - 使用独立的限流器

    注意:
      BaoStock 主要提供历史K线数据，实时行情能力有限。
      分钟级K线不支持（BaoStock 仅提供日/周/月线）。
    """

    name = "baostock"
    priority = 40

    capabilities = {
        "kline": True,
        "kline_priority": 5,
        "kline_tf": {"1D", "1W"},
        "quote": True,
        "quote_priority": 50,
        "batch_quote": True,
        "batch_quote_priority": 50,
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

        通过 BaoStock HTTP API 获取历史K线数据。
        BaoStock 仅支持日线、周线、月线，不支持分钟线。

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式
            timeout:   请求超时秒数

        Returns:
            K线数据列表
        """
        bs_code = _to_baostock_code(code)
        if not bs_code:
            return []
        period = _BS_PERIOD.get(timeframe)
        if period is None:
            return []
        adj_type = _BS_ADJ.get(adj, "1")

        _baostock_limiter.wait()

        # BaoStock HTTP API (data.baostock.com)
        # 计算起始日期（往前推 count 个交易日，粗略按 1.5 倍计算自然日）
        from datetime import timedelta
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(count * 1.8))).strftime("%Y-%m-%d")

        url = "http://api.baostock.com/kline/kline"
        params = {
            "code": bs_code,
            "start": start_date,
            "end": end_date,
            "frequency": period,
            "adjustflag": adj_type,
        }

        try:
            resp = requests.get(
                url,
                headers=get_request_headers(),
                params=params, timeout=timeout,
            )
            text = resp.text or ""
        except Exception as e:
            logger.warning("[BaoStock K线] 请求失败 %s: %s", code, e)
            return []

        # BaoStock 返回格式: 日期,开盘,收盘,最高,最低,成交量,成交额,...
        # 每行一条数据，逗号分隔
        out: List[Dict[str, Any]] = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("date") or line.startswith("error"):
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                dt_str = parts[0].strip()
                ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
                o = float(parts[1]) if parts[1] and parts[1] != "0" else 0
                c = float(parts[2]) if parts[2] and parts[2] != "0" else 0
                h = float(parts[3]) if parts[3] and parts[3] != "0" else 0
                low = float(parts[4]) if parts[4] and parts[4] != "0" else 0
                v = float(parts[5]) if len(parts) > 5 and parts[5] else 0
                if o == 0 and c == 0:
                    continue
                out.append({
                    "time": ts, "open": round(o, 4), "high": round(h, 4),
                    "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2),
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

        通过 BaoStock 行情接口获取当日行情快照。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        bs_code = _to_baostock_code(code)
        if not bs_code:
            return None

        _baostock_quote_limiter.wait()

        # BaoStock 实时行情接口
        url = "http://api.baostock.com/realtime/realtime"
        params = {"code": bs_code}

        try:
            resp = requests.get(
                url,
                headers=get_request_headers(),
                params=params, timeout=timeout,
            )
            data = resp.json() if resp.text.startswith("{") else {}
        except Exception as e:
            logger.warning("[BaoStock 行情] 请求失败 %s: %s", code, e)
            return None

        if not data:
            return None

        # 解析 BaoStock 行情响应
        # 字段: time, open, high, low, close, volume, amount, ...
        last = float(data.get("close", 0) or 0)
        prev = float(data.get("preClose", 0) or 0)
        if last == 0 and prev == 0:
            # 尝试从最新K线获取
            return None

        open_p = float(data.get("open", 0) or 0)
        high = float(data.get("high", 0) or 0)
        low = float(data.get("low", 0) or 0)
        vol = float(data.get("volume", 0) or 0)
        name = str(data.get("name", "")).strip()

        chg = round(last - prev, 4) if prev else 0.0
        return {
            "symbol": bs_code, "name": name,
            "last": last, "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
            "open": open_p, "high": high, "low": low,
            "previousClose": prev, "volume": vol,
        }

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情。

        通过逐只调用 fetch_quote 实现。

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
