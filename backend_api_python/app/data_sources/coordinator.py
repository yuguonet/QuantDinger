# -*- coding: utf-8 -*-
"""
协助层 (Coordinator) — 数据源并发调度的核心引擎

=== 在整个链路中的位置 ===

  路由层 (routes/)
      ↓
  服务层 (services/kline.py, portfolio_monitor.py, ...)
      ↓
  数据源门面 (DataSourceFactory)
      ↓
  ★ 协助层 (Coordinator) ← 你在这里
      ↓
  数据源层 (data_sources/cn_stock.py, us_stock.py, ...)
      ↓
  Provider 层 (tencent, sina, eastmoney, akshare, ...)

=== 核心职责 ===

  1. 动态任务队列:   源干完一个 symbol 立刻拿下一个，不闲着（负载均衡）
  2. 并发控制:       每个源的线程数不超过其 max_workers 配置
  3. 熔断联动:       跳过已熔断的源；连续失败过多自动停用该源
  4. 源自动发现:     从 Provider 层按能力/周期/市场自动获取可用源（不硬编码）
  5. 指定源优先:     支持 preferred_source 直接指定数据源，失败后自动回退其他源
  6. Race 模式:      实时行情场景，所有源并发抢答，第一个成功的直接返回

=== 两种调度模式 ===

  模式 A — K线批量获取 (coordinate_kline):
    多只股票 × 多个源 → 动态队列分配 → 每只股票只要有一个源成功就算成功
    场景: 批量加载历史K线、回测数据准备、批量分析

  模式 B — 实时行情 Race (coordinate_ticker):
    1只股票 × 多个源 → 并发抢答 → 第一个返回有效价格的直接用
    场景: 获取实时报价、自选股价格刷新

  模式 D — 全市场批量K线 (coordinate_market_kline):
    Coordinator 分组 → 共享队列 → 多个 Provider 并发取组 → 每个 Provider 调自己的 fetch_market_kline → 合并结果
    场景: 全市场K线加载、全市场行情快照

=== 两种源指定方式 ===

  方式 1 — 自动发现（推荐）:
    不传 sources 参数，传 market="CNStock"
    → Coordinator 调用 Provider 层自动发现可用源
    → 好处: 新增/删除 Provider 无需改调用方

  方式 2 — 手动指定（兼容旧代码）:
    传入 sources=[(name, fetch_fn), ...]
    → 好处: 调用方完全控制用哪些源

=== 函数命名说明（容易混淆的）===

  coordinate_kline  → 实际含义: "并发批量拉K线，动态队列分配多源"
  coordinate_ticker → 实际含义: "实时行情多源Race，谁先返回用谁"
  direct_call       → 实际含义: "直接调用，不加任何协调逻辑"
  _mark_failed      → 实际含义: "标记某源对某symbol失败，放回队列尝试下一个源，或彻底放弃"
  _mark_success     → 实际含义: "标记某symbol获取成功，从队列中移除"
"""

from __future__ import annotations

import atexit
import concurrent.futures
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.data_sources.source_config import (
    SourceConfig, get_source_config, get_sources_for_market, get_all_enabled_sources,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 熔断器 — 两态状态机: Closed → Open → Closed
# ================================================================

class CircuitBreaker:
    """
    熔断器 — 两态状态机: Closed → Open → Closed。

    每个数据源可配置独立的熔断器实例，支持按源名称分别跟踪故障状态。

    状态机:
        ┌─────────┐  连续失败 ≥ 阈值  ┌──────┐  冷却期结束  ┌─────────┐
        │ Closed  │ ───────────────→ │ Open │ ──────────→ │ Closed  │
        │ (正常)  │ ←─────────────── │(熔断)│             │ (恢复)  │
        └─────────┘    请求成功       └──────┘             └─────────┘

    行为:
        冷却期结束后，自动恢复为 Closed 状态，所有请求正常放行。
        如果恢复后再次连续失败，重新进入 Open。

    线程安全性:
        使用 threading.Lock 保护所有状态读写，多线程并发调用安全。
    """

    _CLOSED = "closed"
    _OPEN = "open"

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        name: str = "default",
    ):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._name = name
        self._failures: Dict[str, int] = {}
        self._state: Dict[str, str] = {}
        self._tripped_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_available(self, source: str) -> bool:
        with self._lock:
            state = self._state.get(source, self._CLOSED)
            if state == self._CLOSED:
                return True
            # OPEN — 检查冷却是否到期
            elapsed = time.time() - self._tripped_at[source]
            if elapsed >= self._cooldown_seconds:
                # 冷却到期，直接恢复 Closed
                self._state[source] = self._CLOSED
                self._failures[source] = 0
                self._tripped_at.pop(source, None)
                logger.info("[熔断器:%s] %s 冷却结束，恢复正常", self._name, source)
                return True
            return False

    def record_success(self, source: str):
        with self._lock:
            self._failures[source] = 0
            self._state.pop(source, None)
            self._tripped_at.pop(source, None)

    def record_failure(self, source: str, reason: str = ""):
        with self._lock:
            self._failures[source] = self._failures.get(source, 0) + 1
            if self._failures[source] >= self._failure_threshold:
                self._state[source] = self._OPEN
                self._tripped_at[source] = time.time()
                logger.warning(
                    "[熔断器:%s] %s 连续失败 %d 次，熔断 %ds (原因: %s)",
                    self._name, source, self._failures[source],
                    self._cooldown_seconds, reason,
                )

    def reset(self, source: str = None):
        with self._lock:
            if source:
                self._failures.pop(source, None)
                self._state.pop(source, None)
                self._tripped_at.pop(source, None)
            else:
                self._failures.clear()
                self._state.clear()
                self._tripped_at.clear()


