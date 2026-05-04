"""
国内市场宏观数据 — 统一数据入口

缓存设计:
    单层文件缓存 — 每个 endpoint 一个 Pickle 文件
    读路径: 读文件 → 检查过期 → 返回
    写路径: 后台线程写文件，不阻塞读
    双刷新: 超时自动刷新 + refresh() 强制刷新

用法:
    from app.market_cn.china_market import get_china_macro, get_fear_greed
    data = get_china_macro()
    fg   = get_fear_greed()
"""
import pickle
import os as _os
import threading
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

logger = logging.getLogger(__name__)


# ============================================================
#  缓存配置（每个 endpoint 独立有效期）
# ============================================================

_CACHE_CONFIG = {
    "china_fg":                {"ttl": 1800,  "refresh": 1440},
    "hot_sectors":             {"ttl": 600,   "refresh": 480},
    "sector_trend_industry":   {"ttl": 1800,  "refresh": 1440},
    "sector_trend_concept":    {"ttl": 1800,  "refresh": 1440},
    "sector_prediction":       {"ttl": 1800,  "refresh": 1440},
    "sector_cycle_industry":   {"ttl": 3600,  "refresh": 2880},
    "sector_cycle_concept":    {"ttl": 3600,  "refresh": 2880},
    "china_macro":             {"ttl": 86400, "refresh": 72000},
}

_DEFAULT_CFG = {"ttl": 120, "refresh": 90}


# ============================================================
#  文件缓存目录
# ============================================================


# ============================================================
#  缓存文件（单文件，所有 endpoint 合并存储）
# ============================================================

_CACHE_FILE = None
_cache_store = {}    # {endpoint: {"data": ..., "ts": float}}
_cache_lock = threading.Lock()


def _cache_path():
    global _CACHE_FILE
    if _CACHE_FILE is None:
        _CACHE_FILE = _os.path.join(_os.getcwd(), "data", "market_cn_cache", "cache.pkl")
        _os.makedirs(_os.path.dirname(_CACHE_FILE), exist_ok=True)
    return _CACHE_FILE


def _cache_load():
    """从文件加载全部缓存到内存。"""
    global _cache_store
    path = _cache_path()
    if not _os.path.exists(path):
        return
    try:
        with open(path, "rb") as f:
            _cache_store = pickle.load(f)
    except Exception as e:
        logger.warning("加载缓存文件失败: %s", e)
        _cache_store = {}


def _cache_save():
    """将内存中的全部缓存原子写入文件。"""
    path = _cache_path()
    tmp = f"{path}.tmp.{_os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(_cache_store, f)
        _os.replace(tmp, path)
    except Exception as e:
        logger.warning("写缓存文件失败: %s", e)
        if _os.path.exists(tmp):
            try:
                _os.remove(tmp)
            except OSError:
                pass


def cache_get(endpoint):
    """读缓存: 从内存字典取数据。"""
    entry = _cache_store.get(endpoint)
    if entry is None:
        return None
    return entry.get("data")


def cache_is_stale(endpoint):
    """检查缓存是否过期。"""
    cfg = _CACHE_CONFIG.get(endpoint, _DEFAULT_CFG)
    entry = _cache_store.get(endpoint)
    if entry is None:
        return True
    return (time.time() - entry.get("ts", 0)) >= cfg["refresh"]


def cache_put(endpoint, data):
    """写缓存: 更新内存 + 触发文件保存。"""
    ts = time.time()
    with _cache_lock:
        _cache_store[endpoint] = {"data": data, "ts": ts}
        _cache_save()


# ============================================================
#  数据源单例
# ============================================================

_china_data = None
_china_data_lock = threading.Lock()


def _get_china_data():
    global _china_data
    if _china_data is None:
        with _china_data_lock:
            if _china_data is None:
                from .china_stock import ChinaData
                _china_data = ChinaData()
    return _china_data


# ============================================================
#  后台刷新
# ============================================================

_refresh_locks = {}
_refresh_locks_guard = threading.Lock()


def _get_lock(endpoint):
    with _refresh_locks_guard:
        if endpoint not in _refresh_locks:
            _refresh_locks[endpoint] = threading.Lock()
        return _refresh_locks[endpoint]


def _do_fetch(endpoint):
    """根据 endpoint 调用对应的数据源拉取函数。"""
    fetchers = {
        "china_macro":              _fetch_china_macro,
        "china_fg":                 _fetch_fear_greed,
        "hot_sectors":              _fetch_hot_sectors,
        "sector_trend_industry":    lambda: _fetch_sector_trend("industry"),
        "sector_trend_concept":     lambda: _fetch_sector_trend("concept"),
        "sector_prediction":        _fetch_sector_prediction,
        "sector_cycle_industry":    lambda: _fetch_sector_cycle("industry"),
        "sector_cycle_concept":     lambda: _fetch_sector_cycle("concept"),
    }
    fn = fetchers.get(endpoint)
    if fn is None:
        logger.error("未知 endpoint: %s", endpoint)
        return None
    try:
        return fn()
    except Exception as e:
        logger.error("拉取 %s 失败: %s", endpoint, e)
        return None


