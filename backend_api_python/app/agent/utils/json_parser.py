"""
JSON 容错解析器 

处理 LLM 返回的不规范 JSON：
  1. 提取 ```json ... ``` 代码块
  2. 定位 { } 边界
  3. 修复尾随逗号
  4. 解析并返回
"""

import re
import json
from typing import Any, Dict, Optional

from .logger import get_logger

logger = get_logger("utils.json_parser")


def safe_parse_json(text: str, default: Any = None) -> Any:
    """
    安全解析可能不规范的 JSON 文本

    Args:
        text: LLM 原始输出文本
        default: 解析失败时的默认返回值

    Returns:
        解析后的 Python 对象，或 default
    """
    if not text or not text.strip():
        return default

    # Step 1: 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 2: 提取 markdown 代码块
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1))
        except json.JSONDecodeError:
            text = code_block_match.group(1)

    # Step 3: 定位 JSON 边界
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        # 尝试数组
        start = text.find("[")
        end = text.rfind("]")

    if start != -1 and end > start:
        json_str = text[start:end + 1]

        # Step 4: 修复尾随逗号 (,} 或 ,])
        json_str = re.sub(r",\s*([}\]])", r"\1", json_str)

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 边界提取后仍解析失败: {e}")
            logger.debug(f"提取内容: {json_str[:200]}...")

    logger.error(f"JSON 解析彻底失败，原文前200字符: {text[:200]}")
    return default


# ── Shell 格式正则（按优先级排序）─────────────────────────────
_JSON_PATTERNS = [
    r'```json\s*\n?(.*?)\n?\s*```',                    # markdown JSON 块
    r'(\{[^{}]*"reply"[^{}]*"data"[^{}]*\})',        # 壳格式：reply+data（无嵌套）
    r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',      # 旧格式：行内 action+score
    r'(\{[^{}]*"score"[^{}]*"action"[^{}]*\})',      # 旧格式：行内 score+action
    r'(\{\s*"reply"\s*:\s*"[^"]+".*?"data"\s*:\s*\{.*?\}\s*\})',  # 壳格式：嵌套
]


def extract_json(content: Any, default: Any = None) -> Any:
    """从内容中提取第一个有效 JSON dict。

    优先级：
      1. dict 直接返回
      2. 整体是合法 JSON → 直接解析
      3. markdown JSON 块提取
      4. 正则模式匹配（简单/嵌套壳格式）
      5. 括号平衡法提取最长 JSON 块

    Args:
        content: str 或 dict。dict 直接返回；str 尝试多种方式解析。
        default: 解析失败时的默认返回值

    Returns:
        解析成功的 dict，或 default。
    """
    if isinstance(content, dict):
        return content

    if not isinstance(content, str) or not content.strip():
        return default

    # 1. 整体是合法 JSON
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

    # 3. 正则模式匹配（简单壳格式）
    for pat in _JSON_PATTERNS[1:]:
        m = re.search(pat, content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue

    # 4. 括号平衡法
    pos = 0
    while pos < len(content):
        start = content.find('{', pos)
        if start == -1:
            break
        result = _extract_json_from_offset(content, start)
        if result is not None:
            return result
        pos = start + 1

    return default


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


def extract_decision(content: Any, default: Any = None) -> Any:
    """提取含 "action" 字段的决策 JSON。

    兼容两种格式：
      1. 旧格式：action/score 在顶层
      2. 通用壳：action/score 在 data 字段内
    """
    data = extract_json(content, default=None)
    if not data:
        return default
    # 顶层有 action
    if "action" in data:
        return data
    # 壳格式：action 在 data 里
    inner = data.get("data", {})
    if isinstance(inner, dict) and "action" in inner:
        return inner
    return default
