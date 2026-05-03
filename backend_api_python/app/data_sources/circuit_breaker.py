# -*- coding: utf-8 -*-
"""
熔断器模块 — 数据源故障保护

模块职责:
    实现熔断器模式 (Circuit Breaker Pattern)，当某个数据源连续失败超过阈值时，
    自动"熔断"（拒绝后续请求），经过冷却期后进入半开状态尝试恢复。
    防止故障数据源拖慢整个系统。

设计原理 — 熔断器状态机:
    ┌─────────┐  连续失败 ≥ 阈值  ┌──────┐  冷却期结束  ┌───────────┐
    │ Closed  │ ───────────────→ │ Open │ ──────────→ │ Half-Open │
    │ (正常)  │                   │(熔断)│             │ (试探)    │
    └─────────┘ ←─────────────── └──────┘ ←────────── └───────────┘
         ↑          请求成功                        │
         └──────────────────────────────────────────┘  请求失败 → 回到 Open

    - Closed (正常): 请求正常通过，记录失败次数
    - Open (熔断): 所有请求立即拒绝，不再访问故障源
    - Half-Open (试探): 冷却期结束后，允许一次请求通过试探恢复

在架构中的位置:
    数据源层 — 位于 Provider 和限流器之间，作为请求的最后守门人

关键依赖:
    - app.utils.logger: 日志记录
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    """
    熔断器 — 连续失败超阈值则熔断，冷却后恢复。

    每个数据源可配置独立的熔断器实例，支持按源名称分别跟踪故障状态。

    设计模式:
        状态模式 — 内部通过 _tripped_at 字典跟踪每个源的熔断状态

    线程安全性:
        使用 threading.Lock 保护所有状态读写，多线程并发调用安全。

    关键属性:
        _failure_threshold: 连续失败多少次触发熔断
        _cooldown_seconds: 熔断后的冷却时间（秒）
        _failures: {source: 连续失败次数} 的映射
        _tripped_at: {source: 熔断触发时间戳} 的映射

    Args:
        failure_threshold: 触发熔断的连续失败次数阈值
        cooldown_seconds: 熔断后的冷却时间（秒）
        name: 熔断器名称，用于日志区分

    Examples:
        >>> cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=300)
        >>> if cb.is_available("tencent"):
        ...     try:
        ...         data = fetch()
        ...         cb.record_success("tencent")
        ...     except Exception as e:
        ...         cb.record_failure("tencent", str(e))
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        name: str = "default",
    ):
        """
        初始化熔断器。

        Args:
            failure_threshold: 触发熔断的连续失败次数阈值
            cooldown_seconds:  熔断后的冷却时间 (秒)
            name:              熔断器名称 (用于日志标识)
        """
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._name = name
        self._failures: Dict[str, int] = {}
        self._tripped_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_available(self, source: str) -> bool:
        """
        检查指定数据源是否可用（未处于熔断状态）。

        状态判断逻辑:
        1. 如果源不在 _tripped_at 中 → 可用（Closed 状态）
        2. 如果冷却期已过 → 可用（从 Open → Half-Open，清除熔断标记）
        3. 否则 → 不可用（Open 状态，仍在冷却期中）

        Args:
            source: 数据源名称

        Returns:
            True 表示可用，False 表示已熔断
        """
        with self._lock:
            if source not in self._tripped_at:
                # Closed 状态：从未熔断或已恢复
                return True
            elapsed = time.time() - self._tripped_at[source]
            if elapsed >= self._cooldown_seconds:
                # 冷却期结束，进入 Half-Open 状态，允许试探
                del self._tripped_at[source]
                self._failures[source] = 0
                logger.info("[熔断器:%s] %s 冷却结束，恢复可用", self._name, source)
                return True
            # Open 状态：仍在冷却期，拒绝请求
            return False

    def record_success(self, source: str):
        """
        记录请求成功 — 重置失败计数。

        当源处于 Half-Open 状态时，一次成功即恢复到 Closed 状态。
        当源处于 Closed 状态时，清零失败计数器。

        Args:
            source: 数据源名称
        """
        with self._lock:
            self._failures[source] = 0
            if source in self._tripped_at:
                del self._tripped_at[source]

    def record_failure(self, source: str, reason: str = ""):
        """
        记录请求失败 — 累加失败计数，超阈值则触发熔断。

        每次调用将该源的连续失败计数 +1，当计数达到 failure_threshold 时，
        记录熔断触发时间，进入 Open 状态。

        Args:
            source: 数据源名称
            reason: 失败原因，记录到日志便于排查
        """
        with self._lock:
            self._failures[source] = self._failures.get(source, 0) + 1
            if self._failures[source] >= self._failure_threshold:
                # 达到阈值，触发熔断
                self._tripped_at[source] = time.time()
                logger.warning(
                    "[熔断器:%s] %s 连续失败 %d 次，熔断 %ds (原因: %s)",
                    self._name, source, self._failures[source],
                    self._cooldown_seconds, reason,
                )

    def reset(self, source: str = None):
        """
        手动重置熔断器。

        Args:
            source: 指定数据源名称则只重置该源，为 None 则重置所有源
        """
        with self._lock:
            if source:
                self._failures.pop(source, None)
                self._tripped_at.pop(source, None)
            else:
                self._failures.clear()
                self._tripped_at.clear()


# ================================================================
# 全局熔断器实例
# ================================================================

# 实时行情熔断器：连续失败3次触发，冷却300秒（5分钟）
# 实时行情对时效性要求高，冷却期不宜过长
_realtime_cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=300, name="realtime")

# 海外行情熔断器：连续失败2次触发，冷却900秒（15分钟）
# 海外数据源访问更不稳定，阈值更低、冷却期更长
_overseas_cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=900, name="overseas")


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器实例"""
    return _realtime_cb


def get_overseas_circuit_breaker() -> CircuitBreaker:
    """获取海外行情熔断器实例"""
    return _overseas_cb
