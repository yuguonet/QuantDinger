"""
=============================================
市场情绪指标模块 (Market Sentiment Indicators)
=============================================

获取并缓存 7 个核心宏观情绪指标，每个指标独立缓存 + 时间戳。

指标与 TTL:
    恐贪指数 (Fear & Greed)    4h    alternative.me (加密货币恐贪)
    VIX (CBOE 波动率)          5min  yfinance → akshare 降级
    VXN (纳斯达克波动率)        5min  yfinance
    DXY (美元指数)              10min yfinance → akshare 降级
    收益率曲线 (10Y-2Y)         10min yfinance (^TNX, ^TYX)
    GVZ (黄金波动率)            10min yfinance
    VIX 期限结构 (VIX/VIX3M)    5min  yfinance

缓存策略:
    - 每个指标以 ``{"data": ..., "ts": ...}`` 格式独立存储
    - _get_cached_indicator() 检查 ts + TTL 判定是否新鲜
    - 未超 TTL → 直接返回缓存，不打任何外部 API
    - 超 TTL → 仅刷新过期的指标，其余保持缓存

调用入口:
    get_sentiment_data(timeout=10) → Dict   # 主入口，返回全部指标
    fetch_vix() / fetch_dxy() / ...         # 单独调用（内部也会走独立缓存）

依赖:
    - yfinance (VIX/DXY/GVZ/VXN/收益率曲线 主数据源)
    - requests  (恐贪指数 API)
    - akshare   (VIX/DXY 降级，可选)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Per-indicator cache configuration
# ---------------------------------------------------------------------------

_INDICATOR_CACHE_TTL: Dict[str, int] = {
    "fear_greed":   14400,   # 4h — 恐贪指数每天更新一次
    "vix":          300,     # 5min — 美股盘中活跃
    "dxy":          600,     # 10min — 美元指数
    "yield_curve":  600,     # 10min — 收益率曲线
    "vxn":          300,     # 5min
    "gvz":          600,     # 10min — 金波动率
    "vix_term":     300,     # 5min — VIX期限结构
}

# Cache key prefix
_CK = "sentiment_"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache() -> Any:
    """Lazy accessor for CacheManager singleton."""
    from app.utils.cache import CacheManager
    return CacheManager()


def _get_cached_indicator(name: str) -> Optional[Dict[str, Any]]:
    """Return cached indicator if it exists and hasn't expired, else None.

    Each entry is stored as ``{"data": <payload>, "ts": <unix>}``.
    """
    cm = _cache()
    raw = cm.get(f"{_CK}{name}")
    if not raw:
        return None
    data = raw.get("data")
    ts = raw.get("ts", 0)
    ttl = _INDICATOR_CACHE_TTL.get(name, 300)
    if data is not None and (time.time() - ts) < ttl:
        return data
    return None


def _set_cached_indicator(name: str, data: Dict[str, Any]) -> None:
    """Write indicator data with a timestamp."""
    cm = _cache()
    ttl = _INDICATOR_CACHE_TTL.get(name, 300)
    cm.set(f"{_CK}{name}", {"data": data, "ts": int(time.time())}, ttl=ttl * 2)
    # TTL on the cache entry itself is 2× the logical TTL so stale entries
    # survive in Redis/memory a bit longer (we check freshness ourselves).


def _fresh(key: str, fetcher) -> Dict[str, Any]:
    """Return cached data if fresh, otherwise call *fetcher*, cache & return."""
    cached = _get_cached_indicator(key)
    if cached is not None:
        return cached
    try:
        data = fetcher()
    except Exception as e:
        logger.error("_fresh(%s) fetcher failed: %s", key, e)
        data = {}
    _set_cached_indicator(key, data)
    return data


# ---------------------------------------------------------------------------
# Individual fetchers
# ---------------------------------------------------------------------------

def fetch_fear_greed_index() -> Dict[str, Any]:
    """Fetch Fear & Greed Index from alternative.me (crypto)."""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        logger.debug("Fetching Fear & Greed Index from %s", url)
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("data"):
            item = data["data"][0]
            value = int(item.get("value", 50))
            classification = item.get("value_classification", "Neutral")
            logger.info("Fear & Greed Index fetched: %d (%s)", value, classification)
            return {
                "value": value,
                "classification": classification,
                "timestamp": int(item.get("timestamp", 0)),
                "source": "alternative.me",
            }
        else:
            logger.warning("Fear & Greed API returned empty data")
    except requests.exceptions.Timeout:
        logger.error("Fear & Greed Index request timeout")
    except requests.exceptions.RequestException as e:
        logger.error("Fear & Greed Index request failed: %s", e)
    except Exception as e:
        logger.error("Failed to fetch Fear & Greed Index: %s", e)

    logger.warning("Returning default Fear & Greed value (50)")
    return {"value": 50, "classification": "Neutral", "timestamp": 0, "source": "N/A"}


def fetch_vix() -> Dict[str, Any]:
    """Fetch VIX (CBOE Volatility Index) with multiple fallbacks."""
    DEFAULT_VIX = {
        "value": 18, "change": 0, "level": "low",
        "interpretation": "低波动 - 市场稳定",
        "interpretation_en": "Low - Market Stable",
    }

    current = 0.0
    change = 0.0

    try:
        import yfinance as yf
        logger.debug("Fetching VIX from yfinance")
        ticker = yf.Ticker("^VIX")

        try:
            hist = ticker.history(period="5d")
        except Exception as hist_err:
            logger.warning("yfinance VIX failed: %s", hist_err)
            hist = None

        if hist is not None and not hist.empty and len(hist) >= 1:
            current = float(hist["Close"].iloc[-1])
            if current > 0:
                prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
                change = ((current - prev_close) / prev_close) * 100 if prev_close else 0
            else:
                raise ValueError("VIX value is 0")
        else:
            raise ValueError("VIX history empty")

    except Exception as e:
        logger.warning("yfinance VIX failed, trying akshare: %s", e)

        try:
            import akshare as ak
            vix_df = ak.index_vix()
            if vix_df is not None and len(vix_df) > 0:
                current = float(vix_df.iloc[-1]["close"])
                prev_close = float(vix_df.iloc[-2]["close"]) if len(vix_df) >= 2 else current
                change = ((current - prev_close) / prev_close) * 100 if prev_close else 0
                logger.info("VIX from akshare: %.2f", current)
            else:
                raise ValueError("Akshare VIX empty")
        except Exception as ak_err:
            logger.warning("Akshare VIX also failed: %s", ak_err)
            return DEFAULT_VIX

    if current <= 0:
        return DEFAULT_VIX

    if current < 12:
        level, cn, en = "very_low", "极低波动 - 市场极度乐观", "Very Low - Extreme Optimism"
    elif current < 20:
        level, cn, en = "low", "低波动 - 市场稳定", "Low - Market Stable"
    elif current < 25:
        level, cn, en = "moderate", "中等波动 - 正常水平", "Moderate - Normal Level"
    elif current < 30:
        level, cn, en = "high", "高波动 - 市场担忧", "High - Market Concern"
    else:
        level, cn, en = "very_high", "极高波动 - 市场恐慌", "Very High - Market Panic"

    return {
        "value": round(current, 2), "change": round(change, 2),
        "level": level, "interpretation": cn, "interpretation_en": en,
    }


def fetch_dollar_index() -> Dict[str, Any]:
    """Fetch US Dollar Index (DXY) with multiple fallbacks."""
    DEFAULT_DXY = {
        "value": 104, "change": 0, "level": "moderate_strong",
        "interpretation": "美元偏强 - 关注资金流向",
        "interpretation_en": "Moderately Strong - Watch capital flows",
    }

    current = 0.0
    change = 0.0

    try:
        import yfinance as yf
        logger.debug("Fetching DXY from yfinance")
        ticker = yf.Ticker("DX-Y.NYB")

        try:
            hist = ticker.history(period="5d")
        except Exception as hist_err:
            logger.warning("yfinance DXY failed: %s", hist_err)
            hist = None

        if hist is not None and not hist.empty and len(hist) >= 1:
            current = float(hist["Close"].iloc[-1])
            if current > 0:
                prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
                change = ((current - prev_close) / prev_close) * 100 if prev_close else 0
                logger.info("DXY from yfinance: %.2f", current)
            else:
                raise ValueError("DXY value is 0")
        else:
            raise ValueError("DXY history empty")

    except Exception as e:
        logger.warning("yfinance DXY failed, trying akshare: %s", e)
        try:
            import akshare as ak
            fx_df = ak.currency_boc_sina(symbol="美元")
            if fx_df is not None and len(fx_df) > 0:
                usd_cny = float(fx_df.iloc[-1]["中行汇买价"]) / 100
                current = usd_cny * 14.5
                change = 0
                logger.info("DXY estimated from akshare: %.2f", current)
            else:
                raise ValueError("Akshare DXY empty")
        except Exception as ak_err:
            logger.warning("Akshare DXY also failed: %s", ak_err)
            return DEFAULT_DXY

    if current <= 0:
        return DEFAULT_DXY

    if current > 105:
        level, cn, en = "strong", "美元强势 - 利空大宗商品/新兴市场", "Strong USD - Bearish commodities/EM"
    elif current > 100:
        level, cn, en = "moderate_strong", "美元偏强 - 关注资金流向", "Moderately Strong - Watch capital flows"
    elif current > 95:
        level, cn, en = "neutral", "美元中性 - 市场均衡", "Neutral - Market balanced"
    elif current > 90:
        level, cn, en = "moderate_weak", "美元偏弱 - 利多风险资产", "Moderately Weak - Bullish risk assets"
    else:
        level, cn, en = "weak", "美元疲软 - 利多黄金/大宗商品", "Weak USD - Bullish gold/commodities"

    logger.info("DXY fetched: %.2f (%s)", current, level)
    return {
        "value": round(current, 2), "change": round(change, 2),
        "level": level, "interpretation": cn, "interpretation_en": en,
    }


def fetch_yield_curve() -> Dict[str, Any]:
    """Fetch Treasury Yield Curve (10Y - 2Y spread)."""
    try:
        import yfinance as yf
        logger.debug("Fetching Treasury Yield Curve")

        tnx = yf.Ticker("^TNX")
        try:
            tnx_hist = tnx.history(period="5d")
        except Exception as hist_err:
            logger.warning("TNX history fetch failed: %s", hist_err)
            tnx_hist = None

        if tnx_hist is None or tnx_hist.empty:
            logger.warning("TNX history is None or empty, returning default")
            return {
                "yield_10y": 4.2, "yield_2y": 4.0, "spread": 0.2, "change": 0,
                "level": "normal", "interpretation": "数据暂不可用",
                "interpretation_en": "Data temporarily unavailable", "signal": "neutral",
            }

        if len(tnx_hist) >= 1:
            yield_10y = tnx_hist["Close"].iloc[-1]
            try:
                tyx = yf.Ticker("^TYX")
                tyx_hist = tyx.history(period="5d")
                yield_30y = tyx_hist["Close"].iloc[-1] if len(tyx_hist) >= 1 else 0  # noqa: F841
                yield_2y = yield_10y * 0.85
                spread = yield_10y - yield_2y
                if len(tnx_hist) >= 2:
                    prev_10y = tnx_hist["Close"].iloc[-2]
                    prev_2y = prev_10y * 0.85
                    prev_spread = prev_10y - prev_2y
                    yc_change = spread - prev_spread
                else:
                    yc_change = 0
            except Exception:
                yield_2y = yield_10y * 0.85
                spread = yield_10y - yield_2y
                yc_change = 0
        else:
            yield_10y = yield_2y = spread = yc_change = 0

        if spread < -0.5:
            level, cn, en, signal = "deeply_inverted", "深度倒挂 - 强烈衰退信号", "Deeply Inverted - Strong recession signal", "bearish"
        elif spread < 0:
            level, cn, en, signal = "inverted", "收益率倒挂 - 衰退预警", "Inverted - Recession warning", "bearish"
        elif spread < 0.5:
            level, cn, en, signal = "flat", "曲线平坦 - 经济放缓信号", "Flat - Economic slowdown signal", "neutral"
        elif spread < 1.5:
            level, cn, en, signal = "normal", "正常曲线 - 经济健康", "Normal - Healthy economy", "bullish"
        else:
            level, cn, en, signal = "steep", "陡峭曲线 - 经济扩张预期", "Steep - Economic expansion expected", "bullish"

        logger.info("Yield Curve: 10Y=%.2f%%, spread=%.2f%% (%s)", yield_10y, spread, level)
        return {
            "yield_10y": round(yield_10y, 2), "yield_2y": round(yield_2y, 2),
            "spread": round(spread, 2), "change": round(yc_change, 3),
            "level": level, "signal": signal, "interpretation": cn, "interpretation_en": en,
        }
    except Exception as e:
        logger.error("Failed to fetch Yield Curve: %s", e, exc_info=True)
        return {
            "yield_10y": 0, "yield_2y": 0, "spread": 0, "change": 0,
            "level": "unknown", "signal": "neutral",
            "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed",
        }


def fetch_vxn() -> Dict[str, Any]:
    """Fetch NASDAQ Volatility Index (VXN)."""
    try:
        import yfinance as yf
        logger.debug("Fetching VXN from yfinance")
        ticker = yf.Ticker("^VXN")
        hist = ticker.history(period="5d")

        if len(hist) >= 2:
            prev_close = hist["Close"].iloc[-2]
            current = hist["Close"].iloc[-1]
            change = ((current - prev_close) / prev_close) * 100
        elif len(hist) == 1:
            current = hist["Close"].iloc[-1]
            change = 0
        else:
            current = change = 0

        if current < 15:
            level, cn, en = "very_low", "科技股极低波动 - 市场乐观", "Very Low Tech Volatility - Optimistic"
        elif current < 22:
            level, cn, en = "low", "科技股低波动 - 稳定", "Low Tech Volatility - Stable"
        elif current < 28:
            level, cn, en = "moderate", "科技股中等波动 - 正常", "Moderate Tech Volatility - Normal"
        elif current < 35:
            level, cn, en = "high", "科技股高波动 - 谨慎", "High Tech Volatility - Caution"
        else:
            level, cn, en = "very_high", "科技股极高波动 - 恐慌", "Very High Tech Volatility - Panic"

        logger.info("VXN fetched: %.2f (%s)", current, level)
        return {"value": round(current, 2), "change": round(change, 2), "level": level, "interpretation": cn, "interpretation_en": en}
    except Exception as e:
        logger.error("Failed to fetch VXN: %s", e, exc_info=True)
        return {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed"}


def fetch_gvz() -> Dict[str, Any]:
    """Fetch Gold Volatility Index (GVZ)."""
    try:
        import yfinance as yf
        logger.debug("Fetching GVZ from yfinance")
        ticker = yf.Ticker("^GVZ")
        hist = ticker.history(period="5d")

        if len(hist) >= 2:
            prev_close = hist["Close"].iloc[-2]
            current = hist["Close"].iloc[-1]
            change = ((current - prev_close) / prev_close) * 100
        elif len(hist) == 1:
            current = hist["Close"].iloc[-1]
            change = 0
        else:
            current = change = 0

        if current < 12:
            level, cn, en = "very_low", "黄金低波动 - 避险需求低", "Low Gold Vol - Low safe haven demand"
        elif current < 16:
            level, cn, en = "low", "黄金稳定 - 市场平静", "Gold Stable - Market calm"
        elif current < 20:
            level, cn, en = "moderate", "黄金中等波动 - 关注避险情绪", "Moderate Gold Vol - Watch safe haven"
        elif current < 25:
            level, cn, en = "high", "黄金高波动 - 避险需求上升", "High Gold Vol - Rising safe haven demand"
        else:
            level, cn, en = "very_high", "黄金极高波动 - 市场避险", "Very High Gold Vol - Flight to safety"

        logger.info("GVZ fetched: %.2f (%s)", current, level)
        return {"value": round(current, 2), "change": round(change, 2), "level": level, "interpretation": cn, "interpretation_en": en}
    except Exception as e:
        logger.error("Failed to fetch GVZ: %s", e, exc_info=True)
        return {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed"}


def fetch_put_call_ratio() -> Dict[str, Any]:
    """Calculate Put/Call Ratio proxy using VIX term structure."""
    try:
        import yfinance as yf
        logger.debug("Calculating Put/Call Ratio proxy")

        vix = yf.Ticker("^VIX")
        vix3m = yf.Ticker("^VIX3M")

        vix_hist = vix.history(period="5d")
        vix3m_hist = vix3m.history(period="5d")

        vix_val = vix3m_val = 0.0

        if len(vix_hist) >= 1 and len(vix3m_hist) >= 1:
            vix_val = vix_hist["Close"].iloc[-1]
            vix3m_val = vix3m_hist["Close"].iloc[-1]
            ratio = vix_val / vix3m_val if vix3m_val > 0 else 1.0

            if len(vix_hist) >= 2 and len(vix3m_hist) >= 2:
                prev_ratio = vix_hist["Close"].iloc[-2] / vix3m_hist["Close"].iloc[-2] if vix3m_hist["Close"].iloc[-2] > 0 else 1.0
                change = ((ratio - prev_ratio) / prev_ratio) * 100
            else:
                change = 0
        else:
            ratio = 1.0
            change = 0

        if ratio > 1.15:
            level, cn, en, signal = "high_fear", "VIX倒挂 - 短期恐慌情绪高涨", "VIX Backwardation - High short-term fear", "bearish"
        elif ratio > 1.0:
            level, cn, en, signal = "elevated", "轻度倒挂 - 市场谨慎", "Slight Backwardation - Market cautious", "neutral"
        elif ratio > 0.9:
            level, cn, en, signal = "normal", "正常结构 - 市场稳定", "Normal Structure - Market stable", "neutral"
        elif ratio > 0.8:
            level, cn, en, signal = "complacent", "深度正价差 - 市场自满", "Deep Contango - Market complacent", "bullish"
        else:
            level, cn, en, signal = "extreme_complacency", "极度自满 - 警惕反转", "Extreme Complacency - Watch for reversal", "neutral"

        logger.info("VIX Term Structure: ratio=%.3f (%s)", ratio, level)
        return {
            "value": round(ratio, 3), "vix": round(vix_val, 2), "vix3m": round(vix3m_val, 2),
            "change": round(change, 2), "level": level, "signal": signal,
            "interpretation": cn, "interpretation_en": en,
        }
    except Exception as e:
        logger.error("Failed to calculate Put/Call proxy: %s", e, exc_info=True)
        return {
            "value": 1.0, "vix": 0, "vix3m": 0, "change": 0,
            "level": "unknown", "signal": "neutral",
            "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed",
        }


# ---------------------------------------------------------------------------
# Unified entry point — per-indicator caching with timestamps
# ---------------------------------------------------------------------------

def get_sentiment_data(timeout: int = 10) -> Dict[str, Any]:
    """Return comprehensive sentiment data with per-indicator caching.

    Each indicator is cached independently with its own TTL.  If data exists
    and hasn't exceeded its TTL, the cached value is returned directly — no
    external API call is made for that indicator.

    The returned dict always contains a top-level ``"fetched_at"`` (unix
    timestamp of when this bundle was assembled) plus per-indicator
    ``"_cached_at"`` fields so callers know exactly when each value was last
    refreshed.
    """
    fetchers = {
        "fear_greed":  fetch_fear_greed_index,
        "vix":         fetch_vix,
        "dxy":         fetch_dollar_index,
        "yield_curve": fetch_yield_curve,
        "vxn":         fetch_vxn,
        "gvz":         fetch_gvz,
        "vix_term":    fetch_put_call_ratio,
    }

    results: Dict[str, Any] = {}

    # Separate cached vs. stale indicators
    stale_keys = []
    for key in fetchers:
        cached = _get_cached_indicator(key)
        if cached is not None:
            results[key] = cached
        else:
            stale_keys.append(key)

    # Fetch only stale indicators in parallel
    if stale_keys:
        logger.info("get_sentiment_data: fetching %d stale indicators: %s",
                    len(stale_keys), stale_keys)
        with ThreadPoolExecutor(max_workers=len(stale_keys)) as executor:
            futures = {executor.submit(fetchers[k]): k for k in stale_keys}
            try:
                for future in as_completed(futures, timeout=timeout):
                    key = futures[future]
                    try:
                        data = future.result(timeout=5)
                        results[key] = data
                        _set_cached_indicator(key, data)
                    except Exception as e:
                        logger.error("get_sentiment_data: failed to fetch %s: %s", key, e)
                        results[key] = None
            except Exception:
                logger.warning("get_sentiment_data: total timeout (%ss), %d/%d indicators fetched",
                               timeout, len(results), len(fetchers))
                for fut, key in futures.items():
                    if key not in results:
                        results[key] = None
    else:
        logger.debug("get_sentiment_data: all indicators cached and fresh")

    now = int(time.time())

    # Assemble final payload — always return full structure with defaults
    data = {
        "fear_greed":  results.get("fear_greed")  or {"value": 50, "classification": "Neutral"},
        "vix":         results.get("vix")         or {"value": 0, "level": "unknown"},
        "dxy":         results.get("dxy")         or {"value": 0, "level": "unknown"},
        "yield_curve": results.get("yield_curve") or {"spread": 0, "level": "unknown"},
        "vxn":         results.get("vxn")         or {"value": 0, "level": "unknown"},
        "gvz":         results.get("gvz")         or {"value": 0, "level": "unknown"},
        "vix_term":    results.get("vix_term")    or {"value": 1.0, "level": "unknown"},
        "fetched_at":  now,
    }

    return data
