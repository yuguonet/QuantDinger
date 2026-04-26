"""
国内市场宏观数据 — 统一数据入口
所有国内宏观数据通过此模块获取，缓存机制内聚，对外不暴露。

用法:
    from app.market_cn.china_market import get_china_macro, get_fear_greed
    data = get_china_macro()       # 自动判断缓存/远端
    fg   = get_fear_greed()        # 同上
"""
import json as _json
import threading
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

logger = logging.getLogger(__name__)


# ============================================================
#  缓存 TTL / 过期阈值
# ============================================================

_CACHE_TTL = {
    "china_macro":        3600,
    "china_fg":           1800,
    "china_policy":       1800,
    "hot_sectors":        120,
    "sector_trend":       1800,
    "sector_prediction":  1800,
    "sector_cycle":       3600,
}

_STALE_AFTER = {
    "china_macro":        1800,
    "china_fg":           900,
    "china_policy":       900,
    "hot_sectors":        60,
    "sector_trend":       900,
    "sector_prediction":  900,
    "sector_cycle":       1800,
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
    return f"dp:market_cn_{endpoint}"


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
    ttl = _CACHE_TTL.get(endpoint, 120)
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
    wait = _STALE_AFTER.get(endpoint, 120)
    if elapsed < wait:
        return
    lock = _get_refresh_lock(endpoint)

    def _bg():
        if not lock.acquire(blocking=False):
            return
        try:
            logger.info("[market_cn] 后台刷新 %s (过期 %ds > %ds)", endpoint, elapsed, wait)
            result = fetch_fn()
            if result is not None:
                _write_cache(endpoint, result)
        except Exception as e:
            logger.error("[market_cn] 后台刷新 %s 失败: %s", endpoint, e)
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
#  宏观经济数据
# ============================================================

def get_china_macro() -> dict:
    """国内宏观经济: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR"""
    def _fetch():
        from .china_stock import ChinaData
        data = ChinaData()

        fetchers = [
            ("gdp", data.gdp),
            ("cpi", data.cpi),
            ("ppi", data.ppi),
            ("pmi", data.pmi),
            ("m2", data.m2),
            ("lpr", data.lpr),
            ("social_financing", data.social_financing),
            ("trade", data.trade),
        ]
        macro = {}

        def _fetch_one(name, fn):
            try:
                df = fn()
                if df is not None and len(df) > 0:
                    records = df.tail(6).fillna("").to_dict(orient="records")
                    return name, {"columns": list(df.columns), "latest": records, "count": len(df)}
                return name, {"columns": [], "latest": [], "count": 0}
            except Exception as e:
                logger.error("china-macro %s 失败: %s", name, e)
                return name, {"columns": [], "latest": [], "count": 0, "error": str(e)}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_one, n, f): n for n, f in fetchers}
            done, not_done = futures_wait(futures, timeout=60)
            for fut in done:
                name, result = fut.result(timeout=0)
                macro[name] = result
            for fut in not_done:
                name = futures[fut]
                logger.warning("china-macro %s 超时", name)
                macro[name] = {"columns": [], "latest": [], "count": 0, "error": "timeout"}

        return {
            "code": 1,
            "msg": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": macro,
        }

    return _cached_fetch("china_macro", _fetch, timeout=60) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


# ============================================================
#  贪婪恐惧指数
# ============================================================

def get_fear_greed() -> dict:
    """A股市场贪婪恐惧指数 (7维度综合)"""
    def _fetch():
        from .fear_greed_index import fear_greed_index
        return fear_greed_index()

    data = _cached_fetch("china_fg", _fetch, timeout=30)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


# ============================================================
#  政策解读
# ============================================================

def get_policy() -> dict:
    """AI政策解读（关键词版）"""
    def _fetch():
        from .policy_analysis import get_policy_keywords, analyze_policy_impact
        policy_items = get_policy_keywords()
        impacts = analyze_policy_impact(policy_items) if policy_items else []
        return {
            "code": 1, "msg": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": {"policy_items": policy_items[:30], "impacts": impacts[:20]},
        }

    return _cached_fetch("china_policy", _fetch, timeout=30) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


# ============================================================
#  热门板块
# ============================================================

def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    """热门板块 & 概念板块实时分析"""
    def _fetch():
        from .hot_sectors import get_all_hot_sectors
        return get_all_hot_sectors(industry_limit=industry_limit, concept_limit=concept_limit)

    data = _cached_fetch("hot_sectors", _fetch, timeout=20)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


