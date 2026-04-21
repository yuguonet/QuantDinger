"""
Global Market Dashboard APIs.

Provides aggregated global market data including:
- Major indices (US, Europe, Japan, Korea, Australia, India)
- Forex pairs
- Crypto prices
- Market heatmap data (crypto, stocks, forex)
- Economic calendar with impact indicators
- Fear & Greed Index / VIX
- Financial news (Chinese & English)

Endpoints:
- GET  /api/global-market/overview       - Read overview from cache
- GET  /api/global-market/sentiment      - Read sentiment from cache
- POST /api/global-market/refresh        - Fetch remote → write cache
"""

from __future__ import annotations

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request

from app.utils.logger import get_logger

from app.data_providers import get_cached, set_cached, CACHE_TTL
from app.data_providers.crypto import fetch_crypto_prices
from app.data_providers.forex import fetch_forex_pairs
from app.data_providers.commodities import fetch_commodities
from app.data_providers.indices import fetch_stock_indices
from app.data_providers.sentiment import (
    fetch_fear_greed_index, fetch_vix, fetch_dollar_index,
    fetch_yield_curve, fetch_vxn, fetch_gvz, fetch_put_call_ratio,
)
from app.data_providers.news import fetch_financial_news, get_economic_calendar
from app.data_providers.heatmap import generate_heatmap_data
from app.data_providers.opportunities import (
    analyze_opportunities_crypto, analyze_opportunities_stocks,
    analyze_opportunities_local_stocks, analyze_opportunities_forex,
)

logger = get_logger(__name__)

global_market_bp = Blueprint("global_market", __name__)

# 缓存过期等待时间（秒）：超过这个时间没更新，后台自动去拉远端
STALE_AFTER = {
    "overview":  300,   # 5 分钟
    "sentiment": 300,   # 5 分钟
}

# 防止并发刷新的锁
_refresh_locks = {
    "overview":  threading.Lock(),
    "sentiment": threading.Lock(),
}


# =====================================================================
# 读 — 返回缓存，同时检查是否过期需要刷新
# =====================================================================

@global_market_bp.route("/overview", methods=["GET"])
def market_overview():
    """Read overview from cache.  Auto-refreshes in background if stale."""
    cached = _read_stale("market_overview")
    if cached:
        # 定时器比对：现在时间 - 日期戳 > 等待时间 → 后台取远端
        _maybe_refresh("overview", cached.get("timestamp", 0))
        return jsonify({"code": 1, "msg": "success", "data": cached})
    # 缓存完全空 → 首次启动，同步拉一次
    data = _fetch_overview()
    return jsonify({"code": 1, "msg": "success", "data": data})


@global_market_bp.route("/sentiment", methods=["GET"])
def market_sentiment():
    """Read sentiment from cache.  Auto-refreshes in background if stale."""
    cached = _read_stale_sentiment()
    if cached:
        _maybe_refresh("sentiment", cached.get("fetched_at", 0))
        return jsonify({"code": 1, "msg": "success", "data": cached})
    # 缓存完全空 → 首次启动，同步拉一次
    data = _fetch_sentiment()
    return jsonify({"code": 1, "msg": "success", "data": data})


# =====================================================================
# 写 — 点刷新按钮触发：取远端 → 成功后写缓存
# =====================================================================

@global_market_bp.route("/refresh", methods=["POST"])
def refresh_data():
    """Fetch fresh data from remote, then write to cache on success."""
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")  # "overview" | "sentiment" | "all"

    results = {}

    if target in ("overview", "all"):
        try:
            results["overview"] = _fetch_overview()
        except Exception as e:
            logger.error("refresh overview failed: %s", e, exc_info=True)
            results["overview_error"] = str(e)

    if target in ("sentiment", "all"):
        try:
            results["sentiment"] = _fetch_sentiment()
        except Exception as e:
            logger.error("refresh sentiment failed: %s", e, exc_info=True)
            results["sentiment_error"] = str(e)

    ok = "overview_error" not in results and "sentiment_error" not in results
    return jsonify({"code": 1 if ok else 0, "msg": "refreshed" if ok else "partial", "data": results})


