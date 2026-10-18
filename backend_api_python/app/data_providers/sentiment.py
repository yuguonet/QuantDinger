"""
sentiment.py — 情绪数据聚合模块（单文件版）

数据源: akshare → alternative.me（VIX 族整族摘除后，见 _ORDER 上方记录）
已删除: 新浪、东方财富
已移除: yfinance（2026-10-17，Yahoo 持续 read timeout，详见 _ORDER 上方说明）、
        DXY 指标（同日，剩余三源均拿不到值）、
        VIX 族 vix/vxn/gvz/vix_term（2026-10-18：用户不需要 VIX 具体值，
        且实测 TwelveData 免费计划对 VIX/VXN/GVZ 全 symbol invalid、
        US02Y/US10Y 亦 invalid，tencent 仅 usVIX 有效）

环境变量:
  TWELVE_DATA_API_KEY  — TwelveData API Key（与 config_loader 注册名一致，
        少写下划线会恒为空；commodities 侧的黄金源仍在使用该 key）
"""

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
# 2026-10-18 实测（免费计划）: VIX/VXN/GVZ/DXY 均 symbol invalid,
# US02Y/US10Y 亦 invalid ⇒ _TwelveData 在 sentiment 侧已无有效指标,
# 仅保留 us2y/us10y 条目作恢复对照（commodities 侧的黄金 XAU/USD 不受影响）
_TD_SYMBOLS = {
    "us2y": "US2Y", "us10y": "US10Y",
}
_TD_RANGES = {
    "us2y": (0.05, 10), "us10y": (0.05, 10),
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
#  yfinance 源已于 2026-10-17 整源移除（Yahoo 持续超时，
#  且它排在_ORDER 首位导致每次刷新先耗 60s+ 等报错）。
#  若要恢复：需重连query2.finance.yahoo.com 或配 YFINANCE_PROXY，
#  然后在 _ORDER 对应指标首位补回 "yfinance" 并重新启用下面的类。
#  保留 _YF_SYMBOLS 仅作恢复时的标的对照参考，当前无代码引用。
# ============================================================

_YF_SYMBOLS = {
    "vix": "^VIX", "vix3m": "^VIX3M",
    "vxn": "^VXN", "gvz": "^GVZ", "dxy": "DX-Y.NYB",
}


# ============================================================
#  腾讯
# ============================================================

_TX_BASE = "https://qt.gtimg.cn/q="
# 2026-10-18: vix/vxn/gvz/dxy 键随 VIX 族与 DXY 摘除一并删除。
# 实测 tencent 仅 usVIX 有效 (21.67)，usVXN/usGVZ/usDXY 均返空；
# 保留 tnx 仅作恢复对照（当前无 fetch 方法消费）
_TX_SYMBOLS = {
    "tnx": "usTNX",
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

    def fetch_yield_curve(self): return None


# ============================================================
#  聚合器
# ============================================================

class sentiment:
    """
    情绪数据聚合器

    优先级:
      Yield:     akshare → tencent
      Fear&Greed: alternative.me

    2026-10-17 摘除的源与指标（依据实测，不是猜测）:
      - yfinance 全线失效: Yahoo query2.finance.yahoo.com 持续 read timeout=10，
        ^VIX/^VXN/^GVZ/^VIX3M/DX-Y.NYB 全部 "No data found"，
        而它排在 vix/vxn/gvz/dxy/vix_term 五项的首位或次位，
        导致每次刷新先把 60s+耗在等 Yahoo 报错上。整源摘除。
      - DXY 整条移除: 摘掉 yfinance 后剩余三源实测全部拿不到值。
        若日后要恢复 DXY，需先补 TWELVE_DATA_API_KEY 或给 akshare 补实现。

    2026-10-18 摘除的源与指标（依据实测，不是猜测）:
      - VIX 族 vix/vxn/gvz/vix_term 整族摘除: 用户不需要 VIX 具体值；
        且实测 TwelveData 免费计划对 VIX/VXN/GVZ 全 symbol invalid、
        US02Y/US10Y 亦 invalid（yield_curve 的 twelvedata 源随之摘除），
        tencent 仅 usVIX 有效（usVXN/usGVZ 返空）。每轮刷新 3 条
        "ALL sources failed" ERROR 属纯噪音，故整族移除。
        恢复路径: 升级 TwelveData 套餐或改用其他 VIX 数据源后，
        按 _TD_SYMBOLS/_TX_SYMBOLS 的恢复对照注释补回条目与方法。
    """

    _ORDER = {
        "yield_curve":  ["akshare", "tencent"],
        "fear_greed":   ["altme"],
    }

    def __init__(self):
        self._p: dict[str, object] = {
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

    def get_yield_curve(self) -> Optional[dict]:
        """
        收益率曲线
        返回 {"y2": float, "y10": float, "spread": float} 或 None
        """
        return self._run("yield_curve", "fetch_yield_curve")

    def get_fear_greed(self) -> Optional[int]:
        """CNN 恐惧贪婪指数 (0=极度恐惧, 100=极度贪婪)"""
        r = self._run("fear_greed", "fetch_fear_greed_index")
        return r["value"] if r else None

    def get_all(self) -> dict:
        """
        并发获取所有指标。

        Returns:
            {
                "yield_curve": {"y2": 4.05, "y10": 4.35, "spread": 0.30},
                "fear_greed": 12,
            }
        """
        tasks = {
            "yield_curve": self.get_yield_curve,
            "fear_greed":  self.get_fear_greed,
        }
        results: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
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


def fetch_yield_curve() -> Optional[dict]:
    return _get_sentiment()._run("yield_curve", "fetch_yield_curve")


def fetch_fear_greed_index() -> Optional[dict]:
    return _get_sentiment()._run("fear_greed", "fetch_fear_greed_index")
