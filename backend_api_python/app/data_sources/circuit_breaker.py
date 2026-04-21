# -*- coding: utf-8 -*-
"""
===================================
熔断器模块 (Circuit Breaker)
===================================

管理数据源的熔断/冷却状态，避免连续失败时反复请求导致雪崩。

状态机:
    CLOSED（正常） --连续失败N次--> OPEN（熔断）--冷却时间到--> HALF_OPEN（半开）
    HALF_OPEN --成功--> CLOSED
    HALF_OPEN --失败--> OPEN

双策略:
    - 国内数据源（宽松）:  failure_threshold=5, cooldown=5min, half_open_max=2
    - 海外数据源（严格）:  failure_threshold=2, cooldown=15min, half_open_max=1

使用方式:
    from app.data_sources.circuit_breaker import (
        get_realtime_circuit_breaker,   # 国内源
        get_overseas_circuit_breaker,   # 海外源
    )
    cb = get_overseas_circuit_breaker()
    if not cb.is_available("source_name"):
        return  # 跳过该数据源
    # ... 发起请求 ...
    if ok:
        cb.record_success("source_name")
    else:
        cb.record_failure("source_name", error="reason")

线程安全: 各状态字典操作非原子性，但 CPython GIL 保证单字典操作安全。
多线程并发写同一 source 时存在极小概率的状态竞争，实际场景可忽略。
"""

import time
import logging
from typing import Dict, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常状态
    OPEN = "open"          # 熔断状态（不可用）
    HALF_OPEN = "half_open"  # 半开状态（试探性请求）


class CircuitBreaker:
    """
    熔断器 - 管理数据源的熔断/冷却状态
    
    策略：
    - 连续失败 N 次后进入熔断状态
    - 熔断期间跳过该数据源
    - 冷却时间后自动恢复半开状态
    - 半开状态下单次成功则完全恢复，失败则继续熔断
    """
    
    def __init__(
        self,
        failure_threshold: int = 3,       # 连续失败次数阈值
        cooldown_seconds: float = 300.0,  # 冷却时间（秒），默认5分钟
        half_open_max_calls: int = 1      # 半开状态最大尝试次数
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        
        # 各数据源状态 {source_name: {state, failures, last_failure_time, half_open_calls}}
        self._states: Dict[str, Dict[str, Any]] = {}
    
    def _get_state(self, source: str) -> Dict[str, Any]:
        """获取或初始化数据源状态"""
        if source not in self._states:
            self._states[source] = {
                'state': CircuitState.CLOSED,
                'failures': 0,
                'last_failure_time': 0.0,
                'half_open_calls': 0,
                'last_error': None
            }
        return self._states[source]
    
    def is_available(self, source: str) -> bool:
        """
        检查数据源是否可用
        
        返回 True 表示可以尝试请求
        返回 False 表示应跳过该数据源
        """
        state = self._get_state(source)
        current_time = time.time()
        
        if state['state'] == CircuitState.CLOSED:
            return True
        
        if state['state'] == CircuitState.OPEN:
            # 检查冷却时间
            time_since_failure = current_time - state['last_failure_time']
            if time_since_failure >= self.cooldown_seconds:
                # 冷却完成，进入半开状态
                state['state'] = CircuitState.HALF_OPEN
                state['half_open_calls'] = 0
                logger.info(f"[熔断器] {source} 冷却完成，进入半开状态")
                return True
            else:
                remaining = self.cooldown_seconds - time_since_failure
                logger.debug(f"[熔断器] {source} 处于熔断状态，剩余冷却时间: {remaining:.0f}s")
                return False
        
        if state['state'] == CircuitState.HALF_OPEN:
            # 半开状态下限制请求次数
            if state['half_open_calls'] < self.half_open_max_calls:
                state['half_open_calls'] += 1
                return True
            return False
        
        return True
    
    def record_success(self, source: str) -> None:
        """记录成功请求"""
        state = self._get_state(source)
        
        if state['state'] == CircuitState.HALF_OPEN:
            # 半开状态下成功，完全恢复
            logger.info(f"[熔断器] {source} 半开状态请求成功，恢复正常")
        
        # 重置状态
        state['state'] = CircuitState.CLOSED
        state['failures'] = 0
        state['half_open_calls'] = 0
        state['last_error'] = None
    
    def record_failure(self, source: str, error: Optional[str] = None) -> None:
        """记录失败请求"""
        state = self._get_state(source)
        current_time = time.time()

        state['failures'] += 1
        state['last_failure_time'] = current_time
        state['last_error'] = error

        if state['state'] == CircuitState.HALF_OPEN:
            # 半开状态下失败，继续熔断
            state['state'] = CircuitState.OPEN
            state['half_open_calls'] = 0
            logger.warning(f"[熔断器] {source} 半开状态请求失败，继续熔断 {self.cooldown_seconds}s")
        elif state['failures'] >= self.failure_threshold:
            # 达到阈值，进入熔断
            state['state'] = CircuitState.OPEN
            state['half_open_calls'] = 0
            logger.warning(f"[熔断器] {source} 连续失败 {state['failures']} 次，进入熔断状态 "
                          f"(冷却 {self.cooldown_seconds}s)")
            if error:
                logger.warning(f"[熔断器] 最后错误: {error}")
    
    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有数据源状态"""
        return {
            source: {
                'state': info['state'].value,
                'failures': info['failures'],
                'last_error': info['last_error']
            }
            for source, info in self._states.items()
        }
    
    def reset(self, source: Optional[str] = None) -> None:
        """重置熔断器状态"""
        if source:
            if source in self._states:
                del self._states[source]
                logger.info(f"[熔断器] 已重置 {source} 的熔断状态")
        else:
            self._states.clear()
            logger.info("[熔断器] 已重置所有数据源的熔断状态")


# ============================================
# 全局熔断器实例
# ============================================

# ── 国内数据源熔断器（宽松策略）──
# 国内源（腾讯/东方财富/新浪/AkShare）接口相对稳定，网络抖动多为临时性的
# 允许更多失败次数，冷却时间更短，半开状态多试几次再判定
_domestic_circuit_breaker = CircuitBreaker(
    failure_threshold=5,      # 连续失败5次才熔断
    cooldown_seconds=300.0,   # 冷却5分钟
    half_open_max_calls=2     # 半开状态尝试2次
)

# ── 海外数据源熔断器（严格策略）──
# 海外源（CCXT/Finnhub/yfinance/TwelveData/Tiingo）受网络波动、代理不稳定、
# API 限流等因素影响更大，应更快熔断、更长冷却，避免雪崩式重试
_overseas_circuit_breaker = CircuitBreaker(
    failure_threshold=2,      # 连续失败2次即熔断
    cooldown_seconds=900.0,   # 冷却15分钟
    half_open_max_calls=1     # 半开状态只试1次
)


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取国内数据源熔断器（宽松策略）"""
    return _domestic_circuit_breaker


def get_overseas_circuit_breaker() -> CircuitBreaker:
    """获取海外数据源熔断器（严格策略）"""
    return _overseas_circuit_breaker
