"""
Global Market Dashboard APIs.

Provides aggregated global market data including:
- Market sentiment (Fear & Greed, VIX, DXY, yield curve, VXN, GVZ, VIX term structure)
- Commodities (gold, silver, oil, natural gas)
- Stock indices — 独立接口，按需调用 (S&P 500, Dow, NASDAQ, DAX, FTSE, CAC40, Nikkei, KOSPI, ASX, SENSEX)
- Forex pairs (bundled with indices endpoint)
- Crypto prices (bundled with indices endpoint)
- Market heatmap, financial news, economic calendar

Read-write separation:
- GET  endpoints read from cache
- POST /refresh fetches remote → writes cache

Endpoints:
- GET  /api/global-market/sentiment      - Sentiment indicators + commodities (from cache)
- GET  /api/global-market/indices         - Indices + forex + crypto (from cache, independent)
- POST /api/global-market/refresh         - Fetch remote → write cache (target: sentiment|indices|all)
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
    "sentiment": 300,   # 5 分钟
}

# 防止并发刷新的锁
_refresh_locks = {
    "sentiment": threading.Lock(),
    "indices":   threading.Lock(),
}


# =====================================================================
# 读 — 返回缓存，同时检查是否过期需要刷新
# =====================================================================

@global_market_bp.route("/sentiment", methods=["GET"])
def market_sentiment():
    """读 sentiment + commodities 缓存，路由层合并返回。"""
    cached = _read_stale_sentiment()
    if cached:
        _maybe_refresh("sentiment", cached.get("fetched_at", 0))
        return jsonify({"code": 1, "msg": "success", "data": cached})
    # 缓存完全空 → 首次启动，同步拉一次（两个独立 fetch，各写各缓存）
    _fetch_sentiment()
    _fetch_commodities()
    return jsonify({"code": 1, "msg": "success", "data": _read_stale_sentiment()})


@global_market_bp.route("/indices", methods=["GET"])
def market_indices():
    """独立接口：读全球股指缓存。前端按需调用，不在 overview 里附带。"""
    cached = get_cached("stock_indices", CACHE_TTL["stock_indices"])
    if cached:
        return jsonify({"code": 1, "msg": "success", "data": cached})
    # 缓存空 → 同步拉一次
    data = _fetch_indices()
    return jsonify({"code": 1, "msg": "success", "data": data})


# =====================================================================
# 写 — 点刷新按钮触发：取远端 → 成功后写缓存
# =====================================================================

@global_market_bp.route("/refresh", methods=["POST"])
def refresh_data():
    """Fetch fresh data from remote, then write to cache on success."""
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")  # "sentiment" | "indices" | "all"

    results = {}

    if target in ("sentiment", "all"):
        # sentiment + commodities 独立拉取，各写各缓存，并行执行
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sent = pool.submit(_fetch_sentiment)
            f_comm = pool.submit(_fetch_commodities)
            try:
                results["sentiment"] = f_sent.result(timeout=20)
            except Exception as e:
                logger.error("refresh sentiment failed: %s", e, exc_info=True)
                results["sentiment_error"] = str(e)
            try:
                results["commodities"] = f_comm.result(timeout=20)
            except Exception as e:
                logger.error("refresh commodities failed: %s", e, exc_info=True)
                results["commodities_error"] = str(e)

    if target in ("indices", "all"):
        try:
            results["indices"] = _fetch_indices()
        except Exception as e:
            logger.error("refresh indices failed: %s", e, exc_info=True)
            results["indices_error"] = str(e)

    ok = "sentiment_error" not in results and "indices_error" not in results
    return jsonify({"code": 1 if ok else 0, "msg": "refreshed" if ok else "partial", "data": results})


# =====================================================================
# 内部：从远端拉取 → 写缓存
# =====================================================================

def _fetch_sentiment():
    """只拉 7 个宏观情绪指标，写 sentiment 专用缓存。"""
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

    # 写 sentiment 专用缓存
    from app.data_providers.sentiment import _set_cached_indicator
    for key, data in results.items():
        if data is not None:
            _set_cached_indicator(key, data)

    return results


def _fetch_commodities():
    """拉大宗商品，写通用缓存。"""
    data = fetch_commodities()
    if data:
        set_cached("commodities_data", data, CACHE_TTL.get("commodities", 600))
    return data or []


# =====================================================================
# 内部：读缓存（含过期数据）
# =====================================================================

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

    # 同时读大宗商品缓存
    commodities_data = None
    try:
        if cm_obj.is_redis:
            v = cm_obj._client.get("dp:commodities_data")
            if v:
                commodities_data = json.loads(v)
        else:
            with cm_obj._client._lock:
                entry = cm_obj._client._cache.get("dp:commodities_data")
                if entry:
                    commodities_data = json.loads(entry[0])
    except Exception:
        pass

    return _pack_sentiment(results, fetched_at=oldest_ts, commodities=commodities_data)


def _pack_sentiment(results, fetched_at=None, commodities=None):
    """Assemble standard sentiment response.

    fetched_at: original cache timestamp; defaults to now only for fresh data.
    commodities: 大宗商品数据，合并进 sentiment 返回。
    """
    return {
        "fear_greed":  results.get("fear_greed")  or {"value": 50, "classification": "Neutral"},
        "vix":         results.get("vix")         or {"value": 0, "level": "unknown"},
        "dxy":         results.get("dxy")         or {"value": 0, "level": "unknown"},
        "yield_curve": results.get("yield_curve") or {"spread": 0, "level": "unknown"},
        "vxn":         results.get("vxn")         or {"value": 0, "level": "unknown"},
        "gvz":         results.get("gvz")         or {"value": 0, "level": "unknown"},
        "vix_term":    results.get("vix_term")    or {"value": 1.0, "level": "unknown"},
        "commodities": commodities or [],
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
            if target == "sentiment":
                # 两个独立 fetch 并行
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pool.submit(_fetch_sentiment)
                    pool.submit(_fetch_commodities)
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
