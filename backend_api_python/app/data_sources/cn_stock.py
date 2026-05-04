"""
中国A股数据源 — Coordinator 统一调度

架构: 
  get_ticker()      → Coordinator race 模式（并发，第一个成功的返回）
  get_kline()       → Coordinator 动态队列（单只），自动从 Provider 层发现源
  get_kline_batch() → Coordinator 动态队列（批量），月线走日线聚合

数据源:
  由 Coordinator 从 Provider 层自动发现（@register 注册的所有源），
  按 kline_priority 排序，支持 preferred_source 指定源。
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.normalizer import (
    normalize_cn_code,
)
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
)
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


# ================================================================
# 超时封装工具
# ================================================================

_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="cnstock-timeout",
)


def _get_timeout() -> float:
    """统一获取超时配置"""
    return float(DataSourceConfig.DEFAULT_TIMEOUT or 10)


def _fetch_with_timeout(
    func: Callable,
    *args,
    timeout: Optional[float] = None,
    source_name: str = "",
    **kwargs,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    在独立线程中执行 func，超时后放弃。

    Returns:
        (result, error)  —— result 非 None 表示成功，error 非 None 表示失败原因。
    """
    if timeout is None:
        timeout = _get_timeout()

    future = _TIMEOUT_EXECUTOR.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
        return result, None
    except concurrent.futures.TimeoutError:
        logger.warning(f"[超时] {source_name} 调用超时 ({timeout}s)")
        future.cancel()
        return None, f"{source_name} timeout ({timeout}s)"
    except Exception as e:
        logger.warning(f"[异常] {source_name} 调用失败: {e}")
        return None, f"{source_name} error: {e}"


# ================================================================
# K线数据校验
# ================================================================

def _validate_kline_result(bars: List[Dict[str, Any]], min_bars: int = 1) -> bool:
    """
    校验K线数据基本合理性。
    返回 True 表示数据可用，False 表示应丢弃。
    """
    if not bars or len(bars) < min_bars:
        return False

    last = bars[-1]
    if not isinstance(last, dict):
        return False

    t = last.get("time")
    if not t or not isinstance(t, (int, float)) or t <= 0:
        return False

    if last.get("close", 0) <= 0:
        return False

    h, low = last.get("high", 0), last.get("low", 0)
    if h > 0 and low > 0 and h < low:
        return False

    return True


def _strip_cn_prefix(code: str) -> str:
    """
    安全剥离 A股代码的 SH/SZ/BJ 前缀。
    """
    s = (code or "").strip()
    upper = s.upper()
    if upper.startswith(("SH", "SZ", "BJ")) and len(s) >= 3:
        return s[2:]
    return s


# ================================================================
# 日线聚合辅助（月线批量拉取用）
# ================================================================

