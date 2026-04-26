"""
熔断器
参考 daily_stock_analysis 项目实现
用于管理数据源的熔断/冷却状态，避免连续失败时反复请求导致雪崩。

状态机:
    CLOSED（正常） --失败N次--> OPEN（熔断）--冷却时间到--> HALF_OPEN（半开）
    HALF_OPEN --成功--> CLOSED
    HALF_OPEN --失败--> OPEN
"""
import time
from typing import Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    """熔断器"""
    
    def __init__(
        self,
        failure_threshold: int = 2,
        cooldown_seconds: float = 180.0,
        half_open_max_calls: int = 1
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        
        # 状态存储
        self._states: Dict[str, dict] = {}
    
    def _get_state(self, source: str) -> dict:
        """获取数据源的熔断状态"""
        if source not in self._states:
            self._states[source] = {
                'status': 'CLOSED',  # CLOSED, OPEN, HALF_OPEN
                'failure_count': 0,
                'last_failure_time': 0,
                'half_open_calls': 0,
            }
        return self._states[source]
    
    def is_available(self, source: str) -> bool:
        """检查数据源是否可用"""
        state = self._get_state(source)
        
        if state['status'] == 'CLOSED':
            return True
        
        if state['status'] == 'OPEN':
            # 检查冷却时间是否已过
            elapsed = time.time() - state['last_failure_time']
            if elapsed >= self.cooldown_seconds:
                state['status'] = 'HALF_OPEN'
                state['half_open_calls'] = 0
                logger.info(f"[熔断器] {source} 冷却结束，进入半开状态")
                return True
            return False
        
        if state['status'] == 'HALF_OPEN':
            return state['half_open_calls'] < self.half_open_max_calls
        
        return True
    
    def record_success(self, source: str):
        """记录成功"""
        state = self._get_state(source)
        if state['status'] == 'HALF_OPEN':
            logger.info(f"[熔断器] {source} 半开状态成功，恢复正常")
        state['status'] = 'CLOSED'
        state['failure_count'] = 0
        state['half_open_calls'] = 0
    
    def record_failure(self, source: str, error: str = ""):
        """记录失败"""
        state = self._get_state(source)
        
        if state['status'] == 'HALF_OPEN':
            state['half_open_calls'] += 1
            if state['half_open_calls'] >= self.half_open_max_calls:
                state['status'] = 'OPEN'
                state['last_failure_time'] = time.time()
                logger.warning(f"[熔断器] {source} 半开状态失败，重新熔断 ({error})")
            return
        
        state['failure_count'] += 1
        state['last_failure_time'] = time.time()
        
        if state['failure_count'] >= self.failure_threshold:
            state['status'] = 'OPEN'
            logger.warning(
                f"[熔断器] {source} 连续失败 {state['failure_count']} 次，"
                f"熔断 {self.cooldown_seconds}s ({error})"
            )
    
    def reset(self, source: str):
        """重置熔断状态"""
        if source in self._states:
            del self._states[source]
    
    def reset_all(self):
        """重置所有熔断状态"""
        self._states.clear()
    
    def get_status(self, source: str) -> dict:
        """获取数据源的熔断状态"""
        state = self._get_state(source)
        return {
            'source': source,
            'status': state['status'],
            'failure_count': state['failure_count'],
            'cooldown_remaining': max(
                0,
                self.cooldown_seconds - (time.time() - state['last_failure_time'])
            ) if state['status'] == 'OPEN' else 0,
        }


# 实时行情熔断器（更严格的策略）
_realtime_circuit_breaker = CircuitBreaker(
    failure_threshold=2,      # 连续失败2次熔断
    cooldown_seconds=180.0,   # 冷却3分钟
    half_open_max_calls=1
)


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器"""
    return _realtime_circuit_breaker


# 向后兼容：海外源熔断器（指向同一实例）
def get_overseas_circuit_breaker() -> CircuitBreaker:
    """获取海外数据源熔断器（向后兼容，等同于 get_realtime_circuit_breaker）"""
    return _realtime_circuit_breaker
