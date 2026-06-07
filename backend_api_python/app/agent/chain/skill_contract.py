# -*- coding: utf-8 -*-
"""
Skill Output Contract — Skill 输出契约。

定义每个 skill 应该输出的格式模板，以及从原始文本中解析结构化数据的函数。

解析策略（按优先级）：
1. 尝试从 skill 输出中提取 JSON 块（```json...``` 或直接的 {}）
2. 提取 direction, confidence, score, signal, factors
3. 如果是纯文本，回退到关键词匹配（兼容过渡期）
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 输出模板（注入到 skill instructions 末尾）
# ═══════════════════════════════════════════════════════════════

SKILL_OUTPUT_TEMPLATE = """

## 输出格式（必须遵守）

你的 final_answer 必须包含以下JSON结构（可以嵌在正文中）：

```json
{
  "direction": "bullish/bearish/neutral",
  "confidence": 0.0-1.0,
  "score": 0-100,
  "signal": "一句话信号摘要",
  "factors": [
    {"name": "因子名", "value": "值", "score": 0-100, "status": "ok"}
  ]
}
```

规则：
- score: 0=极度看空, 50=中性, 100=极度看多
- confidence: 基于数据充分程度，不是基于方向确定性
- status: "ok"=有数据, "missing"=数据缺失
- 缺失的因子必须标记 status:"missing"，不能编造值
- factors 中每个因子都应该对应你分析的一个具体维度
"""


# ═══════════════════════════════════════════════════════════════
# 解析函数
# ═══════════════════════════════════════════════════════════════

def parse_skill_output(raw_text: str) -> Dict[str, Any]:
    """从 skill 原始输出中解析结构化数据。

    Args:
        raw_text: skill 的原始文本输出。

    Returns:
        {
            "direction": "bullish"/"bearish"/"neutral",
            "confidence": float (0-1),
            "score": float (0-100) or None,
            "signal": str,
            "factors": [{"name": str, "value": str, "score": float, "status": str}],
            "parse_method": "json"/"keyword"/"fallback",
        }
    """
    if not raw_text or not isinstance(raw_text, str):
        return _empty_result("empty_input")

    # 策略1: 提取 JSON 块
    result = _try_parse_json(raw_text)
    if result:
        result["parse_method"] = "json"
        return result

    # 策略2: 关键词匹配（兼容过渡期）
    result = _try_parse_keywords(raw_text)
    if result:
        result["parse_method"] = "keyword"
        return result

    # 策略3: 兜底 — 返回中性
    return _empty_result("fallback")


def _empty_result(reason: str) -> Dict[str, Any]:
    """返回空结果。"""
    return {
        "direction": "neutral",
        "confidence": 0.0,
        "score": None,
        "signal": "",
        "factors": [],
        "parse_method": reason,
    }


# ═══════════════════════════════════════════════════════════════
# JSON 解析策略
# ═══════════════════════════════════════════════════════════════

def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """尝试从文本中提取并解析 JSON 块。"""
    # 模式1: ```json ... ``` 代码块
    json_blocks = re.findall(r'```json\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    for block in json_blocks:
        result = _parse_json_block(block.strip())
        if result:
            return result

    # 模式2: 直接的 JSON 对象（{...}）
    # 从后往前找，因为通常 JSON 在输出末尾
    json_objects = _find_json_objects(text)
    for obj_str in reversed(json_objects):
        result = _parse_json_block(obj_str)
        if result:
            return result

    return None


def _find_json_objects(text: str) -> List[str]:
    """从文本中提取所有可能的 JSON 对象字符串。"""
    objects = []
    depth = 0
    start = -1

    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(text[start:i + 1])
                start = -1

    return objects


def _parse_json_block(block: str) -> Optional[Dict[str, Any]]:
    """尝试解析一个 JSON 块，提取标准化字段。"""
    try:
        data = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # 检查是否包含方向字段（最低要求）
    if "direction" not in data and "score" not in data:
        return None

    result = {}

    # direction
    direction = data.get("direction", "neutral")
    if isinstance(direction, str):
        direction = direction.lower().strip()
        if direction not in ("bullish", "bearish", "neutral"):
            # 尝试中文映射
            cn_map = {"看多": "bullish", "看涨": "bullish", "看空": "bearish",
                      "看跌": "bearish", "中性": "neutral"}
            direction = cn_map.get(direction, "neutral")
        result["direction"] = direction
    else:
        result["direction"] = "neutral"

    # confidence
    confidence = data.get("confidence", 0.5)
    if isinstance(confidence, (int, float)):
        if confidence > 1:
            confidence = confidence / 100
        result["confidence"] = max(0.0, min(1.0, float(confidence)))
    else:
        result["confidence"] = 0.5

    # score
    score = data.get("score")
    if isinstance(score, (int, float)):
        result["score"] = max(0.0, min(100.0, float(score)))
    else:
        result["score"] = None

    # signal
    signal = data.get("signal", "")
    result["signal"] = str(signal)[:200] if signal else ""

    # factors
    factors = data.get("factors", [])
    if isinstance(factors, list):
        parsed_factors = []
        for f in factors:
            if isinstance(f, dict):
                parsed_factors.append({
                    "name": str(f.get("name", "")),
                    "value": str(f.get("value", "")),
                    "score": _safe_float(f.get("score")),
                    "status": f.get("status", "ok"),
                })
        result["factors"] = parsed_factors
    else:
        result["factors"] = []

    return result


def _safe_float(val: Any) -> Optional[float]:
    """安全转 float。"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════
