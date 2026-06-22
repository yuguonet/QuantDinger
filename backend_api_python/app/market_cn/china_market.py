"""
国内市场宏观数据 — 统一数据入口

缓存设计:
    单层文件缓存 — 每个 endpoint 一个 Pickle 文件
    读路径: 读文件 → 检查过期 → 返回
    写路径: 后台线程写文件，不阻塞读
    双刷新: 超时自动刷新 + refresh() 强制刷新

用法:
    from app.market_cn.china_market import get_fear_greed, get_hot_sectors
    fg = get_fear_greed()
    sectors = get_hot_sectors()
"""
import pickle
import os as _os
import threading
import time
import logging
import atexit
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

# ============================================================
#  缓存硬上限
# ============================================================
_CACHE_STORE_MAX = 200          # _cache_store 最多保留 200 个 endpoint

logger = logging.getLogger(__name__)


# ============================================================
#  缓存配置（每个 endpoint 独立有效期）
# ============================================================

_CACHE_CONFIG = {
    "sector_trend_industry":   {"ttl": 1800,  "refresh": 1440},
    "sector_trend_concept":    {"ttl": 1800,  "refresh": 1440},
    "sector_prediction":       {"ttl": 1800,  "refresh": 1440},
    "sector_cycle_industry":   {"ttl": 3600,  "refresh": 2880},
    "sector_cycle_concept":    {"ttl": 3600,  "refresh": 2880},
}

_DEFAULT_CFG = {"ttl": 120, "refresh": 90}


# ============================================================
#  文件缓存目录
# ============================================================


# ============================================================
#  缓存文件（单文件，所有 endpoint 合并存储）
# ============================================================

_CACHE_FILE = None
_cache_store = OrderedDict()  # {endpoint: {"data": ..., "ts": float}} — LRU 顺序
_cache_lock = threading.Lock()
_cache_dirty = False  # 延迟写标记，避免每次 put 都 pickle

# 有界线程池：后台异步刷新最多 2 个并发，用完回收
_refresh_async_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="market_cn_refr")
atexit.register(lambda: _refresh_async_pool.shutdown(wait=False))


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
            loaded = pickle.load(f)
        # 转为 OrderedDict，淘汰超限条目
        if isinstance(loaded, dict):
            _cache_store = OrderedDict(loaded)
            _cache_evict()
        else:
            _cache_store = OrderedDict()
    except Exception as e:
        logger.warning("加载缓存文件失败: %s", e)
        _cache_store = OrderedDict()


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


def _cache_evict():
    """淘汰超限条目（LRU：淘汰最旧的 20%）。调用方需持有 _cache_lock。"""
    if len(_cache_store) <= _CACHE_STORE_MAX:
        return
    evict_count = max(1, len(_cache_store) // 5)
    for _ in range(evict_count):
        _cache_store.popitem(last=False)  # FIFO：淘汰最旧的
    logger.info("[cache] 淘汰 %d 个旧条目，剩余 %d", evict_count, len(_cache_store))


def cache_put(endpoint, data):
    """写缓存: 更新内存 + 标记脏（延迟写盘）。"""
    global _cache_dirty
    ts = time.time()
    with _cache_lock:
        # 先删后插，保证 LRU 顺序（最新在末尾）
        _cache_store.pop(endpoint, None)
        _cache_store[endpoint] = {"data": data, "ts": ts}
        _cache_evict()
        _cache_dirty = True


def cache_flush():
    """将脏缓存写盘（批量化，避免多次全量 pickle）。"""
    global _cache_dirty
    if not _cache_dirty:
        return
    with _cache_lock:
        try:
            _cache_save()
        finally:
            _cache_dirty = False


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
    _refresh_async_pool.submit(_refresh, endpoint, force)


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
    cache_flush()
    logger.info("[warmup] 后台刷新已启动")


# ============================================================
#  对外 API — 上层直接拿缓存
# ============================================================

def get_fear_greed() -> dict:
    """A股市场贪婪恐惧指数 (7维度综合)"""
    if _rt_fear_greed is not None:           # ① 内存缓存
        return _rt_fear_greed
    try:                                      # ② 远端拉取
        data = _fetch_fear_greed()
        return {"code": 1 if data else 0, "msg": "success", "data": data or {}}
    except Exception as e:
        logger.error("fear_greed 失败: %s", e)
        return {"code": 0, "msg": "获取失败", "data": {}}


def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    """热门板块 & 概念板块实时分析"""
    if _rt_hot_sectors is not None:          # ① 内存缓存
        return _rt_hot_sectors
    try:                                      # ② 远端拉取
        data = _fetch_hot_sectors()
        return {"code": 1 if data else 0, "msg": "success", "data": data or {}}
    except Exception as e:
        logger.error("hot_sectors 失败: %s", e)
        return {"code": 0, "msg": "获取失败", "data": {}}


def get_sector_trend(board_type="industry") -> dict:
    """板块1个月趋势 + 6个月周期 + 预测 — 直接从 DB 计算，无缓存"""
    data = _fetch_sector_trend(board_type)
    return {"code": 1, "msg": "success", "data": data or {}}


def get_sector_prediction() -> dict:
    """今日热门板块预测 — 直接从 DB 计算，无缓存"""
    return _fetch_sector_prediction()


def get_sector_cycle(board_type="industry") -> dict:
    """板块6个月周期分析 — 直接从 DB 计算，无缓存"""
    return _fetch_sector_cycle(board_type)


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
    """板块历史排名数据 — 直接从 DB 读取，无缓存"""
    days = min(max(days, 1), 250)
    try:
        from .sector_history import get_sector_history as _get_history
        rows = _get_history(board_type=board_type, days=days)
        return {"code": 1, "msg": "success", "count": len(rows), "data": rows}
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        return {"code": 0, "msg": str(e), "data": []}


def get_emotion_history(hours=None, date=None) -> dict:
    """情绪指数历史数据 — 直接从文件读取，无缓存"""
    from .emotion import get_emotion_history as _get
    return _get(hours=hours)


def get_policy() -> dict:
    """政策新闻 — 纯读: 直接从 DB 加载，无内存缓存"""
    try:
        from app.services.news_search import get_news_cache_manager
        from app.services.news_analysis import composite_score

        cache_mgr = get_news_cache_manager()
        cached_items = cache_mgr.get_items("POLICY", "CNStock")
        if not cached_items:
            return {"code": 0, "msg": "暂无政策数据（等待 scheduler 每日刷新）", "data": {}}

        news_list = [
            {
                "title": r.get("title", ""),
                "link": r.get("url", ""),
                "snippet": (r.get("snippet", "") or "")[:200],
                "source": r.get("source", ""),
                "published": r.get("published_date", ""),
                "sentiment": r.get("sentiment", "neutral"),
                "sentiment_score": r.get("sentiment_score"),
            }
            for r in cached_items
        ]

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
                "score": score_info.get("composite_score", 0),
                "direction": score_info.get("direction", "中性"),
                "count": len(news_list),
            },
        }
    except Exception as e:
        logger.warning("DB 读取 POLICY 失败: %s", e)
        return {"code": 0, "msg": f"读取失败: {e}", "data": {}}