def _refresh(endpoint, force=False):
    """后台刷新: 拉取远端 → 写入文件缓存。"""
    cfg = _CACHE_CONFIG.get(endpoint, _DEFAULT_CFG)
    lock = _get_lock(endpoint)

    if not force and not cache_is_stale(endpoint):
        return

    if not lock.acquire(blocking=False):
        return

    try:
        logger.info("[refresh] %s %s", "强制" if force else "后台", endpoint)
        data = _do_fetch(endpoint)
        if data is not None:
            cache_put(endpoint, data)
    except Exception as e:
        logger.error("[refresh] %s 失败: %s", endpoint, e)
    finally:
        lock.release()


def _refresh_async(endpoint, force=False):
    threading.Thread(target=_refresh, args=(endpoint, force), daemon=True).start()


# ============================================================
#  超时刷新守护线程
# ============================================================

_BG_INTERVAL = 60


def _bg_watchdog():
    """每 60 秒扫描一次，过期自动刷新。"""
    while True:
        time.sleep(_BG_INTERVAL)
        for endpoint in _CACHE_CONFIG:
            if cache_is_stale(endpoint):
                _refresh_async(endpoint)


# ============================================================
#  冷启动预热
# ============================================================

def _warmup():
    """启动时从文件加载缓存 + 后台刷新全部 endpoint。"""
    logger.info("[warmup] 缓存预热开始")
    _cache_load()
    logger.info("[warmup] 从文件加载 %d 个 endpoint", len(_cache_store))
    for endpoint in _CACHE_CONFIG:
        if cache_is_stale(endpoint):
            _refresh_async(endpoint, force=True)
    logger.info("[warmup] 后台刷新已启动")


# ============================================================
#  对外 API — 上层直接拿缓存
# ============================================================

def get_china_macro() -> dict:
    """国内宏观经济: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR"""
    data = cache_get("china_macro")
    if data is not None:
        return data
    _refresh("china_macro", force=True)
    return cache_get("china_macro") or {"code": 0, "msg": "获取失败", "data": {}}


def get_fear_greed() -> dict:
    """A股市场贪婪恐惧指数 (7维度综合)"""
    data = cache_get("china_fg")
    if data is not None:
        return data
    _refresh("china_fg", force=True)
    data = cache_get("china_fg")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    """热门板块 & 概念板块实时分析"""
    data = cache_get("hot_sectors")
    if data is not None:
        return data
    _refresh("hot_sectors", force=True)
    data = cache_get("hot_sectors")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


def get_sector_trend(board_type="industry") -> dict:
    """板块1个月趋势 + 6个月周期 + 预测"""
    key = f"sector_trend_{board_type}"
    data = cache_get(key)
    if data is not None:
        return data
    _refresh(key, force=True)
    data = cache_get(key)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}


def get_sector_prediction() -> dict:
    """今日热门板块预测"""
    data = cache_get("sector_prediction")
    if data is not None:
        return data
    _refresh("sector_prediction", force=True)
    data = cache_get("sector_prediction")
    return data or {"code": 0, "msg": "获取失败", "data": {}}


def get_sector_cycle(board_type="industry") -> dict:
    """板块6个月周期分析"""
    key = f"sector_cycle_{board_type}"
    data = cache_get(key)
    if data is not None:
        return data
    _refresh(key, force=True)
    data = cache_get(key)
    return data or {"code": 0, "msg": "获取失败", "data": {}}


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


def get_sector_history(board_type="industry", days=30) -> dict:
    """板块历史排名数据（无缓存，每次直接读 feather）"""
    days = min(max(days, 1), 200)
    try:
        from app.utils.cache_file import cache_db
        from .sector_history import get_sector_history as _get_history
        db = cache_db()
        rows = _get_history(db, board_type=board_type, days=days)
        return {"code": 1, "msg": "success", "count": len(rows), "data": rows}
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        return {"code": 0, "msg": str(e), "data": []}


def get_emotion_history(hours=None, date=None) -> dict:
    """情绪指数历史数据（无缓存，每次直接读 feather）"""
    try:
        from app.utils.cache_file import cache_db
        from app.interfaces.emotion_scheduler import query_emotion_history
        db = cache_db()
        history = query_emotion_history(db, date=date, hours=hours)
        return {"code": 1, "msg": "success", "history": history}
    except Exception as e:
        logger.error("查询情绪历史失败: %s", e)
        return {"code": 0, "msg": str(e), "history": []}


def get_policy() -> dict:
    """AI政策解读 — 直接调用，由 news.py PostgreSQL 缓存管理"""
    return _fetch_policy()


# ============================================================
#  强制刷新
# ============================================================

