"""
Unified data provider layer for global market data.

Shared cache and utility helpers live here; domain-specific fetchers are
organised into sub-modules (crypto, forex, …).

The cache layer delegates to :class:`app.utils.cache.CacheManager`, which
transparently uses **Redis** when configured (``CACHE_ENABLED=true``) or
falls back to a thread-safe in-memory dict.
"""
from __future__ import annotations

from typing import Any, Optional

from app.utils.logger import get_logger
from app.data_sources.normalizer import safe_float  # noqa: F401 — re-export for data_providers submodules

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Cache – wraps CacheManager singleton
# ---------------------------------------------------------------------------

CACHE_TTL = {
    "crypto_heatmap": 300,
    "forex_pairs": 120,
    "stock_indices": 120,
    "market_overview": 300,          # 5min — 金/油等大宗商品不需要太频繁
    "market_heatmap": 300,
    "commodities": 300,              # 5min
    "market_news": 180,
    "economic_calendar": 3600,
    "market_sentiment": 21600,       # legacy, 不再直接使用
    "trading_opportunities": 3600,
    # ── 情绪/宏观指标独立 TTL ──
    "sentiment_fear_greed": 14400,   # 4h — 恐贪指数每天更新一次
    "sentiment_vix": 300,            # 5min — 美股盘中活跃
    "sentiment_vxn": 300,            # 5min
    "sentiment_dxy": 600,            # 10min — 美元指数
    "sentiment_yield_curve": 600,    # 10min — 收益率曲线
    "sentiment_gvz": 600,            # 10min — 金波动率
    "sentiment_vix_term": 300,       # 5min — VIX期限结构
}

_DEFAULT_TTL = 60


def _cm():
    """Lazy singleton accessor to avoid import-time side effects."""
    from app.utils.cache import CacheManager
    return CacheManager()


def get_cached(key: str, ttl: int | None = None) -> Optional[Any]:
    """Return cached data if not expired.

    *ttl* is respected by :class:`CacheManager` at write-time; for the
    Redis path every key already has an inherent TTL.  For the in-memory
    fallback the ``CacheManager.get`` handles expiry internally.
    """
    return _cm().get(f"dp:{key}")


def set_cached(key: str, data: Any, ttl: int | None = None):
    """Write a cache entry with the appropriate TTL."""
    effective_ttl = ttl or CACHE_TTL.get(key, _DEFAULT_TTL)
    _cm().set(f"dp:{key}", data, ttl=effective_ttl)


def clear_cache():
    """Clear all cached data (used by /refresh endpoint).

    For Redis this deletes dp:* keys; for the in-memory backend it clears
    the whole dict (acceptable – only market data lives here).
    """
    cm = _cm()
    if hasattr(cm, '_client') and hasattr(cm._client, 'clear'):
        cm._client.clear()
    else:
        try:
            import redis as _redis
            if isinstance(cm._client, _redis.Redis):
                for key in cm._client.scan_iter("dp:*"):
                    cm._client.delete(key)
        except Exception:
            pass

