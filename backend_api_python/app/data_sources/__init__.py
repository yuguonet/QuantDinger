"""
=============================================
数据源模块 (Data Sources)
=============================================

支持多种市场的 K 线数据获取，三层防护保障稳定性:

1. 熔断器 (Circuit Breaker)
   - 国内源（宽松）:  5次失败 / 5min冷却 / 半开试2次
   - 海外源（严格）:  2次失败 / 15min冷却 / 半开试1次
   - 空结果不触发熔断，仅异常才计数

2. 数据缓存 (Cache Manager)
   - 实时行情: 20min TTL, 6000条上限
   - K线数据: 5min TTL,  500条上限
   - 股票信息: 24h TTL,  6000条上限

3. 防封禁 (Rate Limiter)
   - 随机 User-Agent 轮换
   - 请求间隔 + 随机抖动
   - 指数退避重试

导出:
    DataSourceFactory            数据源工厂（按市场类型获取数据源）
    get_realtime_circuit_breaker 国内源熔断器
    get_overseas_circuit_breaker 海外源熔断器
    DataCache / get_*_cache      缓存管理器
    RateLimiter / random_sleep   限流工具
"""
from app.data_sources.factory import DataSourceFactory
from app.data_sources.circuit_breaker import (
    CircuitBreaker,
    get_realtime_circuit_breaker,
    get_overseas_circuit_breaker
)
from app.data_sources.cache_manager import (
    DataCache,
    get_realtime_cache,
    get_kline_cache,
    get_stock_info_cache
)
from app.data_sources.rate_limiter import (
    RateLimiter,
    get_random_user_agent,
    random_sleep,
    retry_with_backoff
)

__all__ = [
    # 工厂
    'DataSourceFactory',
    # 熔断器
    'CircuitBreaker',
    'get_realtime_circuit_breaker',
    'get_overseas_circuit_breaker',
    # 缓存
    'DataCache',
    'get_realtime_cache',
    'get_kline_cache',
    'get_stock_info_cache',
    # 限流器
    'RateLimiter',
    'get_random_user_agent',
    'random_sleep',
    'retry_with_backoff',
]
