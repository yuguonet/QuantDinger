# -*- coding: utf-8 -*-
"""
DataSourceFactory — 去重 / 复权 / 市场解析 / 统一入口

定位:
  KlineService(缓存层) → DataSourceFactory(本层) → Coordinator(调度层)

职责:
  1. 请求去重: InflightDedup 防止同一 symbol 并发重复请求
  2. 复权:     统一调用 adjust_kline 处理前/后复权
  3. 市场解析: resolve_market / normalize_symbols
  4. 统一入口: 所有外部调用方只与本层交互

不负责:
  - 调度策略（fallback / race / 并发）→ 委托给 Coordinator
  - 直接与 Provider 通信 → 委托给 Coordinator
  - 缓存读写（由 KlineService 处理）

调用链:
  单只K线:  Factory.fetch_kline → dedup → Coordinator.fetch_single_kline → adjust
  批量K线:  Factory.fetch_kline_batch → Coordinator.coordinate_kline → adjust
  单只行情: Factory.fetch_ticker → dedup → Coordinator.fetch_single_ticker
  批量行情: Factory.fetch_ticker_batch → Coordinator.fetch_batch_ticker
"""

from __future__ import annotations

import threading
import concurrent.futures
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from app.data_sources.adjustment import adjust_kline
from app.data_sources.circuit_breaker import CircuitBreaker, get_realtime_circuit_breaker
from app.data_sources.coordinator import get_coordinator
from app.data_sources.normalizer import detect_market, to_canonical, normalize_hk_code
from app.data_sources.provider import get_providers
from app.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


# ================================================================
# 请求去重 — 同一 symbol 正在取时，等结果不重复发
# ================================================================

class InflightDedup:
    """
    请求去重器。

    如果 symbol A 正在被某个线程取数据，其他线程对 A 的请求
    直接等结果，不重复发 API 调用。
    """

    def __init__(self, max_workers: int = 4):
        self._lock = threading.Lock()
        self._inflight: Dict[str, concurrent.futures.Future] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="dedup",
        )

    def get_or_submit(self, key: str, fn: Callable[[], T]) -> T:
        with self._lock:
            if key in self._inflight:
                future = self._inflight[key]
            else:
                future = self._executor.submit(fn)
                self._inflight[key] = future

        try:
            return future.result(timeout=30)
        finally:
            with self._lock:
                if key in self._inflight and self._inflight[key] is future:
                    del self._inflight[key]


# ================================================================
# 市场类型
# ================================================================

MARKET_CN_STOCK = "CNStock"
MARKET_HK_STOCK = "HKStock"
MARKET_US_STOCK = "USStock"
MARKET_CRYPTO   = "Crypto"
MARKET_FOREX    = "Forex"
MARKET_FUTURES  = "Futures"

_MARKET_ALIASES = {
    "CNStock":  MARKET_CN_STOCK,
    "HKStock":  MARKET_HK_STOCK,
    "USStock":  MARKET_US_STOCK,
    "Crypto":   MARKET_CRYPTO,
    "Forex":    MARKET_FOREX,
    "Futures":  MARKET_FUTURES,
    "CN":       MARKET_CN_STOCK,
    "HK":       MARKET_HK_STOCK,
    "US":       MARKET_US_STOCK,
    "A":        MARKET_CN_STOCK,
    "A股":      MARKET_CN_STOCK,
    "港股":     MARKET_HK_STOCK,
    "美股":     MARKET_US_STOCK,
    "加密":     MARKET_CRYPTO,
    "外汇":     MARKET_FOREX,
    "期货":     MARKET_FUTURES,
}


def _resolve_market(market: str, symbol: str) -> str:
    if market:
        return _MARKET_ALIASES.get(market, market)

    s = (symbol or "").strip().upper()
    if "/" in s:
        return MARKET_CRYPTO

    exchange, _ = detect_market(symbol)
    if exchange in ("SH", "SZ", "BJ"):
        return MARKET_CN_STOCK
    if exchange == "HK":
        return MARKET_HK_STOCK

    return ""


def _parse_symbols(symbol: str) -> List[str]:
    return [s.strip() for s in symbol.split(",") if s.strip()]