# 全局熔断器实例
_realtime_cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0, name="realtime")


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器实例"""
    return _realtime_cb

# ================================================================
# 全局常量
# ================================================================

# 单次 fetch 的超时上限（秒）。
# Coordinator 层的兜底超时，防止某个源的 fetch_fn 卡死导致整个队列阻塞。
# 比 SourceConfig 里的超时更严格 — 这是硬上限。
PER_TASK_TIMEOUT = 20.0

# 队列为空后等待新任务的超时（秒）。
# worker 线程取不到任务时会阻塞等待，超时后认为所有工作已完成，退出循环。
QUEUE_DRAIN_TIMEOUT = 3.0

# 单个源连续失败次数上限。超过后该源的 worker 线程自动退出，不再尝试。
# 避免一个完全不可用的源反复失败浪费时间。
MAX_SOURCE_FAILS = 5

# 超时辅助线程池 — 长生命周期，避免每次 _fetch_with_timeout 都创建新线程池。
# max_workers=8 足够覆盖并发的超时监控需求（实际 fetch 在主池执行，这里只是等待+取消）。
_timeout_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="coord-timeout"
)
atexit.register(_timeout_pool.shutdown, wait=False)


# ================================================================
# Provider 适配器 — 统一接口签名
# ================================================================
#
# 背景: Provider 层的接口签名和 Coordinator 期望的不一致。
# Provider 返回 NotSupportedResult（表示"我不支持这个"），Coordinator 期望 None。
# 这两个适配器做的就是这个转换。
#

def _make_provider_fetch_fn(provider, adj: str = "qfq") -> Callable:
    """
    K线适配器: 把 Provider.fetch_kline 包装成 Coordinator 能用的 fetch_fn。

    签名转换:
      Provider:  provider.fetch_kline(code, timeframe, count, adj="qfq") -> List[Dict] | NotSupportedResult
      Coordinator 期望:  fetch_fn(symbol, timeframe, limit) -> List[Dict] | None

    转换规则:
      - NotSupportedResult（布尔值为 False）→ 返回 None → Coordinator 跳过该源
      - 空列表 → 返回 None → Coordinator 判定失败，尝试下一个源
      - 非空列表 → 直接返回 → Coordinator 判定成功

    Args:
        adj: 复权方式 — "qfq"(前复权,默认) / "hfq"(后复权) / ""(不复权)
    """
    def fetch_fn(symbol: str, timeframe: str, limit: int):
        try:
            result = provider.fetch_kline(symbol, timeframe, limit, adj=adj)
            if not result:  # None / [] / NotSupportedResult 都走这里
                return None
            return result
        except Exception as e:
            logger.debug("[适配器] %s.fetch_kline(%s) 异常: %s",
                        provider.name, symbol, e)
            return None

    fetch_fn.__name__ = f"provider_{provider.name}"
    return fetch_fn


def _make_provider_quote_fn(provider) -> Callable:
    """
    行情适配器: 把 Provider.fetch_ticker 包装成 Coordinator 能用的 fetch_fn。

    签名转换:
      Provider:  provider.fetch_ticker(code, timeout=8) -> Dict | None | NotSupportedResult
      Coordinator 期望:  fetch_fn(symbol) -> Dict | None

    注意: fetch_fn 只接收 symbol 一个参数（和 K线适配器不同，没有 timeframe/limit）。
    """
    def fetch_fn(symbol: str):
        try:
            result = provider.fetch_ticker(symbol)
            if not result:
                return None
            return result
        except Exception as e:
            logger.debug("[适配器] %s.fetch_ticker(%s) 异常: %s",
                        provider.name, symbol, e)
            return None

    fetch_fn.__name__ = f"provider_{provider.name}_quote"
    return fetch_fn


def _discover_sources(
    market: str,
    timeframe: str,
    cb: CircuitBreaker,
    preferred_source: str = "",
    capability: str = "kline",
    adj: str = "qfq",
    skip_cb_filter: bool = False,
) -> List[Tuple[str, Callable, SourceConfig]]:
    """
    源自动发现 — 从 Provider 层获取可用数据源列表。

    这是 Coordinator "不硬编码数据源" 的关键。调用方只需告诉 Coordinator
    "我要 CNStock 的 K线"，Coordinator 自己去找哪些 Provider 能提供。

    流程:
      1. 调用 Provider 层的 get_providers() → 按 priority 排序的 Provider 列表
      2. 过滤掉已熔断的源（cb.is_available）
      3. 用适配器把 Provider 的 fetch 方法转成 Coordinator 的 fetch_fn
      4. 如果指定了 preferred_source，将其排到第一位

    Args:
        market:    市场名称（"CNStock" / "HKStock" / "USStock" / ...）
        timeframe: K线周期（"1D" / "5m" / ...）。capability="quote" 时可为空。
        cb:        熔断器实例
        preferred_source: 指定的首选源名称（如 "tencent"）
        capability: 能力类型
          - "kline"  → 获取K线数据（默认）
          - "quote"  → 获取实时行情
        adj: 复权方式（仅 capability="kline" 时生效）
          - "qfq"  → 前复权（默认）
          - "hfq"  → 后复权
          - ""     → 不复权

    Returns:
        [(源名称, fetch_fn, 源配置), ...]
        fetch_fn 签名:
          - capability="kline": fetch_fn(symbol, timeframe, limit) -> List[Dict] | None
          - capability="quote": fetch_fn(symbol) -> Dict | None
    """
    from app.data_sources.provider import get_providers

    # 从 Provider 层获取按 priority 排序的源
    providers = get_providers(
        capability=capability,
        timeframe=timeframe if capability == "kline" else None,
        market=market,
    )

    if not providers:
        logger.warning("[协助层] Provider 层无可用源: market=%s capability=%s", market, capability)
        return []

    result = []
    preferred_item = None

    # 根据 capability 选择适配器（K线 vs 行情的接口签名不同）
    if capability == "quote":
        adapter = _make_provider_quote_fn
    else:
        # K线适配器: 传入 adj，由适配器闭包捕获
        adapter = lambda p: _make_provider_fetch_fn(p, adj=adj)

    for p in providers:
        # 熔断检查 — 跳过已熔断的源（skip_cb_filter=True 时跳过此检查）
        if not skip_cb_filter and not cb.is_available(p.name):
            logger.debug("[协助层] Provider %s 已熔断，跳过", p.name)
            continue

        # 获取源配置（含 max_workers、超时等并发参数）
        cfg = get_source_config(p.name)

        # 适配 fetch_fn
        fetch_fn = adapter(p)

        item = (p.name, fetch_fn, cfg)

        # 指定源单独记下，最后排到第一位
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
#
# 为什么不用 queue.Queue？
# 因为需要 put_back（放回队尾）功能 — 当一个源获取某 symbol 失败时，
# 把这个 symbol 放回队列让其他源尝试。标准库的 Queue 没有这个语义。
#

class _WorkQueue:
    """
    阻塞任务队列 — 支持"取任务 → 失败放回 → 其他源接手"的工作模式。

    典型流程:
      1. worker A 从队列取到 symbol "AAPL"
      2. worker A 用 tencent 源获取失败
      3. 调用 put_back("AAPL") 放回队尾
      4. worker B（sina 源）取到 "AAPL"，获取成功
      5. 调用 task_done() 标记完成

    线程安全: 所有操作都加了 threading.Condition 锁。
    """

    def __init__(self, items: List[str]):
        self._items = list(items)
        self._cond = threading.Condition()
        self._done = False      # True 表示"所有工作已完成，不再接受新任务"
        self._pending = 0       # 正在被 worker 处理中的任务数

    def get(self) -> Optional[str]:
        """
        取下一个任务。

        行为:
          - 队列有任务 → 立刻返回
          - 队列空但有 pending 任务 → 阻塞等待（最多 QUEUE_DRAIN_TIMEOUT 秒）
          - 队列空且无 pending 任务 → 返回 None（worker 应退出）

        Returns:
            symbol 字符串，或 None（表示可以退出了）
        """
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
        """批量取任务（用于 get_kline_batch 等批量场景）"""
        with self._cond:
            actual = min(batch_size, len(self._items))
            if actual <= 0:
                return []
            batch = self._items[:actual]
            del self._items[:actual]
            self._pending += len(batch)
            return batch

    def batch_task_done(self, count: int):
        """
        批量标记任务完成 — 配合 get_batch() 使用。

        当通过 get_batch() 一次取出 N 个任务时，处理完成后
        需要调用 batch_task_done(N) 来正确递减 pending 计数。
        """
        with self._cond:
            self._pending = max(0, self._pending - count)
            if not self._items and self._pending == 0:
                self._cond.notify_all()

    def put_back(self, sym: str):
        """
        放回队尾 — 当某源获取失败时，把 symbol 放回让其他源接手。

        这是动态队列的核心: 一个源失败不代表 symbol 失败，放回去让别的源试。
        """
        with self._cond:
            self._items.append(sym)
            self._pending = max(0, self._pending - 1)
            self._cond.notify()  # 唤醒一个等待的 worker

    def task_done(self):
        """
        标记一个任务完成（成功，不再放回队列）。

        当队列空且 pending 归零时，唤醒所有等待线程（让它们退出）。
        """
        with self._cond:
            self._pending = max(0, self._pending - 1)
            if not self._items and self._pending == 0:
                self._cond.notify_all()

    def drain_done(self):
        """
        强制标记所有工作完成 — 用于超时后强制唤醒所有等待的 worker 线程。
        """
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
    协助层 — 并发调度引擎。

    提供三种调度模式:
      - coordinate_kline:        K线批量获取（动态队列 + 多源 fallback）
      - coordinate_ticker:       实时行情 Race（多源并发抢答）
      - coordinate_market_kline: 全市场批量K线（Coordinator分组 + 多Provider并发取组）
      - coordinate_batch_quotes: 批量行情（单源批量请求 + 多源 fallback）

    两种模式的区别:
      coordinate_kline:  N只股票 × M个源 → 动态分配 → 每只股票只要有一个源成功就行
      coordinate_ticker: 1只股票 × M个源 → 并发抢答 → 第一个返回有效数据的直接用
      coordinate_market_kline: 全市场 × Coordinator分组 → 多Provider并发取组 → Provider调自己的fetch_market_kline → 合并结果
      coordinate_batch_quotes: N只股票 × 单源批量 → 逐源 fallback → 第一个成功的直接用
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ================================================================
    # 模式 A: K线获取 — 假批量，动态队列 + 多源 fallback
    # ================================================================
    #
    # 注意: 这里说的"批量"是假批量。
    #   - 没有真正的批量 API，每个 symbol 都是逐只单独请求 Provider
    #   - 所谓"批量"靠的是动态队列 + 多线程并发模拟出来的
    #   - 真正的批量接口见 coordinate_batch_quotes（单次请求拿多只）
    #
    # 工作流程（以 3 只股票、2 个源为例）:
    #
    #   初始队列: [AAPL, TSLA, MSFT]
    #   tencent worker 1 取到 AAPL → 获取成功 → 从队列移除
    #   tencent worker 2 取到 TSLA → 获取失败 → put_back 放回队尾
    #   sina worker 1 取到 MSFT → 获取成功 → 从队列移除
    #   sina worker 2 空闲 → 取到 TSLA（被 tencent 放回的）→ 获取成功
    #
    #   结果: AAPL(tencent) MSFT(sina) TSLA(sina) — 全部成功
    #
    # 关键设计:
    #   - 每个源的并发数由 SourceConfig.max_workers 控制
    #   - 一个源连续失败 MAX_SOURCE_FAILS 次后自动停用（不浪费时间）
    #   - 每个 symbol 会被所有可用源各试一次，全部失败才算失败
    #

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
        adj: str = "qfq",
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        K线批量获取 — 动态队列模式。

        这是 Coordinator 最核心的方法。当需要批量拉取多只股票的K线时调用。

        典型调用方:
          - CNStockDataSource.get_kline_batch()
          - KlineService（批量分析场景）
          - BacktestService（回测数据准备）

        Args:
            symbols:   股票代码列表（1 只或多只均可）
            timeframe: K 线周期（"1D" / "5m" / "1H" / ...）
            limit:     K 线条数
            cb:        熔断器
            market:    市场名称（"CNStock" / "HKStock"），用于自动发现源
            timeout:   总超时（秒），超时后未完成的 symbol 记为失败
            preferred_source: 指定首选源（如 "tencent"），优先使用，失败后回退
            sources:   手动指定源列表（可选）。为 None 时自动从 Provider 层发现。
                       格式: [(name, fetch_fn), ...]
                       fetch_fn 签名: fetch_fn(symbol, timeframe, limit) -> List[Dict] | None
            adj:       复权方式 — "qfq"(前复权,默认) / "hfq"(后复权) / ""(不复权)

        Returns:
            (results, failed)
            - results: {symbol: [kline_bars]} — 仅包含成功获取到数据的 symbol
            - failed:  [symbol, ...] — 所有源都尝试过但全部失败的 symbol
        """
        if not symbols:
            return {}, list(symbols)

        # ── 第一步: 获取可用源列表 ──
        # 两种方式: 自动发现（推荐）或 手动指定（兼容旧代码）
        if sources is not None:
            # 手动指定模式 — 调用方传入 [(name, fetch_fn), ...]
            source_map = {name: fn for name, fn in sources}
            if preferred_source and preferred_source in source_map:
                available = self._get_preferred_available(
                    preferred_source, market, source_map, cb
                )
            else:
                available = self._get_available_sources(market, source_map, cb)
        else:
            # 自动发现模式 — 从 Provider 层获取源
            discovered = _discover_sources(market, timeframe, cb, preferred_source, adj=adj)
            if not discovered:
                logger.warning("[协助层] 市场 %s 无可用源", market)
                return {}, list(symbols)
            available = [(name, cfg) for name, _, cfg in discovered]
            source_map = {name: fn for name, fn, _ in discovered}

        if not available:
            logger.warning("[协助层] 市场 %s 无可用源", market)
            return {}, list(symbols)

        # ── 第二步: 初始化动态队列和共享状态 ──
        wq = _WorkQueue(symbols)                    # 任务队列
        results: Dict[str, List[Dict[str, Any]]] = {}  # 成功的结果
        results_lock = threading.Lock()
        failed: List[str] = []                       # 全部源都失败的 symbol
        failed_lock = threading.Lock()

        # 记录每个 symbol 已经被哪些源尝试过（避免重复尝试）
        symbol_tried: Dict[str, Set[str]] = {}
        symbol_tried_lock = threading.Lock()

        # 记录每个源的连续失败次数（超过 MAX_SOURCE_FAILS 后该源自动停用）
        source_consecutive_fails: Dict[str, int] = {}
        fails_lock = threading.Lock()

        # ── 第三步: 定义内部辅助函数 ──

        def _get_consecutive_fails(name: str) -> int:
            """查询某源的连续失败次数"""
            with fails_lock:
                return source_consecutive_fails.get(name, 0)

        def _inc_consecutive_fails(name: str):
            """某源失败一次，连续失败计数 +1"""
            with fails_lock:
                source_consecutive_fails[name] = source_consecutive_fails.get(name, 0) + 1

        def _reset_consecutive_fails(name: str):
            """某源成功一次，连续失败计数归零"""
            with fails_lock:
                source_consecutive_fails[name] = 0

        def _mark_success(sym: str, bars: List[Dict[str, Any]], source_name: str):
            """
            标记某 symbol 获取成功。
            成功后该 symbol 从队列中彻底移除（不再让其他源尝试）。

            Returns:
                True  = 首次成功（调用方应 task_done）
                False = 已被其他源抢先成功（调用方不应 task_done，避免重复计数）
            """
            with results_lock:
                if sym in results:
                    return False  # 已被其他源成功获取，忽略重复
                results[sym] = bars
                return True

        def _mark_failed(sym: str, source_name: str):
            """
            标记某源对某 symbol 获取失败。

            行为:
              - 如果还有未尝试的源 → 把 symbol 放回队列（让其他源接手）
              - 如果所有源都试过了 → 标记为彻底失败，从队列中移除

            这就是"动态队列"的核心: 一个源失败 ≠ symbol 失败，放回去让别的源试。
            """
            with results_lock:
                if sym in results:
                    return  # 已经被其他源成功获取了，忽略

            with symbol_tried_lock:
                tried = symbol_tried.setdefault(sym, set())
                tried.add(source_name)
                untried = [name for name, _ in available if name not in tried]

            if untried:
                # 二次检查: put_back 前再确认一次，避免已成功的 symbol 被重复放回
                with results_lock:
                    if sym in results:
                        return
                # 还有未尝试的源 → 放回队尾，让其他 worker 接手
                wq.put_back(sym)
            else:
                # 所有源都试过了，全部失败 → 彻底放弃
                with failed_lock:
                    if sym not in failed:
                        failed.append(sym)
                wq.task_done()

        def _fetch_with_timeout(fn: Callable, sym: str, tf: str, lim: int,
                                timeout_s: float) -> Optional[List[Dict[str, Any]]]:
            """
            带超时的单次 fetch 调用。

            使用全局共享的 _timeout_pool 执行 fetch_fn，超时后自动取消。
            防止某个源的 fetch_fn 卡死（比如网络不通但不报错）导致整个队列阻塞。
            """
            future = _timeout_pool.submit(fn, sym, tf, lim)
            try:
                return future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                logger.warning("[协助层] %s 获取 %s 超时 (%ss)", fn.__name__, sym, timeout_s)
                future.cancel()
                return None

        def _process_symbol(sym: str, source_name: str, fetch_fn: Callable,
                            cfg: SourceConfig) -> bool:
            """
            处理单个 symbol 的获取请求。

            流程:
              1. 检查是否已被其他源成功获取（避免重复工作）
              2. 记录该源已尝试过此 symbol
              3. 调用 fetch_fn 获取数据（带超时保护）
              4. 成功 → 记录结果 + 重置连续失败计数
              5. 失败 → 记录失败 + 递增连续失败计数 + 可能放回队列

            Returns:
                True = 获取成功, False = 获取失败
            """
            # 已被其他源成功获取，跳过
            with results_lock:
                if sym in results:
                    wq.task_done()
                    return True

            # 记录"该源已尝试过此 symbol"
            with symbol_tried_lock:
                symbol_tried.setdefault(sym, set()).add(source_name)

            start_time = time.time()
            try:
                # 调用 fetch_fn（带超时保护）
                bars = _fetch_with_timeout(fetch_fn, sym, timeframe, limit, PER_TASK_TIMEOUT)
                elapsed = time.time() - start_time

                if bars:
                    # 成功
                    cb.record_success(source_name)       # 通知熔断器
                    cfg.record(True, elapsed)            # 记录统计
                    is_first = _mark_success(sym, bars, source_name)
                    _reset_consecutive_fails(source_name)
                    if is_first:
                        wq.task_done()
                    return True
                else:
                    # 失败（返回了空结果）
                    cb.record_failure(source_name, "empty")
                    cfg.record(False, elapsed)
                    _inc_consecutive_fails(source_name)
                    _mark_failed(sym, source_name)       # 可能放回队列
                    return False
            except Exception as e:
                # 失败（抛了异常）
                elapsed = time.time() - start_time
                cb.record_failure(source_name, str(e))
                cfg.record(False, elapsed)
                logger.debug("[协助层] %s 获取 %s 失败: %s", source_name, sym, e)
                _inc_consecutive_fails(source_name)
                _mark_failed(sym, source_name)
                return False

        def _worker(source_name: str, cfg: SourceConfig, fetch_fn: Callable):
            """
            单个源的 worker 线程主循环。

            不断从队列取 symbol → 获取数据 → 成功/失败处理，直到:
              - 队列为空（get() 返回 None）
              - 连续失败过多（>= MAX_SOURCE_FAILS）
              - 源被熔断（cb.is_available 返回 False）
            """
            while True:
                # 检查是否应该退出
                if _get_consecutive_fails(source_name) >= MAX_SOURCE_FAILS:
                    break
                if not cb.is_available(source_name):
                    break

                # 从队列取下一个 symbol
                sym = wq.get()
                if sym is None:
                    break  # 队列为空，退出

                _process_symbol(sym, source_name, fetch_fn, cfg)

        # ── 第四步: 构建线程池并启动 ──
        #
        # 每个源分配 max_workers 个线程，所有源的线程放在同一个线程池里。
        # 例如: tencent(max_workers=3) + sina(max_workers=2) → 总共 5 个线程
        #
        total_threads = 0
        thread_plan = []
        for name, cfg in available:
            fn = source_map[name]
            # 线程数取 max_workers 和 symbols 数量的较小值（没必要开比 symbol 还多的线程）
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

            # 等待所有 worker 完成（加 2 秒余量）
            concurrent.futures.wait(futures, timeout=timeout + 2)

        # ── 第五步: 清理 — 收集剩余未处理的 symbol ──
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

        # 输出统计日志
        stats = " | ".join(cfg.stats_summary() for _, cfg in available)
        logger.info("[协助层] 完成: %d成功 %d失败 | %s", len(results), len(failed), stats)

        return results, failed

    # ================================================================
    # 模式 B: 实时行情 Race — 多源并发抢答
    # ================================================================
    #
    # 和 coordinate_kline 的区别:
    #   coordinate_kline:  N只股票，动态队列，每只股票可能被多个源依次尝试
    #   coordinate_ticker: 1只股票，所有源同时开跑，第一个成功的直接返回
    #
    # 为什么用 Race？
    #   实时行情对延迟敏感。与其等一个源超时再试下一个，不如同时发请求，
    #   谁先返回有效数据就用谁。网络好的源 100ms 就返回了，不用等慢的源 5 秒超时。
    #

    def coordinate_ticker(
        self,
        symbol: str,
        sources: Optional[List[Tuple[str, Callable]]] = None,
        cb: CircuitBreaker = None,
        timeout: float = 8.0,
        preferred_source: str = "",
        market: str = "",
        max_race_sources: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """
        实时行情 Race 模式 — 所有源并发，第一个返回有效价格的直接用。

        典型调用方:
          - CNStockDataSource.get_ticker()
          - KlineService.get_realtime_price()
          - 自选股价格刷新

        Args:
            symbol: 股票代码（单只）
            sources: [(name, fetch_fn), ...]。为 None 时自动发现。
                     fetch_fn 签名: fetch_fn(symbol) -> Dict | None
            cb:      熔断器
            timeout: 超时（秒）
            preferred_source: 指定首选源。如果可用，优先 race 该源。
            market:  市场名称（"CNStock"），用于自动发现源

        Returns:
            第一个成功获取到的有效 Dict（含 last/change/changePercent 等字段），
            全部失败返回 None。
        """
        # ── 获取可用源 ──
        if sources is not None:
            # 手动指定模式
            if not sources:
                return None
            if preferred_source:
                preferred = [(n, fn) for n, fn in sources if n == preferred_source and cb.is_available(n)]
                others = [(n, fn) for n, fn in sources if n != preferred_source and cb.is_available(n)]
                available = preferred + others
            else:
                available = [(name, fn) for name, fn in sources if cb.is_available(name)]
        else:
            # 自动发现模式 — race 模式跳过熔断过滤，所有源都尝试（快的先返回）
            discovered = _discover_sources(
                market=market,
                timeframe="",
                cb=cb,
                preferred_source=preferred_source,
                capability="quote",
                skip_cb_filter=True,
            )
            if not discovered:
                logger.warning("[协助层] ticker %s market=%s 无可用源", symbol, market)
                return None
            available = [(name, fn) for name, fn, _ in discovered]

        if not available:
            logger.warning("[协助层] ticker %s 无可用源", symbol)
            return None

        # 限制抢答源数量 — 按 priority 排序（discovered 已排好序），取前 max_race_sources 个
        if max_race_sources > 0 and len(available) > max_race_sources:
            available = available[:max_race_sources]

        # ── Race: 前 N 个源并发抢答，第一个成功的直接返回 ──
        result_holder: List[Tuple[str, Dict[str, Any]]] = []
        done_event = threading.Event()  # 用于通知"已经有结果了，其他线程可以停了"
        lock = threading.Lock()

        def _race_one(source_name: str, fetch_fn: Callable):
            """
            单个源的 race 任务。

            注意: 即使 done_event 已经被设置（别的源已经成功了），
            这个函数还是会执行完当前的 fetch 调用（无法中断正在进行的网络请求）。
            但下次循环会提前返回。
            """
            if done_event.is_set():
                return  # 别的源已经成功了，不用再试

            try:
                start = time.time()
                result = fetch_fn(symbol)
                elapsed = time.time() - start

                if result and ("last" in result or "price" in result):
                    # 获取到有效数据
                    cb.record_success(source_name)
                    cfg = get_source_config(source_name)
                    cfg.record(True, elapsed)

                    with lock:
                        if not result_holder:
                            # 第一个成功的结果
                            result_holder.append((source_name, result))
                            done_event.set()  # 通知其他线程: 已有结果
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
            # 等待第一个成功的结果，或超时
            done_event.wait(timeout=timeout)

        if result_holder:
            source_name, result = result_holder[0]
            logger.info("[协助层] ticker %s 命中 %s", symbol, source_name)
            return result

        logger.warning("[协助层] ticker %s 所有源失败", symbol)
        return None

    # ================================================================
    # 模式 C: 批量行情 — 优先走 fetch_batch_quotes
    # ================================================================

    def coordinate_batch_quotes(
        self,
        symbols: List[str],
        cb: CircuitBreaker,
        market: str = "",
        timeout: float = 15.0,
        preferred_source: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量行情获取 — 优先走 fetch_batch_quotes，逐源 fallback。

        和 coordinate_ticker 的区别:
          coordinate_ticker:       1只股票 × 多源并发抢答
          coordinate_batch_quotes: N只股票 × 单源批量请求 × 多源 fallback

        流程:
          1. 从 Provider 层发现支持 batch_quote 的源（按 priority 排序）
          2. 逐源尝试 fetch_batch_quotes(symbols)
          3. 第一个成功返回非空结果的直接用
          4. 全部失败返回空 dict

        Args:
            symbols: 股票代码列表
            cb:      熔断器
            market:  市场名称（"CNStock" / "HKStock" / ...）
            timeout: 超时（秒）
            preferred_source: 指定首选源（如 "tencent"），优先尝试

        Returns:
            {symbol: quote_dict} — 仅包含成功获取到的 symbol
        """
        if not symbols:
            return {}

        from app.data_sources.provider import get_providers

        # 发现支持 batch_quote 的源
        providers = get_providers(capability="batch_quote", market=market)
        if not providers:
            logger.warning("[协助层] batch_quotes market=%s 无可用源", market)
            return {}

        # 按 preferred_source 排序
        if preferred_source:
            preferred = [p for p in providers if p.name == preferred_source]
            others = [p for p in providers if p.name != preferred_source]
            providers = preferred + others

        # 逐源尝试
        for p in providers:
            if not cb.is_available(p.name):
                logger.debug("[协助层] batch_quotes %s 已熔断，跳过", p.name)
                continue

            cfg = get_source_config(p.name)
            start = time.time()
            try:
                result = p.fetch_batch_quotes(symbols, timeout=timeout)
                elapsed = time.time() - start

                if result:
                    cb.record_success(p.name)
                    cfg.record(True, elapsed)
                    logger.info("[协助层] batch_quotes %d只 命中 %s (%.2fs)",
                                len(result), p.name, elapsed)
                    return result
                else:
                    cb.record_failure(p.name, "empty")
                    cfg.record(False, elapsed)
                    logger.debug("[协助层] batch_quotes %s 返回空", p.name)
            except Exception as e:
                elapsed = time.time() - start
                cb.record_failure(p.name, str(e))
                cfg.record(False, elapsed)
                logger.debug("[协助层] batch_quotes %s 失败: %s", p.name, e)

        logger.warning("[协助层] batch_quotes %d只 所有源失败", len(symbols))
        return {}

    # ================================================================
    # 模式 D: 全市场批量K线 — 优先走 fetch_market_kline，逐源 fallback
    # ================================================================

    # ================================================================
    # 死源追踪器 — 内存方式，4小时有效期
    # ================================================================

    # _dead_sources: {source_name: last_dead_timestamp}
    # 当 ≥2 个有效源时，超时的源标记为死源，不参与下一轮任务分配
    _dead_sources: Dict[str, float] = {}
    _dead_sources_lock = threading.Lock()
    _DEAD_SOURCE_TTL = 4 * 3600  # 4小时

    def _is_source_dead(self, source_name: str) -> bool:
        """检查源是否被标记为死源（且未过期）"""
        with self._dead_sources_lock:
            ts = self._dead_sources.get(source_name)
            if ts is None:
                return False
            if time.time() - ts > self._DEAD_SOURCE_TTL:
                # 过期，自动恢复
                del self._dead_sources[source_name]
                return False
            return True

    def _mark_source_dead(self, source_name: str):
        """标记源为死源"""
        with self._dead_sources_lock:
            self._dead_sources[source_name] = time.time()
            logger.warning("[协助层] 标记 %s 为死源 (TTL=%dh)", source_name, self._DEAD_SOURCE_TTL // 3600)

    def _mark_source_alive(self, source_name: str):
        """标记源为活源（成功获取数据后调用）"""
        with self._dead_sources_lock:
            self._dead_sources.pop(source_name, None)

    def coordinate_market_kline(
        self,
        cb: CircuitBreaker,
        market: str = "",
        timeframe: str = "1D",
        count: int = 120,
        adj: str = "qfq",
        timeout: float = 15.0,
        preferred_source: str = "",
        start_date: str = "",
        end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量K线 — Coordinator 分组 + 多 Provider 并发取组模式。

        数据流:
          1. 股票按50只一组分组，放入共享队列
          2. 每个支持 kline_batch 的 Provider 启动一个 worker 线程
          3. 各 Provider worker 从队列中取一组，调自己的 fetch_market_kline(symbols=组)
          4. 先完成的 Provider 立即取下一组，直到队列为空
          5. 超时的组放回队尾给其他源重试
          6. 全局超时后，合并所有已获取的数据返回

        Coordinator 只负责分组和调度，不参与具体获取逻辑。
        每个 Provider 内部自行决定并发策略和获取方式。

        Args:
            cb:      熔断器
            market:  市场名称（"CNStock" / "HKStock" / ...）
            timeframe: K线周期（"1D" / "5m" / ...）
            count:   每只股票的数据条数
            adj:     复权方式（"qfq" 前复权 / "hfq" 后复权 / "" 不复权）
            timeout: 全局超时（秒）
            preferred_source: 指定首选源（如 "tencent"），优先尝试
            start_date: 起始日期（"YYYY-MM-DD"）
            end_date:   结束日期（"YYYY-MM-DD"），为空则取今天

        Returns:
            {code: kline_bars} — 合并所有源成功获取的数据
        """
        from app.data_sources.provider import get_providers, NotSupportedResult
        from queue import Queue, Empty

        # ── 第一步: 发现支持 kline_batch 的源 ──
        providers = get_providers(capability="kline_batch", timeframe=timeframe, market=market)

        if not providers:
            logger.warning("[协助层] market_kline market=%s tf=%s 无可用源", market, timeframe)
            return {}

        # 过滤掉已熔断的源和死源
        available_providers = []
        for p in providers:
            if not cb.is_available(p.name):
                logger.debug("[协助层] market_kline %s 已熔断，跳过", p.name)
                continue
            if self._is_source_dead(p.name):
                logger.debug("[协助层] market_kline %s 已标记为死源，跳过", p.name)
                continue
            available_providers.append(p)

        if not available_providers:
            logger.warning("[协助层] market_kline market=%s tf=%s 无可用源(全部熔断/死亡)", market, timeframe)
            return {}

        # 按 preferred_source 排序
        if preferred_source:
            preferred = [p for p in available_providers if p.name == preferred_source]
            others = [p for p in available_providers if p.name != preferred_source]
            available_providers = preferred + others

        # ── 调用 prepare() 做下载前准备，失败的源直接剔除 ──
        prepared_providers = []
        for p in available_providers:
            try:
                prepare_fn = getattr(p, 'prepare', None)
                if prepare_fn and not prepare_fn():
                    logger.warning("[协助层] market_kline %s prepare() 返回 False，跳过", p.name)
                    continue
                prepared_providers.append(p)
            except Exception as e:
                logger.warning("[协助层] market_kline %s prepare() 异常: %s，跳过", p.name, e)
        available_providers = prepared_providers

        if not available_providers:
            logger.warning("[协助层] market_kline market=%s tf=%s 无可用源(prepare全部失败)", market, timeframe)
            return {}

        # ── 第二步: 获取股票列表并分组 ──
        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        all_codes = symbols
        if not all_codes:
            logger.warning("[协助层] market_kline 获取股票列表失败")
            return {}

        # ── 在 coordinator 层解析 count，始终为 int，不留给 provider 走旁路 ──
        if count is None:
            from app.data_sources.provider import calc_kline_count
            from datetime import datetime, timezone, timedelta
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            effective_end = end_date if end_date else today
            effective_start = start_date if start_date else effective_end
            count = calc_kline_count(timeframe, effective_start, effective_end)
            logger.info("[协助层] market_kline count 由 None 解析为 %d (start=%s end=%s)",
                        count, effective_start, effective_end)

        group_size = 50
        groups = [all_codes[i:i + group_size] for i in range(0, len(all_codes), group_size)]
        total_groups = len(groups)

        logger.info("[协助层] market_kline %d只 → %d组(每组%d) → %d源并发: %s",
                    len(all_codes), total_groups, group_size,
                    len(available_providers),
                    " | ".join(p.name for p in available_providers))

        # ── 第三步: 共享任务队列 ──
        task_queue: Queue = Queue()
        for idx, group in enumerate(groups):
            task_queue.put((idx, group, 0))  # (组号, 代码列表, 重试次数)

        # ── 第四步: 共享结果和状态 ──
        result: Dict[str, List[Dict[str, Any]]] = {}
        result_lock = threading.Lock()

        source_stats: Dict[str, Dict[str, int]] = {}
        stats_lock = threading.Lock()

        # 退出条件计数器: 提交时 +1，完成/超时/丢弃时 -1
        # 比 Queue.empty() 可靠 — 不受并发 get/put 竞态影响
        pending_groups = 0
        pending_lock = threading.Lock()

        per_task_timeout = 20.0  # 每组超时 20s
        max_group_retries = 3    # 单组最大重试次数，超过丢弃

        # 全局停止信号
        global_stop = threading.Event()

        # ── 第五步: 非阻塞调度 — Dispatcher + per-Provider executor ──
        #
        # 超时重试策略:
        #   队列充裕 (queued > num_providers) → 超时组放回重试，"多少搞点是点"
        #   队列紧张 (queued ≤ num_providers) → 超时组直接丢弃，不浪费时间
        #   超过 max_group_retries 次 → 无论如何丢弃
        #
        # TODO: Race 模式（第二步优化）
        #   当队列紧张 + 有空闲 Provider 时，将超时组同时提交给多个 Provider，
        #   谁先返回用谁的结果（类似 coordinate_ticker 的抢答模式）。
        #   实现要点:
        #     - _inflight 追踪: {group_idx: [future1, future2, ...]}
        #     - Race 提交: 同一组提交到多个 executor future
        #     - 结果去重: 第一个返回的合并结果，后续返回的丢弃
        #     - 超时判定: 以最早提交时间为准，而非单个 future

        def _fetch_group(provider, group_codes):
            """
            单次获取: 调 Provider.fetch_market_kline，返回 {code: bars}。
            非阻塞提交到 executor 后由 Dispatcher 轮询结果。
            """
            return provider.fetch_market_kline(
                timeframe=timeframe,
                count=count,
                adj=adj,
                timeout=int(per_task_timeout),
                start_date=start_date,
                end_date=end_date,
                symbols=group_codes,
            )

        # 每个 Provider 一个单线程 executor + 状态追踪
        provider_map = {p.name: p for p in available_providers}  # name → provider
        provider_executors = {}
        provider_futures = {}       # name → (future, group_idx, remaining)
        provider_consecutive_timeout = {}

        for p in available_providers:
            provider_executors[p.name] = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"mkline-{p.name}"
            )
            provider_futures[p.name] = None  # 空闲
            provider_consecutive_timeout[p.name] = 0
            with stats_lock:
                source_stats[p.name] = {"ok": 0, "fail": 0, "groups": 0, "timeout": 0}

        def _submit_next(name: str):
            """给 Provider 派下一组（非阻塞）"""
            while True:
                try:
                    group_idx, group_codes, retry_count = task_queue.get_nowait()
                except Empty:
                    return  # 无任务
                with result_lock:
                    remaining = [c for c in group_codes if c not in result]
                if not remaining:
                    continue  # 已被其他源完成，跳过
                future = _timed_submit(
                    provider_executors[name],
                    _fetch_group,
                    provider_map[name],
                    remaining,
                )
                provider_futures[name] = (future, group_idx, remaining, retry_count)
                with pending_lock:
                    nonlocal pending_groups
                    pending_groups += 1
                with stats_lock:
                    source_stats[name]["groups"] += 1
                return

        def _dispatcher():
            """
            主调度线程 — 轮询所有 Provider 的 future：
              - 完成 → 合并结果 → 立刻派下一组
              - 超时 → 标记失败 → 放回队列 → 派下一组
              - 全部空闲且队列为空 → 退出
            """
            nonlocal pending_groups
            while not global_stop.is_set():
                all_idle = True

                for p in available_providers:
                    name = p.name

                    # 跳过已停止的源
                    if name not in provider_executors:
                        continue

                    entry = provider_futures.get(name)
                    if entry is None:
                        # 空闲 → 派任务
                        _submit_next(name)
                        entry = provider_futures.get(name)
                        if entry is None:
                            continue  # 确实无任务

                    future, group_idx, remaining, retry_count = entry

                    # 还没完成 → 跳过，下轮再查
                    if not future.done():
                        all_idle = False
                        continue

                    all_idle = False
                    elapsed = getattr(future, '_start_time', 0)
                    elapsed = time.time() - elapsed if elapsed else 0
                    provider_futures[name] = None  # 标记空闲

                    try:
                        group_result = future.result(timeout=0)
                    except Exception as e:
                        group_result = None
                        logger.debug("[Dispatcher] %s 组%d 异常: %s", name, group_idx, e)

                    if group_result and not isinstance(group_result, NotSupportedResult):
                        # 成功
                        with result_lock:
                            result.update(group_result)
                        with pending_lock:
                            pending_groups -= 1
                        with stats_lock:
                            source_stats[name]["ok"] += len(group_result)
                        cb.record_success(name)
                        self._mark_source_alive(name)
                        provider_consecutive_timeout[name] = 0
                        logger.debug("[Dispatcher] %s 组%d 完成: %d只 (%.1fs)",
                                    name, group_idx, len(group_result), elapsed)

                    elif isinstance(group_result, NotSupportedResult):
                        # 不支持 → 放回队列给其他源 + 该源退出
                        with pending_lock:
                            pending_groups -= 1
                        with result_lock:
                            still_missing = [c for c in remaining if c not in result]
                        if still_missing:
                            task_queue.put((group_idx, still_missing, retry_count))
                        with stats_lock:
                            source_stats[name]["fail"] += 1
                        logger.debug("[Dispatcher] %s 不支持 %s, 退出", name, timeframe)
                        provider_executors[name].shutdown(wait=False)
                        del provider_executors[name]
                        del provider_futures[name]
                        continue

                    elif elapsed > per_task_timeout:
                        # 超时 — 按策略决定重试或丢弃
                        provider_consecutive_timeout[name] += 1
                        with stats_lock:
                            source_stats[name]["timeout"] += 1
                        cb.record_failure(name, "timeout")

                        # 判断是否重试: 队列充裕 + 未超重试上限
                        queued = task_queue.qsize()
                        num_active = len(provider_executors)
                        should_retry = (retry_count < max_group_retries
                                        and queued > num_active)

                        if should_retry:
                            with result_lock:
                                still_missing = [c for c in remaining if c not in result]
                            if still_missing:
                                task_queue.put((group_idx, still_missing, retry_count + 1))
                                logger.debug("[Dispatcher] %s 组%d 超时，放回重试 (%d/%d, 队列%d)",
                                            name, group_idx, retry_count + 1, max_group_retries, queued)
                            else:
                                # 全部已被其他源完成，释放 pending
                                with pending_lock:
                                    pending_groups -= 1
                        else:
                            # 丢弃，释放 pending
                            with pending_lock:
                                pending_groups -= 1
                            logger.debug("[Dispatcher] %s 组%d 超时，丢弃 (%d/%d, 队列%d)",
                                        name, group_idx, retry_count, max_group_retries, queued)

                        # 连续超时 → 停止该源
                        if provider_consecutive_timeout[name] >= 3:
                            if len(provider_executors) >= 2:
                                self._mark_source_dead(name)
                            logger.warning("[Dispatcher] %s 连续超时3次，停止参与", name)
                            provider_executors[name].shutdown(wait=False)
                            del provider_executors[name]
                            del provider_futures[name]
                        else:
                            # 替换 executor: 旧线程还在跑（无法中断），
                            # 新组提交到新 executor 不用等旧线程结束
                            old_ex = provider_executors[name]
                            provider_executors[name] = concurrent.futures.ThreadPoolExecutor(
                                max_workers=1, thread_name_prefix=f"mkline-{name}"
                            )
                            old_ex.shutdown(wait=False)
                        continue

                    else:
                        # 普通失败
                        with pending_lock:
                            pending_groups -= 1
                        with stats_lock:
                            source_stats[name]["fail"] += 1
                        cb.record_failure(name, "empty")

                    # 完成/失败后立刻派下一组
                    _submit_next(name)

                # 所有 Provider 空闲且无在飞任务 → 退出
                if all_idle:
                    with pending_lock:
                        if pending_groups <= 0 and task_queue.empty():
                            break

                time.sleep(0.1)  # 轮询间隔 100ms

        # 给 future 打上提交时间戳（用于超时判断）
        def _timed_submit(executor, fn, *args, **kwargs):
            future = executor.submit(fn, *args, **kwargs)
            future._start_time = time.time()
            return future

        # 启动 Dispatcher 线程
        dispatcher_thread = threading.Thread(
            target=_dispatcher, name="mkline-dispatcher", daemon=True
        )
        dispatcher_thread.start()

        # 等待 Dispatcher 完成或全局超时
        dispatcher_thread.join(timeout=timeout)

        # 标记停止，清理 executor + 残留 future
        global_stop.set()
        for name, entry in list(provider_futures.items()):
            if entry is not None:
                future = entry[0]
                future.cancel()  # 尝试取消（跑中的无法取消，但排队中的可以）
        for name, ex in provider_executors.items():
            ex.shutdown(wait=False, cancel_futures=True)  # Python 3.9+: 取消排队中的 future

        dispatcher_thread.join(timeout=3)

        # ── 第七步: 收集统计信息 ──
        stats_lines = []
        with stats_lock:
            for name, st in source_stats.items():
                dead = self._is_source_dead(name)
                still_active = name in provider_executors
                status = "💀死" if dead else ("⏱停" if not still_active else "✅活")
                stats_lines.append(
                    f"{name}: {st['ok']}只成功 {st['fail']}组失败 "
                    f"{st['groups']}组完成 {st['timeout']}次超时 {status}"
                )

        logger.info("[协助层] market_kline 完成: %d只数据 | %s",
                    len(result), " | ".join(stats_lines))

        return result

    # ================================================================
    # 透传模式 — 不加任何协调逻辑
    # ================================================================

    @staticmethod
    def direct_call(fn: Callable, *args, **kwargs):
        """
        直接调用 — 不加任何并发/重试/熔断逻辑。

        使用场景: 当调用方已经知道自己要调什么、不需要 Coordinator 的调度能力时。
        例如: CNStockDataSource.get_batch_quotes() 直接调用 Provider 的 fetch_batch_quotes()。

        为什么不直接调 fn？
        保留统一入口，方便以后加日志/监控/限流等横切关注点。
        """
        return fn(*args, **kwargs)

    # ================================================================
    # 内部工具方法
    # ================================================================

    def _get_available_sources(
        self,
        market: str,
        source_map: Dict[str, Callable],
        cb: CircuitBreaker,
    ) -> List[Tuple[str, SourceConfig]]:
        """
        获取可用源列表（自动发现模式的 fallback）。

        过滤条件:
          1. source_map 中有对应的 fetch_fn（Provider 层注册了该源）
          2. 熔断器未熔断该源

        排序: 按 SourceConfig.effective_weight 降序（权重高的优先）。

        Args:
            market:    市场名称
            source_map: {name: fetch_fn} — Provider 层注册的源
            cb:        熔断器

        Returns:
            [(源名称, 源配置), ...] — 按权重降序排列
        """
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
        获取可用源列表，但指定源排在第一位。

        用于 preferred_source 场景: 调用方说"我要用 tencent"，
        这个方法确保 tencent 排在第一个，其他源作为 fallback 排后面。

        如果指定源不可用（未注册或已熔断），回退到默认排序并打 warning。
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
# 全局单例
# ================================================================

_coordinator = Coordinator()


def get_coordinator() -> Coordinator:
    """
    获取全局 Coordinator 单例。

    整个应用只有一个 Coordinator 实例（线程安全，内部无状态）。
    调用方: CNStockDataSource, DataSourceFactory, routes/*, services/*
    """
    return _coordinator


def Coordinator_direct_call(fn: Callable, *args, **kwargs):
    """
    Coordinator 的直接调用入口 — 不走动态队列/Race/熔断，直接执行 fn。

    命名来源: Coordinator.direct_call() 的模块级快捷方式。
    前缀 "Coordinator_" 标明出处，避免与普通工具函数混淆。

    使用场景:
      - Provider 层的 batch_quote 等接口形状不适合 coordinate_kline / coordinate_ticker 时
      - 调用方已自行处理错误恢复，不需要 Coordinator 的调度保护

    Args:
        fn: 要直接调用的函数
        *args, **kwargs: 透传给 fn 的参数

    Returns:
        fn 的返回值

    示例:
        from app.data_sources.coordinator import Coordinator_direct_call
        result = Coordinator_direct_call(provider.fetch_batch_quotes, symbols)
    """
    return _coordinator.direct_call(fn, *args, **kwargs)
