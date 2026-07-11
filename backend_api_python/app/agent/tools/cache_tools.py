# -*- coding: utf-8 -*-
"""
Cache Tools — LLM 按需读取之前步骤的执行结果。

配合 RoundCache 使用，避免重复调用工具。
"""
from __future__ import annotations

from typing import Any, Dict


def read_cache(key: str) -> Any:
    """读取之前步骤的执行结果。

    Args:
        key: 缓存键名，格式 'phase{id}_step{N}'，如 'phase0_step1'、'phase1_step2'

    返回之前步骤的输出结果，或错误信息。
    可用缓存键可通过 list_cache() 查看。
    """
    from agents.round_cache import get_current_cache
    cache = get_current_cache()
    if cache is None:
        return {"error": "缓存未初始化"}

    result = cache.get(key)
    if result is None:
        available = cache.keys()
        return {
            "error": f"缓存中不存在 key '{key}'",
            "available_keys": available[:20],
        }
    # 如果是 JSON 字符串，自动解析为 dict/list
    if isinstance(result, str):
        import json
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def list_cache() -> Dict[str, Any]:
    """列出当前可用的所有缓存条目。"""
    from agents.round_cache import get_current_cache
    cache = get_current_cache()
    if cache is None:
        return {"error": "缓存未初始化"}

    index = cache.index()
    if not index:
        return {"message": "当前无缓存数据"}

    return {
        "total": len(index),
        "items": index,
    }
