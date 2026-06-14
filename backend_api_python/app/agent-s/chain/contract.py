# -*- coding: utf-8 -*-
"""
Skill Contract — SkillReport 解析契约。

Skill 层输出标准化：
  每个 Skill 的 final_answer 必须包含 JSON 结构（direction/confidence/score/signal/factors）
  本模块负责从 LLM 原始输出中解析出 SkillReport。

核心规则 — Skill 层职责边界：
  ✅ 允许输出：score / direction / confidence / signal / factors / analysis（事实描述）
  ❌ 禁止输出：action（buy/sell/hold/skip）— 这是 Chain 层的权力
  Skill 只负责"打分+描述事实"，不负责"给操作建议"

解析策略（三重降级）：
  1. JSON 块 — 直接提取 ```json ... ``` 中的结构化数据
  2. 关键词匹配 — 从文本中提取 direction/score/signal
  3. 兜底 — neutral + 默认分数
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport

logger = logging.getLogger(__name__)


def parse_skill_output(raw_output: str, skill_name: str = "") -> SkillReport:
    """从 LLM 原始输出解析 SkillReport。

    三重降级：JSON 块 → 关键词匹配 → 兜底 neutral。

    Skill 层职责边界：
      ✅ 输出: score / direction / confidence / signal / factors / analysis（事实描述）
      ❌ 禁止: action（buy/sell/hold/skip）— 由 Chain 层决定

    Args:
        raw_output: LLM 的原始 final_answer 文本
        skill_name: 技能名

    Returns:
        SkillReport
    """
    if not raw_output:
        return SkillReport(skill_name=skill_name, status="missing", error="无输出")

    # 策略 1: JSON 块
    report = _try_parse_json_block(raw_output, skill_name)
    if report:
        return _sanitize_report(report)

    # 策略 2: 关键词匹配
    report = _try_parse_keywords(raw_output, skill_name)
    if report:
        return _sanitize_report(report)

    # 策略 3: 兜底
    logger.warning("[Contract] 无法解析 skill 输出，兜底 neutral: %s", skill_name)
    return _sanitize_report(SkillReport(
        skill_name=skill_name,
        score=50.0,
        confidence=0.0,
        direction="neutral",
        signal="输出格式不规范，无法解析",
        analysis=raw_output[:2000],
        status="ok",
    ))


def _sanitize_report(report: SkillReport) -> SkillReport:
    """清理 SkillReport，确保 Skill 层不越权。

    - 移除 output_data 中的 action 字段（Skill 无权决定操作）
    - 清理 analysis 中的操作建议语句
    """
    # 移除 output_data 中的 action
    if report.output_data and "action" in report.output_data:
        del report.output_data["action"]

    # 清理 analysis 中的操作建议（替换为事实描述）
    if report.analysis:
        report.analysis = _strip_action_advice(report.analysis)

    return report


# 操作建议关键词 → 替换为事实描述
_ACTION_PATTERNS = [
    # 中文操作建议
    (r'建议[适量分批]*(买入|建仓|加仓|抄底|补仓)', r'技术面显示偏多信号'),
    (r'建议[适量]*(卖出|减仓|清仓|离场|止损)', r'技术面显示偏空信号'),
    (r'可以[适量分批]*(买入|建仓|加仓|抄底)', r'技术面显示偏多信号'),
    (r'可以[适量]*(卖出|减仓|清仓)', r'技术面显示偏空信号'),
    (r'[可建议]*(持有|观望|等待)', r'技术面显示中性信号'),
    (r'[操作建议：：]*\s*(买入|卖出|持有|观望)', r'方向：\1'),
    # 英文操作建议
    (r'[Ss]uggest\w*\s+(buy|sell|hold)', r'technical signal: \1'),
    (r'[Rr]ecommend\w*\s+(buy|sell|hold)', r'technical signal: \1'),
]


def _strip_action_advice(text: str) -> str:
    """从分析文本中移除操作建议语句，保留事实描述。"""
    import re
    for pattern, replacement in _ACTION_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


def _try_parse_json_block(raw: str, skill_name: str) -> Optional[SkillReport]:
    """尝试从 JSON 块中解析。"""
    # 匹配 ```json ... ``` 或 ``` ... ```
    patterns = [
        r'```json\s*\n?(.*?)\n?\s*```',
        r'```\s*\n?(.*?)\n?\s*```',
        r'(\{[^{}]*"direction"[^{}]*"score"[^{}]*\})',
        r'(\{[^{}]*"score"[^{}]*"direction"[^{}]*\})',
    ]

    for pattern in patterns:
        match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                data = json.loads(match.group(1).strip())
                if isinstance(data, dict) and ("direction" in data or "score" in data):
                    return _dict_to_report(data, skill_name, raw)
            except (json.JSONDecodeError, TypeError):
                continue

    return None


def _try_parse_keywords(raw: str, skill_name: str) -> Optional[SkillReport]:
    """从关键词中提取方向和分数。"""
    raw_lower = raw.lower()

    # 方向
    direction = "neutral"
    if any(kw in raw_lower for kw in ["极度看多", "强烈买入", "bullish"]):
        direction = "bullish"
    elif any(kw in raw_lower for kw in ["看多", "买入", "建议买", "buy"]):
        direction = "bullish"
    elif any(kw in raw_lower for kw in ["极度看空", "强烈卖出", "bearish"]):
        direction = "bearish"
    elif any(kw in raw_lower for kw in ["看空", "卖出", "建议卖", "sell"]):
        direction = "bearish"

    # 分数
    score = 50.0
    score_patterns = [
        r'(?:score|评分|分数)[：:\s]*(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s*[/／]\s*100',
    ]
    for pat in score_patterns:
        m = re.search(pat, raw)
        if m:
            try:
                score = float(m.group(1))
                score = max(0, min(100, score))
                break
            except ValueError:
                pass

    # 如果方向和分数不一致，以方向为准调整分数
    if direction == "bullish" and score < 50:
        score = 65.0
    elif direction == "bearish" and score > 50:
        score = 35.0

    # 置信度
    confidence = 0.5
    conf_patterns = [
        r'(?:confidence|置信度)[：:\s]*(\d+(?:\.\d+)?)',
    ]
    for pat in conf_patterns:
        m = re.search(pat, raw)
        if m:
            try:
                val = float(m.group(1))
                confidence = val if val <= 1.0 else val / 100.0
                break
            except ValueError:
                pass

    # 信号
    signal = ""
    signal_patterns = [
        r'(?:signal|信号)[：:\s]*(.+?)(?:\n|$)',
    ]
    for pat in signal_patterns:
        m = re.search(pat, raw)
        if m:
            signal = m.group(1).strip()[:200]
            break

    if not signal:
        # 取第一行非空文字
        for line in raw.split("\n"):
            line = line.strip()
            if line and len(line) > 5 and not line.startswith("#"):
                signal = line[:100]
                break

    return SkillReport(
        skill_name=skill_name,
        score=score,
        confidence=confidence,
        direction=direction,
        signal=signal,
        analysis=raw[:2000],
        status="ok",
    )


def _dict_to_report(data: Dict[str, Any], skill_name: str, raw: str) -> SkillReport:
    """将解析出的 dict 转为 SkillReport。"""
    direction = data.get("direction", "neutral")
    if direction not in ("bullish", "bearish", "neutral"):
        direction = "neutral"

    score = data.get("score", 50.0)
    if score is None:
        score = 50.0
    score = max(0, min(100, float(score)))

    confidence = data.get("confidence", 0.5)
    if confidence is None:
        confidence = 0.5
    if confidence > 1.0:
        confidence = confidence / 100.0

    factors = []
    for f in data.get("factors", []):
        if isinstance(f, dict):
            f_name = f.get("name", "")
            f_value = f.get("value", "")
            # 防御：确保 value 是字符串，不是 tuple/list 等
            if not isinstance(f_value, str):
                f_value = str(f_value)
            f_score = f.get("score")
            if isinstance(f_score, (tuple, list)):
                f_score = None
            factors.append(FactorItem(
                name=f_name,
                value=f_value,
                score=f_score,
                weight=f.get("weight", 1.0),
                status=f.get("status", "ok"),
            ))

    return SkillReport(
        skill_name=skill_name,
        score=score,
        confidence=confidence,
        direction=direction,
        signal=data.get("signal", ""),
        factors=factors,
        analysis=raw[:2000],
        output_data=data,
        status="ok",
    )


def extract_tools_called(raw: str) -> List[str]:
    """从 LLM 输出中提取工具调用列表。"""
    tools = []
    # 匹配 tool_name(...) 或 "tool_name" 模式
    patterns = [
        r'(?:调用|call|use|使用)\s*[`"\']?(\w+)[`"\']?\s*[\(（]',
        r'"tool_name"\s*:\s*"(\w+)"',
        r'agent_get_kline|get_realtime_quote|analyze_trend|calculate_ma|'
        r'get_volume_analysis|analyze_pattern|get_indicator_snapshot|'
        r'get_chip_distribution|search_stock_news|get_dragon_tiger',
    ]
    for pat in patterns:
        for m in re.finditer(pat, raw):
            tool = m.group(1) if m.lastindex else m.group(0)
            if tool and tool not in tools:
                tools.append(tool)
    return tools
