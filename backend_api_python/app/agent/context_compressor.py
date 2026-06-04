# -*- coding: utf-8 -*-
"""
Context Compressor — 跨轮上下文压缩。

agent.run() 结束后，把本轮结果（分析内容 + 工具调用）压缩成结构化 markdown，
存入 session store，下一轮作为上下文注入。

策略：规则引擎优先 + LLM 降级
  1. 规则引擎提取结构化字段 (<1ms, 零 LLM 调用)
  2. 质量检查：提取结果 >= 80字 且 含关键信息 → 直接用
  3. 质量不足 → LLM 降级压缩
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 规则引擎压缩
# ============================================================

# 结论/建议/风险段标题
_SECTION_HEADERS = re.compile(
    r'(?:^|\n)\s*(?:'
    r'#{1,3}\s*'
    r')?'
    r'(?:结论|总结|建议|判断|综合|评估|风险|提示|注意|操作建议|'
    r'分析结论|短期|中期|长期|后市|展望|策略|'
    r'技术面|基本面|资金面|消息面|'
    r'行情|走势|趋势|'
    r'修改|创建|删除|改动|变更|修复|重构'
    r')\s*[:\uff1a]?',
    re.IGNORECASE,
)

# 含金融数据的行
_DATA_LINE = re.compile(
    r'\d+[\.\d]*[%\uff05]'
    r'|\d+[\.\d]*[\u4e07\u4ebf]'
    r'|\d+[\.\d]*\u5143'
    r'|(?:\u6da8|\u8dcc|\u5e45).*?\d+'
    r'|(?:\u4ef7|\u6307\u6570|\u70b9\u4f4d).*?\d+'
    r'|(?:MACD|KDJ|RSI|BOLL|MA|\u5e03\u6797)'
    r'|(?:\u91d1\u53c9|\u6b7b\u53c9|\u8d85\u4e70|\u8d85\u5356|\u80cc\u79bb)'
    r'|(?:\u51c0\u6d41\u5165|\u51c0\u6d41\u51fa|\u4e3b\u529b|\u5317\u5411)'
    r'|(?:\u6da8\u505c|\u8dcc\u505c|\u6da8\u5e45|\u8dcc\u5e45|\u6362\u624b)',
    re.IGNORECASE,
)

# 股票代码/名称
_STOCK_PATTERN = re.compile(
    r'(?:[\u4e00-\u9fff]{2,5})?\s*[\uff08\(]\d{6}[\uff09\)]'
    r'|\b\d{6}\b'
    r'|(?:[A-Z]{1,5})\b'
)

# 文件路径/代码任务
_CODE_PATTERN = re.compile(
    r'(?:\u4fee\u6539|\u521b\u5efa|\u5220\u9664|\u91cd\u6784|\u4f18\u5316)\s*(?:\u4e86)?\s*(?:\u6587\u4ef6|\u4ee3\u7801|\u51fd\u6570|\u7c7b|\u6a21\u5757)?'
    r'|(?:\.py|\.js|\.ts|\.vue|\.json|\.yaml|\.md)\b'
    r'|(?:def |class |import |from )'
    r'|(?:\u6587\u4ef6|\u8def\u5f84|\u76ee\u5f55)\s*[:\uff1a]?\s*\S+',
    re.IGNORECASE,
)

# 行首标记
_LINE_MARKER = re.compile(r'^\s*(?:[-*\u2022]\s*|\d+[.\u3001\uff09\uff09]\s*|#{1,3}\s+|>\s*)')

# 空行/纯标点行
_EMPTY_LINE = re.compile(r'^\s*$|^\s*[-=*_]{3,}\s*$')


def compress_context_rule(
    output: str,
    tool_calls: List[Dict] = None,
    max_len: int = 300,
) -> Tuple[str, bool]:
    """
    规则引擎压缩 agent 输出。

    Returns:
        (压缩文本, 是否高质量)
    """
    if not output:
        return "", False

    text = _clean_output(output)
    if len(text) <= max_len:
        return text, True

    lines = text.split('\n')
    lines = [l for l in lines if not _EMPTY_LINE.match(l)]

    if len(lines) <= 2:
        return _smart_truncate(text, max_len), False

    # 每行打分
    scored: List[Tuple[float, int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        score = _score_line(stripped, i, len(lines))
        scored.append((score, i, stripped))

    scored.sort(key=lambda x: (-x[0], x[1]))

    # 贪心拼接
    selected_indices = set()
    total_len = 0
    for score, idx, line in scored:
        if score <= 0:
            continue
        line_len = len(line) + 1
        if total_len + line_len > max_len:
            remaining = max_len - total_len - 1
            if remaining > 20:
                selected_indices.add(idx)
            break
        selected_indices.add(idx)
        total_len += line_len
        if total_len >= max_len:
            break

    result_lines = [lines[i].strip() for i in sorted(selected_indices)]
    result = '\n'.join(result_lines)

    # 追加工具名
    tool_names = _extract_tool_names(tool_calls)
    if tool_names:
        tool_line = "- \u5de5\u5177: " + ', '.join(tool_names)
        if len(result) + len(tool_line) + 1 <= max_len:
            result = result + '\n' + tool_line

    is_high_quality = _check_quality(result, output)

    if not is_high_quality:
        result = _smart_truncate(text, max_len)

    return result, is_high_quality


def _score_line(line: str, position: int, total: int) -> float:
    """给每行打分"""
    score = 0.0

    if _SECTION_HEADERS.search(line):
        score += 5.0

    data_hits = len(_DATA_LINE.findall(line))
    score += min(data_hits * 2.0, 6.0)

    if _STOCK_PATTERN.search(line):
        score += 2.0

    if _CODE_PATTERN.search(line):
        score += 3.0

    if position <= 2:
        score += 2.0
    elif position >= total - 3:
        score += 3.0

    if _LINE_MARKER.match(line):
        score += 1.0

    line_len = len(line)
    if line_len < 5:
        score -= 2.0
    elif line_len > 150:
        score -= 1.0

    return score


def _extract_tool_names(tool_calls: List[Dict]) -> List[str]:
    if not tool_calls:
        return []
    seen = set()
    names = []
    for tc in tool_calls:
        name = tc.get("tool", "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _check_quality(result: str, original: str) -> bool:
    """质量检查"""
    if len(result) < 80:
        return False
    has_data = bool(re.search(r'\d+', result))
    has_keyword = bool(re.search(
        r'结论|建议|风险|涨|跌|技术|资金|MACD|KDJ|工具|修改|创建',
        result,
    ))
    has_lines = result.count('\n') >= 1
    return (has_data or has_keyword) and has_lines


def _clean_output(text: str) -> str:
    text = re.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', text)
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _smart_truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for sep in ['\n', '\u3002', '\uff01', '\uff1f', '\uff1b', '.', '!', '?', ';']:
        pos = cut.rfind(sep)
        if pos > max_len * 0.5:
            return cut[:pos + 1].rstrip()
    return cut.rstrip() + "\u2026"


# ============================================================
# LLM 降级压缩
# ============================================================

_COMPRESS_PROMPT = """将以下 Agent 分析结果压缩为结构化 markdown 摘要。