def _normalize_symbols(symbols: List[str], market: str) -> List[str]:
    result = []
    for sym in symbols:
        if market == "HKStock":
            result.append(normalize_hk_code(sym))
        else:
            canon = to_canonical(sym)
            result.append(canon if canon else sym)
    return result


# ================================================================
# DataSourceFactory — 去重 / 复权 / 市场解析 / 统一入口
# ================================================================

class DataSourceFactory:
    """
    数据源工厂层 — 统一入口。

    负责去重、复权、市场解析，调度全部委托 Coordinator。
    """

    def __init__(self):
        self._cb = get_realtime_circuit_breaker()
        self._dedup = InflightDedup()
        self._coordinator = get_coordinator()

    # ═══════════════════════════════════════════════════════════════════
    #  K线 — 单只
    # ═══════════════════════════════════════════════════════════════════

    def fetch_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        market: str,
        adj: str = "qfq",
    ) -> Optional[List[Dict[str, Any]]]:
        """获取单只K线 — 去重 + Coordinator fallback + 复权"""
        raw = self.fetch_kline_raw(symbol, timeframe, limit, market)
        if raw:
            return adjust_kline(symbol, raw, adj)
        return None

    def fetch_kline_raw(
        self, symbol: str, timeframe: str, limit: int, market: str,
    ) -> Optional[List]:
        """获取单只K线原始数据 — 去重 + Coordinator fallback，不复权"""
        return self._dedup.get_or_submit(
            f"kline:{symbol}:{timeframe}:{limit}",
            lambda: self._coordinator.fetch_single_kline(
                symbol, timeframe, limit, market, self._cb,
            )[0],
        )

    # ═══════════════════════════════════════════════════════════════════
    #  K线 — 批量
    # ═══════════════════════════════════════════════════════════════════

    def fetch_kline_batch(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        market: str,
        adj: str = "qfq",
        on_raw_data: Optional[Callable[[str, List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量K线 — Coordinator 并发协调 + 统一复权"""
        from app.data_sources.provider import get_providers as _gp
        providers = _gp("kline", timeframe=timeframe, market=market or None)

        fetched, failed = self._coordinator.coordinate_kline(
            symbols=symbols,
            timeframe=timeframe,
            limit=limit,
            providers=providers,
            cb=self._cb,
            market=market,
        )

        if failed:
            logger.warning(
                f"[批量K线] {len(failed)} 只所有源均失败: "
                f"{failed[:5]}{'...' if len(failed) > 5 else ''}"
            )

        result = {}
        for sym, bars in fetched.items():
            if bars:
                if on_raw_data is not None:
                    try:
                        on_raw_data(sym, bars)
                    except Exception as e:
                        logger.warning("[批量K线] on_raw_data 回调失败 %s: %s", sym, e)
                result[sym] = adjust_kline(sym, bars, adj)

        logger.info("[批量K线] DataSourceFactory: %d/%d 成功", len(result), len(symbols))
        return result

    # ═══════════════════════════════════════════════════════════════════
    #  行情 — 单只
    # ═══════════════════════════════════════════════════════════════════

    def fetch_ticker(
        self,
        symbol: str,
        market: str,
    ) -> Optional[Dict[str, Any]]:
        """获取单只行情 — 去重 + Coordinator race"""
        return self._dedup.get_or_submit(
            f"ticker:{symbol}",
            lambda: self._coordinator.fetch_single_ticker(
                symbol, market, self._cb,
            )[0],
        )

    # ═══════════════════════════════════════════════════════════════════
    #  行情 — 批量
    # ═══════════════════════════════════════════════════════════════════

    def fetch_ticker_batch(
        self,
        symbols: List[str],
        market: str,
    ) -> Dict[str, Dict[str, Any]]:
        """批量行情 — Coordinator race 批量接口 + 逐只 fallback"""
        return self._coordinator.fetch_batch_ticker(symbols, market, self._cb)

    # ═══════════════════════════════════════════════════════════════════
    #  公共工具
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def resolve_market(market: str, symbol: str) -> str:
        return _resolve_market(market, symbol)

    @staticmethod
    def parse_symbols(symbol: str) -> List[str]:
        return _parse_symbols(symbol)

    @staticmethod
    def normalize_symbols(symbols: List[str], market: str) -> List[str]:
        return _normalize_symbols(symbols, market)

    def source_stats(self) -> Dict[str, Any]:
        from app.data_sources.source_config import get_all_enabled_sources
        return {
            cfg.name: {
                "qps": round(cfg.throughput, 2),
                "success_rate": round(cfg.success_rate, 3),
                "avg_latency": round(cfg.avg_latency, 3),
                "effective_weight": round(cfg.effective_weight(), 2),
                "max_workers": cfg.max_workers,
                "markets": list(cfg.markets),
            }
            for cfg in get_all_enabled_sources()
        }


# ================================================================
# SourceAdapter — 兼容旧 BaseDataSource 接口
# ================================================================

class SourceAdapter:
    """适配器：包装 Factory，暴露旧 BaseDataSource 接口"""

    def __init__(self, market: str):
        self._market = market
        self._factory = get_factory()

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        raw = self._factory.fetch_kline_raw(symbol, timeframe, limit, self._market)
        if not raw:
            return []
        adjusted = adjust_kline(symbol, raw, "qfq")
        if after_time and adjusted:
            adjusted = [b for b in adjusted if b.get("time", 0) >= after_time]
        if before_time and adjusted:
            adjusted = [b for b in adjusted if b.get("time", 0) < before_time]
        adjusted.sort(key=lambda x: x.get("time", 0))
        return adjusted

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        result = self._factory.fetch_ticker(symbol, self._market)
        return result or {"last": 0, "symbol": symbol}


# ================================================================
# classmethod 兼容层
# ================================================================

def _cm_get_kline(
    cls, market: str, symbol: str, timeframe: str, limit: int,
    before_time: Optional[int] = None, after_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    source = SourceAdapter(market)
    return source.get_kline(symbol, timeframe, limit, before_time, after_time)


def _cm_get_kline_batch(
    cls, market: str, symbols: List[str], timeframe: str, limit: int,
    cached_symbols: Optional[set] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    resolved = _resolve_market(market, symbols[0] if symbols else "")
    return _factory.fetch_kline_batch(symbols, timeframe, limit, resolved)


def _cm_get_ticker(cls, market: str, symbol: str) -> Dict[str, Any]:
    resolved = _resolve_market(market, symbol)
    result = _factory.fetch_ticker(symbol, resolved)
    return result or {"last": 0, "symbol": symbol}


def _cm_get_source(cls, market: str) -> SourceAdapter:
    return SourceAdapter(market)


def _cm_get_data_source(cls, name: str) -> SourceAdapter:
    key = (name or "").strip().lower()
    market_map = {
        "crypto": "Crypto", "binance": "Crypto", "okx": "Crypto",
        "bybit": "Crypto", "bitget": "Crypto", "kucoin": "Crypto",
        "gate": "Crypto", "mexc": "Crypto", "kraken": "Crypto", "coinbase": "Crypto",
        "futures": "Futures", "forex": "Forex", "fx": "Forex",
        "cnstock": "CNStock", "hkstock": "HKStock", "usstock": "USStock",
    }
    market = market_map.get(key, "Crypto")
    return SourceAdapter(market)


DataSourceFactory.get_kline = classmethod(_cm_get_kline)           # type: ignore
DataSourceFactory.get_kline_batch = classmethod(_cm_get_kline_batch)  # type: ignore
DataSourceFactory.get_ticker = classmethod(_cm_get_ticker)         # type: ignore
DataSourceFactory.get_source = classmethod(_cm_get_source)         # type: ignore
DataSourceFactory.get_data_source = classmethod(_cm_get_data_source)  # type: ignore
DataSourceFactory.normalize_market = staticmethod(_resolve_market)  # type: ignore


# ================================================================
# 全局实例
# ================================================================

_factory = DataSourceFactory()


def get_factory() -> DataSourceFactory:
    """获取全局 DataSourceFactory 实例"""
    return _factory
