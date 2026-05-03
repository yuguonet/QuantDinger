"""
trading_day.py — A 股交易日历（外部接口）

数据来源优先级：
  1. DB 缓存表 trading_day_cache（akshare 写入，可信）
  2. akshare 实时拉取（增量写入 DB）
  3. DB kline 表反推（只用于内存，不写缓存）

内存缓存 1 天过期，过期后自动从 DB 刷新（无需重启进程）。

用法：
    from trading_day import is_trading_day, trading_days_between

    if is_trading_day("2026-05-03"):
        ...

    n = trading_days_between("2026-04-01", "2026-05-01")
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

# ── 路径 ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPTIMIZER_DIR = os.path.join(_PROJECT_ROOT, "optimizer")
_BACKEND_DIR = os.path.join(_PROJECT_ROOT, "backend_api_python")
for _p in (_OPTIMIZER_DIR, _BACKEND_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TZ_SH = timezone(timedelta(hours=8))

_CACHE_TTL = timedelta(days=1)


class _TradingDayCache:
    """交易日历单例，内存缓存 + DB 持久化 + akshare 增量更新"""

    def __init__(self, market: str = "CNStock"):
        self._market = market
        self._dates: frozenset[str] | None = None
        self._loaded_at: datetime | None = None
        self._lock = threading.Lock()

    def _is_stale(self) -> bool:
        if self._dates is None or self._loaded_at is None:
            return True
        return datetime.now(TZ_SH) - self._loaded_at > _CACHE_TTL

    def get(self) -> frozenset[str]:
        """获取交易日集合，过期自动刷新"""
        if self._is_stale():
            with self._lock:
                if self._is_stale():
                    self._refresh()
        return self._dates

    def _refresh(self):
        """刷新缓存：DB 缓存优先 → akshare 增量 → DB 反推兜底"""
        pool = self._get_pool()
        self._init_cache_table(pool)
        cached = self._load_from_db(pool)

        # 缓存够新 → 直接用
        if cached:
            cached_max = max(cached)
            today = datetime.now(TZ_SH).strftime("%Y-%m-%d")
            if cached_max >= (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d"):
                self._dates = frozenset(cached)
                self._loaded_at = datetime.now(TZ_SH)
                return

        # akshare 增量更新
        ak_dates = self._fetch_akshare()
        if ak_dates and len(ak_dates) > 100:
            new_dates = ak_dates - cached
            self._bulk_insert(pool, new_dates)
            self._dates = frozenset(cached | ak_dates)
            self._loaded_at = datetime.now(TZ_SH)
            return

        # akshare 不行 → DB kline 反推（只内存，不写缓存）
        if cached:
            db_dates = self._deduce_from_kline(pool)
            if db_dates:
                self._dates = frozenset(cached | db_dates)
            else:
                self._dates = frozenset(cached)
            self._loaded_at = datetime.now(TZ_SH)
            return

        # 缓存空 + akshare 挂 → DB 反推（不写缓存）
        db_dates = self._deduce_from_kline(pool)
        if not db_dates:
            raise RuntimeError("交易日历构建失败：缓存为空、akshare 无数据、数据库也反推不出")
        self._dates = frozenset(db_dates)
        self._loaded_at = datetime.now(TZ_SH)

    # ── DB 操作 ──

    def _get_pool(self):
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        return mgr._get_pool(self._market)

    def _init_cache_table(self, pool):
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trading_day_cache (
                    trade_date  VARCHAR(10) PRIMARY KEY,
                    updated_at  TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()

    def _load_from_db(self, pool) -> set[str]:
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date FROM trading_day_cache"
            )
            rows = cur.fetchall()
        return {r[0] for r in rows}

    def _bulk_insert(self, pool, dates: set[str]):
        if not dates:
            return
        with pool.connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO trading_day_cache (trade_date) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                [(d,) for d in dates]
            )
            conn.commit()

    # ── 数据源 ──

    @staticmethod
    def _fetch_akshare() -> set[str]:
        from check_continuity import _fetch_trading_days_from_akshare
        return _fetch_trading_days_from_akshare()

    @staticmethod
    def _deduce_from_kline(pool) -> set[str]:
        from check_continuity import _deduce_trading_days_from_db
        return _deduce_trading_days_from_db(pool)


# ── 全局单例 ──
_cache = _TradingDayCache()


# ── 对外接口 ──

def is_trading_day(d: str) -> bool:
    """判断日期是否为交易日（字符串格式 YYYY-MM-DD）"""
    return d in _cache.get()


def trading_days_between(d1: str, d2: str) -> int:
    """计算两个日期之间的交易日数量（不含 d1，含 d2 前一天）"""
    if d1 >= d2:
        return 0
    dates = _cache.get()
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    count = 0
    while cur < end:
        if cur.strftime("%Y-%m-%d") in dates:
            count += 1
        cur += timedelta(days=1)
    return count


def get_trading_day_set() -> frozenset[str]:
    """获取完整交易日集合（慎用，数据量大）"""
    return _cache.get()


def refresh():
    """强制刷新缓存（下次访问时生效）"""
    with _cache._lock:
        _cache._loaded_at = None
