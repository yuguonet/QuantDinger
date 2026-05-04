# -*- coding: utf-8 -*-
"""
协助层 (Coordinator) — 数据源层与调度层之间的并发协调

定位:
  调度层(CNStockDataSource) → 协助层(Coordinator) → 数据源层(函数)

核心职责:
  1. 动态队列: 源干完一个活立刻拿下一个，不闲着
  2. 并发控制: 每个源的并发数不超过其 max_workers 配置
  3. 吞吐跟踪: 记录每个源的实际 QPS，动态调整分配优先级
  4. 失败处理: 每个 symbol 最多被所有可用源各试一次，全部失败则放弃

接口:
  sources = [("tencent", fetch_fn), ("sina", fetch_fn), ...]
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
    协助层 — 动态队列 + 吞吐反馈。
    """

    def __init__(self):
        self._lock = threading.Lock()

    def coordinate_kline(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        sources: List[Tuple[str, Callable]],
        cb: CircuitBreaker,
        market: str = "",
        timeout: float = 15.0,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        协调 K 线获取 — 动态队列模式。

        Args:
            symbols: 股票代码列表（1 只或多只均可）
            timeframe: K 线周期
            limit: K 线条数
            sources: [(name, fetch_fn), ...]
                     fetch_fn(symbol, timeframe, limit) -> List[Dict] | None
            cb: 熔断器
            market: 市场名称（用于查 SourceConfig）
            timeout: 总超时

        Returns:
            (results, failed)
        """
        if not symbols or not sources:
            return {}, list(symbols)

        # 1. 构建 source 映射 + 找可用源
        source_map = {name: fn for name, fn in sources}
        available = self._get_available_sources(market, source_map, cb)
        if not available:
            logger.warning("[协助层] 市场 %s 无可用源", market)
            return {}, list(symbols)

        # 2. 构建阻塞任务队列 + 结果收集
        wq = _WorkQueue(symbols)
        results: Dict[str, List[Dict[str, Any]]] = {}
        results_lock = threading.Lock()
        failed: List[str] = []
        failed_lock = threading.Lock()

        # per-symbol 失败记录
        symbol_tried: Dict[str, Set[str]] = {}
        symbol_tried_lock = threading.Lock()

        # per-source 连续失败计数
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
                    return  # 已被其他源成功获取

            with symbol_tried_lock:
                tried = symbol_tried.setdefault(sym, set())
                tried.add(source_name)
                untried = [name for name, _ in available if name not in tried]

            if untried:
                wq.put_back(sym)  # 还有源没试过 → 放回队尾
            else:
                with failed_lock:
                    if sym not in failed:
                        failed.append(sym)
                wq.task_done()  # 彻底失败，不再重试

        def _fetch_with_timeout(fn: Callable, sym: str, tf: str, lim: int,
                                timeout_s: float) -> Optional[List[Dict[str, Any]]]:
            """带超时兜底的 fetch 调用"""
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as task_pool:
                future = task_pool.submit(fn, sym, tf, lim)
                try:
                    return future.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    logger.warning("[协助层] %s 获取 %s 超时 (%ss)", fn.__name__, sym, timeout_s)
                    return None

        def _process_symbol(sym: str, source_name: str, fetch_fn: Callable,
                            cfg: SourceConfig) -> bool:
            """处理单个 symbol。返回是否成功。"""
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
            """单个源的 worker 循环"""
            while True:
                if _get_consecutive_fails(source_name) >= MAX_SOURCE_FAILS:
                    break
                if not cb.is_available(source_name):
                    break

                sym = wq.get()
                if sym is None:
                    break  # 队列空 + 超时 → 退出
                _process_symbol(sym, source_name, fetch_fn, cfg)

        # 3. 构建线程池
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

            # 等待所有 worker 完成
            concurrent.futures.wait(futures, timeout=timeout + 2)

        # 4. 标记完成 + 收集剩余
        wq.drain_done()

        # 5. 队列中未处理的 symbol 标记为失败
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

        # 6. 打印各源吞吐统计
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
    ) -> Optional[Dict[str, Any]]:
        """
        实时行情 Race 模式 — 所有源并发，第一个成功的直接返回。

        Args:
            symbol: 股票代码
            sources: [(name, fetch_fn), ...]  fetch_fn(symbol) -> Dict | None
            cb: 熔断器
            timeout: 超时

        Returns:
            第一个成功的 Dict，全部失败返回 None
        """
        if not sources:
            return None

        # 过滤熔断源
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
                    # 记录吞吐（ticker 视为 1 条 K 线的成功请求）
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
            # 等第一个完成或超时
            done_event.wait(timeout=timeout)

        if result_holder:
            source_name, result = result_holder[0]
            logger.info("[协助层] ticker %s 命中 %s", symbol, source_name)
            return result

        logger.warning("[协助层] ticker %s 所有源失败", symbol)
        return None

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


# ================================================================
# 全局实例
# ================================================================

_coordinator = Coordinator()


def get_coordinator() -> Coordinator:
    """获取全局协助层实例"""
    return _coordinator
