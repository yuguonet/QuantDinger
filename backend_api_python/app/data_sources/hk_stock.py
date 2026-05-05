# -*- coding: utf-8 -*-
"""
港股/H股数据源 — 直接调用 Provider 层

架构:
  get_ticker()      → 逐源尝试 fetch_ticker，第一个成功的直接返回
  get_kline()       → 逐源尝试 fetch_kline，第一个成功的直接返回

数据源:
  从 Provider 层按 HKStock market 自动发现，按 priority 排序。
  tencent   (priority=10)  ← 首选，国内直连
  akshare   (priority=50)  ← 兜底
"""

from __future__ import annotations

import time
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.normalizer import normalize_hk_code
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.cache_manager import (
    get_realtime_cache,
    get_kline_cache,
    generate_kline_cache_key,
)
from app.data_sources.provider import get_providers
from app.data_sources.source_config import get_source_config
from app.config.data_sources import DataSourceConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_timeout() -> float:
    """统一获取超时配置"""
    return float(DataSourceConfig.DEFAULT_TIMEOUT or 10)


class HKStockDataSource(BaseDataSource):
    """港股/H股数据源 — 直接调用 Provider，不经过 Coordinator"""

    name = "HKStock/direct"

    def __init__(self):
        self.cb = get_realtime_circuit_breaker()
        self.realtime_cache = get_realtime_cache()
        self.kline_cache = get_kline_cache()

    def _get_hk_providers(self, capability: str = "kline"):
        """获取支持港股的 Provider 列表（按 priority 排序，过滤已熔断的）"""
        providers = get_providers(
            capability=capability,
            market="HKStock",
        )
        return [p for p in providers if self.cb.is_available(p.name)]

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新报价 — 逐源尝试，第一个成功的直接返回"""
        code = normalize_hk_code(symbol)

        # 先检查缓存
        cache_key = f"ticker:{code}"
        cached = self.realtime_cache.get(cache_key)
        if cached:
            return cached

        providers = self._get_hk_providers(capability="quote")
        if not providers:
            logger.warning("[港股行情] 无可用 Provider: %s", symbol)
            return {"last": 0, "symbol": code}

        for p in providers:
            try:
                start = time.time()
                result = p.fetch_ticker(code)
                elapsed = time.time() - start

                if result and ("last" in result or "price" in result):
                    self.cb.record_success(p.name)
                    cfg = get_source_config(p.name)
                    cfg.record(True, elapsed)
                    self.realtime_cache.set(cache_key, result, ttl=600)
                    logger.info("[港股行情] %s 命中 %s", symbol, p.name)
                    return result
                else:
                    self.cb.record_failure(p.name, "empty")
                    cfg = get_source_config(p.name)
                    cfg.record(False, elapsed)
            except Exception as e:
                self.cb.record_failure(p.name, str(e))
                cfg = get_source_config(p.name)
                cfg.record(False, 0)
                logger.debug("[港股行情] %s %s 失败: %s", p.name, symbol, e)

        logger.warning("[港股行情] 所有 Provider 均失败: %s", symbol)
        return {"last": 0, "symbol": code}

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线 — 逐源尝试，第一个成功的直接返回"""
        code = normalize_hk_code(symbol)
        tf = timeframe
        lim = max(int(limit or 300), 1)

        # 先检查缓存
        cache_key = generate_kline_cache_key(code, tf, lim, before_time)
        cached = self.kline_cache.get(cache_key)
        if cached:
            return cached

        providers = self._get_hk_providers(capability="kline")
        if not providers:
            logger.warning("[港股K线] 无可用 Provider: %s", symbol)
            return []

        bars = None
        for p in providers:
            try:
                start = time.time()
                result = p.fetch_kline(code, tf, lim)
                elapsed = time.time() - start

                if result:
                    self.cb.record_success(p.name)
                    cfg = get_source_config(p.name)
                    cfg.record(True, elapsed)
                    bars = result
                    logger.info("[港股K线] %s tf=%s 命中 %s bars=%d",
                               symbol, tf, p.name, len(bars))
                    break
                else:
                    self.cb.record_failure(p.name, "empty")
                    cfg = get_source_config(p.name)
                    cfg.record(False, elapsed)
            except Exception as e:
                self.cb.record_failure(p.name, str(e))
                cfg = get_source_config(p.name)
                cfg.record(False, 0)
                logger.debug("[港股K线] %s %s 失败: %s", p.name, symbol, e)

        if not bars:
            logger.warning("[港股K线终止] %s tf=%s 所有 Provider 失败", symbol, tf)
            return []

        # 过滤 + 截断
        out = self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

        # 写入缓存
        kline_ttl = 300.0 if tf in ("1D", "1W") else 120.0
        self.kline_cache.set(cache_key, out, ttl=kline_ttl)

        logger.info("[港股K线成功] %s tf=%s bars=%d", symbol, tf, len(out))
        return out
