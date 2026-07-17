# -*- coding: utf-8 -*-
"""通用格式转换工具 — 把任意格式转为 LLM 易读的字符串。"""
from typing import Any


def format_result(result: Any, max_depth: int = 3, max_items: int = 20) -> str:
    """把任意格式的数据转换为 LLM 容易理解的字符串。

    支持: dict, list, str, int, float, None
    自动处理嵌套结构，跳过内部字段（_开头）。

    Args:
        result: 任意格式的数据
        max_depth: 最大递归深度（防止过深嵌套）
        max_items: dict/list 最多显示的项数

    Returns:
        格式化的字符串

    Examples:
        >>> format_result({"name": "茅台", "price": 1800.0})
        'name: 茅台\nprice: 1800.0'

        >>> format_result({"error": "数据不足"})
        '❌ 数据不足'

        >>> format_result([1, 2, 3])
        '1\n2\n3'
    """
    if result is None:
        return "无数据"

    if isinstance(result, str):
        # 尝试解析 JSON 字符串
        if result.startswith('{') or result.startswith('['):
            try:
                import json
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(result, str):
            return result

    return _format_any(result, depth=0, max_depth=max_depth, max_items=max_items)


def _format_any(obj: Any, depth: int, max_depth: int, max_items: int) -> str:
    """递归格式化任意对象。"""
    if depth > max_depth:
        return "..."

    if obj is None:
        return "无数据"

    if isinstance(obj, bool):
        return "是" if obj else "否"

    if isinstance(obj, (int, float)):
        # 格式化数字
        if isinstance(obj, float):
            if abs(obj) >= 1e8:
                return f"{obj/1e8:.2f}亿"
            elif abs(obj) >= 1e4:
                return f"{obj/1e4:.2f}万"
            else:
                return f"{obj:.2f}"
        return str(obj)

    if isinstance(obj, str):
        return obj

    if isinstance(obj, dict):
        return _format_dict(obj, depth, max_depth, max_items)

    if isinstance(obj, (list, tuple)):
        return _format_list(obj, depth, max_depth, max_items)

    return str(obj)


def _format_dict(d: dict, depth: int, max_depth: int, max_items: int) -> str:
    """格式化 dict。"""
    if not d:
        return "空"

    # 检查是否有 error 字段
    if "error" in d:
        return f"❌ {d['error']}"

    lines = []
    count = 0
    for k, v in d.items():
        # 跳过内部字段
        if k.startswith('_'):
            continue
        if count >= max_items:
            lines.append(f"... (还有 {len(d) - count} 项)")
            break

        # 格式化值
        if isinstance(v, dict):
            if len(v) == 0:
                formatted = "空"
            else:
                formatted = "\n" + _format_any(v, depth + 1, max_depth, max_items)
                # 缩进子项
                formatted = formatted.replace("\n", "\n  ")
        elif isinstance(v, (list, tuple)):
            formatted = _format_any(v, depth + 1, max_depth, max_items)
        else:
            formatted = _format_any(v, depth + 1, max_depth, max_items)

        lines.append(f"{k}: {formatted}")
        count += 1

    return "\n".join(lines)


def _format_list(lst: (list, tuple), depth: int, max_depth: int, max_items: int) -> str:
    """格式化 list。"""
    if not lst:
        return "空"

    lines = []
    for i, item in enumerate(lst[:max_items]):
        if isinstance(item, dict):
            # dict 项：显示摘要
            summary = _dict_summary(item)
            lines.append(f"{i+1}. {summary}")
        else:
            lines.append(f"{i+1}. {_format_any(item, depth + 1, max_depth, max_items)}")

    if len(lst) > max_items:
        lines.append(f"... (还有 {len(lst) - max_items} 项)")

    return "\n".join(lines)


def _dict_summary(d: dict) -> str:
    """提取 dict 的摘要（取前3个字段）。"""
    if not d:
        return "空"

    # 跳过内部字段
    items = [(k, v) for k, v in d.items() if not k.startswith('_')]
    if not items:
        return "空"

    # 取前3个字段
    parts = []
    for k, v in items[:3]:
        if isinstance(v, (int, float)):
            parts.append(f"{k}={v}")
        elif isinstance(v, str) and len(v) < 20:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=...")

    return " | ".join(parts)
