# -*- coding: utf-8 -*-
"""
RoundCache — 扁平缓存，生命周期=单次 TaskAgent.chat()。

设计目标：
  - 工具执行结果自动写入缓存
  - LLM 通过 read_cache(key) 按需读取之前的数据
  - 避免工具输出累积导致 token 增长
  - 请求级生命周期，用完即释

使用方式：
  cache = RoundCache()
  cache.put("analyze_trend_300129", result)
  cache.get("analyze_trend_300129")  # 读取
  cache.index()  # 列出所有可用缓存
"""
from __future__ import annotations

from typing import Any, Dict, List


class RoundCache:
    """扁平缓存，生命周期=单次 TaskAgent.chat()"""

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._round_keys: set[str] = set()  # 本轮新增的 key

    def put(self, key: str, value: Any):
        """写入缓存。"""
        self._data[key] = value
        self._round_keys.add(key)

    def get(self, key: str) -> Any:
        """读取缓存。不存在返回 None。"""
        return self._data.get(key)

    def keys(self) -> List[str]:
        """返回所有缓存键。"""
        return list(self._data.keys())

    def index(self) -> Dict[str, str]:
        """返回 {key: type_hint} 供注入到上下文。"""
        return {
            k: self._infer_type(v)
            for k, v in self._data.items()
        }

    def flush_round_keys(self) -> List[str]:
        """取出本轮写入了哪些 key。"""
        keys = list(self._round_keys)
        self._round_keys.clear()
        return keys

    def clear(self):
        """清空缓存。"""
        self._data.clear()
        self._round_keys.clear()

    def _infer_type(self, value: Any) -> str:
        """推断值的类型提示。"""
        if isinstance(value, dict):
            return "dict"
        if isinstance(value, list):
            return f"list[{len(value)}]"
        if isinstance(value, str):
            if len(value) > 200:
                return f"str[{len(value)}字符]"
            return "str"
        return type(value).__name__


# 模块级实例（请求级，需要在每次 chat() 时重置）
_current_cache: RoundCache | None = None


def get_current_cache() -> RoundCache | None:
    """获取当前缓存实例。"""
    return _current_cache


def set_current_cache(cache: RoundCache | None):
    """设置当前缓存实例。"""
    global _current_cache
    _current_cache = cache


def reset_current_cache():
    """重置当前缓存（创建新实例）。"""
    global _current_cache
    _current_cache = RoundCache()
    return _current_cache
