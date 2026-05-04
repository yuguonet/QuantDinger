# -*- coding: utf-8 -*-
"""
协助层 (Coordinator) — 统一调度：fallback / race / 并发

定位:
  DataSourceFactory(入口层) → Coordinator(本层) → Providers(数据源层)

核心职责:
  1. sequential_fallback: 单只按优先级逐源尝试（K线默认策略）
  2. race:                多源并发竞赛，第一个有效结果返回（行情默认策略）
  3. 动态队列批量并发:    源干完一个活立刻拿下一个，不闲着
  4. 并发控制:            每个源的并发数不超过其 max_workers 配置
  5. 吞吐跟踪:            记录每个源的实际 QPS，动态调整分配优先级

设计原则:
  - Factory 负责去重、复权、市场解析；本层负责所有调度
  - 所有 Provider 交互都经过本层，Factory 不直接碰 Provider
  - 能批量的绝对不并发（调用 provider.fetch_kline_batch）
  - 不能批量的按源并发（每个源独立线池，互不干扰）
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

from app.data_sources.source_config import (
    SourceConfig, get_source_config, get_sources_for_market,
)
from app.data_sources.circuit_breaker import CircuitBreaker
from app.data_sources.provider import get_providers
from app.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# 单次请求的超时上限（秒），兜底防止 fetch_kline 卡死
PER_TASK_TIMEOUT = 20.0

# 队列空时等待的超时（秒），超时后认为所有工作已完成
QUEUE_DRAIN_TIMEOUT = 3.0


# ================================================================
# 调度策略 — fallback / race
# ================================================================

def sequential_fallback(
    symbol: str,
    providers: List[Tuple[str, Callable[[], Optional[T]]]],
    cb: CircuitBreaker,
    validate: Callable[[T], bool] = lambda x: x is not None,
    timeout: float = PER_TASK_TIMEOUT,
) -> Tuple[Optional[T], Optional[str]]:
    """
    顺序 fallback — 按优先级逐源尝试，成功就停。

    适合 K线（每只1次HTTP，race浪费API）。

    Args:
        symbol:     股票代码
        providers:  [(name, fetcher), ...]
        cb:         熔断器
        validate:   结果校验
        timeout:    单源超时秒数（防止单个 provider 卡住阻塞整个链）

    Returns:
        (result, source_name) 或 (None, None)
    """
    for name, fetcher in providers:
        if not cb.is_available(name):
            continue
        try:
            # 带超时调用，防止 provider 卡住
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fetcher)
                try:
                    result = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    logger.warning("[fallback] %s 超时 (%ss)", name, timeout)
                    cb.record_failure(name, "timeout")
                    continue

            if validate(result):
                cb.record_success(name)
                return result, name
            cb.record_failure(name, "empty/invalid")
        except Exception as e:
            cb.record_failure(name, str(e))

    return None, None


def race(
    providers: List[Tuple[str, Callable[[], Optional[T]]]],
    cb: CircuitBreaker,
    timeout: float = 8.0,
    validate: Callable[[T], bool] = lambda x: x is not None,
) -> Tuple[Optional[T], Optional[str]]:
    """
    并发竞赛 — 多源同时取，第一个有效结果返回。

    适合行情（有批量接口，race代价低）。

    Args:
        providers: [(name, fetcher), ...]
        cb:        熔断器
        timeout:   超时秒数
        validate:  结果校验

    Returns:
        (result, source_name) 或 (None, None)
    """
    available = [(n, f) for n, f in providers if cb.is_available(n)]
    if not available:
        return None, None

    if len(available) == 1:
        name, fn = available[0]
        try:
            result = fn()
            if validate(result):
                cb.record_success(name)
                return result, name
            cb.record_failure(name, "empty/invalid")
        except Exception as e:
            cb.record_failure(name, str(e))
        return None, None

    result_holder: Dict[str, Any] = {"result": None, "source": None}
    done_event = threading.Event()
    lock = threading.Lock()

    def _try(source_name: str, fetcher: Callable) -> None:
        try:
            data = fetcher()
            if done_event.is_set():
                return
            if validate(data):
                with lock:
                    if result_holder["result"] is None:
                        result_holder["result"] = data
                        result_holder["source"] = source_name
                        done_event.set()
                cb.record_success(source_name)
            else:
                cb.record_failure(source_name, "empty/invalid")
        except Exception as e:
            if not done_event.is_set():
                cb.record_failure(source_name, str(e))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(available))
    try:
        futures = {executor.submit(_try, n, f): n for n, f in available}
        done_event.wait(timeout=timeout + 1)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return result_holder["result"], result_holder["source"]


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
        with self._cond:
            actual = min(batch_size, len(self._items))
            if actual <= 0:
                return []
            batch = self._items[:actual]
            del self._items[:actual]
            self._pending += len(batch)
            return batch

    def put_back(self, sym: str):
        with self._cond:
            self._items.append(sym)
            self._pending = max(0, self._pending - 1)
            self._cond.notify()

    def task_done(self):
        with self._cond:
            self._pending = max(0, self._pending - 1)
            if not self._items and self._pending == 0:
                self._cond.notify_all()

    def drain_done(self):
        with self._cond:
            self._done = True
            self._cond.notify_all()

    @property
    def is_empty(self) -> bool:
        with self._cond:
            return len(self._items) == 0


# ================================================================
# Coordinator — 统一调度层
# ================================================================

class Coordinator:
    """
    协助层 — 统一调度：fallback / race / 批量并发。

    所有 Provider 交互都经过本层。
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ── 单只 K线: fallback ──────────────────────────────────────

    def fetch_single_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        market: str,
        cb: CircuitBreaker,
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        获取单只K线 — 按优先级逐源 fallback。

        Args:
            symbol:    股票代码
            timeframe: K线周期
            limit:     数据条数
            market:    市场类型
            cb:        熔断器

        Returns:
            (kline_bars, source_name) 或 (None, None)
        """
        providers = [
            (p.name, lambda p=p: p.fetch_kline(symbol, timeframe, limit))
            for p in get_providers("kline", timeframe=timeframe, market=market or None)
        ]
        result, src = sequential_fallback(symbol, providers, cb)
        if result:
            logger.info("[K线] %s tf=%s 来源=%s bars=%d", symbol, timeframe, src, len(result))
        else:
            names = [n for n, _ in providers]
            logger.warning("[K线] %s tf=%s 全部源失败: %s", symbol, timeframe, names)
        return result, src

    # ── 单只行情: race ──────────────────────────────────────────

    def fetch_single_ticker(
        self,
        symbol: str,
        market: str,
        cb: CircuitBreaker,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        获取单只行情 — 多源并发 race。

        Args:
            symbol: 股票代码
            market: 市场类型
            cb:     熔断器

        Returns:
            (ticker_dict, source_name) 或 (None, None)
        """
        providers = [
            (p.name, lambda p=p: p.fetch_quote(symbol))
            for p in get_providers("quote", market=market or None)
        ]
        result, src = race(providers, cb)
        if result:
            logger.info("[行情] %s 来源=%s", symbol, src)
        else:
            names = [n for n, _ in providers]
            logger.warning("[行情] %s 全部源失败: %s", symbol, names)
        return result, src

    # ── 批量行情: race 批量接口 + 逐只 fallback ─────────────────

    def fetch_batch_ticker(
        self,
        symbols: List[str],
        market: str,
        cb: CircuitBreaker,
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量行情 — race 批量接口 + 逐只 fallback。

        流程:
          1. race 多源并发调用 fetch_quotes_batch（一次HTTP取多只）
          2. 未取到的 symbol fallback 到逐只 race

        Args:
            symbols: 股票代码列表
            market:  市场类型
            cb:      熔断器

        Returns:
            {symbol: ticker_dict}
        """
        providers = get_providers("quote", market=market or None)

        # race 批量接口
        batch_providers = [
            (p.name, lambda p=p: p.fetch_quotes_batch(symbols))
            for p in providers
        ]
        batch, src = race(
            batch_providers, cb,
            validate=lambda d: bool(d),
        )
        if batch:
            logger.info("[批量行情] %s race 取到 %d/%d 只", src, len(batch), len(symbols))
        else:
            logger.info("[批量行情] 所有批量源均失败，fallback 到逐只模式")

        if not batch:
            batch = {}

        # 逐只 fallback 补齐
        missing = [s for s in symbols if s not in batch]
        if missing:
            filled = 0
            for sym in missing:
                try:
                    data, _ = self.fetch_single_ticker(sym, market, cb)
                    if data and data.get("last", 0) > 0:
                        batch[sym] = data
                        filled += 1
                except Exception:
                    pass
            logger.info("[批量行情] 逐只 fallback 补齐 %d/%d 只", filled, len(missing))

        return batch

    # ── 批量 K线: 动态队列并发 ──────────────────────────────────

    def coordinate_kline(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        providers: List[Any],
        cb: CircuitBreaker,
        market: str = "",
        timeout: float = 15.0,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        协调批量K线获取 — 动态队列模式。

        Returns:
            (results, failed)
        """
        if not symbols or not providers:
            return {}, list(symbols)

        # 1. 构建 provider 映射 + 找可用源
        provider_map = {p.name: p for p in providers}
        source_configs = self._get_available_sources(market, provider_map, cb)
        if not source_configs:
            logger.warning("[协助层] 市场 %s 无可用源", market)
            return {}, list(symbols)

        # 2. 构建阻塞任务队列 + 结果收集
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
                    wq.task_done()
                    return

            with symbol_tried_lock:
                tried = symbol_tried.setdefault(sym, set())
                tried.add(source_name)
                untried = [name for name, _ in source_configs if name not in tried]

            if untried:
                wq.task_done()
                wq.put_back(sym)
            else:
                with failed_lock:
                    if sym not in failed:
                        failed.append(sym)
                wq.task_done()

        def _fetch_with_timeout(provider: Any, sym: str, tf: str, lim: int,
                                timeout_s: float) -> Optional[List[Dict[str, Any]]]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as task_pool:
                future = task_pool.submit(provider.fetch_kline, sym, tf, lim)
                try:
                    return future.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    logger.warning("[协助层] %s 获取 %s 超时 (%ss)", provider.name, sym, timeout_s)
                    return None

        def _process_symbol(sym: str, source_name: str, provider: Any,
                            cfg: SourceConfig) -> bool:
            with results_lock:
                if sym in results:
                    wq.task_done()
                    return True

            with symbol_tried_lock:
                symbol_tried.setdefault(sym, set()).add(source_name)

            start_time = time.time()
            try:
                bars = _fetch_with_timeout(provider, sym, timeframe, limit, PER_TASK_TIMEOUT)
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

        def _worker(source_name: str, cfg: SourceConfig, provider: Any):
            while True:
                if _get_consecutive_fails(source_name) >= MAX_SOURCE_FAILS:
                    break
                if not cb.is_available(source_name):
                    break

                if cfg.batch_capable:
                    batch = wq.get_batch(cfg.batch_size)
                    if not batch:
                        break
                    for sym in batch:
                        _process_symbol(sym, source_name, provider, cfg)
                else:
                    sym = wq.get()
                    if sym is None:
                        break
                    _process_symbol(sym, source_name, provider, cfg)

        # 3. 构建线程池
        total_threads = 0
        thread_plan = []
        for name, cfg in source_configs:
            if name not in provider_map:
                continue
            tc = 1 if cfg.batch_capable else min(cfg.max_workers, len(symbols))
            thread_plan.append((name, cfg, provider_map[name], tc))
            total_threads += tc

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=total_threads, thread_name_prefix="coord"
        ) as pool:
            futures = []
            for name, cfg, provider, tc in thread_plan:
                for _ in range(tc):
                    futures.append(pool.submit(_worker, name, cfg, provider))

            concurrent.futures.wait(futures, timeout=timeout + 2)

        # 4. 标记完成 + 收集剩余
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

        # 5. 打印统计
        stats = " | ".join(cfg.stats_summary() for _, cfg in source_configs)
        logger.info("[协助层] 完成: %d成功 %d失败 | %s", len(results), len(failed), stats)

        return results, failed

    # ── 内部工具 ─────────────────────────────────────────────────

    def _get_available_sources(
        self,
        market: str,
        provider_map: Dict[str, Any],
        cb: CircuitBreaker,
    ) -> List[Tuple[str, SourceConfig]]:
        if market:
            configs = get_sources_for_market(market)
        else:
            from app.data_sources.source_config import get_all_enabled_sources
            configs = get_all_enabled_sources()

        available = []
        for cfg in configs:
            if cfg.name not in provider_map:
                continue
            if not cb.is_available(cfg.name):
                logger.debug("[协助层] 源 %s 已熔断，跳过", cfg.name)
                continue
            available.append((cfg.name, cfg))

        return available


# ================================================================
# 全局实例
# ================================================================

_coordinator = Coordinator()


def get_coordinator() -> Coordinator:
    """获取全局协助层实例"""
    return _coordinator
