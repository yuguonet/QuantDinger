"""
Cache utilities.
Local-first behavior: use in-memory cache by default.
Redis is only used when explicitly enabled via environment variables.
"""
import time
import threading
from typing import Optional, Any
import json

from app.utils.logger import get_logger
from app.config import CacheConfig

logger = get_logger(__name__)


class MemoryCache:
    """内存缓存（Redis 不可用时的备选方案）"""
    
    MAX_SIZE = 10000  # 最大条目数，超出后清理过期条目
    
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[str]:
        with self._lock:
            if key in self._cache:
                data, expiry = self._cache[key]
                if expiry > time.time():
                    return data
                else:
                    del self._cache[key]
            return None
    
    def setex(self, key: str, ttl: int, value: str):
        with self._lock:
            # 容量保护：写入前清理过期条目
            if len(self._cache) >= self.MAX_SIZE:
                self._evict_expired()
                # 仍然超限则移除最旧的 20%
                if len(self._cache) >= self.MAX_SIZE:
                    items = sorted(self._cache.items(), key=lambda x: x[1][1])
                    for k, _ in items[:len(items) // 5]:
                        del self._cache[k]
            expiry = time.time() + ttl
            self._cache[key] = (value, expiry)
    
    def delete(self, key: str):
        with self._lock:
            if key in self._cache:
                del self._cache[key]
    
    def clear(self):
        with self._lock:
            self._cache.clear()

    def _evict_expired(self):
        """清理过期条目（调用者需持有 _lock）"""
        now = time.time()
        expired = [k for k, (_, exp) in self._cache.items() if exp <= now]
        for k in expired:
            del self._cache[k]


class CacheManager:
    """缓存管理器"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self._client = None
        self._use_redis = False

        # Local-first: do NOT touch Redis unless explicitly enabled.
        if not CacheConfig.ENABLED:
            self._client = MemoryCache()
            self._use_redis = False
            return

        # Try Redis only when enabled.
        try:
            import redis
            from app.config import RedisConfig

            self._client = redis.Redis(
                host=RedisConfig.HOST,
                port=RedisConfig.PORT,
                db=RedisConfig.DB,
                password=RedisConfig.PASSWORD,
                decode_responses=True,
                socket_connect_timeout=RedisConfig.CONNECT_TIMEOUT,
                socket_timeout=RedisConfig.SOCKET_TIMEOUT
            )
            self._client.ping()
            self._use_redis = True
            logger.info("Redis cache connected")
        except Exception as e:
            # Fall back silently (keep startup logs clean in local mode).
            logger.info(f"Redis is enabled but unavailable; using in-memory cache instead: {e}")
            self._client = MemoryCache()
            self._use_redis = False
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        try:
            data = self._client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.error(f"Cache read failed: {e}")
            return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """设置缓存"""
        try:
            self._client.setex(key, ttl, json.dumps(value))
        except Exception as e:
            logger.error(f"Cache write failed: {e}")
    
    def delete(self, key: str):
        """删除缓存"""
        try:
            self._client.delete(key)
        except Exception as e:
            logger.error(f"Cache delete failed: {e}")
    
    @property
    def is_redis(self) -> bool:
        return self._use_redis