def _month_start_from_dt(dt: datetime) -> int:
    """计算给定 datetime 所在月初的 Unix 时间戳"""
    return int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def _aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """将日线聚合为月线"""
    if not daily_bars:
        return []
    bars = sorted(daily_bars, key=lambda x: x.get("time", 0))
    groups: Dict[int, List[Dict]] = {}
    order: List[int] = []
    for bar in bars:
        t = bar.get("time", 0)
        if not t:
            continue
        dt = datetime.fromtimestamp(t, tz=timezone(timedelta(hours=8)))
        ms = _month_start_from_dt(dt)
        if ms not in groups:
            groups[ms] = []
            order.append(ms)
        groups[ms].append(bar)
    result = []
    for ms in order:
        chunk = groups[ms]
        if not chunk:
            continue
        result.append({
            "time": ms,
            "open": float(chunk[0].get("open", 0)),
            "high": max(float(b.get("high", 0)) for b in chunk),
            "low": min(float(b.get("low", 0)) for b in chunk),
            "close": float(chunk[-1].get("close", 0)),
            "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — Coordinator 动态队列 + 自动源发现"""

    name = "CNStock/multi-source"

    def __init__(self):
        self.circuit_breaker = get_realtime_circuit_breaker()
        self.realtime_cache = get_realtime_cache()
        self.kline_cache = get_kline_cache()

    # ----------------------------------------------------------
    # 实时行情 / 报价（串行 fallback，不走 Coordinator）
    # ----------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新报价 — Coordinator 从 Provider 层自动发现源，race 模式"""
        code = normalize_cn_code(symbol)

        # 先检查缓存
        cache_key = f"ticker:{code}"
        cached = self.realtime_cache.get(cache_key)
        if cached:
            return cached

        # 交给 Coordinator（自动从 Provider 层发现源，race 模式）
        result = get_coordinator().coordinate_ticker(
            symbol=code,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=min(_get_timeout(), 8),
        )

        if result:
            self.realtime_cache.set(cache_key, result, ttl=600)
            return result

        logger.warning(f"[行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": code}

    # ----------------------------------------------------------
    # 批量当日行情（供 K 线服务合成当日 K 线用）
    # ----------------------------------------------------------

    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量获取当日行情 — 透传 Provider 层 batch_quote 接口"""
        from app.data_sources.provider import get_providers
        providers = get_providers(capability="batch_quote", market="CNStock")
        if not providers:
            return {}
        # 按优先级逐源尝试（passthrough 透传，失败自动降级）
        for p in providers:
            result = get_coordinator().passthrough(p.fetch_quotes_batch, symbols)
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
    ) -> List[Dict[str, Any]]:
        """获取单只股票 K 线 — Coordinator 从 Provider 层自动发现源并调度"""
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # 先检查缓存
        cache_key = generate_kline_cache_key(code, tf, lim, before_time)
        cached = self.kline_cache.get(cache_key)
        if cached:
            return cached

        # 交给 Coordinator（自动从 Provider 层发现源，无需手动构建 sources）
        results, failed = get_coordinator().coordinate_kline(
            symbols=[code],
            timeframe=tf,
            limit=lim,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=_get_timeout() + 5,
        )

        bars = results.get(code)
        if not bars:
            logger.warning(f"[K线终止] {symbol} tf={tf} 所有数据源失败")
            return []

        # 过滤 + 截断
        out = self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

        # 写入缓存
        kline_ttl = 300.0 if tf in ("1D", "1W") else 120.0
        self.kline_cache.set(cache_key, out, ttl=kline_ttl)

        logger.info(f"[K线成功] {symbol} tf={tf} bars={len(out)}")
        return out

    def get_kline_batch(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        cached_symbols: Optional[set] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量获取多只股票 K 线 — Coordinator 自动源发现 + 动态队列。
        月线先走日线批量再聚合。
        """
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        result: Dict[str, List[Dict[str, Any]]] = {}

        # ── 月线：走日线批量 + 聚合 ──
        if tf == "1M":
            daily_limit = min(limit * 21 + 100, 5000)
            daily_result = self.get_kline_batch(
                symbols, "1D", daily_limit, cached_symbols=cached_symbols,
            )
            for sym, daily_bars in daily_result.items():
                if daily_bars:
                    result[sym] = _aggregate_daily_to_monthly(daily_bars, limit)
            return result

        # ── 分离已缓存 / 未缓存 ──
        if cached_symbols is None:
            cached_symbols = set()

        cached_normalized = {normalize_cn_code(s) for s in cached_symbols}
        uncached = [s for s in symbols if normalize_cn_code(s) not in cached_normalized]
        already_cached = [s for s in symbols if normalize_cn_code(s) in cached_normalized]

        # 已缓存的直接读
        for sym in already_cached:
            cached = self.kline_cache.get(
                generate_kline_cache_key(normalize_cn_code(sym), tf, limit, None)
            )
            if cached:
                result[sym] = cached

        if not uncached:
            return result

        # ── 未缓存的交给 Coordinator（自动源发现）──
        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[normalize_cn_code(s) for s in uncached],
            timeframe=tf,
            limit=limit,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=_get_timeout() + 10,
        )

        # 写入缓存 + 合并结果
        for sym, bars in coord_results.items():
            key = generate_kline_cache_key(sym, tf, limit, None)
            kline_ttl = 300.0 if tf in ("1D", "1W") else 120.0
            self.kline_cache.set(key, bars, ttl=kline_ttl)
            result[sym] = bars

        if failed:
            logger.warning(f"[K线批量] {len(failed)} 只失败: {failed[:5]}...")

        return result
