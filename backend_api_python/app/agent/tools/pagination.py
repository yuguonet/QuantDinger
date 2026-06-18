# -*- coding: utf-8 -*-
"""
Pagination & Cache — 对大数据量工具返回值做分页处理。

设计目标：
  1. 工具函数返回完整数据后，自动缓存到内存，只返回第 N 页给 agent
  2. agent 可通过 page_tool 翻页查看后续数据
  3. 缓存自动过期（TTL），不泄露跨 session 数据

使用方式：
  方式 A — 装饰器（推荐，零侵入）：
      @paginated(page_size=20)
      @tool(description="...", ...)
      def get_dragon_tiger(...):
          return {"stocks": data, ...}  # 原样返回完整数据

  方式 B — 手动包装：
      result = paginate_result(
          cache_key="dragon_tiger_2026-06-05",
          data=data,           # list 或 dict
          page=1, page_size=20,
          data_key="stocks",   # 如果 data 是 dict，指定列表字段名
      )
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. 内存缓存
# ═══════════════════════════════════════════════════════════════

class _PageCache:
    """线程安全的分页缓存。"""

    def __init__(self, ttl: int = 600, max_entries: int = 200):
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max = max_entries

    def put(self, key: str, data: Any, data_key: str = "") -> None:
        """存入完整数据。"""
        with self._lock:
            self._maybe_cleanup()
            self._store[key] = {
                "data": data,
                "data_key": data_key,
                "created": time.time(),
                "accessed": time.time(),
            }

    def get(self, key: str) -> Optional[Tuple[Any, str]]:
        """取出缓存。返回 (data, data_key) 或 None。"""
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if time.time() - entry["created"] > self._ttl:
                self._store.pop(key, None)
                return None
            entry["accessed"] = time.time()
            return entry["data"], entry["data_key"]

    def remove(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def _maybe_cleanup(self):
        """超过 max_entries 时淘汰最旧的条目。"""
        if len(self._store) < self._max:
            return
        # 按 accessed 排序，删最旧的 20%
        sorted_keys = sorted(
            self._store.keys(),
            key=lambda k: self._store[k]["accessed"],
        )
        to_remove = max(1, len(sorted_keys) // 5)
        for k in sorted_keys[:to_remove]:
            self._store.pop(k, None)


# 全局单例
_cache = _PageCache()


# ═══════════════════════════════════════════════════════════════
# 2. 分页逻辑
# ═══════════════════════════════════════════════════════════════

def _extract_list(data: Any, data_key: str) -> List:
    """从返回值中提取要分页的列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data_key:
        val = data.get(data_key)
        if isinstance(val, list):
            return val
    # fallback: 如果 dict 里只有一个 list 值，用它
    if isinstance(data, dict):
        list_fields = [v for v in data.values() if isinstance(v, list) and len(v) > 0]
        if len(list_fields) == 1:
            return list_fields[0]
    return []


def paginate_result(
    cache_key: str,
    data: Any,
    page: int = 1,
    page_size: int = 20,
    data_key: str = "",
) -> Dict[str, Any]:
    """对数据做分页处理，返回第 page 页 + 分页元信息。

    完整数据存入缓存，后续通过 get_page() 翻页。

    Args:
        cache_key: 缓存键（用于后续翻页）
        data: 完整返回值（list 或 dict）
        page: 页码，从 1 开始
        page_size: 每页条数
        data_key: 如果 data 是 dict，指定列表字段名
    """
    # 存入缓存
    _cache.put(cache_key, data, data_key)

    return _format_page(cache_key, data, page, page_size, data_key)


def _format_page(
    cache_key: str,
    data: Any,
    page: int,
    page_size: int,
    data_key: str,
) -> Dict[str, Any]:
    """格式化某一页的输出。"""
    items = _extract_list(data, data_key)
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    page_items = items[start:end]

    # 构造返回：保留原始 dict 的非列表字段 + 分页数据 + 元信息
    result: Dict[str, Any] = {}

    # 保留原始非列表字段（date, count, text 等）
    if isinstance(data, dict):
        for k, v in data.items():
            if k == data_key:
                continue  # 列表字段由分页数据替代
            if isinstance(v, list):
                continue  # 跳过其他列表
            result[k] = v

    # 分页数据
    page_data_key = data_key or "items"
    result[page_data_key] = page_items

    # 分页元信息
    result["_pagination"] = {
        "cache_key": cache_key,
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }

    # 提示信息
    hint_parts = [f"第{page}/{total_pages}页，共{total}条"]
    if page < total_pages:
        hint_parts.append(f"还有{total - end}条未显示")
    result["_hint"] = " | ".join(hint_parts)

    return result


