"""
国内市场宏观数据 — 统一数据入口
所有国内宏观数据通过此模块获取，缓存机制内聚，对外不暴露。

用法:
    from app.market_cn.china_market import get_china_macro, get_fear_greed
    data = get_china_macro()       # 自动判断缓存/远端
    fg   = get_fear_greed()        # 同上
"""
import json as _json
import os as _os
import threading
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

logger = logging.getLogger(__name__)


# ============================================================
#  缓存 TTL / 过期阈值80%
# ============================================================

_CACHE_TTL = {
    "china_fg":           1800,
    "hot_sectors":        600,
    "sector_trend":       1800,
    "sector_prediction":  1800,
    "sector_cycle":       3600,
}

_STALE_AFTER = {
    "china_fg":           1440,
    "hot_sectors":        480,
    "sector_trend":       1440,
    "sector_prediction":  1440,
    "sector_cycle":       2880,
}

# 文件缓存 TTL（秒）— 低频更新数据用文件持久化，重启不丢
_FILE_CACHE_TTL = {
    "china_macro":  86400,    # 1天 — 宏观数据月度/季度更新
    "china_policy": 43200,    # 半天 — 政策解读日更数次
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
_china_data_instance = None
_china_data_lock = threading.Lock()


def _get_refresh_lock(key):
    with _refresh_locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


def _cache_key(endpoint):
    return f"dp:market_cn_{endpoint}"


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
    ttl = _CACHE_TTL.get(endpoint, 120)
    payload = _json.dumps({"data": data, "ts": time.time()}, ensure_ascii=False, default=str)
    try:
        cm = _get_cm()
        key = _cache_key(endpoint)
        cm.set(key, payload, ttl=ttl)
    except Exception as e:
        logger.warning("写缓存失败 [%s]: %s", endpoint, e)


def _get_china_data():
    """ChinaData 单例，避免每次 _fetch 都重新实例化（省去 _check_sources 开销）。"""
    global _china_data_instance
    if _china_data_instance is None:
        with _china_data_lock:
            if _china_data_instance is None:
                from .china_stock import ChinaData
                _china_data_instance = ChinaData()
    return _china_data_instance


# ============================================================
#  文件缓存 — 用于低频更新数据（宏观 / 政策）
#  持久化到磁盘，服务重启不丢；按日期过期。
# ============================================================

_FILE_CACHE_DIR = None


def _file_cache_dir():
    """文件缓存目录，与 feather 数据同级。"""
    global _FILE_CACHE_DIR
    if _FILE_CACHE_DIR is None:
        _FILE_CACHE_DIR = _os.path.join(_os.getcwd(), "data", "market_cn_cache")
        _os.makedirs(_FILE_CACHE_DIR, exist_ok=True)
    return _FILE_CACHE_DIR


def _file_cache_path(endpoint):
    return _os.path.join(_file_cache_dir(), f"{endpoint}.json")


def _read_file_cache(endpoint):
    """读取文件缓存，返回 (data, mtime)。过期或不存在返回 (None, 0)。"""
    path = _file_cache_path(endpoint)
    if not _os.path.exists(path):
        return None, 0
    try:
        mtime = _os.path.getmtime(path)
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return data, mtime
    except Exception as e:
        logger.warning("读取文件缓存失败 [%s]: %s", endpoint, e)
        return None, 0


def _write_file_cache(endpoint, data):
    """原子写入文件缓存（先 .tmp 再 rename）。"""
    path = _file_cache_path(endpoint)
    tmp_path = f"{path}.tmp.{_os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, default=str)
        _os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("写入文件缓存失败 [%s]: %s", endpoint, e)
        if _os.path.exists(tmp_path):
            try:
                _os.remove(tmp_path)
            except OSError:
                pass


def _file_cache_fetch(endpoint, fetch_fn):
    """文件缓存统一入口：有且未过期直接返回，过期则返回旧数据、后台异步刷新。"""
    ttl = _FILE_CACHE_TTL.get(endpoint, 86400)
    data, cached_ts = _read_file_cache(endpoint)
    if data is not None:
        elapsed = time.time() - cached_ts
        if elapsed < ttl:
            return data
        # 已过期：先返回旧数据，后台刷新
        lock = _get_refresh_lock(f"file_{endpoint}")
        def _bg():
            if not lock.acquire(blocking=False):
                return
            try:
                logger.info("[file-cache] 后台刷新 %s (过期 %.0fs > %ds)", endpoint, elapsed, ttl)
                result = fetch_fn()
                if result is not None:
                    _write_file_cache(endpoint, result)
            except Exception as e:
                logger.error("[file-cache] 后台刷新 %s 失败: %s", endpoint, e)
            finally:
                lock.release()
        threading.Thread(target=_bg, daemon=True).start()
        return data
    # 无缓存：同步获取
    result = fetch_fn()
    if result is not None:
        _write_file_cache(endpoint, result)
    return result


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

def _china_macro_fetch() -> dict:
    """宏观经济原始获取逻辑（无缓存）。"""
    data = _get_china_data()

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


def _china_policy_fetch() -> dict:
    """政策解读原始获取逻辑 — 通过 fetch_financial_news 统一接口获取。"""
    from app.services.news_service import fetch_financial_news, get_news_cache_manager
    resp = fetch_financial_news(lang="all", market="CNStock", symbol="POLICY")
    cache_mgr = get_news_cache_manager()
    items = cache_mgr.get_items("POLICY", "CNStock")
    score_info = cache_mgr.calc_score("POLICY", "CNStock")
    news_list = resp.get("cn", []) + resp.get("en", [])
    return {
        "code": 1, "msg": "success",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "news": news_list,
            "items": items,
            "score": score_info,
        },
    }


