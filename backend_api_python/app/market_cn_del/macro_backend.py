"""
国内宏观数据后端 — 统一入口类

核心原则: 缓存优先，有就直接用，没有才去拿。
过期数据后台静默刷新，不阻塞调用方。

缓存: data/market_cn_cache/macro_backend.pkl
TTL:  恐贪 5min, 期货 2min, 热门板块 10min, 政策 30min

用法:
    from app.market_cn.macro_backend import MacroCNBackend
    backend = MacroCNBackend()

    # 惰性拿: 有缓存直接返回，没有才等
    data = backend.fetch_all()

    # 强制刷: 立即从远端拉
    data = backend.refresh()

    # AI 分析兼容: 可直接替换 get_fear_greed()
    fg = backend.fetch_sentiment_compat()
"""
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import (
    cache_get, cache_put, cache_is_fresh, cache_get_or_fetch,
)

logger = logging.getLogger(__name__)

# ── 缓存 TTL (秒) ────────────────────────────────────────────

TTL_SENTIMENT = 300       # 恐贪 5min
TTL_FUTURES = 120         # 期货 2min
TTL_HOT_SECTORS = 600     # 热门板块 10min
TTL_POLICY = 1800         # 政策 30min


# ── 后台刷新 ─────────────────────────────────────────────────

_refreshing: dict = {}   # endpoint -> True
_refresh_lock = threading.Lock()


def _bg_refresh(endpoint: str, fetcher, ttl: int):
    """后台静默刷新，不阻塞任何人。"""
    with _refresh_lock:
        if _refreshing.get(endpoint):
            return
        _refreshing[endpoint] = True

    try:
        data = fetcher()
        if data is not None:
            cache_put(endpoint, data)
    except Exception as e:
        logger.debug("后台刷新 %s 失败: %s", endpoint, e)
    finally:
        with _refresh_lock:
            _refreshing[endpoint] = False


def _lazy_get(endpoint: str, ttl: int, fetcher):
    """惰性获取: 有缓存直接返回，过期则后台刷，无缓存才阻塞。

    返回顺序:
      1. 缓存新鲜 → 直接返回 (0ms)
      2. 缓存过期 → 返回旧数据 + 后台异步刷新 (0ms)
      3. 无缓存 → 阻塞调用 fetcher (慢)
    """
    # 有缓存?
    data = cache_get(endpoint)
    if data is not None:
        # 新鲜?
        if cache_is_fresh(endpoint, ttl):
            return data
        # 过期: 返回旧数据，后台刷
        threading.Thread(
            target=_bg_refresh, args=(endpoint, fetcher, ttl),
            daemon=True,
        ).start()
        return data

    # 无缓存: 必须阻塞
    try:
        data = fetcher()
        if data is not None:
            cache_put(endpoint, data)
        return data
    except Exception as e:
        logger.error("首次获取 %s 失败: %s", endpoint, e)
        return None


