"""
国际市场宏观数据 — 统一数据入口
所有国际市场数据通过此模块获取，缓存机制内聚，对外不暴露。

用法:
    from app.data_providers.global_market import get_sentiment, get_indices
    data = get_sentiment()     # 自动判断缓存/远端
    idx  = get_indices()       # 同上
"""
import json as _json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


# ============================================================
#  缓存 TTL / 过期阈值
# ============================================================

_CACHE_TTL = {
    "sentiment":          300,
    "commodities":        300,
    "indices":            120,
    "heatmap":            300,
    "news":               180,
    "economic_calendar":  3600,
    "opportunities":      3600,
}

_STALE_AFTER = {
    "sentiment":          300,
    "commodities":        300,
    "indices":            60,
    "heatmap":            150,
    "news":               90,
    "economic_calendar":  1800,
    "opportunities":      1800,
}


# ============================================================
#  缓存内部实现
# ============================================================

_refresh_locks = {}
_refresh_locks_guard = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()


def _get_refresh_lock(key):
    with _refresh_locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


def _cache_key(endpoint):
    return f"dp:intl_{endpoint}"


def _read_cache(endpoint):
    try:
        from app.utils.cache import CacheManager
        cm = CacheManager()
        key = _cache_key(endpoint)
        if cm.is_redis:
            raw = cm._client.get(key)
            if raw:
                entry = _json.loads(raw)
                return entry.get("data"), entry.get("ts", 0)
        else:
            with cm._client._lock:
                entry = cm._client._cache.get(key)
                if entry:
                    parsed = _json.loads(entry[0])
                    return parsed.get("data"), parsed.get("ts", 0)
    except Exception:
        pass
    return None, 0


def _write_cache(endpoint, data):
    ttl = _CACHE_TTL.get(endpoint, 300)
    payload = _json.dumps({"data": data, "ts": time.time()}, ensure_ascii=False, default=str)
    try:
        from app.utils.cache import CacheManager
        cm = CacheManager()
        key = _cache_key(endpoint)
        cm.set(key, payload, ttl=ttl)
    except Exception as e:
        logger.warning("写缓存失败 [%s]: %s", endpoint, e)


def _maybe_refresh_bg(endpoint, fetch_fn, cached_ts):
    if not cached_ts:
        return
    elapsed = time.time() - cached_ts
    wait = _STALE_AFTER.get(endpoint, 300)
    if elapsed < wait:
        return
    lock = _get_refresh_lock(endpoint)

    def _bg():
        if not lock.acquire(blocking=False):
            return
        try:
            logger.info("[global_market] 后台刷新 %s (过期 %ds > %ds)", endpoint, elapsed, wait)
            result = fetch_fn()
            if result is not None:
                _write_cache(endpoint, result)
        except Exception as e:
            logger.error("[global_market] 后台刷新 %s 失败: %s", endpoint, e)
        finally:
            lock.release()

    threading.Thread(target=_bg, daemon=True).start()


def _coalesce(key, fn, timeout=30):
    event = None
    should_run = False
    with _inflight_lock:
        if key in _inflight:
            event = _inflight[key][0]
        else:
            event = threading.Event()
            _inflight[key] = (event, None)
            should_run = True

    if should_run:
        result = None
        try:
            result = fn()
        except Exception as e:
            logger.error(f"coalesce [{key}] 执行异常: {e}")
        finally:
            with _inflight_lock:
                _inflight[key] = (event, result)
                event.set()

            def _cleanup():
                time.sleep(3)
                with _inflight_lock:
                    _inflight.pop(key, None)
            threading.Thread(target=_cleanup, daemon=True).start()
        return result
    else:
        event.wait(timeout=timeout)
        with _inflight_lock:
            entry = _inflight.get(key)
            return entry[1] if entry else None


def _cached_fetch(endpoint, fetch_fn, timeout=30):
    """统一缓存读取：有缓存直接返回（过期则后台刷新），无缓存走远端。"""
    data, cached_ts = _read_cache(endpoint)
    if data is not None:
        _maybe_refresh_bg(endpoint, fetch_fn, cached_ts)
        return data
    result = _coalesce(endpoint, fetch_fn, timeout=timeout)
    if result is not None:
        _write_cache(endpoint, result)
    return result


def _safe_refresh(endpoint, fn):
    lock = _get_refresh_lock(endpoint)
    if not lock.acquire(blocking=False):
        return False
    try:
        data = _coalesce(endpoint, fn, timeout=60)
        if data is not None:
            _write_cache(endpoint, data)
            return True
        return False
    finally:
        lock.release()


# ############################################################
#  以下为对外数据函数
# ############################################################

# ============================================================
#  情绪指标 + 大宗商品
# ============================================================