def get_china_macro() -> dict:
    """国内宏观经济: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR (文件缓存, 1天)"""
    return _file_cache_fetch("china_macro", _china_macro_fetch) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


# ============================================================
#  贪婪恐惧指数
# ============================================================

def _fear_greed_fetch():
    """贪恐指数原始获取（无缓存）。"""
    from .fear_greed_index import fear_greed_index
    return fear_greed_index()


def get_fear_greed() -> dict:
    """A股市场贪婪恐惧指数 (7维度综合)"""
    data = _cached_fetch("china_fg", _fear_greed_fetch, timeout=30)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


# ============================================================
#  政策解读
# ============================================================

def get_policy() -> dict:
    """AI政策解读（关键词版, 文件缓存, 半天）"""
    return _file_cache_fetch("china_policy", _china_policy_fetch) or {
        "code": 0, "msg": "获取失败", "data": {}
    }


# ============================================================
#  热门板块
# ============================================================

def _hot_sectors_fetch(industry_limit=15, concept_limit=15):
    """热门板块原始获取（无缓存）。"""
    from .hot_sectors import get_all_hot_sectors
    return get_all_hot_sectors(industry_limit=industry_limit, concept_limit=concept_limit)


def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    """热门板块 & 概念板块实时分析"""
    data = _cached_fetch("hot_sectors",
                         lambda: _hot_sectors_fetch(industry_limit, concept_limit),
                         timeout=20)
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
    """手动刷新：强制从远端拉取并写入缓存，不走 _cached_fetch / _file_cache_fetch。"""
    # 文件缓存 endpoint
    FILE_MAP = {
        "china_macro":  ("china_macro", _china_macro_fetch),
        "china_policy": ("china_policy", _china_policy_fetch),
    }
    # 内存缓存 endpoint
    MEM_MAP = {
        "china_fg":    _fear_greed_fetch,
        "hot_sectors": _hot_sectors_fetch,
    }

    all_targets = list(set(list(FILE_MAP.keys()) + list(MEM_MAP.keys())))
    targets = all_targets if target == "all" else [target]
    results = {}

    def _do_refresh(ep):
        if ep in FILE_MAP:
            cache_key, fetch_fn = FILE_MAP[ep]
            data = fetch_fn()
            if data is not None:
                _write_file_cache(cache_key, data)
                return "ok"
            return "failed"
        elif ep in MEM_MAP:
            data = MEM_MAP[ep]()
            if data is not None:
                _write_cache(ep, data)
                return "ok"
            return "failed"
        return "unknown endpoint"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_do_refresh, t): t for t in targets}
        done, _ = futures_wait(futures, timeout=120)
        for fut in done:
            key = futures[fut]
            try:
                results[key] = fut.result(timeout=0)
            except Exception as e:
                results[key] = f"error: {e}"

    return results
