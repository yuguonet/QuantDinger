"""
国际市场宏观数据 — 统一数据入口
所有国际市场数据通过此模块获取，缓存机制内聚，对外不暴露。

用法:
    from app.data_providers.global_market import get_sentiment, get_indices
    data = get_sentiment()     # 自动判断缓存/远端
    idx  = get_indices()       # 同上
"""
import json as _json
import os as _os
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


# ============================================================
#  缓存 TTL / 过期阈值
#  STALE_AFTER = TTL 的 60%，过期后先返回旧数据、后台刷新
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
    "sentiment":          180,
    "commodities":        180,
    "indices":            72,
    "heatmap":            180,
    "news":               108,
    "economic_calendar":  2160,
    "opportunities":      2160,
}


# ============================================================
#  缓存内部实现
# ============================================================

_refresh_locks = {}
_refresh_locks_guard = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()

# 模块级单例
_cm_instance = None


def _get_refresh_lock(key):
    with _refresh_locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


def _cache_key(endpoint):
    return f"dp:intl_{endpoint}"


def _get_cm():
    """模块级 CacheManager 单例，避免每次调用都 import + 实例化。"""
    global _cm_instance
    if _cm_instance is None:
        from app.utils.cache import CacheManager
        _cm_instance = CacheManager()
    return _cm_instance


def _read_cache(endpoint):
    try:
        cm = _get_cm()
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
        cm = _get_cm()
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
            logger.info("[global_market] 后台刷新 %s (过期 %.0fs > %ds)", endpoint, elapsed, wait)
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
#  原始获取函数（无缓存）— 手动刷新调用这些
# ############################################################

def _sentiment_fetch() -> dict:
    """7 个宏观情绪指标 + 大宗商品，原始获取。"""
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
            for f in as_completed(futures, timeout=20):
                key = futures[f]
                try:
                    results[key] = f.result(timeout=5)
                except Exception as e:
                    logger.error("fetch sentiment %s failed: %s", key, e)
                    results[key] = None
        except Exception:
            logger.warning("sentiment fetch total timeout")

    for key, data in results.items():
        if data is not None:
            _set_cached_indicator(key, data)

    commodities = fetch_commodities()
    return _pack_sentiment(results, commodities=commodities)


def _indices_fetch() -> dict:
    """全球股指 + 外汇 + 加密货币，原始获取。"""
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
            for f in as_completed(futures, timeout=25):
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


def _heatmap_fetch():
    from .heatmap import generate_heatmap_data
    return generate_heatmap_data()


def _news_fetch(lang="all"):
    from .news import fetch_financial_news
    return fetch_financial_news(lang)


def _calendar_fetch():
    from .news import get_economic_calendar
    return get_economic_calendar()


def _opportunities_fetch() -> list:
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
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(scanner): label for label, scanner in scanners}
        for fut in as_completed(futs, timeout=60):
            label = futs[fut]
            try:
                fut.result(timeout=5)
            except Exception as e:
                logger.error("opportunities %s failed: %s", label, e)
    opportunities.sort(key=lambda x: abs(x.get("change_24h", 0)), reverse=True)
    return opportunities


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


# ############################################################
#  对外数据函数 — 带缓存，惰性刷新
# ############################################################

def get_sentiment() -> dict:
    """7 个宏观情绪指标 + 大宗商品，自动缓存。"""
    data = _cached_fetch("sentiment", _sentiment_fetch, timeout=30)
    if data is None:
        data = _pack_sentiment({})
    return {"code": 1, "msg": "success", "data": data}


def get_indices() -> dict:
    """全球股指 + 外汇 + 加密货币"""
    data = _cached_fetch("indices", _indices_fetch, timeout=25)
    if data is None:
        data = {"indices": [], "forex": [], "crypto": []}
    return {"code": 1, "msg": "success", "data": data}


def get_heatmap() -> dict:
    data = _cached_fetch("heatmap", _heatmap_fetch, timeout=20)
    return {"code": 1, "msg": "success", "data": data or {}}


def get_news(lang="all") -> dict:
    data = _cached_fetch("news", lambda: _news_fetch(lang), timeout=20)
    return {"code": 1, "msg": "success", "data": data or {}}


def get_calendar() -> dict:
    data = _cached_fetch("economic_calendar", _calendar_fetch, timeout=20)
    return {"code": 1, "msg": "success", "data": data or []}


def get_opportunities() -> dict:
    data = _cached_fetch("opportunities", _opportunities_fetch, timeout=60)
    return {"code": 1, "msg": "success", "data": data or []}


# ============================================================
#  手动刷新 — 强制拉新，绕过缓存
# ============================================================

_REFRESH_MAP = {
    "sentiment":         _sentiment_fetch,
    "indices":           _indices_fetch,
    "heatmap":           _heatmap_fetch,
    "news":              _news_fetch,
    "economic_calendar": _calendar_fetch,
    "opportunities":     _opportunities_fetch,
}


def refresh(target="all") -> dict:
    """手动刷新：强制从远端拉取并写入缓存，不走 _cached_fetch。"""
    results = {}
    targets = list(_REFRESH_MAP.keys()) if target == "all" else [target]

    def _do_refresh(endpoint):
        fetch_fn = _REFRESH_MAP[endpoint]
        try:
            data = fetch_fn()
            if data is not None:
                _write_cache(endpoint, data)
                return "ok"
            return "failed"
        except Exception as e:
            return f"error: {e}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_do_refresh, t): t for t in targets if t in _REFRESH_MAP}
        for fut in as_completed(futures, timeout=120):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = f"error: {e}"

    return results
