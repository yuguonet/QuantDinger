# -*- coding: utf-8 -*-
"""通达信数据源 Provider — 老牌行情软件接口"""
from __future__ import annotations
import json, re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import requests
from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import get_request_headers, retry_with_backoff, RateLimiter
from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger
logger = get_logger(__name__)

_tdx_limiter = RateLimiter(min_interval=1.0, jitter_min=0.5, jitter_max=1.5)
_tdx_quote_limiter = RateLimiter(min_interval=0.6, jitter_min=0.2, jitter_max=1.0)

_TDX_MARKET = {"SH": 1, "SZ": 0, "BJ": 0}
_TDX_PERIOD = {"1m": 8, "5m": 0, "15m": 1, "30m": 2, "1H": 3, "1D": 4, "1W": 5}

def _to_tdx_params(code):
    market, digits = detect_market(code)
    if not market or not digits: return None
    mkt = _TDX_MARKET.get(market)
    if mkt is None: return None
    return (mkt, digits)

@register(priority=25)
class TdxDataSource:
    name = "tdx"; priority = 20
    capabilities = {"kline": True, "kline_priority": 20, "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
                    "kline_batch": True, "quote": True, "quote_priority": 25,
                    "batch_quote": False, "batch_quote_priority": 30, "hk": False, "markets": {"CNStock"}}

    def fetch_market_kline(self, timeframe="1D", count=300, adj="qfq", timeout=15, start_date="", end_date=""):
        """全市场批量K线 — count=None 走批量行情（1 HTTP），count 有值走并发 K 线"""
        # count=None 且无 start_date → 走 fetch_batch_quotes（1 HTTP 拿 N 只）
        from app.data_sources.provider import _resolve_market_kline_count
        count = _resolve_market_kline_count(timeframe, count, start_date)
        if count is None:
            from app.data_sources.provider import _all_market_kline_via_quotes
            return _all_market_kline_via_quotes(self, timeframe=timeframe, timeout=timeout)

        from app.data_sources.provider import _fetch_all_cn_codes
        codes = _fetch_all_cn_codes()
        if not codes: return {}
        import concurrent.futures; result = {}
        def _f(c): return c, self.fetch_kline(c, timeframe, count, adj=adj, timeout=timeout, start_date=start_date, end_date=end_date)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(codes), 8)) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(_f, c) for c in codes]):
                try:
                    c, bars = fut.result()
                    if bars: result[c] = bars
                except Exception: pass
        return result

    @retry_with_backoff(max_attempts=3, base_delay=1.5, max_delay=10.0, exceptions=(requests.exceptions.RequestException, ConnectionError, TimeoutError))
    def fetch_kline(self, code, timeframe="1D", count=300, adj="qfq", timeout=10, start_date="", end_date=""):
        if start_date:
            from app.data_sources.provider import calc_kline_count; count = calc_kline_count(timeframe, start_date, end_date)
        params = _to_tdx_params(code)
        if not params: return []
        mkt, digits = params; period = _TDX_PERIOD.get(timeframe)
        if period is None: return []
        _tdx_limiter.wait()
        url = "https://d.10jqka.com.cn/v6/line/hs_{}/01/last{}.js".format(digits, min(int(count), 800))
        if timeframe != "1D":
            url = "https://d.10jqka.com.cn/v6/line/hs_{}/0{}/last{}.js".format(digits, period, min(int(count), 800))
        try:
            resp = requests.get(url, headers=get_request_headers(referer="https://stockpage.10jqka.com/"), timeout=timeout)
            resp.encoding = "utf-8"; text = resp.text or ""
        except Exception as e: logger.warning("[通达信K线] 请求失败 %s: %s", code, e); return []
        m = re.search(r'"data"\s*:\s*"([^"]+)"', text)
        if not m: m = re.search(r'"([^"]*\d{8}[^"]*)"', text)
        if not m: return []
        raw = m.group(1); out = []
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg: continue
            parts = seg.split(";")
            if len(parts) < 5: parts = seg.split(",")
            if len(parts) < 5: continue
            try:
                dt_str = parts[0].strip()
                if len(dt_str) == 8 and dt_str.isdigit(): ts = int(datetime.strptime(dt_str, "%Y%m%d").timestamp())
                elif len(dt_str) >= 10: ts = int(datetime.strptime(dt_str[:10], "%Y-%m-%d").timestamp())
                else: continue
                o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                v = float(parts[5]) if len(parts) > 5 else 0
                if o > 10000 and c < 100: o, h, l, c = o/100, h/100, l/100, c/100
                if o == 0 and c == 0: continue
                out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4), "volume": round(v, 2)})
            except (ValueError, TypeError, IndexError): continue
        out.sort(key=lambda x: x["time"]); return out[-count:] if len(out) > count else out

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=8.0, exceptions=(requests.exceptions.RequestException, ConnectionError, TimeoutError))
    def fetch_ticker(self, code, timeout=8):
        params = _to_tdx_params(code)
        if not params: return None
        mkt, digits = params
        _tdx_quote_limiter.wait()
        try:
            resp = requests.get("https://d.10jqka.com.cn/v6/realtime/hs_{}/last.js".format(digits),
                headers=get_request_headers(referer="https://stockpage.10jqka.com/"), timeout=timeout)
            resp.encoding = "utf-8"; text = resp.text or ""
        except Exception as e: logger.warning("[通达信行情] 请求失败 %s: %s", code, e); return None
        m = re.search(r'\{[^}]+\}', text)
        if not m: return None
        try: data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError): return None
        last = float(data.get("399", data.get("last", 0)) or 0)
        prev = float(data.get("400", data.get("prev_close", 0)) or 0)
        if last == 0 and prev == 0: return None
        open_p = float(data.get("401", data.get("open", 0)) or 0)
        high = float(data.get("402", data.get("high", 0)) or 0)
        low = float(data.get("403", data.get("low", 0)) or 0)
        vol = float(data.get("404", data.get("volume", 0)) or 0)
        name = str(data.get("100", data.get("name", ""))).strip()
        chg = round(last - prev, 4) if prev else 0.0
        return {"last": last, "change": chg, "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                "high": high, "low": low, "open": open_p, "previousClose": prev, "name": name, "symbol": f"{digits}"}

    def fetch_batch_quotes(self, codes, timeout=10):
        return NotSupportedResult(self.name, "fetch_batch_quotes")


