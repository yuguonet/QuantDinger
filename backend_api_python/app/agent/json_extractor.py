# -*- coding: utf-8 -*-
"""
JSON Extractor — 从 Agent 输出中提取结构化 JSON 的统一工具。

公开接口：
  extract_json(content) → dict | None    从内容中提取第一个有效 JSON 块
  extract_decision(content) → dict | None 提取含 action/score 的决策 JSON
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


# 统一的 JSON 块匹配模式（按优先级排序）
_JSON_PATTERNS = [
    r'```json\s*\n?(.*?)\n?\s*```',                    # markdown JSON 块
    r'(\{[^{}]*"reply"[^{}]*"data"[^{}]*\})',        # 壳格式：reply+data（无嵌套）
    r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',      # 旧格式：行内 action+score
    r'(\{[^{}]*"score"[^{}]*"action"[^{}]*\})',      # 旧格式：行内 score+action
    r'(\{\s*"reply"\s*:\s*"[^"]+".*?"data"\s*:\s*\{.*?\}\s*\})',  # 壳格式：嵌套
]


def extract_json(content: Any) -> Optional[Dict[str, Any]]:
    """从内容中提取第一个有效 JSON dict。

    优先级：
      1. dict 直接返回
      2. 整体是合法 JSON → 直接解析
      3. markdown JSON 块提取
      4. 正则模式匹配（简单/嵌套壳格式）
      5. 括号平衡法提取最长 JSON 块

    Args:
        content: str 或 dict。dict 直接返回；str 尝试多种方式解析。

    Returns:
        解析成功的 dict，或 None。
    """
    if isinstance(content, dict):
        return content

    if not isinstance(content, str) or not content.strip():
        return None

    # 1. 整体是合法 JSON（LLM 直接输出 JSON 的情况）
    stripped = content.strip()
    if stripped.startswith('{'):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. markdown JSON 块
    m = re.search(r'```json\s*\n?(.*?)\n?\s*```', content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. 正则模式匹配（简单壳格式，无嵌套）
    for pat in _JSON_PATTERNS[1:]:  # 跳过 markdown pattern，已处理
        m = re.search(pat, content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue

    # 4. 括号平衡法：遍历所有 '{' 起始位置，提取第一个完整 JSON 对象
    pos = 0
    while pos < len(content):
        start = content.find('{', pos)
        if start == -1:
            break
        result = _extract_json_from_offset(content, start)
        if result is not None:
            return result
        pos = start + 1

    return None


def _extract_json_from_offset(content: str, start: int) -> Optional[Dict[str, Any]]:
    """从指定偏移位置开始，用括号平衡法提取 JSON。"""
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(content)):
        c = content[i]
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                candidate = content[start:i + 1]
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, TypeError):
                    break
    return None


def extract_decision(content: Any) -> Optional[Dict[str, Any]]:
    """提取含 "action" 字段的决策 JSON。

    兼容两种格式：
      1. 旧格式：action/score 在顶层
      2. 通用壳：action/score 在 data 字段内
    """
    data = extract_json(content)
    if not data:
        return None
    # 顶层有 action（旧格式）
    if "action" in data:
        return data
    # 壳格式：action 在 data 里
    inner = data.get("data", {})
    if isinstance(inner, dict) and "action" in inner:
        return inner
    return None

