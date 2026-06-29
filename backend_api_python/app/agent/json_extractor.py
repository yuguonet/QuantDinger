# -*- coding: utf-8 -*-
"""
JSON Extractor — 从 Agent 输出中提取结构化 JSON 的统一工具。

消除 agent.py / trace_collector.py 中重复的 JSON 提取逻辑。

公开接口：
  extract_json(content) → dict | None    从内容中提取第一个有效 JSON 块
  extract_decision(content) → dict | None 提取含 action/score 的决策 JSON
  extract_field(content, field) → Any     从 JSON 中提取单个字段
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


# 统一的 JSON 块匹配模式（按优先级排序）
_JSON_PATTERNS = [
    r'```json\s*\n?(.*?)\n?\s*```',                    # markdown JSON 块
    r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',        # 行内 action+score（无嵌套）
    r'(\{[^{}]*"score"[^{}]*"action"[^{}]*\})',        # 行内 score+action（无嵌套）
    r'(\{\s*"action"\s*:\s*"[^"]+".*?"score"\s*:\s*\d+.*?\})',  # 嵌套 JSON（含数组/对象）
]


def extract_json(content: Any) -> Optional[Dict[str, Any]]:
    """从内容中提取第一个有效 JSON dict。

    Args:
        content: str 或 dict。dict 直接返回；str 尝试模式匹配。

    Returns:
        解析成功的 dict，或 None。
    """
    if isinstance(content, dict):
        return content

    if not isinstance(content, str) or not content.strip():
        return None

    for pat in _JSON_PATTERNS:
        m = re.search(pat, content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue

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


def extract_field(content: Any, field: str) -> Optional[Any]:
    """从 JSON 中提取单个字段。

    兼容壳格式：先查顶层，再查 data 字段。

    Args:
        content: str 或 dict
        field: 字段名（如 "score", "action", "direction"）

    Returns:
        字段值，或 None。
    """
    data = extract_json(content)
    if not data:
        return None
    if field in data:
        return data[field]
    inner = data.get("data", {})
    if isinstance(inner, dict) and field in inner:
        return inner[field]
    return None


def extract_score_and_action(content: Any) -> tuple:
    """便捷方法：同时提取 score 和 action。

    Returns:
        (score: float, action: str) — 缺失时返回默认值 (50.0, "hold")
    """
    data = extract_decision(content)
    if not data:
        return 50.0, "hold"
    score = float(data.get("score", 50))
    action = data.get("action", "hold")
    if action not in ("buy", "sell", "hold", "skip"):
        action = "hold"
    return max(0, min(100, score)), action