def get_page(cache_key: str, page: int, page_size: int = 0) -> Dict[str, Any]:
    """从缓存中取指定页。自动识别列表模式/文本模式。

    Args:
        cache_key: 缓存键
        page: 目标页码
        page_size: 每页条数（列表模式）或块大小（文本模式），0=沿用上次
    """
    cached = _cache.get(cache_key)
    if not cached:
        return {"error": f"缓存已过期或不存在: {cache_key}", "retriable": False}

    data, data_key = cached

    # 文本模式 → 委托给 get_text_page
    if data_key == "__text_mode__":
        return get_text_page(cache_key, page, page_size)

    # 列表模式
    items = _extract_list(data, data_key)

    if page_size <= 0:
        page_size = 20

    return _format_page(cache_key, data, page, page_size, data_key)


def get_cache_summary(cache_key: str) -> Optional[Dict[str, Any]]:
        """获取缓存摘要信息。

    Args:
        cache_key: 缓存键名
    """
    cached = _cache.get(cache_key)
    if not cached:
        return None
    data, data_key = cached
    items = _extract_list(data, data_key)
    return {
        "cache_key": cache_key,
        "total_items": len(items),
        "data_key": data_key,
    }


# ═══════════════════════════════════════════════════════════════
# 2b. 文本截断（大字符串/代码/文件内容）
# ═══════════════════════════════════════════════════════════════

