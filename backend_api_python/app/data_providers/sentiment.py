"""
sentiment.py — 情绪数据聚合模块（单文件版）

数据源: TwelveData(优先) → yfinance → tencent → akshare → alternative.me
已删除: 新浪、东方财富

环境变量:
  TWELVEDATA_API_KEY  — TwelveData API Key
  YFINANCE_PROXY      — yfinance 代理地址 (如 http://127.0.0.1:7890)
"""

import os
import re
import time
import logging
import requests
import concurrent.futures
from typing import Optional

logger = logging.getLogger("app.data_providers.sentiment")


# ============================================================
#  TwelveData
# ============================================================

_TD_BASE = "https://api.twelvedata.com"
_TD_SYMBOLS = {
    "vix": "VIX", "vxn": "VXN", "gvz": "GVZ",
    "dxy": "DXY", "us2y": "US2Y", "us10y": "US10Y",
}
_TD_RANGES = {
    "vix": (5, 90), "vxn": (5, 90), "gvz": (5, 90),
    "dxy": (70, 120), "us2y": (0.05, 10), "us10y": (0.05, 10),
}


class _TwelveData:
    """TwelveData — 免费层 8 credits/分钟, 800/天"""

    def __init__(self, api_key: str):
        self.key = api_key
        self._cache: dict[str, tuple[float, dict]] = {}
        self._ttl = 60
        self._last = 0.0
        self._gap = 8.0  # 秒

    def _wait(self):
        diff = time.time() - self._last
        if diff < self._gap:
            time.sleep(self._gap - diff)
        self._last = time.time()

    def _cached(self, k):
        if k in self._cache:
            ts, v = self._cache[k]
            if time.time() - ts < self._ttl:
                return v
            del self._cache[k]
        return None

    def _put(self, k, v):
        self._cache[k] = (time.time(), v)

    def _quote(self, indicator: str) -> Optional[dict]:
        if not self.key:
            return None
        sym = _TD_SYMBOLS.get(indicator)
        if not sym:
            return None

        ck = f"td_{indicator}"
        hit = self._cached(ck)
        if hit:
            return hit

        self._wait()
        try:
            r = requests.get(
                f"{_TD_BASE}/quote",
                params={"symbol": sym, "apikey": self.key},
                timeout=10,
            )
            d = r.json()

            if "code" in d and d["code"] != 200:
                logger.warning("twelvedata %s: %s", sym, d.get("message", ""))
                return None

            close = d.get("close")
            if close is None:
                return None

            val = float(close)
            lo, hi = _TD_RANGES.get(indicator, (0, 9999))
            if not (lo <= val <= hi):
                logger.warning("twelvedata %s: %s out of range", sym, val)
                return None

            out = {"value": val, "source": "twelvedata"}
            self._put(ck, out)
            return out
        except Exception as e:
            logger.warning("twelvedata %s: %s", sym, e)
            return None

    def fetch_vix(self):
        return self._quote("vix")

    def fetch_vxn(self):
        return self._quote("vxn")

    def fetch_gvz(self):
        return self._quote("gvz")

    def fetch_dxy(self):
        return self._quote("dxy")

    def fetch_vix_term(self):
        """TwelveData 没有 VIX3M, 无法算期限结构"""
        return None

    def fetch_yield_curve(self):
        y2 = self._quote("us2y")
        y10 = self._quote("us10y")
        if not y2 or not y10:
            return None
        return {
            "y2": y2["value"], "y10": y10["value"],
            "spread": round(y10["value"] - y2["value"], 4),
            "source": "twelvedata",
        }

    def fetch_fear_greed_index(self):
        return None


# ============================================================
#  yfinance
# ============================================================

_YF_SYMBOLS = {
    "vix": "^VIX", "vix3m": "^VIX3M",
    "vxn": "^VXN", "gvz": "^GVZ", "dxy": "DX-Y.NYB",
}