def refresh(target="all") -> dict:
    """强制刷新: 立即从远端拉取并更新缓存。"""
    all_endpoints = list(_CACHE_CONFIG.keys())
    targets = all_endpoints if target == "all" else [target]
    results = {}

    def _do(ep):
        _refresh(ep, force=True)
        return "ok"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_do, t): t for t in targets}
        done, _ = futures_wait(futs, timeout=120)
        for fut in done:
            key = futs[fut]
            try:
                results[key] = fut.result(timeout=0)
            except Exception as e:
                results[key] = f"error: {e}"

    return results


# ============================================================
#  数据源拉取函数（纯拉取，无缓存逻辑）
# ============================================================

def _fetch_china_macro() -> dict:
    data = _get_china_data()
    fetchers = [
        ("gdp", data.gdp), ("cpi", data.cpi), ("ppi", data.ppi),
        ("pmi", data.pmi), ("m2", data.m2), ("lpr", data.lpr),
        ("social_financing", data.social_financing), ("trade", data.trade),
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
        futs = {pool.submit(_fetch_one, n, f): n for n, f in fetchers}
        done, not_done = futures_wait(futs, timeout=60)
        for fut in done:
            name, result = fut.result(timeout=0)
            macro[name] = result
        for fut in not_done:
            name = futs[fut]
            logger.warning("china-macro %s 超时", name)
            macro[name] = {"columns": [], "latest": [], "count": 0, "error": "timeout"}

    return {
        "code": 1, "msg": "success",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": macro,
    }


def _fetch_fear_greed():
    from .fear_greed_index import fear_greed_index
    return fear_greed_index()


def _fetch_hot_sectors():
    from .hot_sectors import get_all_hot_sectors
    return get_all_hot_sectors()


def _fetch_sector_trend(board_type="industry"):
    from app.utils.cache_file import cache_db
    from .sector_history import get_sector_trend as _get_trend
    return _get_trend(cache_db(), board_type=board_type)


def _fetch_sector_prediction():
    from app.utils.cache_file import cache_db
    from .sector_history import SectorAnalyzer
    analyzer = SectorAnalyzer(cache_db())
    industry = analyzer.full_analysis("industry")
    concept = analyzer.full_analysis("concept")
    return {
        "code": 1, "msg": "success",
        "data": {
            "industry": industry.get("prediction", {}),
            "concept": concept.get("prediction", {}),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _fetch_sector_cycle(board_type="industry"):
    from app.utils.cache_file import cache_db
    from .sector_history import SectorAnalyzer
    analyzer = SectorAnalyzer(cache_db())
    result = analyzer.full_analysis(board_type)
    return {
        "code": 1, "msg": "success",
        "data": {
            "cycle": result.get("cycle", {}),
            "data_days": result.get("data_days", 0),
            "date_range": result.get("date_range", {}),
        },
    }


def _fetch_policy() -> dict:
    from app.services.news_service import fetch_financial_news, get_news_cache_manager
    from app.services.news_analysis import composite_score

    news_list = []
    try:
        resp = fetch_financial_news(lang="all", market="CNStock", symbol="POLICY")
        news_list = resp.get("cn", []) + resp.get("en", [])
    except Exception as e:
        logger.error("fetch_financial_news(POLICY) 异常: %s", e)

    if not news_list:
        try:
            cache_mgr = get_news_cache_manager()
            cached_items = cache_mgr.get_items("POLICY", "CNStock")
            if cached_items:
                news_list = [
                    {
                        "title": r.get("title", ""),
                        "link": r.get("url", ""),
                        "snippet": r.get("snippet", ""),
                        "source": r.get("source", ""),
                        "published": r.get("published_date", ""),
                        "sentiment": r.get("sentiment", "neutral"),
                        "sentiment_score": r.get("sentiment_score"),
                        "category": "政策/宏观:CNStock", "lang": "cn",
                    }
                    for r in cached_items
                ]
        except Exception as e:
            logger.error("DB 缓存降级失败: %s", e)

    if not news_list:
        return {"code": 0, "msg": "暂无政策数据", "data": {}}

    score_articles = [
        {"score": item.get("sentiment_score", 0.0) or 0.0,
         "published_date": item.get("published", "")}
        for item in news_list
    ]
    try:
        score_info = composite_score(score_articles)
    except Exception:
        score_info = {}

    return {
        "code": 1, "msg": "success",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "news": news_list,
            "items": news_list,
            "score": {
                "composite_score": score_info.get("composite_score", 0.0),
                "direction": score_info.get("direction", "中性"),
                "positive": score_info.get("positive_count", 0),
                "negative": score_info.get("negative_count", 0),
                "neutral": score_info.get("neutral_count", 0),
                "veto": score_info.get("veto", False),
                "veto_count": 1 if score_info.get("veto") else 0,
            },
        },
    }


# ============================================================
#  模块初始化
# ============================================================

threading.Thread(target=_warmup, daemon=True).start()
threading.Thread(target=_bg_watchdog, daemon=True).start()