# ============================================================
#  强制刷新
# ============================================================

def refresh(target="all") -> dict:
    """强制刷新: 立即从远端拉取并更新文件缓存。"""
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

    cache_flush()
    return results


# ============================================================
#  数据源拉取函数（纯拉取，无缓存逻辑）
# ============================================================

def _fetch_fear_greed():
    from .fear_greed_index import fear_greed_index
    return fear_greed_index()


def _fetch_hot_sectors():
    from .hot_sectors import get_all_hot_sectors
    return get_all_hot_sectors()


def _fetch_sector_trend(board_type="industry"):
    from .sector_history import get_sector_trend as _get_trend
    return _get_trend(board_type=board_type)


def _fetch_sector_prediction():
    from .sector_history import SectorAnalyzer
    analyzer = SectorAnalyzer()
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
    from .sector_history import SectorAnalyzer
    analyzer = SectorAnalyzer()
    result = analyzer.full_analysis(board_type)
    return {
        "code": 1, "msg": "success",
        "data": {
            "cycle": result.get("cycle", {}),
            "data_days": result.get("data_days", 0),
            "date_range": result.get("date_range", {}),
        },
    }


def _fetch_policy() -> None:
    """政策新闻 — 写入: 触发搜索，数据自动写入 DB (qd_news_cache_items)"""
    from app.services.news_search import fetch_financial_news
    try:
        resp = fetch_financial_news(lang="all", market="CNStock", symbol="POLICY")
        cn_count = len(resp.get("cn", []))
        en_count = len(resp.get("en", []))
        logger.info("[POLICY] 搜索完成: cn=%d, en=%d (已写入DB)", cn_count, en_count)
    except Exception as e:
        logger.error("fetch_financial_news(POLICY) 异常: %s", e)


# ============================================================
#  模块初始化 (后台线程已禁用，需手动调用 _warmup() 启动)
# ============================================================

# threading.Thread(target=_warmup, daemon=True).start()
# threading.Thread(target=_bg_watchdog, daemon=True).start()


# ═══ 内存缓存 + refresh（scheduler 调用）═══

_rt_fear_greed = None
_rt_hot_sectors = None


def refresh_fear_greed():
    global _rt_fear_greed
    try:
        data = _fetch_fear_greed()
        _rt_fear_greed = {"code": 1 if data else 0, "msg": "success", "data": data or {}}
    except Exception as e:
        logger.warning("[refresh] refresh_fear_greed 失败: %s", e)

def refresh_hot_sectors():
    global _rt_hot_sectors
    try:
        data = _fetch_hot_sectors()
        _rt_hot_sectors = {"code": 1 if data else 0, "msg": "success", "data": data or {}}
    except Exception as e:
        logger.warning("[refresh] refresh_hot_sectors 失败: %s", e)

def refresh_policy():
    """写入: 触发搜索，结果自动入库 DB"""
    try:
        _fetch_policy()
    except Exception as e:
        logger.warning("[refresh] refresh_policy 失败: %s", e)