class _YFinance:

    def __init__(self, proxy: str | None = None):
        if proxy:
            import yfinance as _yf
            _yf.set_config(proxy=proxy)
            logger.info("yfinance: proxy=%s", proxy)

    @staticmethod
    def _close(symbol: str) -> Optional[float]:
        import yfinance as _yf
        from datetime import datetime, timedelta

        try:
            t = _yf.Ticker(symbol)
            df = t.history(period="5d")
            if df is None or df.empty:
                end = datetime.now()
                start = end - timedelta(days=10)
                df = t.history(
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                )
            if df is None or df.empty:
                logger.warning("yfinance %s: no data", symbol)
                return None
            val = float(df.iloc[-1]["Close"])
            return val if val > 0 else None
        except Exception as e:
            logger.warning("yfinance %s: %s", symbol, e)
            return None

    def fetch_vix(self):
        v = self._close(_YF_SYMBOLS["vix"])
        return {"value": v, "source": "yfinance"} if v else None

    def fetch_vxn(self):
        v = self._close(_YF_SYMBOLS["vxn"])
        return {"value": v, "source": "yfinance"} if v else None

    def fetch_gvz(self):
        v = self._close(_YF_SYMBOLS["gvz"])
        return {"value": v, "source": "yfinance"} if v else None

    def fetch_dxy(self):
        v = self._close(_YF_SYMBOLS["dxy"])
        if v and 70 < v < 120:
            return {"value": v, "source": "yfinance"}
        return None

    def fetch_vix_term(self):
        vix = self._close(_YF_SYMBOLS["vix"])
        vix3m = self._close(_YF_SYMBOLS["vix3m"])
        if not vix or not vix3m:
            return None
        ratio = round(vix / vix3m, 4)
        return {
            "vix": vix, "vix3m": vix3m,
            "ratio": ratio, "backwardated": ratio > 1.0,
            "source": "yfinance",
        }

    def fetch_yield_curve(self):
        return None

    def fetch_fear_greed_index(self):
        return None


# ============================================================
#  腾讯
# ============================================================

_TX_BASE = "https://qt.gtimg.cn/q="
_TX_SYMBOLS = {
    "vix": "usVIX", "vxn": "usVXN",
    "gvz": "usGVZ", "dxy": "usDXY", "tnx": "usTNX",
}


class _Tencent:

    @staticmethod
    def _raw(code: str) -> Optional[str]:
        try:
            r = requests.get(f"{_TX_BASE}{code}", timeout=5)
            if r.status_code != 200:
                return None
            t = r.text.strip()
            return t if '""' not in t and len(t) >= 30 else None
        except Exception:
            return None

    @staticmethod
    def _price(raw: str) -> Optional[float]:
        m = re.search(r'"(.+)"', raw)
        if not m:
            return None
        fields = m.group(1).split("~")
        for idx in (3, 4, 5, 6):
            if idx >= len(fields):
                break
            try:
                v = float(fields[idx])
                if v > 0:
                    return v
            except ValueError:
                continue
        return None

    def _get(self, key: str, lo=0, hi=9999) -> Optional[dict]:
        sym = _TX_SYMBOLS.get(key)
        if not sym:
            return None
        raw = self._raw(sym)
        if not raw:
            return None
        val = self._price(raw)
        if val is None or not (lo <= val <= hi):
            return None
        return {"value": val, "source": "tencent"}

    def fetch_vix(self):
        return self._get("vix", 5, 90)

    def fetch_vxn(self):
        return self._get("vxn", 5, 90)

    def fetch_gvz(self):
        return self._get("gvz", 5, 90)

    def fetch_dxy(self):
        return self._get("dxy", 70, 120)

    def fetch_vix_term(self):
        return None

    def fetch_yield_curve(self):
        return None

    def fetch_fear_greed_index(self):
        return None


# ============================================================
#  akshare
# ============================================================

