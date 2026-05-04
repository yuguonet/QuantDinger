"""
港股/H股数据源 — Coordinator 统一调度

架构:
  get_ticker()      → Coordinator race 模式（自动从 Provider 层发现源）
  get_kline()       → Coordinator 动态队列（自动从 Provider 层发现源）

数据源:
  由 Coordinator 从 Provider 层自动发现（@register 注册的所有源），
  按 quote_priority / kline_priority 排序。

Provider 层港股源:
  tencent   (priority=10)  ← 首选，国内直连
  hk_stock  (priority=40)  ← 备选，含海外源降级
  akshare   (priority=50)  ← 兜底
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.normalizer import normalize_hk_code
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.cache_manager import (
    get_realtime_cache,
    get_kline_cache,
    generate_kline_cache_key,
)
from app.data_sources.coordinator import get_coordinator
from app.config.data_sources import DataSourceConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_timeout() -> float:
    """统一获取超时配置"""
    return float(DataSourceConfig.DEFAULT_TIMEOUT or 10)


class HKStockDataSource(BaseDataSource):
    """港股/H股数据源 — Coordinator 自动发现 + race 模式"""

    name = "HKStock/multi-source"

    def __init__(self):
        self.cb = get_realtime_circuit_breaker()
        self.realtime_cache = get_realtime_cache()
        self.kline_cache = get_kline_cache()

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新报价 — Coordinator 从 Provider 层自动发现源，race 模式"""
        code = normalize_hk_code(symbol)

        # 先检查缓存
        cache_key = f"ticker:{code}"
        cached = self.realtime_cache.get(cache_key)
        if cached:
            return cached

        # 交给 Coordinator（自动从 Provider 层发现源，race 模式）
        result = get_coordinator().coordinate_ticker(
            symbol=code,
            cb=self.cb,
            market="HKStock",
            timeout=min(_get_timeout(), 8),
        )

        if result:
            self.realtime_cache.set(cache_key, result, ttl=600)
            return result

        logger.warning(f"[港股行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": code}

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线 — Coordinator 从 Provider 层自动发现源并调度"""
        code = normalize_hk_code(symbol)
        tf = timeframe
        lim = max(int(limit or 300), 1)

        # 先检查缓存
        cache_key = generate_kline_cache_key(code, tf, lim, before_time)
        cached = self.kline_cache.get(cache_key)
        if cached:
            return cached

        # 交给 Coordinator（自动从 Provider 层发现源）
        results, failed = get_coordinator().coordinate_kline(
            symbols=[code],
            timeframe=tf,
            limit=lim,
            cb=self.cb,
            market="HKStock",
            timeout=_get_timeout() + 5,
        )

        bars = results.get(code)
        if not bars:
            logger.warning(f"[港股K线终止] {symbol} tf={tf} 所有数据源失败")
            return []

        # 过滤 + 截断
        out = self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

        # 写入缓存
        kline_ttl = 300.0 if tf in ("1D", "1W") else 120.0
        self.kline_cache.set(cache_key, out, ttl=kline_ttl)

        logger.info(f"[港股K线成功] {symbol} tf={tf} bars={len(out)}")
        return out