# 关键词匹配策略（兼容过渡期）
# ═══════════════════════════════════════════════════════════════

_BULLISH_KEYWORDS = [
    "看多", "看涨", "利多", "利好", "买入", "上涨", "强势", "突破", "放量",
    "金叉", "涨停", "主升", "多头", "bullish", "buy", "positive",
    "底部放量", "均线多头", "趋势向好", "动量增强",
]

_BEARISH_KEYWORDS = [
    "看空", "看跌", "利空", "利淡", "卖出", "下跌", "弱势", "破位", "缩量",
    "死叉", "跌停", "空头", "bearish", "sell", "negative",
    "高位放量滞涨", "均线空头", "趋势转弱", "动量衰减",
]


def _try_parse_keywords(text: str) -> Optional[Dict[str, Any]]:
    """关键词匹配策略。"""
    text_lower = text.lower()

    bull_count = sum(1 for w in _BULLISH_KEYWORDS if w in text_lower)
    bear_count = sum(1 for w in _BEARISH_KEYWORDS if w in text_lower)

    if bull_count == 0 and bear_count == 0:
        return None

    if bull_count > bear_count:
        direction = "bullish"
        confidence = min(0.8, 0.3 + (bull_count - bear_count) * 0.1)
    elif bear_count > bull_count:
        direction = "bearish"
        confidence = min(0.8, 0.3 + (bear_count - bull_count) * 0.1)
    else:
        direction = "neutral"
        confidence = 0.3

    # 尝试提取信号摘要（取第一句有意义的话）
    signal = _extract_signal_from_text(text)

    # 尝试提取置信度数值
    conf_match = re.search(r'置信度[：:]\s*(\d+\.?\d*)%?', text)
    if conf_match:
        val = float(conf_match.group(1))
        if val > 1:
            val = val / 100
        confidence = max(confidence, min(1.0, val))

    return {
        "direction": direction,
        "confidence": confidence,
        "score": None,  # 关键词模式无法提取精确分数
        "signal": signal,
        "factors": [],
    }


def _extract_signal_from_text(text: str) -> str:
    """从文本中提取信号摘要。"""
    # 取第一行有意义的内容（跳过空行和短行）
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 10 and not line.startswith(("#", "```", "---")):
            # 截断到 100 字
            return line[:100]
    return ""


# ═══════════════════════════════════════════════════════════════
# 工具详情解析（从旧版迁移）
# ═══════════════════════════════════════════════════════════════

def parse_tool_details(text: str) -> List[Dict[str, Any]]:
    """从 agent 输出中解析工具调用详情。

    支持多种格式：
    - JSON 块: {"tool": "xxx", "success": true}
    - "工具: xxx" 或 "Calling tool: xxx"
    - `tool_name` 反引号格式
    """
    details = []
    seen = set()

    # 模式1: JSON 工具调用块
    for m in re.finditer(r'\{[^{}]*"tool"\s*:\s*"([^"]+)"[^{}]*\}', text):
        try:
            start = text.rfind('{', 0, m.start())
            end = text.find('}', m.end()) + 1
            obj = json.loads(text[start:end])
            name = obj.get("tool", "")
            if name and name not in seen:
                seen.add(name)
                details.append({
                    "name": name,
                    "ok": obj.get("success", obj.get("ok", True)),
                    "ms": obj.get("ms", obj.get("latency_ms", 0)),
                })
        except (json.JSONDecodeError, ValueError):
            pass

    # 模式2: "工具: xxx" 或 "Calling tool: xxx"
    if not details:
        for m in re.finditer(
            r'(?:工具|Calling tool|Used tool|调用工具)[：:]\s*(\w+)', text
        ):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                details.append({"name": name, "ok": True, "ms": 0})

    # 模式3: 反引号工具名
    if not details:
        for m in re.finditer(r'`(\w+)`', text):
            name = m.group(1)
            if ('_' in name or name.startswith(('get_', 'run_', 'list_', 'search'))) and name not in seen:
                seen.add(name)
                details.append({"name": name, "ok": True, "ms": 0})

    return details
