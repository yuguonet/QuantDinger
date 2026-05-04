# -*- coding: utf-8 -*-
"""
协助层 (Coordinator) — 数据源层与调度层之间的并发协调

定位:
  调度层(CNStockDataSource) → 协助层(Coordinator) → 数据源层(Provider)

核心职责:
  1. 动态队列: 源干完一个活立刻拿下一个，不闲着
  2. 并发控制: 每个源的并发数不超过其 max_workers 配置
  3. 吞吐跟踪: 记录每个源的实际 QPS，动态调整分配优先级
  4. 失败处理: 每个 symbol 最多被所有可用源各试一次，全部失败则放弃
  5. 指定源: 支持 preferred_source 直接指定数据源，快速失败后自动回退
  6. 源自动发现: 从 Provider 层按能力/周期/市场自动获取可用源

接口:
  # 自动发现模式（推荐）— Coordinator 从 Provider 层自动发现源
  coordinate_kline(symbols, timeframe, limit, cb, market="CNStock")

  # 手动指定模式（兼容旧调用）— 调用方传入源列表
  coordinate_kline(symbols, timeframe, limit, sources=[...], cb, market="CNStock")

  fetch_fn(symbol: str, timeframe: str, limit: int) -> List[Dict] | None
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.data_sources.source_config import (
    SourceConfig, get_source_config, get_sources_for_market, get_all_enabled_sources,
)
from app.data_sources.circuit_breaker import CircuitBreaker
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 单次请求的超时上限（秒），Coordinator 层兜底，防止 fetch 卡死
PER_TASK_TIMEOUT = 20.0

# 队列空时等待的超时（秒），超时后认为所有工作已完成
QUEUE_DRAIN_TIMEOUT = 3.0


# ================================================================
# Provider 适配器 — 将 Provider.fetch_kline 适配为 Coordinator 的 fetch_fn
# ================================================================

def _make_provider_fetch_fn(provider) -> Callable:
    """
    将 Provider 的 fetch_kline 方法适配为 Coordinator 期望的 fetch_fn 签名。

    Coordinator 期望: fetch_fn(symbol, timeframe, limit) -> List[Dict] | None
    Provider 提供:    provider.fetch_kline(code, timeframe, count) -> List[Dict] | NotSupportedResult

    适配逻辑:
      1. 调用 provider.fetch_kline
      2. 如果返回 NotSupportedResult（不支持该接口），返回 None（Coordinator 跳过）
      3. 如果返回空列表，返回 None（Coordinator 判定失败，尝试下一个源）
      4. 如果返回非空列表，直接返回

    Args:
        provider: Provider 实例（实现 BaseDataSource 协议）

    Returns:
        适配后的 fetch_fn(symbol, timeframe, limit) -> List[Dict] | None
    """
    def fetch_fn(symbol: str, timeframe: str, limit: int):
        try:
            result = provider.fetch_kline(symbol, timeframe, limit)
            # NotSupportedResult 布尔值为 False，和空列表一样处理
            if not result:
                return None
            return result
        except Exception as e:
            logger.debug("[适配器] %s.fetch_kline(%s) 异常: %s",
                        provider.name, symbol, e)
            return None

    fetch_fn.__name__ = f"provider_{provider.name}"
    return fetch_fn


def _discover_sources(
    market: str,
    timeframe: str,
    cb: CircuitBreaker,
    preferred_source: str = "",
) -> List[Tuple[str, Callable, SourceConfig]]:
    """
    从 Provider 层自动发现可用数据源。

    流程:
      1. 调用 get_providers(capability="kline", timeframe=tf, market=market)
         → 按 kline_priority 排序的 Provider 列表
      2. 过滤已熔断的源
      3. 将每个 Provider 的 fetch_kline 适配为 Coordinator 的 fetch_fn
      4. 如果指定了 preferred_source，将其排到第一位

    Args:
        market:    市场名称（"CNStock" / "HKStock"）
        timeframe: K线周期（"1D" / "5m" / ...）
        cb:        熔断器
        preferred_source: 指定的首选源名称

    Returns:
        [(name, fetch_fn, source_config), ...]
    """
    from app.data_sources.provider import get_providers

    # 从 Provider 层获取按 kline_priority 排序的源
    providers = get_providers(
        capability="kline",
        timeframe=timeframe,
        market=market,
    )

    if not providers:
        logger.warning("[协助层] Provider 层无可用源: market=%s tf=%s", market, timeframe)
        return []

    result = []
    preferred_item = None

    for p in providers:
        # 检查熔断器
        if not cb.is_available(p.name):
            logger.debug("[协助层] Provider %s 已熔断，跳过", p.name)
            continue

        # 获取 SourceConfig（含 max_workers 等并发配置）
        cfg = get_source_config(p.name)

        # 适配 fetch_fn
        fetch_fn = _make_provider_fetch_fn(p)

        item = (p.name, fetch_fn, cfg)

        if preferred_source and p.name == preferred_source:
            preferred_item = item
        else:
            result.append(item)

    # 指定源排第一
    if preferred_item:
        logger.info("[协助层] 使用指定源 %s (优先), 回退源 %d 个",
                   preferred_source, len(result))
        result.insert(0, preferred_item)
    elif preferred_source:
        logger.warning("[协助层] 指定源 %s 不可用，使用默认分配", preferred_source)

    return result


# ================================================================
# 线程安全的阻塞任务队列
# ================================================================

class _WorkQueue:
    """
    阻塞任务队列 — 支持等待新任务。

    - get(): 有任务立刻返回，无任务等待最多 QUEUE_DRAIN_TIMEOUT 秒
    - put_back(sym): 放回队尾并唤醒等待线程
    - drain_done(): 标记所有工作完成，唤醒所有等待线程退出
    """

    def __init__(self, items: List[str]):
        self._items = list(items)
        self._cond = threading.Condition()
        self._done = False
        self._pending = 0

    def get(self) -> Optional[str]:
        """取下一个任务。队列空时等待，超时或 done 时返回 None。"""
        with self._cond:
            while not self._items:
                if self._done:
                    return None
                notified = self._cond.wait(timeout=QUEUE_DRAIN_TIMEOUT)
                if not notified and not self._items:
                    return None
            self._pending += 1
            return self._items.pop(0)

    def get_batch(self, batch_size: int) -> List[str]:
        """批量取任务"""
        with self._cond:
            actual = min(batch_size, len(self._items))
            if actual <= 0:
                return []
            batch = self._items[:actual]
            del self._items[:actual]
            self._pending += len(batch)
            return batch

    def put_back(self, sym: str):
        """放回队尾并唤醒等待线程"""
        with self._cond:
            self._items.append(sym)
            self._pending = max(0, self._pending - 1)
            self._cond.notify()

    def task_done(self):
        """标记一个任务完成（不放回队列）"""
        with self._cond:
            self._pending = max(0, self._pending - 1)
            if not self._items and self._pending == 0:
                self._cond.notify_all()

    def drain_done(self):
        """标记所有工作完成，唤醒所有等待线程退出"""
        with self._cond:
            self._done = True
            self._cond.notify_all()

    @property
    def is_empty(self) -> bool:
        with self._cond:
            return len(self._items) == 0


# ================================================================
# 协助层主类
# ================================================================

class Coordinator:
    """
    协助层 — 动态队列 + 吞吐反馈 + Provider 层自动发现。
    """

    def __init__(self):
        self._lock = threading.Lock()

    def coordinate_kline(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        cb: CircuitBreaker,
        market: str = "",
        timeout: float = 15.0,
        preferred_source: str = "",
        sources: Optional[List[Tuple[str, Callable]]] = None,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        协调 K 线获取 — 动态队列模式。

        支持两种模式:
          1. 自动发现模式（推荐）: 不传 sources，Coordinator 从 Provider 层自动发现
          2. 手动指定模式（兼容）: 传入 sources=[(name, fetch_fn), ...]

        Args:
            symbols: 股票代码列表（1 只或多只均可）
            timeframe: K 线周期
            limit: K 线条数
            cb: 熔断器
            market: 市场名称（如 "CNStock"，用于 Provider 层过滤 + SourceConfig 查询）
            timeout: 总超时
            preferred_source: 指定数据源名称（如 "tencent"），优先使用该源
            sources: 手动指定源列表（可选）。为 None 时自动从 Provider 层发现。

        Returns:
            (results, failed)
        """
        if not symbols:
            return {}, list(symbols)

        # ── 源获取：自动发现 or 手动指定 ──
        if sources is not None:
            # 手动指定模式（兼容旧调用）
            source_map = {name: fn for name, fn in sources}
            if preferred_source and preferred_source in source_map:
                available = self._get_preferred_available(
                    preferred_source, market, source_map, cb
                )
            else:
                available = self._get_available_sources(market, source_map, cb)
        else:
            # 自动发现模式 — 从 Provider 层获取源
            discovered = _discover_sources(market, timeframe, cb, preferred_source)
            if not discovered:
                logger.warning("[协助层] 市场 %s 无可用源", market)
                return {}, list(symbols)
            # 转换为 (name, cfg) 格式供后续使用
            available = [(name, cfg) for name, _, cfg in discovered]
            # 构建 source_map
            source_map = {name: fn for name, fn, _ in discovered}

        if not available:
            logger.warning("[协助层] 市场 %s 无可用源", market)
            return {}, list(symbols)

        # ── 以下是原有的动态队列逻辑（不变）──

        wq = _WorkQueue(symbols)
        results: Dict[str, List[Dict[str, Any]]] = {}
        results_lock = threading.Lock()
        failed: List[str] = []
        failed_lock = threading.Lock()

        symbol_tried: Dict[str, Set[str]] = {}
        symbol_tried_lock = threading.Lock()

        source_consecutive_fails: Dict[str, int] = {}
        fails_lock = threading.Lock()
        MAX_SOURCE_FAILS = 5

        def _get_consecutive_fails(name: str) -> int:
            with fails_lock:
                return source_consecutive_fails.get(name, 0)

        def _inc_consecutive_fails(name: str):
            with fails_lock:
                source_consecutive_fails[name] = source_consecutive_fails.get(name, 0) + 1

        def _reset_consecutive_fails(name: str):
            with fails_lock:
                source_consecutive_fails[name] = 0

        def _mark_success(sym: str, bars: List[Dict[str, Any]], source_name: str):
            with results_lock:
                results[sym] = bars

        def _mark_failed(sym: str, source_name: str):
            with results_lock:
                if sym in results:
                    return

            with symbol_tried_lock:
                tried = symbol_tried.setdefault(sym, set())
                tried.add(source_name)
                untried = [name for name, _ in available if name not in tried]

            if untried:
                wq.put_back(sym)
            else:
                with failed_lock:
                    if sym not in failed:
                        failed.append(sym)
                wq.task_done()

        def _fetch_with_timeout(fn: Callable, sym: str, tf: str, lim: int,
                                timeout_s: float) -> Optional[List[Dict[str, Any]]]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as task_pool:
                future = task_pool.submit(fn, sym, tf, lim)
                try:
                    return future.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    logger.warning("[协助层] %s 获取 %s 超时 (%ss)", fn.__name__, sym, timeout_s)
                    return None

        def _process_symbol(sym: str, source_name: str, fetch_fn: Callable,
                            cfg: SourceConfig) -> bool:
            with results_lock:
                if sym in results:
                    wq.task_done()
                    return True

            with symbol_tried_lock:
                symbol_tried.setdefault(sym, set()).add(source_name)

            start_time = time.time()
            try:
                bars = _fetch_with_timeout(fetch_fn, sym, timeframe, limit, PER_TASK_TIMEOUT)
                elapsed = time.time() - start_time

                if bars:
                    cb.record_success(source_name)
                    cfg.record(True, elapsed)
                    _mark_success(sym, bars, source_name)
                    _reset_consecutive_fails(source_name)
                    wq.task_done()
                    return True
                else:
                    cb.record_failure(source_name, "empty")
                    cfg.record(False, elapsed)
                    _inc_consecutive_fails(source_name)
                    _mark_failed(sym, source_name)
                    return False
            except Exception as e:
                elapsed = time.time() - start_time
                cb.record_failure(source_name, str(e))
                cfg.record(False, elapsed)
                logger.debug("[协助层] %s 获取 %s 失败: %s", source_name, sym, e)
                _inc_consecutive_fails(source_name)
                _mark_failed(sym, source_name)
                return False

        def _worker(source_name: str, cfg: SourceConfig, fetch_fn: Callable):
            while True:
                if _get_consecutive_fails(source_name) >= MAX_SOURCE_FAILS:
                    break
                if not cb.is_available(source_name):
                    break

                sym = wq.get()
                if sym is None:
                    break
                _process_symbol(sym, source_name, fetch_fn, cfg)

        # 构建线程池
        total_threads = 0
        thread_plan = []
        for name, cfg in available:
            fn = source_map[name]
            tc = min(cfg.max_workers, len(symbols))
            thread_plan.append((name, cfg, fn, tc))
            total_threads += tc

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=total_threads, thread_name_prefix="coord"
        ) as pool:
            futures = []
            for name, cfg, fn, tc in thread_plan:
                for _ in range(tc):
                    futures.append(pool.submit(_worker, name, cfg, fn))

            concurrent.futures.wait(futures, timeout=timeout + 2)

        wq.drain_done()

        while True:
            sym = wq.get()
            if sym is None:
                break
            with results_lock:
                if sym in results:
                    continue
            with failed_lock:
                if sym not in failed:
                    failed.append(sym)

        stats = " | ".join(cfg.stats_summary() for _, cfg in available)
        logger.info("[协助层] 完成: %d成功 %d失败 | %s", len(results), len(failed), stats)

        return results, failed

    # ── 实时行情 Race 模式 ──────────────────────────────────────

    def coordinate_ticker(
        self,
        symbol: str,
        sources: List[Tuple[str, Callable]],
        cb: CircuitBreaker,
        timeout: float = 8.0,
        preferred_source: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        实时行情 Race 模式 — 所有源并发，第一个成功的直接返回。

        Args:
            symbol: 股票代码
            sources: [(name, fetch_fn), ...]  fetch_fn(symbol) -> Dict | None
            cb: 熔断器
            timeout: 超时
            preferred_source: 指定数据源名称，优先 race 该源

        Returns:
            第一个成功的 Dict，全部失败返回 None
        """
        if not sources:
            return None

        if preferred_source:
            preferred = [(n, fn) for n, fn in sources if n == preferred_source and cb.is_available(n)]
            others = [(n, fn) for n, fn in sources if n != preferred_source and cb.is_available(n)]
            available = preferred + others
        else:
            available = [(name, fn) for name, fn in sources if cb.is_available(name)]

        if not available:
            logger.warning("[协助层] ticker %s 无可用源", symbol)
            return None

        result_holder: List[Tuple[str, Dict[str, Any]]] = []
        done_event = threading.Event()
        lock = threading.Lock()

        def _race_one(source_name: str, fetch_fn: Callable):
            if done_event.is_set():
                return
            try:
                start = time.time()
                result = fetch_fn(symbol)
                elapsed = time.time() - start

                if result and result.get("last", 0) > 0:
                    cb.record_success(source_name)
                    cfg = get_source_config(source_name)
                    cfg.record(True, elapsed)

                    with lock:
                        if not result_holder:
                            result_holder.append((source_name, result))
                            done_event.set()
                else:
                    cb.record_failure(source_name, "empty")
                    cfg = get_source_config(source_name)
                    cfg.record(False, elapsed)
            except Exception as e:
                cb.record_failure(source_name, str(e))
                cfg = get_source_config(source_name)
                cfg.record(False, 0)
                logger.debug("[协助层] ticker %s %s 失败: %s", source_name, symbol, e)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(available), thread_name_prefix="ticker-race"
        ) as pool:
            futures = [
                pool.submit(_race_one, name, fn)
                for name, fn in available
            ]
            done_event.wait(timeout=timeout)

        if result_holder:
            source_name, result = result_holder[0]
            logger.info("[协助层] ticker %s 命中 %s", symbol, source_name)
            return result

        logger.warning("[协助层] ticker %s 所有源失败", symbol)
        return None

    # ── 纯透传 ────────────────────────────────────────────────────

    @staticmethod
    def passthrough(fn: Callable, *args, **kwargs):
        """纯透传，不加任何逻辑"""
        return fn(*args, **kwargs)

    # ── 内部工具 ─────────────────────────────────────────────────

    def _get_available_sources(
        self,
        market: str,
        source_map: Dict[str, Callable],
        cb: CircuitBreaker,
    ) -> List[Tuple[str, SourceConfig]]:
        """获取支持指定市场且未熔断的源列表，按 effective_weight 降序"""
        if market:
            configs = get_sources_for_market(market)
        else:
            configs = get_all_enabled_sources()

        available = []
        for cfg in configs:
            if cfg.name not in source_map:
                continue
            if not cb.is_available(cfg.name):
                logger.debug("[协助层] 源 %s 已熔断，跳过", cfg.name)
                continue
            available.append((cfg.name, cfg))

        return available

    def _get_preferred_available(
        self,
        preferred: str,
        market: str,
        source_map: Dict[str, Callable],
        cb: CircuitBreaker,
    ) -> List[Tuple[str, SourceConfig]]:
        """
        获取可用源列表，指定源排在第一位。
        """
        all_available = self._get_available_sources(market, source_map, cb)

        if not all_available:
            return []

        preferred_item = None
        others = []
        for item in all_available:
            if item[0] == preferred:
                preferred_item = item
            else:
                others.append(item)

        if preferred_item:
            logger.info("[协助层] 使用指定源 %s (优先), 回退源 %d 个",
                       preferred, len(others))
            return [preferred_item] + others
        else:
            logger.warning("[协助层] 指定源 %s 不可用，回退到默认分配", preferred)
            return all_available


# ================================================================
# 全局实例
# ================================================================

_coordinator = Coordinator()


def get_coordinator() -> Coordinator:
    """获取全局协助层实例"""
    return _coordinator
