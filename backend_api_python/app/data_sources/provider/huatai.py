# -*- coding: utf-8 -*-
"""华泰证券(open.hs.cn)数据源 Provider — 券商级行情接口"""
from __future__ import annotations
import json, re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import requests
from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import get_request_headers, retry_with_backoff, RateLimiter
from app.data_sources.provider import register
from app.utils.logger import get_logger
logger = get_logger(__name__)

_hs_limiter = RateLimiter(min_interval=1.0, jitter_min=0.5, jitter_max=1.5)
_hs_quote_limiter = RateLimiter(min_interval=0.6, jitter_min=0.2, jitter_max=1.0)

def _to_hs_secid(code):
    market, digits = detect_market(code)
    if not market or not digits: return ""
    return f"1.{digits}" if market == "SH" else f"0.{digits}"

_HS_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "1D": 101}
_HS_FQT = {"": 0, "qfq": 1, "hfq": 2}

@register(priority=55)
class HuaTaiDataSource:
    name = "huatai"; priority = 30
    capabilities = {"kline": True, "kline_priority": 15, "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D"},
                    "kline_batch": True, "quote": True, "quote_priority": 30,
                    "batch_quote": True, "batch_quote_priority": 30, "hk": False, "markets": {"CNStock"}}

    def fetch_market_kline(self, timeframe="1D", count=300, adj="qfq", timeout=15, start_date="", end_date=""):
        """全市场批量K线 — count=None 走批量行情（1 HTTP），count 有值走并发 K 线"""
        # count=None 且无 start_date → 走 fetch_batch_quotes（1 HTTP 拿 N 只）
        if count is None and (not start_date or _is_today(start_date)):
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
        secid = _to_hs_secid(code)
        if not secid: return []
        klt = _HS_KLT.get(timeframe)
        if klt is None: return []
        fqt = _HS_FQT.get(adj, 1)
        _hs_limiter.wait()
        digits = to_raw_digits(code); market, _ = detect_market(code)
        if timeframe == "1D":
            code_163 = f"0{digits}" if market == "SH" else f"1{digits}"
            try:
                resp = requests.get("https://quotes.money.163.com/service/chddata.html",
                    headers=get_request_headers(referer="https://quotes.money.163.com/"),
                    params={"code": code_163, "start": "20200101", "end": "20501231",
                            "fields": "TOPEN;TCLOSE;HIGH;LOW;VOTURNOVER;VATURNOVER"}, timeout=timeout)
                resp.encoding = "gbk"; text = resp.text or ""
            except Exception as e: logger.warning("[华泰 K线] 请求失败 %s: %s", code, e); return []
            out = []
            for line in text.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("日期") or line.startswith("date"): continue
                parts = line.split(",")
                if len(parts) < 7: continue
                try:
                    dt_str = parts[0].strip().strip("'"); ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
                    c = float(parts[3]) if parts[3] and parts[3] != "None" else 0
                    h = float(parts[4]) if parts[4] and parts[4] != "None" else 0
                    low = float(parts[5]) if parts[5] and parts[5] != "None" else 0
                    o = float(parts[6]) if parts[6] and parts[6] != "None" else 0
                    v = float(parts[7]) if len(parts) > 7 and parts[7] and parts[7] != "None" else 0
                    if o == 0 and c == 0: continue
                    out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2)})
                except (ValueError, TypeError, IndexError): continue
            out.sort(key=lambda x: x["time"]); return out[-count:] if len(out) > count else out
        else:
            try:
                resp = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    headers=get_request_headers(referer="https://open.hs.cn/"),
                    params={"secid": secid, "ut": "fa5fd1943c7b386f172d6893dbbd1835",
                            "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                            "klt": klt, "fqt": fqt, "end": "20500101", "lmt": min(int(count), 5000)}, timeout=timeout)
                data = resp.json()
            except Exception as e: logger.warning("[华泰 分钟K线] 请求失败 %s: %s", code, e); return []
            if not isinstance(data, dict): return []
            klines_data = (data.get("data") or {}).get("klines")
            if not isinstance(klines_data, list): return []
            out = []
            for line in klines_data:
                parts = line.split(",")
                if len(parts) < 7: continue
                try:
                    dt_str = parts[0].strip(); ts = None
                    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try: ts = int(datetime.strptime(dt_str, fmt).timestamp()); break
                        except ValueError: continue
                    if ts is None: continue
                    o, c, h, low, v = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])
                    if o == 0 and c == 0: continue
                    out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(low, 4), "close": round(c, 4), "volume": round(v, 2)})
                except (ValueError, TypeError, IndexError): continue
            out.sort(key=lambda x: x["time"]); return out[-count:] if len(out) > count else out

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=8.0, exceptions=(requests.exceptions.RequestException, ConnectionError, TimeoutError))
    def fetch_ticker(self, code, timeout=8):
        secid = _to_hs_secid(code)
        if not secid: return None
        _hs_quote_limiter.wait()
        try:
            resp = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                headers=get_request_headers(referer="https://open.hs.cn/"),
                params={"secid": secid, "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f171"}, timeout=timeout)
            data = resp.json()
        except Exception as e: logger.warning("[华泰 行情] 请求失败 %s: %s", code, e); return None
        if not isinstance(data, dict): return None
        d = data.get("data")
        if not isinstance(d, dict): return None
        def _f(key, default=0.0):
            v = d.get(key)
            if v is None or v == "-" or v == "": return default
            try: return float(v)
            except (TypeError, ValueError): return default
        last = _f("f43") / 100; prev = _f("f60") / 100
        if last == 0 and prev == 0: return None
        chg = round(last - prev, 4) if prev else 0.0
        return {"symbol": secid, "name": str(d.get("f58", "")).strip(), "last": last, "change": chg,
                "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                "high": _f("f44") / 100, "low": _f("f45") / 100, "open": _f("f46") / 100, "previousClose": prev, "volume": _f("f47"), "amount": _f("f48")}

    def fetch_batch_quotes(self, codes, timeout=10):
        if not codes: return {}
        code_set = {}
        for sym in codes:
            raw = to_raw_digits(sym)
            if raw and raw.isdigit() and len(raw) == 6: code_set[raw] = sym
        if not code_set: return {}
        _hs_quote_limiter.wait()
        try:
            resp = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                headers=get_request_headers(referer="https://open.hs.cn/"),
                params={"pn": 1, "pz": 6000, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                        "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                        "fields": "f2,f5,f6,f12,f15,f16,f17,f18"}, timeout=timeout)
            data = resp.json(); diff = ((data.get("data") or {}).get("diff")) or []
        except Exception as e: logger.warning("[华泰 批量行情] 请求失败: %s", e); return {}
        now = datetime.now(timezone(timedelta(hours=8)))
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        result = {}
        for item in diff:
            item_code = str(item.get("f12", "")).strip(); sym = code_set.get(item_code)
            if not sym: continue
            try:
                last = float(item.get("f2", 0))
                if last <= 0: continue
                prev = float(item.get("f18", 0)); chg = round(last - prev, 4) if prev else 0.0
                result[sym] = {"last": last, "change": chg, "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                               "open": round(float(item.get("f17", 0)), 4), "high": round(float(item.get("f15", 0)), 4),
                               "low": round(float(item.get("f16", 0)), 4), "previousClose": prev,
                               "volume": round(float(item.get("f5", 0)), 2), "name": "", "symbol": sym, "time": today_ts}
            except (ValueError, TypeError): continue
        return result

