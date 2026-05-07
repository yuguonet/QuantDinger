"""
market_cn 公共工具 — 缓存 + 重试 + 类型转换

HTTP session 复用 app.utils.http.get_retry_session()
缓存位置: data/market_cn_cache/macro_backend.pkl
"""
import os
import time
import pickle
import logging
import threading
from functools import wraps
from datetime import datetime

from app.utils.http import get_retry_session

logger = logging.getLogger(__name__)

# ── HTTP session (复用 app.utils.http，补充请求头) ────────────

_session = get_retry_session(retries=2, backoff_factor=1.0)
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://finance.sina.com.cn/",
})

def get_session():
    return _session


# ── 重试装饰器 ────────────────────────────────────────────────

def retry(max_retries=2, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if i < max_retries:
                        time.sleep(delay)
            raise last_err
        return wrapper
    return decorator


# ── 安全类型转换 ──────────────────────────────────────────────

def safe_float(v, default=0.0):
    if v is None or v == "" or v == "-":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def safe_int(v, default=0):
    if v is None or v == "" or v == "-":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

def safe_str(v, default=""):
    if v is None or v == "-":
        return default
    return str(v)


# ── 缓存 ─────────────────────────────────────────────────────

_CACHE_DIR = None
_CACHE_FILE = None
_cache_store: dict = {}
_cache_lock = threading.Lock()

def _cache_dir() -> str:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = os.path.join(os.getcwd(), "data", "market_cn_cache")
        os.makedirs(_CACHE_DIR, exist_ok=True)
    return _CACHE_DIR

def _cache_file() -> str:
    global _CACHE_FILE
    if _CACHE_FILE is None:
        _CACHE_FILE = os.path.join(_cache_dir(), "macro_backend.pkl")
    return _CACHE_FILE

def _cache_load():
    global _cache_store
    path = _cache_file()
    if not os.path.exists(path):
        _cache_store = {}
        return
    try:
        with open(path, "rb") as f:
            _cache_store = pickle.load(f)
    except Exception as e:
        logger.warning("macro_backend 缓存加载失败: %s", e)
        _cache_store = {}

def _cache_save():
    path = _cache_file()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(_cache_store, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("macro_backend 缓存写入失败: %s", e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

def cache_get(endpoint: str):
    """读缓存，返回 data 或 None。"""
    entry = _cache_store.get(endpoint)
    if entry is None:
        return None
    return entry.get("data")

def cache_is_fresh(endpoint: str, ttl: int) -> bool:
    """缓存是否在有效期内。"""
    entry = _cache_store.get(endpoint)
    if entry is None:
        return False
    return (time.time() - entry.get("ts", 0)) < ttl

def cache_put(endpoint: str, data):
    """写缓存。"""
    with _cache_lock:
        _cache_store[endpoint] = {"data": data, "ts": time.time()}
        _cache_save()

def cache_get_or_fetch(endpoint: str, ttl: int, fetcher):
    """通用缓存读取：有缓存且新鲜则返回，否则调 fetcher 并缓存。"""
    data = cache_get(endpoint)
    if data is not None and cache_is_fresh(endpoint, ttl):
        return data
    data = fetcher()
    if data is not None:
        cache_put(endpoint, data)
    return data


# 模块加载时自动读取已有缓存
_cache_load()
