# -*- coding: utf-8 -*-
"""
Agent Cache — 轻量 TTL 缓存（兼容 app.agent.cache 接口）。

工具模块中 `from app.agent.cache import cache` 可正常工作。
"""
from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Any, Callable, Optional

from app.agent.log import logger


class _InMemoryCache:
    """线程安全的内存 TTL 缓存。"""

    def __init__(self, max_size: int = 1000, default_ttl: int = 300):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            if len(self._store) >= self._max_size:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                self._store.pop(oldest, None)
            self._store[key] = (time.monotonic() + (ttl or self._default_ttl), value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def cached(self, prefix: str = "", ttl: int = 300):
        """装饰器：缓存函数结果。"""
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                import hashlib, json
                raw = f"{func.__name__}:{args}:{sorted(kwargs.items())}"
                key = f"{prefix}:{hashlib.md5(raw.encode()).hexdigest()}"
                hit = self.get(key)
                if hit is not None:
                    return hit
                result = func(*args, **kwargs)
                self.set(key, result, ttl=ttl)
                return result
            return wrapper
        return decorator


# 模块级单例
cache = _InMemoryCache()
