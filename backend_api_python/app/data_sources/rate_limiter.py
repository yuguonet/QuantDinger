# -*- coding: utf-8 -*-
"""
限流器模块 — 控制对各数据源的请求频率

模块职责:
    为每个数据源提供独立的请求限流能力，防止因请求过于频繁而被封 IP 或触发
    反爬机制。同时提供带指数退避的重试装饰器，增强网络请求的健壮性。

设计原理:
    - 最小间隔 + 随机抖动: 每次请求间隔 = min_interval + random(jitter_min, jitter_max)
      随机抖动避免固定节奏被服务端识别为爬虫
    - 线程安全: 使用 threading.Lock 保护共享状态，支持多线程并发调用
    - 指数退避重试: 失败后等待时间按 2^n 增长，避免在服务端故障时雪崩式重试

在架构中的位置:
    数据源层 — 被所有 Provider 和复权模块依赖

关键依赖:
    - app.utils.logger: 日志记录
"""

from __future__ import annotations

import random
import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple, Type

from app.utils.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """
    通用限流器 — 最小间隔 + 随机抖动。

    通过在每次请求前调用 wait() 方法，确保两次请求之间的间隔不低于
    min_interval + random(jitter_min, jitter_max)。

    设计模式:
        单例模式 — 每个数据源创建一个全局实例（见模块底部）

    线程安全性:
        使用 threading.Lock 保护 _last_call 状态，多线程调用安全。

    Args:
        min_interval: 两次请求的最小间隔（秒）
        jitter_min: 随机抖动最小值（秒）
        jitter_max: 随机抖动最大值（秒）

    Examples:
        >>> limiter = RateLimiter(min_interval=0.5, jitter_min=0.1, jitter_max=0.5)
        >>> limiter.wait()  # 首次调用不等待
        >>> limiter.wait()  # 至少等待 0.5 + random(0.1, 0.5) 秒
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        jitter_min: float = 0.0,
        jitter_max: float = 0.0,
    ):
        """初始化限流器，设置最小间隔和随机抖动范围"""
        self._min_interval = min_interval
        self._jitter_min = jitter_min
        self._jitter_max = jitter_max
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """
        等待直到可以发起下一次请求。

        计算下次请求的最早时间 = 上次请求时间 + min_interval + random jitter，
        如果当前时间早于该时间点，则 sleep 等待。
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            # 随机抖动：避免固定节奏被反爬识别
            jitter = random.uniform(self._jitter_min, self._jitter_max) if self._jitter_max > 0 else 0
            wait_time = self._min_interval + jitter - elapsed
            if wait_time > 0:
                time.sleep(wait_time)
            self._last_call = time.time()


# ================================================================
# 各源限流器实例
# ================================================================

# 腾讯实时行情限流：最小间隔 0.5s + 0.1~0.5s 随机抖动
# 腾讯 API 对高频请求较敏感，适当限流可避免被封
_tencent_limiter = RateLimiter(min_interval=0.5, jitter_min=0.1, jitter_max=0.5)

# 东财数据限流：最小间隔 0.5s + 0.1~0.5s 随机抖动
# 东财 API 有较严格的频率限制，与腾讯使用相同策略
_eastmoney_limiter = RateLimiter(min_interval=0.5, jitter_min=0.1, jitter_max=0.5)


def get_tencent_limiter() -> RateLimiter:
    """获取腾讯数据源限流器实例"""
    return _tencent_limiter


def get_eastmoney_limiter() -> RateLimiter:
    """获取东财数据源限流器实例"""
    return _eastmoney_limiter


# ================================================================
# 通用请求头
# ================================================================

def get_request_headers(referer: str = None) -> Dict[str, str]:
    """
    构造通用 HTTP 请求头。

    模拟 Chrome 浏览器的 User-Agent，降低被反爬拦截的概率。

    Args:
        referer: 可选的 Referer 头，部分 API 需要合法的来源页面

    Returns:
        HTTP 请求头字典
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if referer:
        headers["Referer"] = referer
    return headers


# ================================================================
# 重试装饰器
# ================================================================

def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """
    带指数退避的重试装饰器。

    算法说明 — 指数退避 (Exponential Backoff):
        1. 第1次失败后等待 base_delay × 2^0 = base_delay 秒
        2. 第2次失败后等待 base_delay × 2^1 = 2×base_delay 秒
        3. 第3次失败后等待 base_delay × 2^2 = 4×base_delay 秒
        4. ...不超过 max_delay 秒
        5. 每次等待时间额外乘以 random(0.8, 1.2) 的抖动系数，避免多客户端同时重试

    适用场景:
        - 网络请求的瞬时故障（超时、连接重置）
        - 数据源临时不可用（503、429 限流）
        - 不适用于永久性错误（401 认证失败、404 资源不存在）

    Args:
        max_attempts: 最大尝试次数（含首次调用）
        base_delay: 基础等待时间（秒）
        max_delay: 最大等待时间上限（秒）
        exceptions: 需要重试的异常类型元组

    Returns:
        装饰器函数

    Examples:
        >>> @retry_with_backoff(max_attempts=3, base_delay=1.0)
        ... def fetch_data():
        ...     return requests.get(url)
    """
    def decorator(fn):
        """装饰器: 包装原函数，添加重试逻辑"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            """重试包装: 指数退避 + 随机抖动"""
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        # 指数退避: base_delay * 2^attempt，不超过 max_delay
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        # 随机抖动: ±20%，避免多客户端同时重试造成雷群效应
                        delay *= random.uniform(0.8, 1.2)
                        logger.debug("[重试] %s 第%d次失败，%.1fs后重试: %s", fn.__name__, attempt + 1, delay, e)
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
