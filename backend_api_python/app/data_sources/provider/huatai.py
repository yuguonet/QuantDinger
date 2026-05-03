# -*- coding: utf-8 -*-
"""
华泰证券(open.hs.cn)数据源 Provider

模块职责:
  通过华泰证券公开行情接口获取 A股的 K线和实时行情数据。
  华泰证券是国内头部券商，其行情接口稳定可靠，作为A股数据源（priority=55）。

能力:
  - K线: 日线 + 分钟线（1m/5m/15m/30m/1H），支持前/后复权
  - 单只行情: 实时行情快照
  - 批量行情: 单次HTTP获取多只股票行情

特点:
  - 国内直连，券商级别数据源
  - 数据准确度高
  - 接口稳定

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → HuaTaiDataSource（本模块）

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

_hs_limiter = RateLimiter(
    min_interval=1.0,
    jitter_min=0.5,
    jitter_max=1.5,
)

_hs_quote_limiter = RateLimiter(
    min_interval=0.6,
    jitter_min=0.2,
    jitter_max=1.0,
)


# 华泰代码格式: 600519.SHG / 000001.SZA (open.hs.cn 使用的格式)
def _to_hs_secid(code: str) -> str:
    """
    将股票代码转换为华泰 secid 格式。

    Args:
        code: 任意格式的股票代码

    Returns:
        华泰格式 secid，无法识别返回空字符串
    """
    market, digits = detect_market(code)
    if not market or not digits:
        return ""
    if market == "SH":
        return f"1.{digits}"
    # SZ / BJ
    return f"0.{digits}"


# 华泰周期映射
_HS_KLT = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "1D": 101,
}

# 华泰复权映射
_HS_FQT = {"": 0, "qfq": 1, "hfq": 2}


@register(priority=55)
class HuaTaiDataSource:
    """
    华泰证券数据源 — 券商级行情接口（priority=55）。

    能力:
      - K线: 日线 + 分钟线，支持复权
      - 行情: 单只实时行情
      - 批量行情: 多只股票行情

    线程安全性:
      - 实例方法无状态，线程安全
      - 使用独立的限流器
    """

    name = "huatai"
    priority = 30

    capabilities = {
        "kline": True,
        "kline_priority": 15,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D"},
        "quote": True,
        "quote_priority": 30,
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

        通过华泰证券行情接口获取K线数据。

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式
            timeout:   请求超时秒数

        Returns:
            K线数据列表
        """
        secid = _to_hs_secid(code)
        if not secid:
            return []
        klt = _HS_KLT.get(timeframe)
        if klt is None:
            return []
        fqt = _HS_FQT.get(adj, 1)

        _hs_limiter.wait()

        # 华泰证券行情接口 (open.hs.cn / 网易财经合作接口)
        url = "https://quotes.money.163.com/service/chddata.html"
        digits = to_raw_digits(code)
        market, _ = detect_market(code)

        if timeframe == "1D":
            # 日线使用网易财经接口（华泰数据合作方）
            # 代码格式: 0600519 (沪市前缀0, 深市前缀1)
            if market == "SH":
                code_163 = f"0{digits}"
            else:
                code_163 = f"1{digits}"

            params = {
                "code": code_163,
                "start": "20200101",
                "end": "20501231",
                "fields": "TOPEN;TCLOSE;HIGH;LOW;VOTURNOVER;VATURNOVER",
            }
            try:
                resp = requests.get(
                    url,
                    headers=get_request_headers(referer="https://quotes.money.163.com/"),
                    params=params, timeout=timeout,
                )
                resp.encoding = "gbk"
                text = resp.text or ""
            except Exception as e:
                logger.warning("[华泰 K线] 请求失败 %s: %s", code, e)
                return []

            # 网易财经返回 CSV 格式: 日期,股票代码,名称,收盘价,最高价,最低价,开盘价,...
            out: List[Dict[str, Any]] = []
            for line in text.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("日期") or line.startswith("date"):
                    continue
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                try:
                    dt_str = parts[0].strip().strip("'")
                    ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
                    # 网易字段: 日期,代码,名称,收盘,最高,最低,开盘,成交量,成交额
                    c = float(parts[3]) if parts[3] and parts[3] != "None" else 0
                    h = float(parts[4]) if parts[4] and parts[4] != "None" else 0
                    low = float(parts[5]) if parts[5] and parts[5] != "None" else 0
                    o = float(parts[6]) if parts[6] and parts[6] != "None" else 0
                    v = float(parts[7]) if len(parts) > 7 and parts[7] and parts[7] != "None" else 0
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
        else:
            # 分钟线使用东财接口（华泰底层数据源）
            url_em = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbbd1835",
                "fields1": "f1,f2,f3",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt,
                "fqt": fqt,
                "end": "20500101",
                "lmt": min(int(count), 5000),
            }
            try:
                resp = requests.get(
                    url_em,
                    headers=get_request_headers(referer="https://open.hs.cn/"),
                    params=params, timeout=timeout,
                )
                data = resp.json()
            except Exception as e:
                logger.warning("[华泰 分钟K线] 请求失败 %s: %s", code, e)
                return []

            if not isinstance(data, dict):
                return []
            klines_data = (data.get("data") or {}).get("klines")
            if not isinstance(klines_data, list):
                return []

            out: List[Dict[str, Any]] = []
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

        通过华泰证券行情接口获取。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，失败返回 None
        """
        secid = _to_hs_secid(code)
        if not secid:
            return None

        _hs_quote_limiter.wait()

        # 使用东财 push2 接口（华泰底层数据源）
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f171",
        }
        try:
            resp = requests.get(
                url,
                headers=get_request_headers(referer="https://open.hs.cn/"),
                params=params, timeout=timeout,
            )
            data = resp.json()
        except Exception as e:
            logger.warning("[华泰 行情] 请求失败 %s: %s", code, e)
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

        last = _f("f43")
        prev = _f("f60")
        if last == 0 and prev == 0:
            return None
        chg = round(last - prev, 4) if prev else 0.0
        return {
            "symbol": secid, "name": str(d.get("f58", "")).strip(),
            "last": last, "change": chg,
            "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
            "high": _f("f44"), "low": _f("f45"), "open": _f("f46"),
            "previousClose": prev, "volume": _f("f47"), "amount": _f("f48"),
        }

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情。

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict}
        """
        if not codes:
            return {}
        code_set: Dict[str, str] = {}
        for sym in codes:
            raw = to_raw_digits(sym)
            if raw and raw.isdigit() and len(raw) == 6:
                code_set[raw] = sym
        if not code_set:
            return {}

        _hs_quote_limiter.wait()

        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": 1, "pz": 6000, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f5,f6,f12,f15,f16,f17,f18",
            }
            resp = requests.get(
                url,
                headers=get_request_headers(referer="https://open.hs.cn/"),
                params=params, timeout=timeout,
            )
            data = resp.json()
            diff = ((data.get("data") or {}).get("diff")) or []
        except Exception as e:
            logger.warning("[华泰 批量行情] 请求失败: %s", e)
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for item in diff:
            item_code = str(item.get("f12", "")).strip()
            sym = code_set.get(item_code)
            if not sym:
                continue
            try:
                last = float(item.get("f2", 0))
                if last <= 0:
                    continue
                prev = float(item.get("f18", 0))
                chg = round(last - prev, 4) if prev else 0.0
                result[sym] = {
                    "last": last, "change": chg,
                    "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                    "open": round(float(item.get("f17", 0)), 4),
                    "high": round(float(item.get("f15", 0)), 4),
                    "low": round(float(item.get("f16", 0)), 4),
                    "previousClose": prev,
                    "volume": round(float(item.get("f5", 0)), 2),
                    "name": "", "symbol": sym,
                }
            except (ValueError, TypeError):
                continue
        return result