def get_sector_stocks(board_code: str, limit=15) -> dict:
    """板块内个股详情（无缓存，实时查）"""
    if not board_code.isalnum():
        return {"code": 0, "msg": "非法板块代码", "data": []}
    try:
        from .hot_sectors import get_sector_detail
        stocks = get_sector_detail(board_code, limit=limit)
        return {"code": 1, "msg": "success", "data": stocks}
    except Exception as e:
        logger.error("sector-detail %s 失败: %s", board_code, e)
        return {"code": 0, "msg": str(e), "data": []}


# ============================================================
#  板块历史分析
# ============================================================

def get_sector_trend(board_type="industry") -> dict:
    """板块1个月趋势 + 6个月周期 + 预测"""
    cache_key = f"sector_trend_{board_type}"

    def _fetch():
        from app.interfaces.cache_file import cache_db
        from .sector_history import get_sector_trend as _get_trend
        db = cache_db()
        return _get_trend(db, board_type=board_type)

    data = _cached_fetch(cache_key, _fetch, timeout=30)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


def get_sector_prediction() -> dict:
    """今日热门板块预测"""
    def _fetch():
        from app.interfaces.cache_file import cache_db
        from .sector_history import SectorAnalyzer
        db = cache_db()
        analyzer = SectorAnalyzer(db)
        industry = analyzer.full_analysis("industry")
        concept = analyzer.full_analysis("concept")
        return {
            "code": 1, "msg": "success",
            "data": {
                "industry": industry.get("prediction", {}),
                "concept": concept.get("prediction", {}),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

    return _cached_fetch("sector_prediction", _fetch, timeout=30) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


def get_sector_history(board_type="industry", days=30) -> dict:
    """板块历史排名数据"""
    days = min(max(days, 1), 200)
    try:
        from app.interfaces.cache_file import cache_db
        from .sector_history import get_sector_history as _get_history
        db = cache_db()
        rows = _get_history(db, board_type=board_type, days=days)
        return {"code": 1, "msg": "success", "count": len(rows), "data": rows}
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        return {"code": 0, "msg": str(e), "data": []}


def get_sector_cycle(board_type="industry") -> dict:
    """板块6个月周期分析"""
    cache_key = f"sector_cycle_{board_type}"

    def _fetch():
        from app.interfaces.cache_file import cache_db
        from .sector_history import SectorAnalyzer
        db = cache_db()
        analyzer = SectorAnalyzer(db)
        result = analyzer.full_analysis(board_type)
        return {
            "code": 1, "msg": "success",
            "data": {
                "cycle": result.get("cycle", {}),
                "data_days": result.get("data_days", 0),
                "date_range": result.get("date_range", {}),
            }
        }

    return _cached_fetch(cache_key, _fetch, timeout=30) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


# ============================================================
#  情绪历史（无缓存，实时查 DB）
# ============================================================

def get_emotion_history(hours=None, date=None) -> dict:
    """情绪指数历史数据"""
    try:
        from app.interfaces.cache_file import cache_db
        from app.interfaces.emotion_scheduler import query_emotion_history
        db = cache_db()
        history = query_emotion_history(db, date=date, hours=hours)
        return {"code": 1, "msg": "success", "history": history}
    except Exception as e:
        logger.error("查询情绪历史失败: %s", e)
        return {"code": 0, "msg": str(e), "history": []}


# ============================================================
#  手动刷新
# ============================================================

def refresh(target="all") -> dict:
    """手动刷新国内宏观缓存，返回各 endpoint 状态"""
    FETCH_MAP = {
        "china_macro":   get_china_macro,
        "china_fg":      get_fear_greed,
        "china_policy":  get_policy,
        "hot_sectors":   lambda: get_hot_sectors(),
    }

    results = {}
    targets = list(FETCH_MAP.keys()) if target == "all" else [target]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for t in targets:
            if t in FETCH_MAP:
                futures[pool.submit(_safe_refresh, t, FETCH_MAP[t])] = t
        done, _ = futures_wait(futures, timeout=120)
        for fut in done:
            key = futures[fut]
            try:
                results[key] = "ok" if fut.result(timeout=0) else "failed"
            except Exception as e:
                results[key] = f"error: {e}"

    return results