class _Akshare:

    def fetch_yield_curve(self) -> Optional[dict]:
        try:
            import akshare as ak

            df = ak.bond_zh_us_rate(start_date="20260101")
            if df is None or df.empty:
                return None

            latest = df.iloc[-1]
            y2 = y10 = None
            for col in df.columns:
                cl = str(col).lower()
                if y2 is None and "美国" in cl and "2" in cl:
                    y2 = latest[col]
                elif y10 is None and "美国" in cl and "10" in cl:
                    y10 = latest[col]

            if y2 is None or y10 is None:
                logger.warning("akshare yield: columns not matched in %s", list(df.columns))
                return None

            y2, y10 = float(y2), float(y10)

            # 合理性校验 — 拦截 y10=0.40 这类解析错误
            if not (0.1 < y2 < 15.0) or not (0.1 < y10 < 15.0):
                logger.warning("akshare yield: y2=%.4f y10=%.4f out of range", y2, y10)
                return None

            return {
                "y2": round(y2, 4), "y10": round(y10, 4),
                "spread": round(y10 - y2, 4),
                "source": "akshare",
            }
        except Exception as e:
            logger.warning("akshare yield: %s", e)
            return None

    def fetch_vix(self):       return None
    def fetch_vxn(self):       return None
    def fetch_gvz(self):       return None
    def fetch_dxy(self):       return None
    def fetch_vix_term(self):  return None
    def fetch_fear_greed_index(self): return None


# ============================================================
#  alternative.me
# ============================================================

class _AltMe:

    def fetch_fear_greed_index(self) -> Optional[dict]:
        try:
            r = requests.get(
                "https://api.alternative.me/fng/",
                params={"limit": 1}, timeout=10,
            )
            entries = r.json().get("data", [])
            if not entries:
                return None
            return {
                "value": int(entries[0]["value"]),
                "classification": entries[0].get("value_classification", ""),
                "source": "alternative.me",
            }
        except Exception as e:
            logger.warning("alternative.me: %s", e)
            return None

    def fetch_vix(self):        return None
    def fetch_vxn(self):        return None
    def fetch_gvz(self):        return None
    def fetch_dxy(self):        return None
    def fetch_vix_term(self):   return None
    def fetch_yield_curve(self): return None


# ============================================================
#  聚合器
# ============================================================

