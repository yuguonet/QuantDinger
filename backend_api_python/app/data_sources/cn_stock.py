"""
中国A股数据源 — Coordinator 统一调度

架构: 
  get_ticker()      → Coordinator race 模式（并发，第一个成功的返回）
  get_kline()       → Coordinator 动态队列（单只/批量），自动从 Provider 层发现源

数据源:
  由 Coordinator 从 Provider 层自动发现（@register 注册的所有源），
  按 kline_priority 排序，支持 preferred_source 指定源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.normalizer import normalize_cn_code
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.coordinator import get_coordinator
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — Coordinator 动态队列 + 自动源发现"""

    name = "CNStock/multi-source"

    def __init__(self):
        self.circuit_breaker = get_realtime_circuit_breaker()

    # ----------------------------------------------------------
    # 实时行情 / 报价（串行 fallback，不走 Coordinator）
    # ----------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取实时行情

        支持单只和批量两种调用方式:
          单只: symbol="600519"
            → 返回 {"last": 1800.0, "change": 15.0, "changePercent": 0.84, "high": ..., "low": ..., "name": "贵州茅台", ...}

          批量: symbol="600519,000001,000690"
            → 返回 {"600519": {"last": ..., ...}, "000001": {"last": ..., ...}, ...}

        单只实现:
          1. Coordinator race 模式（并发请求多个 Provider，第一个成功的返回）
          2. 全部失败 → {"last": 0, "symbol": code}

        批量实现:
          1. 透传 Provider 层 batch_quote 接口（腾讯/新浪一次 HTTP 拿多只）
          2. 按 Provider 优先级逐源尝试，失败自动降级
          3. 无 Provider 可用 → 返回 {}

        Args:
            symbol: 股票代码，多只用逗号分隔（如 "600519,000001"）

        Returns:
            单只: {"last", "change", "changePercent", "high", "low", "open", "previousClose", "name", "symbol", ...}
            批量: {symbol: {"last", "change", ...}, ...}
        """
        # ── 批量模式：逗号分隔 ──
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return {"last": 0, "symbol": symbol}
            return self._get_tickers(symbols)

        # ── 单只模式 ──
        code = normalize_cn_code(symbol)

        # 交给 Coordinator（自动从 Provider 层发现源，race 模式）
        result = get_coordinator().coordinate_ticker(
            symbol=code,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=8,
        )

        if result:
            return result

        logger.warning(f"[行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": code}

    # ----------------------------------------------------------
    # 批量当日行情（供 K 线服务合成当日 K 线用）
    # ----------------------------------------------------------

    def _get_tickers(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量获取实时行情（ticker 格式）— 透传 Provider 层 batch_quote 接口"""
        from app.data_sources.provider import get_providers
        normalized = [normalize_cn_code(s) for s in symbols if s]
        if not normalized:
            return {}
        providers = get_providers(capability="batch_quote", market="CNStock")
        if not providers:
            return {}
        # 按优先级逐源尝试（passthrough 透传，失败自动降级）
        for p in providers:
            result = get_coordinator().passthrough(p.fetch_quotes_batch, normalized)
            if result:
                return result
        return {}

    # ----------------------------------------------------------
    # K线数据 — 统一走 Coordinator（自动源发现）
    # ----------------------------------------------------------

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """
        获取 K 线数据

        支持单只和批量两种调用方式:
          单只: symbol="600519"
            → 返回 [{"time": 1700000000, "open": 1795.0, "high": 1810.0, "low": 1790.0, "close": 1800.0, "volume": 12345}, ...]

          批量: symbol="600519,000001,000690"
            → 返回 {"600519": [bar, ...], "000001": [bar, ...], ...}

        单只实现:
          1. 委托 _get_klines（走 Coordinator）
          2. 过滤 + 截断（before_time / after_time）
          3. 全部失败 → 返回 []

        批量实现:
          1. 委托 _get_klines（Coordinator 动态队列）
          2. 部分失败时，成功的仍返回

        Args:
            symbol: 股票代码，多只用逗号分隔（如 "600519,000001"）
            timeframe: 时间周期（"1D", "1W", "1M", "5m", "15m", "30m", "60m" 等）
            limit: 数据条数
            before_time: 获取此时间之前的数据（Unix 秒）
            after_time: K 线 time 需 >= 此值（回测左边界，Unix 秒）
            adj: 复权方式 — "qfq"(前复权,默认) / "hfq"(后复权) / ""(不复权)

        Returns:
            单只: [bar, ...] — 每个 bar 包含 {"time", "open", "high", "low", "close", "volume"}
            批量: {symbol: [bar, ...], ...}
        """
        # ── 批量模式：逗号分隔 ──
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return []
            return self._get_klines(symbols, timeframe, limit, adj=adj)

        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # 单只 → 走批量，取一个结果
        result = self._get_klines([symbol], tf, lim, adj=adj)
        bars = result.get(code, [])

        if not bars:
            logger.warning(f"[K线终止] {symbol} tf={tf} 所有数据源失败")
            return []

        # 过滤 + 截断
        out = self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

        logger.info(f"[K线成功] {symbol} tf={tf} bars={len(out)}")
        return out

    def _get_klines(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        adj: str = "qfq",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量获取多只股票 K 线 — Coordinator 自动源发现 + 动态队列。

        Args:
            adj: 复权方式 — "qfq"(前复权,默认) / "hfq"(后复权) / ""(不复权)
        """
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        result: Dict[str, List[Dict[str, Any]]] = {}

        # ── 交给 Coordinator（自动源发现，传递 adj）──
        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[normalize_cn_code(s) for s in symbols],
            timeframe=tf,
            limit=limit,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        # 合并结果
        for sym, bars in coord_results.items():
            result[sym] = bars

        if failed:
            logger.warning(f"[K线批量] {len(failed)} 只失败: {failed[:5]}...")

        return result