# =====================================================================
# 内部：从远端拉取 → 写缓存
# =====================================================================

def _fetch_overview():
    """Fetch overview from remote APIs, write to cache, return data."""
    result = {
        "indices": [], "forex": [], "crypto": [], "commodities": [],
        "timestamp": int(time.time()),
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_stock_indices): "indices",
            pool.submit(fetch_forex_pairs): "forex",
            pool.submit(fetch_crypto_prices): "crypto",
            pool.submit(fetch_commodities): "commodities",
        }
        for f in as_completed(futures):
            key = futures[f]
            try:
                data = f.result()
                result[key] = data if data else []
                logger.info("fetched %s: %d items", key, len(result[key]))
            except Exception as e:
                logger.error("fetch %s failed: %s", key, e, exc_info=True)
                result[key] = []

    # 成功后写缓存
    set_cached("market_overview", result, CACHE_TTL["market_overview"])
    set_cached("stock_indices", result["indices"], CACHE_TTL["stock_indices"])
    set_cached("forex_pairs", result["forex"], CACHE_TTL["forex_pairs"])
    set_cached("crypto_prices", result["crypto"], CACHE_TTL["crypto_heatmap"])
    return result


def _fetch_sentiment():
    """Fetch sentiment from remote APIs, write per-indicator cache, return data."""
    fetchers = {
        "fear_greed":  fetch_fear_greed_index,
        "vix":         fetch_vix,
        "dxy":         fetch_dollar_index,
        "yield_curve": fetch_yield_curve,
        "vxn":         fetch_vxn,
        "gvz":         fetch_gvz,
        "vix_term":    fetch_put_call_ratio,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {pool.submit(fn): k for k, fn in fetchers.items()}
        try:
            for f in as_completed(futures, timeout=15):
                key = futures[f]
                try:
                    results[key] = f.result(timeout=5)
                except Exception as e:
                    logger.error("fetch sentiment %s failed: %s", key, e)
                    results[key] = None
        except Exception:
            logger.warning("sentiment fetch total timeout")

    # 成功的指标写缓存
    from app.data_providers.sentiment import _set_cached_indicator
    for key, data in results.items():
        if data is not None:
            _set_cached_indicator(key, data)

    return _pack_sentiment(results)


# =====================================================================
# 内部：读缓存（含过期数据）
# =====================================================================

def _read_stale(cache_key):
    """Read cache entry ignoring TTL — returns data even if expired."""
    cm_obj = _cm()
    dp_key = f"dp:{cache_key}"
    try:
        if cm_obj.is_redis:
            raw = cm_obj._client.get(dp_key)
            if raw:
                return json.loads(raw)
        else:
            with cm_obj._client._lock:
                entry = cm_obj._client._cache.get(dp_key)
                if entry:
                    return json.loads(entry[0])
    except Exception:
        pass
    return None


def _read_stale_sentiment():
    """Read all sentiment indicators from cache (including expired).

    Returns data if at least one indicator is cached. Preserves the original
    fetched_at timestamp so _maybe_refresh can detect staleness correctly.
    """
    from app.data_providers.sentiment import _CK
    cm_obj = _cm()
    keys = ["fear_greed", "vix", "dxy", "yield_curve", "vxn", "gvz", "vix_term"]
    results = {}
    has_any = False
    oldest_ts = int(time.time())

    for key in keys:
        raw = None
        try:
            if cm_obj.is_redis:
                v = cm_obj._client.get(f"{_CK}{key}")
                if v:
                    raw = json.loads(v)
            else:
                with cm_obj._client._lock:
                    entry = cm_obj._client._cache.get(f"{_CK}{key}")
                    if entry:
                        raw = json.loads(entry[0])
        except Exception:
            pass
        if raw and isinstance(raw, dict) and raw.get("data") is not None:
            results[key] = raw["data"]
            has_any = True
            oldest_ts = min(oldest_ts, raw.get("ts", oldest_ts))

    if not has_any:
        return None
    return _pack_sentiment(results, fetched_at=oldest_ts)


def _pack_sentiment(results, fetched_at=None):
    """Assemble standard sentiment response.

    fetched_at: original cache timestamp; defaults to now only for fresh data.
    """
    return {
        "fear_greed":  results.get("fear_greed")  or {"value": 50, "classification": "Neutral"},
        "vix":         results.get("vix")         or {"value": 0, "level": "unknown"},
        "dxy":         results.get("dxy")         or {"value": 0, "level": "unknown"},
        "yield_curve": results.get("yield_curve") or {"spread": 0, "level": "unknown"},
        "vxn":         results.get("vxn")         or {"value": 0, "level": "unknown"},
        "gvz":         results.get("gvz")         or {"value": 0, "level": "unknown"},
        "vix_term":    results.get("vix_term")    or {"value": 1.0, "level": "unknown"},
        "fetched_at":  fetched_at or int(time.time()),
    }


def _cm():
    """Lazy CacheManager singleton."""
    from app.utils.cache import CacheManager
    return CacheManager()


def _maybe_refresh(target, cached_ts):
    """定时器比对：现在时间 - 日期戳 > 等待时间 → 后台线程取远端写缓存"""
    if not cached_ts:
        return
    elapsed = time.time() - cached_ts
    wait = STALE_AFTER.get(target, 300)
    if elapsed < wait:
        return
    # 后台刷新，lock 防止同一 target 并发拉取
    lock = _refresh_locks.get(target)

    def _bg():
        if not lock or not lock.acquire(blocking=False):
            return
        try:
            logger.info("auto-refresh %s (stale %ds > %ds)", target, elapsed, wait)
            if target == "overview":
                _fetch_overview()
            elif target == "sentiment":
                _fetch_sentiment()
        except Exception as e:
            logger.error("auto-refresh %s failed: %s", target, e)
        finally:
            lock.release()

    threading.Thread(target=_bg, daemon=True).start()


# =====================================================================
# 其他端点（保持原样）
# =====================================================================

@global_market_bp.route("/heatmap", methods=["GET"])
def market_heatmap():
    cached = get_cached("market_heatmap", CACHE_TTL["market_heatmap"])
    if cached:
        return jsonify({"code": 1, "msg": "success", "data": cached})
    data = generate_heatmap_data()
    set_cached("market_heatmap", data, CACHE_TTL["market_heatmap"])
    return jsonify({"code": 1, "msg": "success", "data": data})


@global_market_bp.route("/news", methods=["GET"])
def market_news():
    lang = request.args.get("lang", "all")
    cache_key = f"market_news_{lang}"
    cached = get_cached(cache_key, CACHE_TTL["market_news"])
    if cached:
        return jsonify({"code": 1, "msg": "success", "data": cached})
    news = fetch_financial_news(lang)
    set_cached(cache_key, news, CACHE_TTL["market_news"])
    return jsonify({"code": 1, "msg": "success", "data": news})


@global_market_bp.route("/calendar", methods=["GET"])
def economic_calendar():
    cached = get_cached("economic_calendar", 3600)
    if cached:
        return jsonify({"code": 1, "msg": "success", "data": cached})
    events = get_economic_calendar()
    set_cached("economic_calendar", events, 3600)
    return jsonify({"code": 1, "msg": "success", "data": events})


@global_market_bp.route("/opportunities", methods=["GET"])
def trading_opportunities():
    cached = get_cached("trading_opportunities")
    if cached:
        return jsonify({"code": 1, "msg": "success", "data": cached})
    opportunities: list = []
    scanners = [
        ("Crypto", lambda: analyze_opportunities_crypto(opportunities)),
        ("USStock", lambda: analyze_opportunities_stocks(opportunities)),
        ("CNStock", lambda: analyze_opportunities_local_stocks(opportunities, "CNStock")),
        ("HKStock", lambda: analyze_opportunities_local_stocks(opportunities, "HKStock")),
        ("Forex", lambda: analyze_opportunities_forex(opportunities)),
    ]
    for label, scanner in scanners:
        try:
            scanner()
        except Exception as e:
            logger.error("opportunities %s failed: %s", label, e, exc_info=True)
    opportunities.sort(key=lambda x: abs(x.get("change_24h", 0)), reverse=True)
    set_cached("trading_opportunities", opportunities, 3600)
    return jsonify({"code": 1, "msg": "success", "data": opportunities})