def paginate_text(
    cache_key: str,
    text: str,
    chunk_size: int = 4000,
    data_key: str = "text",
) -> Dict[str, Any]:
    """对大文本做截断，缓存全文，返回首段 + 分页信息。

    与列表分页不同，文本按字符数切块。
    agent 可通过 page_tool(cache_key, page=2) 获取后续块。

    Args:
        cache_key: 缓存键
        text: 完整文本
        chunk_size: 每块字符数
        data_key: 返回 dict 中文本字段的 key 名
    """
    total_len = len(text)
    total_pages = max(1, (total_len + chunk_size - 1) // chunk_size)

    # 缓存完整文本（用特殊的 data_key 标记这是文本模式）
    _cache.put(cache_key, {"__text__": text, "__chunk_size__": chunk_size}, data_key="__text_mode__")

    # 返回首块
    first_chunk = text[:chunk_size]

    # 智能截断：尽量在换行处断开
    if total_len > chunk_size:
        cut_pos = first_chunk.rfind("\n")
        if cut_pos > chunk_size * 0.5:
            first_chunk = first_chunk[:cut_pos]

    return {
        data_key: first_chunk,
        "_pagination": {
            "cache_key": cache_key,
            "page": 1,
            "mode": "text",
            "chunk_size": chunk_size,
            "total_chars": total_len,
            "total_pages": total_pages,
            "has_next": total_len > chunk_size,
            "has_prev": False,
        },
        "_hint": f"文本已截断: 显示前{len(first_chunk)}/{total_len}字符 | 用 page_tool 翻页查看后续内容",
    }


def get_text_page(cache_key: str, page: int, chunk_size: int = 0) -> Dict[str, Any]:
        """获取文本分页内容。

    Args:
        cache_key: 缓存键名
        page: 页码
        chunk_size: 每页字符数
    """
    cached = _cache.get(cache_key)
    if not cached:
        return {"error": f"缓存已过期或不存在: {cache_key}", "retriable": False}

    data, data_key = cached

    # 文本模式
    if data_key == "__text_mode__" and isinstance(data, dict) and "__text__" in data:
        text = data["__text__"]
        cs = chunk_size or data.get("__chunk_size__", 4000)
        total_len = len(text)
        total_pages = max(1, (total_len + cs - 1) // cs)
        page = max(1, min(page, total_pages))

        start = (page - 1) * cs
        end = start + cs
        chunk = text[start:end]

        # 智能截断：首尾在换行处断开
        if start > 0:
            nl_pos = chunk.find("\n")
            if nl_pos < len(chunk) * 0.1 and nl_pos >= 0:
                chunk = chunk[nl_pos + 1:]
        if end < total_len:
            nl_pos = chunk.rfind("\n")
            if nl_pos > len(chunk) * 0.5:
                chunk = chunk[:nl_pos]

        return {
            "text": chunk,
            "_pagination": {
                "cache_key": cache_key,
                "page": page,
                "mode": "text",
                "chunk_size": cs,
                "total_chars": total_len,
                "total_pages": total_pages,
                "has_next": end < total_len,
                "has_prev": page > 1,
            },
            "_hint": f"第{page}/{total_pages}块，已显示{start + len(chunk)}/{total_len}字符",
        }

    # 列表模式（fallback 到 get_page）
    return get_page(cache_key, page, chunk_size or 20)


# ═══════════════════════════════════════════════════════════════
# 3. 装饰器：@paginated
# ═══════════════════════════════════════════════════════════════

def _make_cache_key(fn_name: str, args_dict: Dict) -> str:
    """根据函数名 + 参数生成缓存键。"""
    # 排序参数确保相同参数生成相同 key
    sorted_args = sorted(args_dict.items())
    raw = f"{fn_name}:{sorted_args}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"{fn_name}_{short_hash}"


def paginated(
    page_size: int = 20,
    data_key: str = "",
    auto_key: bool = True,
):
    """装饰器：自动为工具函数添加分页支持。

    被装饰的函数照常返回完整数据，装饰器自动：
    1. 缓存完整结果
    2. 返回第 1 页 + 分页元信息
    3. 注入 cache_key 供 agent 翻页

    Args:
        page_size: 每页条数，默认 20
        data_key: 如果返回值是 dict，指定列表字段名。
                  空字符串 = 自动检测（取第一个 list 字段）
        auto_key: 是否自动生成 cache_key（基于函数名+参数）
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # 调用原始函数获取完整数据
            result = fn(*args, **kwargs)

            # 错误结果不分页，直接返回
            if isinstance(result, dict) and result.get("error"):
                return result

            # 生成 cache_key
            if auto_key:
                # 从 kwargs + positional args 构建参数 dict
                import inspect
                sig = inspect.signature(fn)
                params = list(sig.parameters.keys())
                args_dict = {}
                for i, val in enumerate(args):
                    if i < len(params):
                        args_dict[params[i]] = val
                args_dict.update(kwargs)
                # 排除 page/page_size 自身
                args_dict.pop("page", None)
                args_dict.pop("page_size", None)
                key = _make_cache_key(fn.__name__, args_dict)
            else:
                key = kwargs.get("cache_key", fn.__name__)

            # 探测 data_key
            effective_key = data_key
            if not effective_key and isinstance(result, dict):
                for k, v in result.items():
                    if isinstance(v, list) and len(v) > 0:
                        effective_key = k
                        break

            # 检查数据量，小数据不分页
            items = _extract_list(result, effective_key)
            if len(items) <= page_size:
                # 数据量小，直接返回（但仍缓存以便翻页）
                _cache.put(key, result, effective_key)
                if isinstance(result, dict):
                    result["_pagination"] = {
                        "cache_key": key,
                        "page": 1,
                        "page_size": len(items),
                        "total_items": len(items),
                        "total_pages": 1,
                        "has_next": False,
                        "has_prev": False,
                    }
                return result

            # 大数据 → 分页
            return paginate_result(
                cache_key=key,
                data=result,
                page=1,
                page_size=page_size,
                data_key=effective_key,
            )

        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════
# 4. Agent 翻页工具（注册到 tools/registry）
# ═══════════════════════════════════════════════════════════════

    @reg_tool(
        description=(
            "翻页查看上一次查询的缓存数据。当工具返回了 _pagination 字段时，"
            "说明数据已缓存，可用此工具翻页。支持两种模式：\n"
            "1. 列表分页：翻看更多条目（如股票列表、龙虎榜）\n"
            "2. 文本分块：查看被截断的大文本后续内容（如代码、文件、报告）\n"
            "参数 cache_key 和 mode 从 _pagination 中获取。"
        ),
        category="数据查询",
        layer="支撑层",
        domain=[],
    )
    def page_tool(cache_key: str, page: int, page_size: int = 20) -> Dict[str, Any]:
        """翻页查看缓存数据（自动识别列表/文本模式）。

        Args:
            cache_key: 缓存键，从上一次查询结果的 _pagination.cache_key 获取
            page: 目标页码（从 1 开始）
            page_size: 每页条数（列表模式）或块大小（文本模式），默认 20
        """
        if not cache_key:
            return {"error": "cache_key 不能为空", "retriable": False}
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        return get_page(cache_key, page, page_size)    """存入缓存数据。

    Args:
        key: 缓存键名
        data: 要缓存的数据
        data_key: 数据子键
    """    """删除缓存条目。

    Args:
        key: 缓存键名
    """    """分页装饰器。

    Args:
        fn: 被装饰的函数
    """