## 要求
1. 保留关键数据（价格、涨跌幅、指标值、结论）
2. 保留涉及的股票代码和名称
3. 保留调用过的工具名列表
4. 去掉冗余描述，只留要点
5. 如果是代码相关任务，保留修改了哪些文件、做了什么改动
6. 控制在 300 字以内
7. 只输出 markdown，不要其他文字

## Agent 输出
{output}

## 工具调用记录
{tool_calls}
"""


def _compress_context_llm(
    output: str,
    tool_calls: List[Dict] = None,
    model: str = None,
) -> str:
    """LLM 降级压缩"""
    tool_text = ""
    if tool_calls:
        names = [tc.get("tool", "") for tc in tool_calls if tc.get("tool")]
        tool_text = ", ".join(names) if names else "\uff08\u65e0\uff09"
    else:
        tool_text = "\uff08\u65e0\uff09"

    prompt = _COMPRESS_PROMPT.format(output=output[:3000], tool_calls=tool_text)

    try:
        from app.services.llm import LLMService
        import requests

        svc = LLMService(provider=None)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        compress_model = model or os.getenv("AGENT_COMPRESS_MODEL", "").strip() or svc.get_default_model()

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": compress_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 600,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("[Compress:LLM] \u539f\u59cb %d \u5b57 \u2192 \u538b\u7f29 %d \u5b57", len(output), len(summary))
        return summary

    except Exception as e:
        logger.warning("[Compress:LLM] \u8c03\u7528\u5931\u8d25: %s", e)
        return ""


# ============================================================
# 主入口：规则引擎优先 + LLM 降级
# ============================================================

def compress_context(
    output: str,
    tool_calls: List[Dict] = None,
    model: str = None,
    domain: str = "",
) -> str:
    """压缩 agent 本轮输出为 markdown 摘要。

    策略：
      1. output < 200字 → 原文返回
      2. 规则引擎提取 (<1ms)
      3. 质量足够 → 直接返回
      4. 质量不足 → LLM 降级
      5. LLM 也失败 → 截断前 500 字
    """
    if not output:
        return ""

    if len(output) < 200:
        return output

    # Step 1: 规则引擎
    rule_result, is_high_quality = compress_context_rule(output, tool_calls, max_len=300)

    if is_high_quality:
        logger.info(
            "[Compress:Rule] \u539f\u59cb %d \u5b57 \u2192 \u538b\u7f29 %d \u5b57 (\u9ad8\u8d28\u91cf)",
            len(output), len(rule_result),
        )
        return rule_result

    # Step 2: LLM 降级
    logger.info(
        "[Compress] \u89c4\u5219\u5f15\u64ce\u8d28\u91cf\u4e0d\u8db3 (len=%d), \u964d\u7ea7\u5230 LLM",
        len(rule_result),
    )
    llm_result = _compress_context_llm(output, tool_calls, model=model)

    if llm_result and len(llm_result) >= 50:
        logger.info("[Compress:LLM] \u539f\u59cb %d \u5b57 \u2192 \u538b\u7f29 %d \u5b57", len(output), len(llm_result))
        return llm_result

    # Step 3: 最终降级
    fallback = rule_result if rule_result else output[:500]
    logger.warning("[Compress] LLM \u4e5f\u5931\u8d25, \u964d\u7ea7\u622a\u65ad: %d \u5b57", len(fallback))
    return fallback