class MacroCNBackend:
    """国内宏观数据后端，统一外部调用形式。

    fetch_* 方法全部惰性: 有缓存立即返回，无缓存才阻塞。
    refresh() 强制刷新全部。
    """

    # ── 恐贪指数 ─────────────────────────────────────────────

    def fetch_sentiment(self) -> dict:
        """A股恐贪指数 — 东方财富涨跌统计自算。"""
        from .sentiment import fetch_fear_greed
        data = _lazy_get("sentiment", TTL_SENTIMENT, fetch_fear_greed)
        return data or {"error": "恐贪指数获取失败", "score": 50, "label": "未知"}

    # ── 期货波动率 ───────────────────────────────────────────

    def fetch_futures_volatility(self, threshold: float = 0.5) -> dict:
        """期货波动率 — 新浪期货日内振幅。"""
        from .futures_vol import fetch_futures_volatility as _fv
        fetcher = lambda: _fv(threshold=threshold)
        data = _lazy_get("futures_vol", TTL_FUTURES, fetcher)
        return data or {"error": "期货波动率获取失败", "volatile": [], "contracts": []}

    # ── 热门板块 ─────────────────────────────────────────────

    def fetch_hot_sectors(self, industry_limit: int = 15, concept_limit: int = 15) -> dict:
        """热门行业 + 概念板块。复用 hot_sectors.py。"""
        def _fetch():
            try:
                from .hot_sectors import get_all_hot_sectors
                return get_all_hot_sectors(industry_limit, concept_limit)
            except Exception as e:
                logger.error("热门板块获取失败: %s", e)
                return {"error": str(e), "industry": [], "concept": []}
        data = _lazy_get("hot_sectors", TTL_HOT_SECTORS, _fetch)
        return data or {"error": "热门板块获取失败", "industry": [], "concept": []}

    # ── 政策新闻 ─────────────────────────────────────────────

    def fetch_policy(self) -> dict:
        """政策新闻。复用 china_market.get_policy()。"""
        def _fetch():
            try:
                from .china_market import get_policy
                return get_policy()
            except Exception as e:
                logger.error("政策新闻获取失败: %s", e)
                return {"code": 0, "msg": str(e), "data": {}}
        data = _lazy_get("policy", TTL_POLICY, _fetch)
        return data or {"code": 0, "msg": "政策新闻获取失败", "data": {}}

    # ── 一次性拉全部 ────────────────────────────────────────

    def fetch_all(self) -> dict:
        """一次性拉全部宏观数据 (并行，惰性)。

        有缓存的直接返回，无缓存的并行拉取。
        """
        import time
        t0 = time.time()

        tasks = {
            "sentiment": self.fetch_sentiment,
            "futures_volatility": self.fetch_futures_volatility,
            "hot_sectors": self.fetch_hot_sectors,
            "policy": self.fetch_policy,
        }

        results = {}
        # 并行: 每个独立线程，任何一个有缓存就立即返回
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futures, timeout=30):
                name = futures[fut]
                try:
                    results[name] = fut.result(timeout=5)
                except Exception as e:
                    logger.error("fetch_all [%s] 失败: %s", name, e)
                    results[name] = {"error": str(e)}

        elapsed = round((time.time() - t0) * 1000)

        success = sum(1 for v in results.values() if v and not v.get("error"))
        failed = len(results) - success

        results["meta"] = {
            "fetched_at": datetime.now().isoformat(),
            "elapsed_ms": elapsed,
            "success": success,
            "failed": failed,
        }
        return results

    def refresh(self) -> dict:
        """强制刷新全部 (阻塞)。"""
        import time
        from .sentiment import fetch_fear_greed
        from .futures_vol import fetch_futures_volatility as _fv

        t0 = time.time()

        def _do(endpoint, fetcher):
            data = fetcher()
            if data is not None:
                cache_put(endpoint, data)
            return data

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(_do, "sentiment", fetch_fear_greed): "sentiment",
                pool.submit(_do, "futures_vol", lambda: _fv(threshold=0.5)): "futures_volatility",
                pool.submit(_do, "hot_sectors", lambda: (
                    __import__('app.market_cn.hot_sectors', fromlist=['get_all_hot_sectors'])
                    .get_all_hot_sectors(15, 15)
                )): "hot_sectors",
                pool.submit(_do, "policy", lambda: (
                    __import__('app.market_cn.china_market', fromlist=['get_policy'])
                    .get_policy()
                )): "policy",
            }
            results = {}
            for fut in as_completed(futs, timeout=60):
                name = futs[fut]
                try:
                    results[name] = fut.result(timeout=5)
                except Exception as e:
                    results[name] = {"error": str(e)}

        elapsed = round((time.time() - t0) * 1000)
        results["meta"] = {
            "fetched_at": datetime.now().isoformat(),
            "elapsed_ms": elapsed,
            "mode": "force_refresh",
        }
        return results

    # ── AI 分析兼容接口 ──────────────────────────────────────

    def fetch_sentiment_compat(self) -> dict:
        """恐贪指数 — 兼容旧版 china_market.get_fear_greed() 返回格式。

        旧格式:
            {"code": 1, "data": {"composite_score": 55.0, "label": "中性", "indicators": [...]}}

        可直接替换 market_data_collector._get_ashare_factors 中的:
            from app.market_cn.china_market import get_fear_greed
        改为:
            from app.market_cn.macro_backend import MacroCNBackend
            backend = MacroCNBackend()
            resp = backend.fetch_sentiment_compat()
        """
        raw = self.fetch_sentiment()
        if raw.get("error"):
            return {"code": 0, "msg": raw["error"], "data": {}}

        components = raw.get("components", {})
        stats = raw.get("stats", {})

        indicators = []
        up_ratio = components.get("up_ratio", {})
        indicators.append({
            "name": "上涨占比",
            "score": up_ratio.get("score", 50),
            "detail": f"上涨约 {stats.get('up_count', 0)}/{stats.get('total', 0)} ({up_ratio.get('value', 0)}%)",
        })
        limit = components.get("limit_ratio", {})
        indicators.append({
            "name": "涨跌停比",
            "score": limit.get("score", 50),
            "detail": f"涨停 {limit.get('up', 0)} / 跌停 {limit.get('down', 0)}",
        })
        strong = components.get("strong_ratio", {})
        indicators.append({
            "name": "强势股占比",
            "score": strong.get("score", 50),
            "detail": f"涨幅>3%: {strong.get('count', 0)}",
        })

        return {
            "code": 1,
            "msg": "success",
            "data": {
                "composite_score": raw.get("score", 50),
                "composite_label": raw.get("label", "中性"),
                "label": raw.get("label", "中性"),
                "indicators": indicators,
                "stats": stats,
                "timestamp": raw.get("timestamp"),
                "source": raw.get("source"),
            },
        }
