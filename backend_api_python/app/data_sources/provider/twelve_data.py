# -*- coding: utf-8 -*-
"""
Twelve Data 数据源 Provider

模块职责:
  通过 Twelve Data REST API 获取 A股/港股/美股 的 K线和实时行情数据。
  Twelve Data 是海外付费数据源，作为国内源不可用时的兜底选择（priority=100）。

能力:
  - K线: 全周期（1m/5m/15m/30m/1H/4H/1D/1W），支持前/后复权
  - 单只行情: 实时行情快照（/price API）
  - 批量行情: 不支持（逐只查询）
  - 批量K线: 不支持（逐只调用）

限制:
  - 需要 API Key（通过环境变量 TWELVE_DATA_API_KEY 或配置文件加载）
  - 国内访问不稳定，延迟较高
  - 有 API 调用频率限制（取决于订阅计划）
  - 免费计划仅支持部分功能

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → TwelveDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - os: 读取环境变量
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.provider import register, NotSupportedResult
from app.data_sources.rate_limiter import get_request_headers, RateLimiter
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ---------- 限流器 ----------

_twelvedata_limiter = RateLimiter(
    min_interval=1.5,
    jitter_min=0.8,
    jitter_max=3.0,
)


# ---------- API Key 加载 ----------

def _get_api_key() -> str:
    """获取 Twelve Data API Key（优先配置文件，其次环境变量）"""
    try:
        from app.utils.config_loader import load_addon_config
        key = load_addon_config().get("twelve_data", {}).get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    return (os.getenv("TWELVE_DATA_API_KEY") or "").strip()


# ---------- 周期映射 ----------

# 内部周期 → Twelve Data interval 参数
_TD_INTERVAL_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1H": "1h",
    "4H": "4h",
    "1D": "1day",
    "1W": "1week",
}

# 超时与重试配置
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 1.5
_BACKOFF_CAP_SEC = 12.0

# 瞬态错误标识（用于重试判断）
_TRANSIENT_ERR_MARKERS = (
    "remote end closed connection",
    "connection aborted",
    "connection reset",
    "timed out",
    "timeout",
    "max retries exceeded",
    "temporarily unavailable",
    "rate",
    "too many requests",
    "429",
)


def _is_transient(exc: BaseException) -> bool:
    """判断异常是否为瞬态错误（值得重试）"""
    return any(m in str(exc).lower() for m in _TRANSIENT_ERR_MARKERS)


# ---------- 代码转换 ----------

def _td_symbol_and_exchange(code: str) -> tuple[str, str]:
    """
    将股票代码转换为 Twelve Data (symbol, exchange) 格式。

    Twelve Data time_series 需要 exchange 名称（SSE/SZSE/HKEX/BSE），
    而非 MIC 代码（XSHG/XSHE/XHKG）。

    Args:
        code: 股票代码（如 "sh600519", "hk00700", "00700.HK"）

    Returns:
        (symbol, exchange) 元组，如 ("600519", "SSE")
    """
    c = (code or "").strip().upper()

    # 港股: hk00700 → ("0070", "HKEX")
    if c.startswith("HK"):
        num = c[2:]
        if num.isdigit():
            num = str(int(num)).zfill(4)
        return num, "HKEX"
    if c.endswith(".HK"):
        num = c.replace(".HK", "")
        if num.isdigit():
            num = str(int(num)).zfill(4)
        return num, "HKEX"

    # A股: sh600519 → ("600519", "SSE")
    digits = c.lstrip("SHSZBJ")
    if c.startswith("SH") or digits.startswith(("6", "9")):
        return digits, "SSE"
    if c.startswith("BJ") or digits.startswith(("43", "82", "83", "87", "88")):
        return digits, "BSE"
    return digits, "SZSE"


def _parse_td_kline(values: list, count: int) -> List[Dict[str, Any]]:
    """
    解析 Twelve Data 返回的 K线 JSON 数组为标准格式。

    Twelve Data time_series 返回格式:
    {"status": "ok", "values": [{"datetime": "2024-01-01", "open": 100, "high": 105, ...}, ...]}

    Args:
        values: Twelve Data 返回的 values 数组
        count:  最多返回条数

    Returns:
        标准K线列表 [{time, open, high, low, close, volume}, ...]
    """
    out: List[Dict[str, Any]] = []
    for v in values:
        try:
            dt_str = v.get("datetime", "")
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    ts = int(datetime.strptime(dt_str, fmt).timestamp())
                    break
                except ValueError:
                    continue
            if ts is None:
                continue
            o = float(v["open"])
            h = float(v["high"])
            low = float(v["low"])
            c = float(v["close"])
            vol = float(v.get("volume") or 0)
            if o == 0 and c == 0:
                continue
            out.append({
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(vol, 2),
            })
        except (ValueError, TypeError, KeyError):
            continue
    out.sort(key=lambda x: x["time"])
    return out[-count:] if len(out) > count else out


# ================================================================
# Provider 注册
# ================================================================

@register(priority=100)
class TwelveDataSource:
    """
    Twelve Data 数据源 — 海外付费兜底源（priority=100）。

    能力:
      - K线: 全周期（分钟/小时/日/周），通过 time_series API
      - 行情: 单只实时价格（/price API）
      - 批量行情: 不支持（返回 NotSupportedResult）
      - 批量K线: 不支持（返回 NotSupportedResult）

    限制:
      - 需要 API Key
      - 国内访问不稳定
      - 有频率限制

    线程安全性:
      - 实例方法无状态，线程安全
      - 使用独立限流器（_twelvedata_limiter）
    """

    name = "twelvedata"
    priority = 100

    capabilities = {
        "kline": True,
        "kline_priority": 100,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"},
        "kline_batch": False,
        "quote": True,
        "quote_priority": 100,
        "batch_quote": False,
        "batch_quote_priority": 100,
        "hk": True,
        "markets": {"CNStock", "HKStock"},
    }

    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 15,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据 — 通过 Twelve Data time_series API。

        Args:
            code:      股票代码（如 "sh600519", "hk00700"）
            timeframe: K线周期（如 "1D", "5m"）
            count:     数据条数
            adj:       复权方式（Twelve Data 通过 API 参数控制）
            timeout:   请求超时秒数

        Returns:
            K线数据列表，失败返回空列表
        """
        api_key = _get_api_key()
        if not api_key:
            logger.debug("[TwelveData] API Key 未配置，跳过")
            return []

        interval = _TD_INTERVAL_MAP.get(timeframe)
        if not interval:
            return []

        symbol, exchange = _td_symbol_and_exchange(code)
        params = {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "outputsize": min(int(count), 5000),
            "apikey": api_key,
            "format": "JSON",
            "dp": "4",
        }

        url = "https://api.twelvedata.com/time_series"

        for attempt in range(_MAX_ATTEMPTS):
            try:
                _twelvedata_limiter.wait()
                resp = requests.get(url, params=params, timeout=timeout)
                data = resp.json()
                break
            except Exception as e:
                if attempt + 1 < _MAX_ATTEMPTS and _is_transient(e):
                    delay = min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** attempt))
                    time.sleep(delay)
                    continue
                logger.debug("[TwelveData] K线失败 %s/%s tf=%s: %s", symbol, exchange, timeframe, e)
                return []
        else:
            return []

        if data.get("status") != "ok" or "values" not in data:
            msg = data.get("message", "")
            code_err = data.get("code", "")
            if code_err == 429 or "API credits" in msg or "minute limit" in msg:
                logger.warning("[TwelveData] 频率限制 %s/%s: %s", symbol, exchange, msg)
            elif "Pro" in msg or "Venture" in msg:
                logger.debug("[TwelveData] 套餐限制 %s/%s tf=%s: %s", symbol, exchange, timeframe, msg)
            else:
                logger.debug("[TwelveData] 错误 %s/%s tf=%s: %s", symbol, exchange, timeframe, msg)
            return []

        bars = _parse_td_kline(data["values"], count)
        logger.debug("[TwelveData] %s/%s tf=%s 返回 %d 条", symbol, exchange, timeframe, len(bars))
        return bars

    def fetch_kline_batch(
        self, codes: List[str], timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 15,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量K线 — Twelve Data 不支持原生批量，返回 NotSupportedResult"""
        return NotSupportedResult(self.name, "fetch_kline_batch")

    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时价格 — 通过 Twelve Data /price API。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        api_key = _get_api_key()
        if not api_key:
            return None

        symbol, exchange = _td_symbol_and_exchange(code)
        _twelvedata_limiter.wait()

        try:
            resp = requests.get(
                "https://api.twelvedata.com/price",
                params={
                    "symbol": symbol,
                    "exchange": exchange,
                    "apikey": api_key,
                },
                timeout=timeout,
            )
            data = resp.json()
        except Exception as e:
            logger.debug("[TwelveData] 行情失败 %s/%s: %s", symbol, exchange, e)
            return None

        if data.get("status") != "ok":
            return None

        price = data.get("price")
        if not price:
            return None

        try:
            last = float(price)
        except (TypeError, ValueError):
            return None

        if last <= 0:
            return None

        return {
            "last": last,
            "change": 0.0,
            "changePercent": 0.0,
            "high": last,
            "low": last,
            "open": last,
            "previousClose": 0.0,
            "name": "",
            "symbol": f"{symbol}.{exchange}",
        }

    def fetch_quotes_batch(
        self, codes: List[str], timeout: int = 10,
    ) -> Dict[str, Dict[str, Any]]:
        """批量行情 — Twelve Data 不支持批量行情，返回 NotSupportedResult"""
        return NotSupportedResult(self.name, "fetch_quotes_batch")
