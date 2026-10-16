# -*- coding: utf-8 -*-
"""
港股/H股数据源 — 通过 Coordinator 统一调度 Provider 层

架构:
  get_ticker()  → coordinator.coordinate_ticker(market="HKStock")  — 多源 Race 抢答
  get_kline()   → coordinator.coordinate_kline(market="HKStock")   — 多源顺序降级

为什么走 Coordinator（而不是像旧版直接循环 Provider）:
  1. 源发现/优先级/熔断/超时都在协作层统一处理，与 A 股链路同构，避免两套逻辑漂移
  2. 熔断器与 A 股共用 _realtime_cb，源的故障状态全局一致（旧版用了另一个同名熔断器实例，
     港股侧熔断后 A 股侧仍认为该源健康）
  3. 旧版依赖的 app.data_sources.cache_manager 在当前代码库已不存在（导入即崩）

Provider 现状（markets 含 HKStock 的源）:
  tencent (priority=10) — 当前唯一支持港股的 Provider，后续新增源自动纳入，无需改本文件

缓存:
  本模块内置轻量 TTL 缓存（替代已移除的 cache_manager）：
    行情 600s / 日线周线 300s / 分钟线 120s
  key 含 timeframe + limit + before_time，避免不同窗口互相污染。

数据格式:
  get_kline 返回 time 为 Unix 秒（int）的升序列表 — 由 BaseDataSource.filter_and_limit 归一化，
  与 cn_stock/us_stock 等市场数据源保持一致。
"""

from __future__ import annotations

import threading
import time as _time
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.coordinator import get_coordinator
from app.data_sources.normalizer import normalize_hk_code
from app.config.data_sources import DataSourceConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 缓存 TTL（秒）
_TICKER_TTL = 600.0
_KLINE_TTL_DAILY = 300.0
_KLINE_TTL_MINUTE = 120.0


def _get_timeout() -> float:
    """统一获取超时配置"""
    return float(DataSourceConfig.DEFAULT_TIMEOUT or 10)


class _TTLCache:
    """极简线程安全 TTL 缓存（键 → (写入时间, 值)）。

    仅用于港股单只行情/K线的短时复用；容量上限保护内存，超限淘汰最旧。
    """

    def __init__(self, max_size: int = 512):
        self._max_size = max_size
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if ttl > 0 and (_time.time() - ts) > ttl:
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (_time.time(), value)
            if len(self._data) > self._max_size:
                oldest = sorted(self._data, key=lambda k: self._data[k][0])
                for k in oldest[: len(self._data) - self._max_size]:
                    self._data.pop(k, None)


_cache = _TTLCache()


class HKStockDataSource(BaseDataSource):
    """港股/H股数据源 — 经 Coordinator 调度 Provider 层"""

    name = "HKStock/coordinator"

    def __init__(self):
        self.coordinator = get_coordinator()

    # ── 单只行情 ──────────────────────────────────────────────

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新报价 — Coordinator Race 抢答，失败返回占位行情。"""
        code = normalize_hk_code(symbol)
        if not code:
            return {"last": 0, "symbol": symbol}

        cached = _cache.get("ticker:%s" % code, _TICKER_TTL)
        if cached:
            return dict(cached)

        quote = self.coordinator.coordinate_ticker(
            symbol=code,
            market="HKStock",
            timeout=_get_timeout(),
        )
        if quote and (quote.get("last") or quote.get("price")):
            _cache.set("ticker:%s" % code, quote)
            return quote

        logger.warning("[港股行情] 所有源失败: %s", symbol)
        return {"last": 0, "symbol": code}

    # ── K 线 ─────────────────────────────────────────────────

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线 — Coordinator 多源顺序降级，返回 time 为 Unix 秒的升序列表。"""
        code = normalize_hk_code(symbol)
        if not code:
            return []
        lim = max(int(limit or 300), 1)
        cache_key = "kline:%s:%s:%d:%s" % (code, timeframe, lim, before_time or "")

        ttl = _KLINE_TTL_DAILY if timeframe in ("1D", "1W") else _KLINE_TTL_MINUTE
        cached = _cache.get(cache_key, ttl)
        if cached:
            return [dict(b) for b in cached]

        result = self.coordinator.coordinate_kline(
            symbol=code,
            timeframe=timeframe,
            limit=lim,
            market="HKStock",
            timeout=_get_timeout(),
        )
        bars = (result or {}).get("bars") or []
        if not bars:
            logger.warning("[港股K线] 所有源失败: %s tf=%s", symbol, timeframe)
            return []

        # 归一化 time 为 Unix 秒 + 时间窗过滤 + 截断
        out = self.filter_and_limit(
            [dict(b) for b in bars],
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )
        if out:
            _cache.set(cache_key, out)
        logger.info("[港股K线] %s tf=%s 命中 %s bars=%d",
                    symbol, timeframe, result.get("source", "?"), len(out))
        return out
