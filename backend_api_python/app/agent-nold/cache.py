# -*- coding: utf-8 -*-
"""
Agent Cache — 轻量 TTL 缓存（内存 + 可选 Redis）。

用法：
  from app.agent.cache import cache

  cache.set("tool:kline:600519", data, ttl=300)
  data = cache.get("tool:kline:600519")

  @cache.cached(prefix="tool_result", ttl=300)
  def expensive_tool_call(stock_code):
      ...

不依赖外部模块，agent 模块自包含。
Redis 可选：设置 REDIS_URL 环境变量自动启用。
"""
from __future__ import annotations

import hashlib
import json
import os
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
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            # 容量检查：淘汰最旧的
            if len(self._store) >= self._max_size:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest]
            expires_at = time.monotonic() + (ttl or self._default_ttl)
            self._store[key] = (expires_at, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{self._hits / total * 100:.1f}%" if total > 0 else "N/A",
            }


class _RedisCache:
    """Redis 缓存后端（可选）。"""

    def __init__(self, redis_url: str, default_ttl: int = 300):
        import redis
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0
        try:
            self._r.ping()
            logger.info("cache_redis_connected", url=redis_url)
        except Exception as e:
            logger.warning("cache_redis_connect_failed", error=str(e))
            raise

    def get(self, key: str) -> Optional[Any]:
        try:
            raw = self._r.get(key)
            if raw is None:
                self._misses += 1
                return None
            self._hits += 1
            return json.loads(raw)
        except Exception:
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            self._r.setex(key, ttl or self._default_ttl, json.dumps(value, ensure_ascii=False))
        except Exception as e:
            logger.warning("cache_set_failed", key=key, error=str(e))

    def delete(self, key: str) -> None:
        try:
            self._r.delete(key)
        except Exception:
            pass

    def clear(self) -> None:
        # 不要轻易调用！只清 agent:cache: 前缀
        try:
            keys = self._r.keys("agent:cache:*")
            if keys:
                self._r.delete(*keys)
        except Exception:
            pass

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "backend": "redis",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits / total * 100:.1f}%" if total > 0 else "N/A",
        }


class CacheManager:
    """统一缓存接口。"""

    def __init__(self):
        self._backend = None

    def _get_backend(self):
        if self._backend is not None:
            return self._backend

        # 优先 Redis
        redis_url = os.getenv("REDIS_URL", "")
        if redis_url:
            try:
                self._backend = _RedisCache(redis_url)
                return self._backend
            except Exception:
                pass

        # 降级内存
        max_size = int(os.getenv("CACHE_MAX_SIZE", "1000"))
        default_ttl = int(os.getenv("CACHE_TTL", "300"))
        self._backend = _InMemoryCache(max_size=max_size, default_ttl=default_ttl)
        logger.info("cache_memory_initialized", max_size=max_size, default_ttl=default_ttl)
        return self._backend

    def get(self, key: str) -> Optional[Any]:
        return self._get_backend().get(key)

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._get_backend().set(key, value, ttl)

    def delete(self, key: str) -> None:
        self._get_backend().delete(key)

    def clear(self) -> None:
        self._get_backend().clear()

    def stats(self) -> dict:
        return self._get_backend().stats()

    def cached(self, prefix: str, ttl: Optional[int] = None, key_func: Optional[Callable] = None):
        """装饰器：缓存函数结果。

        @cache.cached(prefix="tool_result", ttl=300)
        def get_kline(stock_code, period="daily"):
            ...
        """
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                # 构建缓存 key
                if key_func:
                    cache_key = key_func(*args, **kwargs)
                else:
                    raw = f"{func.__name__}:{args}:{sorted(kwargs.items())}"
                    h = hashlib.md5(raw.encode()).hexdigest()[:12]
                    cache_key = f"{prefix}:{h}"

                # 尝试读缓存
                hit = self.get(cache_key)
                if hit is not None:
                    logger.debug("cache_hit", key=cache_key)
                    return hit

                # 执行并缓存
                result = func(*args, **kwargs)
                if result is not None:
                    self.set(cache_key, result, ttl)
                return result

            return wrapper
        return decorator


# 全局单例
cache = CacheManager()
