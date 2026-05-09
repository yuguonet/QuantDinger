# -*- coding: utf-8 -*-
"""Twelve Data 数据源 Provider — 海外付费兜底源"""
from __future__ import annotations
import os, time
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests
from app.data_sources.provider import register, NotSupportedResult
from app.data_sources.rate_limiter import get_request_headers, RateLimiter
from app.utils.logger import get_logger
logger = get_logger(__name__)

_twelvedata_limiter = RateLimiter(min_interval=1.5, jitter_min=0.8, jitter_max=3.0)

def _get_api_key():
    try:
        from app.utils.config_loader import load_addon_config
        key = load_addon_config().get("twelve_data", {}).get("api_key", "")
        if key: return key
    except Exception: pass
    return (os.getenv("TWELVE_DATA_API_KEY") or "").strip()

_TD_INTERVAL_MAP = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1H": "1h", "4H": "4h", "1D": "1day", "1W": "1week"}
_MAX_ATTEMPTS = 3; _BACKOFF_BASE_SEC = 1.5; _BACKOFF_CAP_SEC = 12.0
_TRANSIENT_ERR_MARKERS = ("remote end closed connection", "connection aborted", "connection reset", "timed out", "timeout", "max retries exceeded", "temporarily unavailable", "rate", "too many requests", "429")

def _is_transient(exc):
    return any(m in str(exc).lower() for m in _TRANSIENT_ERR_MARKERS)

def _td_symbol_and_exchange(code):
    c = (code or "").strip().upper()
    if c.startswith("HK"):
        num = c[2:]
        if num.isdigit(): num = str(int(num)).zfill(4)
        return num, "HKEX"
    if c.endswith(".HK"):
        num = c.replace(".HK", "")
        if num.isdigit(): num = str(int(num)).zfill(4)
        return num, "HKEX"
    digits = c.lstrip("SHSZBJ")
    if c.startswith("SH") or digits.startswith(("6", "9")): return digits, "SSE"
    if c.startswith("BJ") or digits.startswith(("43", "82", "83", "87", "88")): return digits, "BSE"
    return digits, "SZSE"

def _parse_td_kline(values, count):
    out = []
    for v in values:
        try:
            dt_str = v.get("datetime", ""); ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try: ts = int(datetime.strptime(dt_str, fmt).timestamp()); break
                except ValueError: continue
            if ts is None: continue
            o = float(v["open"]); h = float(v["high"]); low = float(v["low"]); c = float(v["close"]); vol = float(v.get("volume") or 0)
            if o == 0 and c == 0: continue
            out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(low, 4), "close": round(c, 4), "volume": round(vol, 2)})
        except (ValueError, TypeError, KeyError): continue
    out.sort(key=lambda x: x["time"]); return out[-count:] if len(out) > count else out

@register(priority=100)
class TwelveDataSource:
    name = "twelvedata"; priority = 100
    capabilities = {"kline": True, "kline_priority": 100, "kline_tf": {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"},
                    "kline_batch": True, "quote": True, "quote_priority": 100,
                    "batch_quote": False, "batch_quote_priority": 100, "hk": True, "markets": {"CNStock", "HKStock"}}

    def fetch_market_kline(self, timeframe="1D", count=300, adj="qfq", timeout=15, start_date="", end_date=""):
        """全市场批量K线 — 不支持"""
        return NotSupportedResult(self.name, "fetch_market_kline")

    def fetch_kline(self, code, timeframe="1D", count=300, adj="qfq", timeout=15, start_date="", end_date=""):
        api_key = _get_api_key()
        if not api_key:
            if start_date:
                from app.data_sources.provider import calc_kline_count
                count = calc_kline_count(timeframe, start_date, end_date)
            logger.debug("[TwelveData] API Key 未配置，跳过"); return []
        interval = _TD_INTERVAL_MAP.get(timeframe)
        if not interval: return []
        symbol, exchange = _td_symbol_and_exchange(code)
        params = {"symbol": symbol, "exchange": exchange, "interval": interval, "outputsize": min(int(count), 5000),
                  "apikey": api_key, "format": "JSON", "dp": "4"}
        for attempt in range(_MAX_ATTEMPTS):
            try:
                _twelvedata_limiter.wait()
                resp = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=timeout)
                data = resp.json(); break
            except Exception as e:
                if attempt + 1 < _MAX_ATTEMPTS and _is_transient(e):
                    time.sleep(min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** attempt))); continue
                logger.debug("[TwelveData] K线失败 %s/%s tf=%s: %s", symbol, exchange, timeframe, e); return []
        else: return []
        if data.get("status") != "ok" or "values" not in data:
            msg = data.get("message", ""); code_err = data.get("code", "")
            if code_err == 429 or "API credits" in msg or "minute limit" in msg: logger.warning("[TwelveData] 频率限制 %s/%s: %s", symbol, exchange, msg)
            return []
        return _parse_td_kline(data["values"], count)

    def fetch_ticker(self, code, timeout=8):
        api_key = _get_api_key()
        if not api_key: return None
        symbol, exchange = _td_symbol_and_exchange(code)
        _twelvedata_limiter.wait()
        try:
            resp = requests.get("https://api.twelvedata.com/quote",
                params={"symbol": symbol, "exchange": exchange, "apikey": api_key}, timeout=timeout)
            data = resp.json()
        except Exception as e: logger.debug("[TwelveData] 行情失败 %s/%s: %s", symbol, exchange, e); return None
        if data.get("status") != "ok": return None
        try: last = float(data.get("close", 0) or 0)
        except (TypeError, ValueError): last = 0
        if last <= 0: return None
        try: prev = float(data.get("previous_close", 0) or 0)
        except (TypeError, ValueError): prev = 0
        chg = round(last - prev, 4) if prev else 0.0
        return {"last": last, "change": chg, "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                "high": float(data.get("high", 0) or 0), "low": float(data.get("low", 0) or 0),
                "open": float(data.get("open", 0) or 0), "previousClose": prev,
                "name": str(data.get("name", "") or ""), "symbol": f"{symbol}.{exchange}"}

    def fetch_batch_quotes(self, codes, timeout=10):
        return NotSupportedResult(self.name, "fetch_batch_quotes")