def get_sentiment() -> dict:
    """7 个宏观情绪指标 + 大宗商品，自动缓存。"""
    def _fetch():
        from .sentiment import (
            fetch_fear_greed_index, fetch_vix, fetch_dollar_index,
            fetch_yield_curve, fetch_vxn, fetch_gvz, fetch_put_call_ratio,
            _set_cached_indicator,
        )
        from .commodities import fetch_commodities

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

        # 同时写 sentiment 专用缓存（保持兼容旧的 _CK 格式）
        for key, data in results.items():
            if data is not None:
                _set_cached_indicator(key, data)

        # 大宗商品
        commodities = fetch_commodities()

        return _pack_sentiment(results, commodities=commodities)

    data = _cached_fetch("sentiment", _fetch, timeout=30)
    if data is None:
        data = _pack_sentiment({})
    return {"code": 1, "msg": "success", "data": data}


def _pack_sentiment(results, commodities=None):
    return {
        "fear_greed":  results.get("fear_greed")  or {"value": 50, "classification": "Neutral"},
        "vix":         results.get("vix")         or {"value": 0, "level": "unknown"},
        "dxy":         results.get("dxy")         or {"value": 0, "level": "unknown"},
        "yield_curve": results.get("yield_curve") or {"spread": 0, "level": "unknown"},
        "vxn":         results.get("vxn")         or {"value": 0, "level": "unknown"},
        "gvz":         results.get("gvz")         or {"value": 0, "level": "unknown"},
        "vix_term":    results.get("vix_term")    or {"value": 1.0, "level": "unknown"},
        "commodities": commodities or [],
        "fetched_at":  int(time.time()),
    }


# ============================================================
#  全球股指
# ============================================================

def get_indices() -> dict:
    """全球股指 + 外汇 + 加密货币"""
    def _fetch():
        from .indices import fetch_stock_indices
        from .forex import fetch_forex_pairs
        from .crypto import fetch_crypto_prices

        results = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(fetch_stock_indices): "indices",
                pool.submit(fetch_forex_pairs): "forex",
                pool.submit(fetch_crypto_prices): "crypto",
            }
            try:
                for f in as_completed(futures, timeout=20):
                    key = futures[f]
                    try:
                        results[key] = f.result(timeout=5)
                    except Exception as e:
                        logger.error("fetch %s failed: %s", key, e)
                        results[key] = []
            except Exception:
                logger.warning("indices fetch total timeout")
        return {
            "indices": results.get("indices", []),
            "forex": results.get("forex", []),
            "crypto": results.get("crypto", []),
        }

    data = _cached_fetch("indices", _fetch, timeout=20)
    if data is None:
        data = {"indices": [], "forex": [], "crypto": []}
    return {"code": 1, "msg": "success", "data": data}


# ============================================================
#  热力图
# ============================================================

def get_heatmap() -> dict:
    def _fetch():
        from .heatmap import generate_heatmap_data
        return generate_heatmap_data()

    data = _cached_fetch("heatmap", _fetch, timeout=20)
    return {"code": 1, "msg": "success", "data": data or {}}


# ============================================================
#  财经新闻
# ============================================================

def get_news(lang="all") -> dict:
    def _fetch():
        from .news import fetch_financial_news
        return fetch_financial_news(lang)

    data = _cached_fetch("news", _fetch, timeout=20)
    return {"code": 1, "msg": "success", "data": data or {}}


# ============================================================
#  经济日历
# ============================================================

def get_calendar() -> dict:
    def _fetch():
        from .news import get_economic_calendar
        return get_economic_calendar()

    data = _cached_fetch("economic_calendar", _fetch, timeout=20)
    return {"code": 1, "msg": "success", "data": data or []}


# ============================================================
#  交易机会
# ============================================================

def get_opportunities() -> dict:
    def _fetch():
        from .opportunities import (
            analyze_opportunities_crypto,
            analyze_opportunities_stocks,
            analyze_opportunities_local_stocks,
            analyze_opportunities_forex,
        )
        opportunities = []
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
                logger.error("opportunities %s failed: %s", label, e)
        opportunities.sort(key=lambda x: abs(x.get("change_24h", 0)), reverse=True)
        return opportunities

    data = _cached_fetch("opportunities", _fetch, timeout=30)
    return {"code": 1, "msg": "success", "data": data or []}


# ============================================================
#  手动刷新
# ============================================================

def refresh(target="all") -> dict:
    """手动刷新国际市场缓存，返回各 endpoint 状态"""
    FETCH_MAP = {
        "sentiment":         get_sentiment,
        "indices":           get_indices,
        "heatmap":           get_heatmap,
        "news":              lambda: get_news(),
        "economic_calendar": get_calendar,
        "opportunities":     get_opportunities,
    }

    results = {}
    targets = list(FETCH_MAP.keys()) if target == "all" else [target]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for t in targets:
            if t in FETCH_MAP:
                futures[pool.submit(_safe_refresh, t, FETCH_MAP[t])] = t
        for fut in as_completed(futures, timeout=120):
            key = futures[fut]
            try:
                results[key] = "ok" if fut.result() else "failed"
            except Exception as e:
                results[key] = f"error: {e}"

    return results
