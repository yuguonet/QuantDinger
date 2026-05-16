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
from collections import deque
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
            # 已经在熔断中，不重复计数
            if self._state.get(source) == self._OPEN:
                return
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
_realtime_cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=120.0, name="realtime")


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器实例"""
    return _realtime_cb

# ================================================================
# 全局常量
# ================================================================

# 单次 fetch 的超时上限（秒）。
# Coordinator 层的兜底超时，防止某个源的 fetch_fn 卡死导致整个队列阻塞。
# 比 SourceConfig 里的超时更严格 — 这是硬上限。
PER_TASK_TIMEOUT = 60.0

# 队列为空后等待新任务的超时（秒）。
# worker 线程取不到任务时会阻塞等待，超时后认为所有工作已完成，退出循环。
QUEUE_DRAIN_TIMEOUT = 3.0

# 超时辅助线程池 — 长生命周期，避免每次 _fetch_with_timeout 都创建新线程池。
# max_workers=8 足够覆盖并发的超时监控需求（实际 fetch 在主池执行，这里只是等待+取消）。
_timeout_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="coord-timeout"
)
atexit.register(_timeout_pool.shutdown, wait=False)


# ================================================================
# 输入标准化 — 统一的 symbols 去重+加前缀
# ================================================================

def _normalize_symbols(symbols, market: str) -> List[str]:
    """
    输入标准化: 给 symbols 加市场前缀 + 去重（保序）。

    Args:
        symbols: 股票代码列表或逗号分隔字符串
        market:  市场名称（"CNStock" / "HKStock" / ...）

    Returns:
        去重后的带前缀代码列表（如 ["SH600519", "SZ000001"]）
    """
    from app.data_sources.normalizer import add_market_prefix

    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(',') if s.strip()]

    seen: set = set()
    result: List[str] = []
    for s in symbols:
        ns = add_market_prefix(s, market)
        if ns and ns not in seen:
            seen.add(ns)
            result.append(ns)
    return result


def _is_valid_kline(bars) -> bool:
    """
    校验 K 线数据是否有效。

    无效判定:
      - None / 非 list
      - 空 list
      - list 内容不是 dict（如 ["error", "N/A"]）
      - dict 里关键字段全为空/0/NaN

    Returns:
        True = 数据可用, False = 应丢弃并重试
    """
    if not bars or not isinstance(bars, list):
        return False
    if len(bars) == 0:
        return False
    # 至少第一个元素得是 dict
    if not isinstance(bars[0], dict):
        return False
    # 检查是否有至少一个非空值（排除 {"code": "SH600519"} 这种只有 code 没行情的）
    first = bars[0]
    has_data = any(
        v is not None and v != "" and v != 0 and v != "0"
        for k, v in first.items()
        if k not in ("code", "symbol", "name")  # 排除标识字段
    )
    return has_data


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
      2. 过滤掉已熔断的源（_realtime_cb.is_available）
      3. 用适配器把 Provider 的 fetch 方法转成 Coordinator 的 fetch_fn
      4. 如果指定了 preferred_source，将其排到第一位

    Args:
        market:    市场名称（"CNStock" / "HKStock" / "USStock" / ...）
        timeframe: K线周期（"1D" / "5m" / ...）。capability="quote" 时可为空。
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
        if not skip_cb_filter and not _realtime_cb.is_available(p.name):
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
          - 队列空但有 pending 任务 → 阻塞等待（有 pending 时最多等 60s，无 pending 时等 QUEUE_DRAIN_TIMEOUT）
          - 队列空且无 pending 任务 → 返回 None（worker 应退出）

        Returns:
            symbol 字符串，或 None（表示可以退出了）
        """
        with self._cond:
            while not self._items:
                if self._done:
                    return None
                # 有 pending 任务时多等 — 其他 worker 可能失败后 put_back
                # 无 pending 时快速退出 — 确实没活干了
                wait_time = 60.0 if self._pending > 0 else QUEUE_DRAIN_TIMEOUT
                notified = self._cond.wait(timeout=wait_time)
                if not notified and not self._items:
                    return None
            self._pending += 1
            return self._items.pop(0)

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
      - coordinate_ticker:       实时行情（单股Race抢答 / 多股走 coordinate_batch_quotes）
      - coordinate_market_kline: 全市场批量K线（Coordinator分组 + 多Provider并发取组）
      - coordinate_batch_quotes: 批量行情（共享队列 + 多源并发消费）
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
    #   - 每个 symbol 会被所有可用源各试一次，全部失败才算失败
    #

    def coordinate_kline(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
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
                    preferred_source, market, source_map
                )
            else:
                available = self._get_available_sources(market, source_map)
        else:
            # 自动发现模式 — 从 Provider 层获取源
            discovered = _discover_sources(market, timeframe, preferred_source, adj=adj)
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

        # ── 第三步: 定义内部辅助函数 ──

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
                    _realtime_cb.record_success(source_name)       # 通知熔断器
                    cfg.record(True, elapsed)            # 记录统计
                    is_first = _mark_success(sym, bars, source_name)
                    if is_first:
                        wq.task_done()
                    return True
                else:
                    # 失败（返回了空结果）
                    _realtime_cb.record_failure(source_name, "empty")
                    cfg.record(False, elapsed)
                    _mark_failed(sym, source_name)       # 可能放回队列
                    return False
            except Exception as e:
                # 失败（抛了异常）
                elapsed = time.time() - start_time
                _realtime_cb.record_failure(source_name, str(e))
                cfg.record(False, elapsed)
                logger.debug("[协助层] %s 获取 %s 失败: %s", source_name, sym, e)
                _mark_failed(sym, source_name)
                return False

        def _worker(source_name: str, cfg: SourceConfig, fetch_fn: Callable):
            """
            单个源的 worker 线程主循环。

            不断从队列取 symbol → 获取数据 → 成功/失败处理，直到:
              - 队列为空（get() 返回 None）
              - 源被熔断（_realtime_cb.is_available 返回 False）
            """
            while True:
                # 检查是否应该退出
                if not _realtime_cb.is_available(source_name):
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
    # 模式 B: 实时行情 — 单股Race抢答 / 多股走批量接口
    # ================================================================
    #
    # 和 coordinate_kline 的区别:
    #   coordinate_kline:  N只股票，动态队列，每只股票可能被多个源依次尝试
    #   coordinate_ticker: 单股→Race并发抢答 / 多股→coordinate_batch_quotes
    #
    # 单股为什么用 Race？
    #   实时行情对延迟敏感。与其等一个源超时再试下一个，不如同时发请求，
    #   谁先返回有效数据就用谁。网络好的源 100ms 就返回了，不用等慢的源 5 秒超时。
    #
    # 多股为什么用批量接口？
    #   批量行情走 fetch_batch_quotes（单次HTTP拿多只），比逐只 Race 更高效。
    #   preferred_source 参数天然保证首选源优先。
    #

    def coordinate_ticker(
        self,
        symbols,
        sources: Optional[List[Tuple[str, Callable]]] = None,
        timeout: float = 8.0,
        preferred_source: str = "",
        market: str = "",
        max_race_sources: int = 3,
    ) -> Dict[str, Any]:
        """
        实时行情 — 统一入口，自动路由单股/批量。

        路由规则:
          单股 → Race 多源并发抢答，第一个返回有效价格的直接用
          多股 → coordinate_batch_quotes 共享队列批量调度

        典型调用方:
          - CNStockDataSource.get_ticker()
          - KlineService.get_realtime_price()
          - 自选股价格刷新

        Args:
            symbols: 股票代码，str 或 List[str]。str 可含逗号（自动拆分）。
            sources: [(name, fetch_fn), ...]。为 None 时自动发现。
                     fetch_fn 签名: fetch_fn(symbol) -> Dict | None
            timeout: 超时（秒）
            preferred_source: 指定首选源。如果可用，优先使用。
            market:  市场名称（"CNStock"），用于自动发现源

        Returns:
            {symbol: quote_dict} — 仅包含成功获取到的 symbol。
            全部失败返回空 dict。
        """
        # ── 入口标准化 ──
        if isinstance(symbols, str):
            sym_list = [s.strip() for s in symbols.split(',') if s.strip()]
        else:
            sym_list = [s.strip() for s in symbols if s and s.strip()]

        if not sym_list:
            return {}

        if len(sym_list) == 1:
            # 单股 → Race 抢答
            result = self._ticker_race(
                symbol=sym_list[0],
                sources=sources,
                timeout=timeout,
                preferred_source=preferred_source,
                market=market,
                max_race_sources=max_race_sources,
            )
            return {sym_list[0]: result} if result else {}
        else:
            # 多股 → 共享队列批量调度
            return self._ticker_batch(
                symbols=sym_list,
                market=market,
                timeout=timeout,
                preferred_source=preferred_source,
            )

    def _ticker_race(
        self,
        symbol: str,
        sources: Optional[List[Tuple[str, Callable]]],
        timeout: float,
        preferred_source: str,
        market: str,
        max_race_sources: int,
    ) -> Optional[Dict[str, Any]]:
        """
        单股 Race 抢答 — 所有源并发，第一个返回有效价格的直接用。

        Args:
            symbol: 股票代码（单只）

        Returns:
            第一个成功获取到的有效 Dict，全部失败返回 None。
        """
        # ── 获取可用源 ──
        if sources is not None:
            if not sources:
                return None
            if preferred_source:
                preferred = [(n, fn) for n, fn in sources if n == preferred_source and _realtime_cb.is_available(n)]
                others = [(n, fn) for n, fn in sources if n != preferred_source and _realtime_cb.is_available(n)]
                available = preferred + others
            else:
                available = [(name, fn) for name, fn in sources if _realtime_cb.is_available(name)]
        else:
            discovered = _discover_sources(
                market=market,
                timeframe="",
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

        if max_race_sources > 0 and len(available) > max_race_sources:
            available = available[:max_race_sources]

        # ── Race: 并发抢答 ──
        result_holder: List[Tuple[str, Dict[str, Any]]] = []
        done_event = threading.Event()
        lock = threading.Lock()

        def _race_one(source_name: str, fetch_fn: Callable):
            if done_event.is_set():
                return
            if not _realtime_cb.is_available(source_name):
                return
            try:
                start = time.time()
                result = fetch_fn(symbol)
                elapsed = time.time() - start

                if result and ("last" in result or "price" in result):
                    _realtime_cb.record_success(source_name)
                    cfg = get_source_config(source_name)
                    cfg.record(True, elapsed)
                    with lock:
                        if not result_holder:
                            result_holder.append((source_name, result))
                            done_event.set()
                else:
                    _realtime_cb.record_failure(source_name, "empty")
                    cfg = get_source_config(source_name)
                    cfg.record(False, elapsed)
            except Exception as e:
                _realtime_cb.record_failure(source_name, str(e))
                cfg = get_source_config(source_name)
                cfg.record(False, 0)
                logger.debug("[协助层] ticker %s %s 失败: %s", source_name, symbol, e)

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(available), thread_name_prefix="ticker-race"
        )
        try:
            futures = [pool.submit(_race_one, name, fn) for name, fn in available]
            done_event.wait(timeout=timeout)
        finally:
            pool.shutdown(wait=False)

        if result_holder:
            source_name, result = result_holder[0]
            logger.info("[协助层] ticker %s 命中 %s", symbol, source_name)
            return result

        logger.warning("[协助层] ticker %s 所有源失败", symbol)
        return None

    def _ticker_batch(
        self,
        symbols: List[str],
        market: str,
        timeout: float,
        preferred_source: str = "",
    ) -> Dict[str, Any]:
        """批量行情 — 委托给 coordinate_batch_quotes。"""
        return self.coordinate_batch_quotes(
            symbols=symbols, market=market, timeout=timeout,
            preferred_source=preferred_source,
        )

    # ================================================================
    # 模式 C: 批量行情 — 优先走 fetch_batch_quotes
    # ================================================================

    _BATCH_GROUP_SIZE = 500       # 分组每组 500 只

    @staticmethod
    def _normalize_market_kline_result(
        raw: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        标准化全市场K线返回结果:
          - key 统一为纯数字代码
        """
        from app.data_sources.normalizer import strip_market_prefix
        out: Dict[str, List[Dict[str, Any]]] = {}
        for key, bars in raw.items():
            out[strip_market_prefix(key)] = bars
        return out

    def coordinate_batch_quotes(
        self,
        symbols: List[str],
        market: str = "",
        timeout: float = 60.0,
        preferred_source: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量行情获取 — 共享队列 + 多源并发消费 + symbol 级别失败追踪。

        按 _BATCH_GROUP_SIZE 分组放入共享队列，每个 Provider 开 N 个 worker 线程
        并发消费。部分返回时只把缺失的 symbol 放回队列，所有源都试过才标记失败。

        Args:
            symbols: 股票代码列表（纯数字或带前缀均可）
            market:  市场名称（"CNStock" / "HKStock" / ...）
            timeout: 超时（秒）
            preferred_source: 指定首选源（如 "tencent"），优先尝试

        Returns:
            {纯数字代码: quote_dict} — 成功获取到的 symbol
        """
        if not symbols:
            return {}

        from app.data_sources.provider import get_providers

        # 输入标准化
        normalized_symbols = _normalize_symbols(symbols, market)
        if not normalized_symbols:
            return {}

        # 发现支持 batch_quote 的源
        providers = get_providers(capability="batch_quote", market=market)
        if not providers:
            logger.warning("[协助层] batch_quotes market=%s 无可用源", market)
            return {}

        # preferred_source 排序 + 熔断过滤
        if preferred_source:
            preferred = [p for p in providers if p.name == preferred_source]
            others = [p for p in providers if p.name != preferred_source]
            providers = preferred + others

        available = [p for p in providers if _realtime_cb.is_available(p.name)]
        if not available:
            logger.warning("[协助层] batch_quotes market=%s 所有源已熔断", market)
            return {}

        raw, failed = self._batch_quotes_queue(normalized_symbols, available, timeout)
        if failed:
            logger.warning("[协助层] batch_quotes %d只失败: %s", len(failed), failed[:10])
        return raw

    def _batch_quotes_queue(
        self,
        symbols: List[str],
        available: list,
        timeout: float,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """
        工作队列 — 每个 worker 独占一批，先回来先取，失败放回队尾。

        原理（假设 5000 只股票，10 个 worker）:
          1. 5000 只进入队列
          2. 10 个 worker 各取 500 只（独占，不重复）
          3. 先返回的 worker 立刻取下一批 500 只
          4. 成功 → 移除；失败 → 放回队尾
          5. 直到队列空

        没有重复抢，没有锁竞争，快源自然多干，慢源自然少干。
        """
        from collections import deque
        from app.data_sources.normalizer import strip_market_prefix

        group_size = self._BATCH_GROUP_SIZE
        num_sources = len(available)

        logger.info("[协助层] batch_quotes %d只 → %d源/%d线程 (batch=%d): %s",
                    len(symbols), num_sources,
                    sum(min(get_source_config(p.name).max_workers,
                            max(1, len(symbols) // group_size + 1))
                        for p in available),
                    group_size,
                    " | ".join(p.name for p in available))

        # 工作队列: (code, tried_sources_set)
        queue: deque = deque()
        for s in symbols:
            queue.append((strip_market_prefix(s), set()))
        queue_lock = threading.Lock()

        # 结果
        result_list: List[Dict[str, Any]] = []
        completed: Set[str] = set()
        failed_set: Set[str] = set()
        failed: List[str] = []
        result_lock = threading.Lock()

        global_stop = threading.Event()
        per_task_timeout = 8.0

        stats: Dict[str, Dict[str, int]] = {
            p.name: {"ok": 0, "fail": 0, "batches": 0} for p in available
        }
        stats_lock = threading.Lock()

        def _grab(provider_name: str, n: int) -> Tuple[List[str], List[set]]:
            """从队列取 n 只。跳过该源已经试过的。返回 (codes, tried_sets)。"""
            with queue_lock:
                codes = []
                tried_list = []
                deferred = []
                while queue and len(codes) < n:
                    code, tried = queue.popleft()
                    if code in completed or code in failed_set:
                        continue
                    if provider_name in tried:
                        deferred.append((code, tried))
                        continue
                    codes.append(code)
                    tried_list.append(tried)
                for item in deferred:
                    queue.append(item)
                return codes, tried_list

        def _submit(provider_name: str, requested: list, tried_list: list, task_result):
            """提交结果。成功→移除；失败→标记该源已试，放回队尾供其他源试。"""
            ok_syms = []

            if task_result:
                with result_lock:
                    for psym, quote in task_result.items():
                        d = strip_market_prefix(psym)
                        if d in completed or d in failed_set:
                            continue
                        quote["symbol"] = d
                        rn = quote.get("name", "")
                        if rn and strip_market_prefix(rn) == d:
                            quote["name"] = ""
                        result_list.append(quote)
                        completed.add(d)
                        ok_syms.append(d)

            # 未返回的 = 失败 → 标记该源已试，放回队尾
            returned_digits = {strip_market_prefix(s) for s in (task_result or {}).keys()}
            fail_items = []
            for i, sym in enumerate(requested):
                d = strip_market_prefix(sym) if not sym.isdigit() else sym
                if d not in returned_digits and d not in completed:
                    tried = tried_list[i] if i < len(tried_list) else set()
                    tried.add(provider_name)
                    if len(tried) >= num_sources:
                        # 所有源都试过了，标记失败
                        with result_lock:
                            if d not in failed_set and d not in completed:
                                failed_set.add(d)
                                failed.append(d)
                    else:
                        fail_items.append((d, tried))

            if fail_items:
                with queue_lock:
                    for code, tried in fail_items:
                        queue.append((code, tried))

            return ok_syms

        def _worker(provider):
            my_ok = 0
            while not global_stop.is_set():
                batch, tried_list = _grab(provider.name, group_size)
                if not batch:
                    return

                try:
                    task_result = provider.fetch_batch_quotes(
                        batch, timeout=int(per_task_timeout)
                    )
                    ok = _submit(provider.name, batch, tried_list, task_result)
                    my_ok += len(ok)
                    with stats_lock:
                        stats[provider.name]["ok"] += len(ok)
                        stats[provider.name]["batches"] += 1
                    if task_result:
                        _realtime_cb.record_success(provider.name)
                    else:
                        stats[provider.name]["fail"] += 1
                        _realtime_cb.record_failure(provider.name, "empty")
                        if my_ok == 0:
                            return
                except Exception as e:
                    _submit(provider.name, batch, tried_list, None)
                    with stats_lock:
                        stats[provider.name]["fail"] += 1
                    _realtime_cb.record_failure(provider.name, str(e))
                    if my_ok == 0:
                        return

        # 启动所有 worker
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=sum(min(get_source_config(p.name).max_workers,
                                max(1, len(symbols) // group_size + 1))
                            for p in available),
            thread_name_prefix="bq"
        )
        all_futures = []
        for provider in available:
            cfg = get_source_config(provider.name)
            n = min(cfg.max_workers, max(1, len(symbols) // group_size + 1))
            for _ in range(n):
                all_futures.append(pool.submit(_worker, provider))

        concurrent.futures.wait(all_futures, timeout=timeout)
        global_stop.set()
        pool.shutdown(wait=False)

        # 剩余补漏
        remaining_codes, remaining_tried = _grab("", group_size * 10)
        if remaining_codes:
            for provider in available:
                if not remaining_codes:
                    break
                batch = remaining_codes[:group_size]
                batch_tried = remaining_tried[:group_size]
                try:
                    r = provider.fetch_batch_quotes(batch, timeout=int(per_task_timeout))
                    if r:
                        _submit(provider.name, batch, batch_tried, r)
                        remaining_codes = [s for s in remaining_codes if s not in completed]
                        remaining_tried = remaining_tried[len(batch):]
                except Exception:
                    pass
            if remaining_codes:
                with result_lock:
                    for sym in remaining_codes:
                        if sym not in completed and sym not in failed_set:
                            failed_set.add(sym)
                            failed.append(sym)

        stat_parts = []
        for name, s in stats.items():
            if s["batches"] > 0 or s["fail"] > 0:
                stat_parts.append(f"{name}: {s['ok']}只/{s['batches']}批")
        logger.info("[协助层] batch_quotes 完成 %d成功 %d失败 | %s",
                    len(result_list), len(failed), " | ".join(stat_parts))

        return {q["symbol"]: q for q in result_list}, failed

    # ================================================================
    # ================================================================
    # 模式 D: 全市场批量K线 — 长效线程 + 主池/重试池
    # ================================================================

    def coordinate_market_kline(
        self,
        market: str = "",
        timeframe: str = "1D",
        count: int = 120,
        adj: str = "qfq",
        timeout: float = 30.0,
        preferred_source: str = "",
        start_date: str = "",
        end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量K线 — 长效线程 + 主池/重试池 + 立即退出。

        核心设计:
          1. 每个源按 MAX_CONCURRENCY 开线程，每个线程是长效的，循环取任务
          2. 主池: 所有待拉取的 symbol，线程各自从池中取
          3. 重试池: 失败/超时的 symbol 放入重试池，标记已试过的源
             - 空闲线程从重试池取"自己没试过的" symbol 继续拉
             - 重试池无锁共享: 哪个线程先拿到有效数据，就把 symbol 从重试池删掉
          4. 彻底失败: 重试池中某 symbol 所有源都试过了 → 删除并记彻底失败+1
          5. 立即退出: 成功数 + 彻底失败数 = 总数 → 立即返回
          6. 数据有效性: 以数据有效且谁先回的为准，后回的直接丢弃

        线程生命周期:
          - 长效: 不因单次失败退出，持续循环取下一个任务
          - 超时保护: 单次 fetch 超时后，将 symbol 归还重试池，标记该源失败，继续下一个
          - 退出条件: stop event 被 set（done+failed=total 或超时兜底）

        Args:
            market: 市场名称（"CNStock" / "HKStock" / ...）
            timeframe: K线周期（"1D" / "5m" / ...）
            count: 每只股票的K线条数
            adj: 复权方式（"qfq" / "hfq" / ""）
            timeout: 总超时（秒），兜底安全阀
            preferred_source: 指定首选源
            start_date: 起始日期
            end_date: 结束日期
            symbols: 股票代码列表，为 None 时自动获取全市场

        Returns:
            {symbol: kline_bars} — 仅包含成功获取的数据
        """
        from collections import deque
        from app.data_sources.provider import get_providers, NotSupportedResult
        from app.data_sources.normalizer import strip_market_prefix

        # ── 第一步: 发现源 + 过滤 ──
        providers = get_providers(capability="kline_batch", timeframe=timeframe, market=market)
        if not providers:
            logger.warning("[market_kline] market=%s tf=%s 无可用源", market, timeframe)
            return {}

        # 熔断过滤
        available = [p for p in providers if _realtime_cb.is_available(p.name)]
        if not available:
            logger.warning("[market_kline] market=%s tf=%s 无可用源(全部熔断)", market, timeframe)
            return {}

        # preferred_source 排第一
        if preferred_source:
            preferred = [p for p in available if p.name == preferred_source]
            others = [p for p in available if p.name != preferred_source]
            available = preferred + others

        # prepare() 过滤
        prepared = []
        for p in available:
            try:
                if getattr(p, 'prepare', None) and not p.prepare():
                    logger.warning("[market_kline] %s prepare() 失败，跳过", p.name)
                    continue
                prepared.append(p)
            except Exception as e:
                logger.warning("[market_kline] %s prepare() 异常: %s，跳过", p.name, e)
        available = prepared

        # 过滤掉没有 fetch_market_kline 方法的源
        available = [p for p in available if getattr(p, 'fetch_market_kline', None)]
        if not available:
            logger.warning("[market_kline] market=%s tf=%s 无可用源(无 fetch_market_kline)", market, timeframe)
            return {}

        # NotSupportedResult 预检测: 开线程前先测一只，不支持的源直接排除
        still_available = []
        test_sym_for_filter = symbols[0] if symbols else None
        for p in available:
            if test_sym_for_filter:
                try:
                    test_result = p.fetch_market_kline(
                        timeframe=timeframe, count=1, adj=adj,
                        timeout=5, symbols=[test_sym_for_filter],
                    )
                    if isinstance(test_result, NotSupportedResult):
                        logger.info("[market_kline] %s 返回 NotSupportedResult，排除", p.name)
                        continue
                except Exception:
                    pass  # 预测异常不排除，让正式运行时处理
            still_available.append(p)
        available = still_available

        if not available:
            logger.warning("[market_kline] 无可用源(全部 NotSupported)")
            return {}

        # ── 第二步: 获取股票列表 + 标准化 ──
        if not symbols:
            from app.utils.basicinfo_db import get_stock_basic_db
            symbols = get_stock_basic_db().market_all_codes(status="active")
        if not symbols:
            logger.warning("[market_kline] 获取股票列表失败")
            return {}

        all_codes = _normalize_symbols(symbols, market)
        if not all_codes:
            logger.warning("[market_kline] 标准化后无有效代码")
            return {}

        total = len(all_codes)

        # count 解析
        if count is None:
            from app.data_sources.provider import calc_kline_count
            from datetime import datetime, timezone, timedelta
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            effective_end = end_date if end_date else today
            effective_start = start_date if start_date else effective_end
            count = calc_kline_count(timeframe, effective_start, effective_end)

        # ── 第三步: 确定每个源的线程数 ──
        # 规则: 有 max_concurrency → 用它；没有 → 1 + 警告
        source_threads: Dict[str, int] = {}
        for p in available:
            mc = getattr(p, 'max_concurrency', None)
            if mc is None:
                logger.warning("[market_kline] %s 未定义 MAX_CONCURRENCY，默认开1个线程", p.name)
                mc = 1
            source_threads[p.name] = mc

        logger.info("[market_kline] %d只 → %d源: %s",
                    total, len(available),
                    " | ".join(f"{p.name}({source_threads[p.name]}线程)" for p in available))

        # ── 第四步: 构建主池 + 重试池 + 共享状态 ──

        num_sources = len(available)

        # 主池: 待拉取的 symbol
        # 规则: 代码数 < 源数 → 主池为空，全部放重试池（让所有源都有机会试）
        main_pool: deque = deque() if total < num_sources else deque(all_codes)
        main_pool_lock = threading.Lock()

        # 重试池: 仅存 symbol，失败记录在 source_fails 中
        retry_pool: deque = deque(all_codes) if total < num_sources else deque()
        retry_pool_lock = threading.Lock()

        # 结果: 谁先回有效数据算谁的，后回的丢弃
        results: Dict[str, List[Dict[str, Any]]] = {}
        results_lock = threading.Lock()

        # 彻底失败计数: 所有源都试过仍失败
        permanent_fail_count = [0]
        permanent_fail_lock = threading.Lock()

        # 完成计数: 成功 + 彻底失败，用于判断退出
        done_count = [0]
        done_lock = threading.Lock()

        # 每个源的失败表: source_name → {symbol_1, symbol_2, ...}
        # 同一源的多个线程共享同一张失败表（不是按线程建表）
        source_fails: Dict[str, Set[str]] = {p.name: set() for p in available}
        source_fails_lock = threading.Lock()

        # 彻底失败集合: 已被2个源试过失败的 symbol（用于从重试池中过滤）
        permanent_fail: Set[str] = set()
        permanent_fail_set_lock = threading.Lock()

        # 全局停止信号: done_count == total 时 set，通知所有线程退出
        stop = threading.Event()

        # 统计
        source_stats: Dict[str, Dict[str, int]] = {
            p.name: {"ok": 0, "fail": 0, "timeout": 0} for p in available
        }
        stats_lock = threading.Lock()

        _PER_TASK_TIMEOUT = PER_TASK_TIMEOUT  # 使用全局常量（60s硬上限）

        # ── 第五步: 内部辅助函数 ──

        def _check_done():
            """检查是否所有 symbol 都有了结果（成功或彻底失败），是则 set stop"""
            with done_lock:
                if done_count[0] >= total:
                    stop.set()

        def _get_symbol(source_name: str) -> Optional[str]:
            """
            从主池或重试池取一个 symbol。

            优先主池（新 symbol），主池空了再看重试池（失败过的 symbol）。
            重试池中只取"该源失败表中没有的" symbol。
            都没有 → 返回 None。
            """
            # 先从主池取
            with main_pool_lock:
                if main_pool:
                    return main_pool.popleft()

            # 主池空了，从重试池取"该源失败表中没有的"
            my_fails = source_fails[source_name]
            with retry_pool_lock:
                deferred = []
                found = None
                while retry_pool:
                    sym = retry_pool.popleft()
                    # 已彻底失败的 symbol 直接丢弃
                    with permanent_fail_set_lock:
                        if sym in permanent_fail:
                            continue
                    if sym in my_fails:
                        deferred.append(sym)
                    else:
                        found = sym
                        break
                # 放回跳过的
                for item in deferred:
                    retry_pool.append(item)
                return found

        def _return_to_retry(sym: str, source_name: str):
            """
            将 symbol 放入重试池（失败/超时后归还）。

            记录到该源的失败表。检查所有源是否都已试过此 symbol → 是则彻底失败。
            """
            with source_fails_lock:
                source_fails[source_name].add(sym)
                # 统计多少个源在此 symbol 上失败了
                fail_count = sum(1 for src in available if sym in source_fails[src.name])

            # 4个源试过就记为彻底失败，从重试池删除
            if fail_count >= 4:
                with permanent_fail_set_lock:
                    permanent_fail.add(sym)
                with permanent_fail_lock:
                    permanent_fail_count[0] += 1
                with done_lock:
                    done_count[0] += 1
                _check_done()
                return

            # 还有源没试 → 放入重试池
            with retry_pool_lock:
                retry_pool.append(sym)

        def _mark_success(sym: str, bars: List[Dict[str, Any]], source_name: str) -> bool:
            """
            标记成功。第一个返回有效数据的算数，后回的丢弃。

            Returns:
                True = 首次成功，计入 done_count
                False = 已被其他源抢先，丢弃
            """
            with results_lock:
                if sym in results:
                    return False  # 已被其他源抢先，丢弃
                results[sym] = bars

            with done_lock:
                done_count[0] += 1
            _check_done()
            return True

        def _try_fetch(source_name: str, provider, sym: str) -> bool:
            """
            尝试用指定源拉取一个 symbol 的K线。

            流程:
              1. 检查是否已被其他源完成（跳过）
              2. 调用 fetch_market_kline(symbols=[sym])
              3. 校验数据有效性 (_is_valid_kline)
              4. 有效 → _mark_success（首次成功才计入，后回丢弃）
              5. 无效/空/异常 → _return_to_retry（标记源失败，放回重试池）

            Returns:
                True = 成功或已由其他源完成
                False = 失败（已归还重试池或标记彻底失败）
            """
            # 已被其他源完成，直接跳过
            with results_lock:
                if sym in results:
                    return True

            start = time.time()
            try:
                task_result = provider.fetch_market_kline(
                    timeframe=timeframe, count=count,
                    adj=adj, timeout=int(_PER_TASK_TIMEOUT),
                    start_date=start_date, end_date=end_date,
                    symbols=[sym],
                )
                elapsed = time.time() - start

                # NotSupported → 该源不支持，视为失败
                if isinstance(task_result, NotSupportedResult):
                    with stats_lock:
                        source_stats[source_name]["fail"] += 1
                    _realtime_cb.record_failure(source_name, "not_supported")
                    _return_to_retry(sym, source_name)
                    return False

                # 有返回数据 → 校验有效性
                if task_result:
                    # 兼容: 返回的 key 可能带前缀也可能不带
                    bars = task_result.get(sym)
                    if bars is None:
                        bars = task_result.get(strip_market_prefix(sym))
                    if bars and _is_valid_kline(bars):
                        # 有效数据 → 尝试标记成功
                        _realtime_cb.record_success(source_name)
                        cfg = get_source_config(source_name)
                        cfg.record(True, elapsed)
                        is_first = _mark_success(sym, bars, source_name)
                        if is_first:
                            with stats_lock:
                                source_stats[source_name]["ok"] += 1
                        return True

                # 空结果或无效数据 → 失败
                _realtime_cb.record_failure(source_name, "empty/invalid")
                cfg = get_source_config(source_name)
                cfg.record(False, elapsed)
                with stats_lock:
                    source_stats[source_name]["fail"] += 1
                _return_to_retry(sym, source_name)
                return False

            except Exception as e:
                elapsed = time.time() - start
                is_timeout = elapsed > _PER_TASK_TIMEOUT
                _realtime_cb.record_failure(source_name, str(e))
                cfg = get_source_config(source_name)
                cfg.record(False, elapsed)
                with stats_lock:
                    if is_timeout:
                        source_stats[source_name]["timeout"] += 1
                    else:
                        source_stats[source_name]["fail"] += 1
                _return_to_retry(sym, source_name)
                return False

        def _worker(provider):
            """
            长效线程主循环 — 不断从主池/重试池取 symbol，拉数据，直到 stop。

            线程不因单次失败退出，持续循环取下一个任务。
            超时后自动将 symbol 归还重试池，标记源失败，继续下一个。
            池子空了等待通知，有新 symbol（重试池被放回）时继续工作。
            """
            name = provider.name
            while not stop.is_set():
                sym = _get_symbol(name)
                if sym is None:
                    # 池子空了，等待: 可能有新 symbol 被放回重试池，或 stop 信号
                    if stop.wait(timeout=2.0):
                        break
                    # 再试一次取 symbol
                    sym = _get_symbol(name)
                    if sym is None:
                        break  # 真的没有了，退出
                _try_fetch(name, provider, sym)

        # ── 第六步: 启动线程池 ──
        total_threads = sum(source_threads[p.name] for p in available)

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=total_threads, thread_name_prefix="mkline"
        )
        all_futures = []
        for provider in available:
            n = source_threads[provider.name]
            for _ in range(n):
                all_futures.append(pool.submit(_worker, provider))

        # 等待: done_count == total 立即返回，否则等到 timeout
        stop.wait(timeout=timeout)
        stop.set()  # 确保所有线程收到退出信号
        pool.shutdown(wait=False)

        # ── 第七步: 收集未处理的 symbol → 彻底失败 ──
        # 主池剩余
        with main_pool_lock:
            while main_pool:
                sym = main_pool.popleft()
                with results_lock:
                    if sym not in results:
                        with permanent_fail_lock:
                            permanent_fail_count[0] += 1

        # 重试池剩余
        with retry_pool_lock:
            while retry_pool:
                sym = retry_pool.popleft()
                with results_lock:
                    if sym not in results:
                        with permanent_fail_lock:
                            permanent_fail_count[0] += 1

        # ── 第八步: 统计 ──
        stats_lines = []
        for name, s in source_stats.items():
            if s["ok"] > 0 or s["fail"] > 0 or s["timeout"] > 0:
                stats_lines.append(f"{name}: {s['ok']}成功 {s['fail']}失败 {s['timeout']}超时")
        logger.info("[market_kline] 完成: %d成功 %d彻底失败 %d只 | %s",
                    len(results), permanent_fail_count[0], total,
                    " | ".join(stats_lines))

        return results

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
            if not _realtime_cb.is_available(cfg.name):
                logger.debug("[协助层] 源 %s 已熔断，跳过", cfg.name)
                continue
            available.append((cfg.name, cfg))

        return available

    def _get_preferred_available(
        self,
        preferred: str,
        market: str,
        source_map: Dict[str, Callable],
    ) -> List[Tuple[str, SourceConfig]]:
        """
        获取可用源列表，但指定源排在第一位。

        用于 preferred_source 场景: 调用方说"我要用 tencent"，
        这个方法确保 tencent 排在第一个，其他源作为 fallback 排后面。

        如果指定源不可用（未注册或已熔断），回退到默认排序并打 warning。
        """
        all_available = self._get_available_sources(market, source_map)

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
