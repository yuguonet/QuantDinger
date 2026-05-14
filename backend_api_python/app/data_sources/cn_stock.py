"""
中国A股数据源

═══════════════════════════════════════════════════════════════
  全远程架构: 所有 K 线数据从 Coordinator 多源获取，不依赖本地 DB
═══════════════════════════════════════════════════════════════

设计原则:
  - 所有周期（1m ~ 1M）统一走 Coordinator 远程拉取
  - 不维护本地 DB，不做周期聚合
  - ticker 实时行情由 Coordinator 统一调度
  - 符号规范化由 Coordinator 统一处理（入口加前缀，出口去前缀）

职责:
  get_ticker()  → 从 Coordinator 取实时行情
  get_kline()   → 从 Coordinator 取 K 线，支持单只/批量
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.coordinator import get_coordinator
from app.data_sources.kline_clean import clean_klines
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — 全远程架构。

    所有 K 线数据通过 Coordinator 从多个远程源获取，
    不依赖本地 DB，不做周期聚合。
    """

    name = "CNStock/multi-source"

    # ── get_ticker: 实时行情 ──

    def get_ticker(self, symbol) -> Dict[str, Any]:
        """获取实时行情。支持单股 / 逗号拼接 / List[str]，均由 Coordinator 统一调度。"""
        if isinstance(symbol, list):
            symbols = [s.strip() for s in symbol if s and s.strip()]
        elif isinstance(symbol, str) and ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
        else:
            symbols = [symbol] if symbol else []

        if not symbols:
            return {"last": 0, "symbol": ""}

        result = get_coordinator().coordinate_ticker(
            symbols=symbols,
            market="CNStock",
            timeout=8,
        )

        if not result:
            logger.warning(f"[行情] 所有数据源均失败: {symbol}")
            return {"last": 0, "symbol": symbols[0] if symbols else ""}

        # 单股模式：返回第一个匹配
        if len(symbols) == 1:
            key = symbols[0]
            if key in result:
                quote = result[key]
                quote["symbol"] = key
                return quote
            logger.warning(f"[行情] 所有数据源均失败: {symbol}")
            return {"last": 0, "symbol": key}

        # 批量模式：返回整个 map
        return result

    # ── get_kline: K 线数据 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。支持逗号分隔的批量模式。"""
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return []
            batch = self._get_klines_remote(symbols, timeframe, limit)
            merged = []
            for sym in symbols:
                merged.extend(batch.get(sym, []))
            return self.filter_and_limit(
                merged, limit=len(merged),
                before_time=before_time, after_time=after_time,
                truncate=False,
            )

        return self._get_kline_remote(
            symbol, timeframe, limit, before_time, after_time
        )

    # ── 远程拉取（Coordinator 调度）──

    def _get_kline_remote(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """单只远程拉取。"""
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[symbol],
            timeframe=tf,
            limit=lim,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        bars = coord_results.get(symbol, [])

        if bars:
            bars = clean_klines(bars, tf)

        return self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

    def _get_klines_remote(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        adj: str = "qfq",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量远程拉取。"""
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        logger.info(
            f"[远程批量] 开始拉取 tf={tf} limit={lim} adj={adj} "
            f"标的数={len(symbols)} 示例={symbols[:3]}"
        )

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=symbols,
            timeframe=tf,
            limit=lim,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        result: Dict[str, List[Dict[str, Any]]] = {}
        total_bars = 0
        for sym, bars in coord_results.items():
            cleaned = clean_klines(bars, tf)
            result[sym] = cleaned
            total_bars += len(cleaned)

        if failed:
            logger.warning(f"[远程批量] {len(failed)}/{len(symbols)} 只失败: {failed[:10]}")

        logger.info(
            f"[远程批量] 完成 tf={tf} "
            f"成功={len(coord_results)}/{len(symbols)} "
            f"失败={len(failed)} 总bar数={total_bars}"
        )

        return result
