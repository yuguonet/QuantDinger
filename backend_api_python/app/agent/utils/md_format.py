# -*- coding: utf-8 -*-
"""
通用 dict → 简短 markdown 格式化。
只输出关键字段，跳过嵌套细节和长列表。
"""
from __future__ import annotations
from typing import Any


def _to_md(data: Any) -> str:
    """dict/list → 简短 markdown 文本。"""
    if isinstance(data, dict):
        return _dict_to_md(data)
    if isinstance(data, list):
        return _list_to_md(data)
    return str(data)


def _dict_to_md(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            continue  # 跳过嵌套，减少 token
        if isinstance(v, str) and len(v) > 100:
            continue  # 跳过长文本
        parts.append(f"- {k}: {v}")
    return "\n".join(parts) if parts else str(d)[:200]


def _list_to_md(lst: list) -> str:
    if not lst:
        return "(空)"
    if isinstance(lst[0], dict):
        parts = []
        for i, item in enumerate(lst[:5], 1):
            vals = [f"{v}" for v in item.values() if isinstance(v, (str, int, float))][:3]
            parts.append(f"{i}. {', '.join(vals)}")
        if len(lst) > 5:
            parts.append(f"...共{len(lst)}项")
        return "\n".join(parts)
    return "\n".join(f"- {x}" for x in lst[:10])