class sentiment:
    """
    情绪数据聚合器

    优先级（每项 TwelveData 排第一）:
      VIX:       twelvedata → yfinance → tencent
      VXN:       twelvedata → yfinance → tencent
      GVZ:       twelvedata → yfinance → tencent
      DXY:       twelvedata → yfinance → tencent → akshare
      Yield:     twelvedata → akshare → tencent
      VIX Term:  yfinance → twelvedata
      Fear&Greed: alternative.me
    """

    _ORDER = {
        "vix":          ["twelvedata", "yfinance", "tencent"],
        "vxn":          ["twelvedata", "yfinance", "tencent"],
        "gvz":          ["twelvedata", "yfinance", "tencent"],
        "dxy":          ["twelvedata", "yfinance", "tencent", "akshare"],
        "yield_curve":  ["twelvedata", "akshare", "tencent"],
        "vix_term":     ["yfinance", "twelvedata"],
        "fear_greed":   ["altme"],
    }

    def __init__(
        self,
        twelvedata_api_key: str = "",
        yfinance_proxy: str | None = None,
    ):
        self._p: dict[str, object] = {
            "twelvedata": _TwelveData(twelvedata_api_key or os.environ.get("TWELVEDATA_API_KEY", "")),
            "yfinance":   _YFinance(yfinance_proxy or os.environ.get("YFINANCE_PROXY")),
            "tencent":    _Tencent(),
            "akshare":    _Akshare(),
            "altme":      _AltMe(),
        }
        logger.info("sentiment ready: %s", list(self._p.keys()))

    # ── 核心调度 ──

    def _run(self, indicator: str, method: str) -> Optional[dict]:
        for src in self._ORDER.get(indicator, []):
            provider = self._p.get(src)
            if not provider:
                continue
            fn = getattr(provider, method, None)
            if not fn:
                continue
            try:
                r = fn()
                if r is not None:
                    logger.info("%s: %s from %s", indicator, r.get("value"), src)
                    return r
            except Exception as e:
                logger.warning("%s @ %s: %s", indicator, src, e)
        logger.error("%s: ALL sources failed", indicator)
        return None

    # ── 公开接口 ──

    def get_vix(self) -> Optional[float]:
        """VIX 恐慌指数"""
        r = self._run("vix", "fetch_vix")
        return r["value"] if r else None

    def get_vxn(self) -> Optional[float]:
        """NASDAQ VIX"""
        r = self._run("vxn", "fetch_vxn")
        return r["value"] if r else None

    def get_gvz(self) -> Optional[float]:
        """黄金 VIX"""
        r = self._run("gvz", "fetch_gvz")
        return r["value"] if r else None

    def get_dxy(self) -> Optional[float]:
        """美元指数"""
        r = self._run("dxy", "fetch_dxy")
        return r["value"] if r else None

    def get_yield_curve(self) -> Optional[dict]:
        """
        收益率曲线
        返回 {"y2": float, "y10": float, "spread": float} 或 None
        """
        return self._run("yield_curve", "fetch_yield_curve")

    def get_vix_term(self) -> Optional[dict]:
        """
        VIX 期限结构
        返回 {"vix": float, "vix3m": float, "ratio": float, "backwardated": bool} 或 None
        """
        return self._run("vix_term", "fetch_vix_term")

    def get_fear_greed(self) -> Optional[int]:
        """CNN 恐惧贪婪指数 (0=极度恐惧, 100=极度贪婪)"""
        r = self._run("fear_greed", "fetch_fear_greed_index")
        return r["value"] if r else None

    def get_all(self) -> dict:
        """
        并发获取所有指标。

        Returns:
            {
                "vix": 21.67,
                "vxn": 25.3,
                "gvz": 18.5,
                "dxy": 104.2,
                "yield_curve": {"y2": 4.05, "y10": 4.35, "spread": 0.30},
                "vix_term": {"ratio": 0.95, "backwardated": False},
                "fear_greed": 12,
            }
        """
        tasks = {
            "vix":         self.get_vix,
            "vxn":         self.get_vxn,
            "gvz":         self.get_gvz,
            "dxy":         self.get_dxy,
            "yield_curve": self.get_yield_curve,
            "vix_term":    self.get_vix_term,
            "fear_greed":  self.get_fear_greed,
        }
        results: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(fn): k for k, fn in tasks.items()}
            for fut in concurrent.futures.as_completed(futs):
                k = futs[fut]
                try:
                    v = fut.result()
                    if v is not None:
                        results[k] = v
                except Exception as e:
                    logger.warning("get_all.%s: %s", k, e)

        logger.info("Sentiment: %d/%d OK", len(results), len(tasks))
        return results


# ============================================================
#  模块级函数 — 兼容 global_market.py import
# ============================================================

import threading as _threading

_sentiment_instance = None
_sentiment_instance_lock = _threading.Lock()


def _get_sentiment() -> sentiment:
    global _sentiment_instance
    if _sentiment_instance is None:
        with _sentiment_instance_lock:
            if _sentiment_instance is None:
                _sentiment_instance = sentiment()
    return _sentiment_instance


def _set_cached_indicator(key: str, data: object) -> None:
    """旧缓存接口（已失效），保留兼容。"""
    pass


def fetch_vix() -> Optional[dict]:
    return _get_sentiment()._run("vix", "fetch_vix")


def fetch_vxn() -> Optional[dict]:
    return _get_sentiment()._run("vxn", "fetch_vxn")


def fetch_gvz() -> Optional[dict]:
    return _get_sentiment()._run("gvz", "fetch_gvz")


def fetch_dollar_index() -> Optional[dict]:
    return _get_sentiment()._run("dxy", "fetch_dxy")


def fetch_yield_curve() -> Optional[dict]:
    return _get_sentiment()._run("yield_curve", "fetch_yield_curve")


def fetch_fear_greed_index() -> Optional[dict]:
    return _get_sentiment()._run("fear_greed", "fetch_fear_greed_index")


def fetch_put_call_ratio() -> Optional[dict]:
    return _get_sentiment()._run("vix_term", "fetch_vix_term")
