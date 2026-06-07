# -*- coding: utf-8 -*-
"""
Context Compressor — 跨轮上下文压缩。

agent.run() 结束后，把本轮结果（分析内容 + 工具调用）压缩成结构化 markdown，
存入 session store，下一轮作为上下文注入。

策略：规则引擎优先 + LLM 降级
  1. 规则引擎提取结构化字段 (<1ms, 零 LLM 调用)
  2. 质量检查：提取结果 >= 80字 且 含关键信息 → 直接用
  3. 质量不足 → LLM 降级压缩

改进点：
  - 结构化股票信息：提取 analyzed_stocks / last_stock / domain / key_conclusions
  - 短内容跳过压缩：低于阈值直接返回原文
  - 老摘要更激进压缩：基于 age_turns 衰减 max_len
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 配置项（可通过环境变量调整）
# ============================================================

# 低于此字数，直接返回原文，不压缩
SKIP_COMPRESS_THRESHOLD = int(os.getenv("COMPRESS_SKIP_THRESHOLD", "500"))
# 规则引擎压缩目标长度（默认/新摘要）
_RULE_MAX_LEN = int(os.getenv("COMPRESS_RULE_MAX_LEN", "300"))
# 老摘要（>= 此轮次后）的激进压缩目标
_AGGRESSIVE_MAX_LEN = int(os.getenv("COMPRESS_AGGRESSIVE_MAX_LEN", "150"))
# 多少轮后开始激进压缩
_AGGRESSIVE_AFTER_TURNS = int(os.getenv("COMPRESS_AGGRESSIVE_TURNS", "3"))
# 结构化信息区块最大长度
_STRUCT_MAX_LEN = 200

# ============================================================
# 结构化股票信息提取
# ============================================================

# 6 位股票代码（含前缀可选）
_STOCK_CODE_RE = re.compile(r'(?:[shszSHSZ]{2})?(\d{6})')
# 中文股票名称（带括号代码）
_STOCK_NAME_CODE_RE = re.compile(r'([\u4e00-\u9fff]{2,6})\s*[\uff08\(](\d{6})[\uff09\)]')
# 纯中文股票名（2-5 字，排除常见动词/虚词）
_STOCK_NAME_RE = re.compile(r'[\u4e00-\u9fff]{2,5}')
_STOCK_STOPWORDS = frozenset({
    "帮我", "分析", "查看", "看看", "查询", "怎么样", "什么", "如何",
    "的", "了", "吗", "吧", "呢", "啊", "一下", "最近", "今天", "昨天",
    "修改", "修复", "创建", "写", "生成", "筛选", "选择", "回测", "启动",
    "停止", "显示", "展示", "项目", "代码", "文件", "目录", "结构",
    "结论", "建议", "风险", "技术", "基本面", "资金面", "消息面",
    "短线", "中线", "长线", "趋势", "走势", "行情", "指标",
    "操作", "买入", "卖出", "持有", "观望", "减仓", "加仓",
})

# 结论/判断句式
_CONCLUSION_RE = re.compile(
    r'(?:结论|综合判断|总体[评看]|操作建议|短线|中线|建议)[：:]\s*(.{10,80})',
    re.IGNORECASE,
)

# 方向性关键词
_DIRECTION_KEYWORDS = {
    "bullish": ["看多", "看涨", "买入", "加仓", "做多", "偏多", "向上", "突破", "强势"],
    "bearish": ["看空", "看跌", "卖出", "减仓", "做空", "偏空", "向下", "破位", "弱势"],
    "neutral": ["观望", "等待", "中性", "震荡", "横盘", "不动", "持有"],
}


def extract_structured_info(
    output: str,
    tool_calls: List[Dict] = None,
    domain: str = "",
) -> Dict[str, Any]:
    """从 agent 输出中提取结构化股票信息。

    Returns:
        {
            "analyzed_stocks": [{"code": "600519", "name": "贵州茅台"}],
            "last_stock": {"code": "600519", "name": "贵州茅台"},
            "domain": "finance",
            "direction": "bullish" | "bearish" | "neutral" | "",
            "key_conclusions": ["短线看多，目标价2100", "止损位1900"],
            "tools_used": ["get_realtime_quote", "analyze_trend"],
            "timestamp": 1234567890.0,
        }
    """
    info: Dict[str, Any] = {
        "analyzed_stocks": [],
        "last_stock": None,
        "domain": domain,
        "direction": "",
        "key_conclusions": [],
        "tools_used": [],
        "timestamp": time.time(),
    }

    if not output:
        return info

    # ── 提取股票代码和名称 ───────────────────────────────────
    seen_codes = set()
    # 优先匹配 "名称(代码)" 格式
    for m in _STOCK_NAME_CODE_RE.finditer(output):
        name, code = m.group(1), m.group(2)
        if code not in seen_codes:
            seen_codes.add(code)
            info["analyzed_stocks"].append({"code": code, "name": name})

    # 再匹配散落的 6 位代码
    for m in _STOCK_CODE_RE.finditer(output):
        code = m.group(1)
        if code not in seen_codes and code.startswith(("0", "3", "6")):
            seen_codes.add(code)
            info["analyzed_stocks"].append({"code": code, "name": ""})

    # 最后一个股票 = 最近分析的
    if info["analyzed_stocks"]:
        info["last_stock"] = info["analyzed_stocks"][-1]

    # ── 提取方向 ─────────────────────────────────────────────
    output_lower = output.lower()
    for direction, keywords in _DIRECTION_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in output_lower)
        if hits >= 2:
            info["direction"] = direction
            break
    # 单关键词命中但出现在结论附近也算
    if not info["direction"]:
        conclusion_area = output[-500:] if len(output) > 500 else output
        for direction, keywords in _DIRECTION_KEYWORDS.items():
            if any(kw in conclusion_area for kw in keywords):
                info["direction"] = direction
                break

    # ── 提取关键结论 ─────────────────────────────────────────
    for m in _CONCLUSION_RE.finditer(output):
        conclusion = m.group(1).strip()
        if conclusion and len(conclusion) >= 5:
            info["key_conclusions"].append(conclusion)

    # 如果没匹配到结论句式，取最后 2-3 句有实质内容的句子
    if not info["key_conclusions"]:
        sentences = re.split(r'[。\n]', output[-600:])
        for s in reversed(sentences):
            s = s.strip()
            if len(s) >= 15 and re.search(r'\d+|结论|建议|风险|看[多空]|买|卖|持有|观望', s):
                info["key_conclusions"].insert(0, s)
                if len(info["key_conclusions"]) >= 3:
                    break

    # ── 提取工具名 ───────────────────────────────────────────
    info["tools_used"] = _extract_tool_names(tool_calls)

    return info


def format_structured_info(info: Dict[str, Any]) -> str:
    """将结构化信息格式化为 markdown 区块，注入到压缩结果前部。"""
    parts = []

    # 股票信息
    stocks = info.get("analyzed_stocks", [])
    if stocks:
        stock_strs = []
        for s in stocks:
            if s["name"]:
                stock_strs.append(f"{s['name']}({s['code']})")
            else:
                stock_strs.append(s["code"])
        parts.append(f"- **分析标的**: {', '.join(stock_strs)}")
        last = info.get("last_stock")
        if last and len(stocks) > 1:
            parts.append(f"- **最近关注**: {last.get('name', '')}({last['code']})")

    # 领域
    domain = info.get("domain", "")
    if domain and domain != "chat":
        parts.append(f"- **领域**: {domain}")

    # 方向
    direction = info.get("direction", "")
    if direction:
        direction_map = {"bullish": "看多", "bearish": "看空", "neutral": "中性/观望"}
        parts.append(f"- **方向**: {direction_map.get(direction, direction)}")

    # 关键结论
    conclusions = info.get("key_conclusions", [])
    if conclusions:
        parts.append("- **关键结论**:")
        for c in conclusions[:3]:
            parts.append(f"  - {c}")

    # 工具
    tools = info.get("tools_used", [])
    if tools:
        parts.append(f"- **工具**: {', '.join(tools[:10])}")

    return "\n".join(parts) if parts else ""


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

# 股票代码/名称（用于行级打分）
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

    # 追加工具名（如果结构化信息中没有）
    tool_names = _extract_tool_names(tool_calls)
    if tool_names:
        tool_line = "- 工具: " + ', '.join(tool_names)
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


# ── 方向性关键词净化（防止跨轮次结论污染）──────────────────
_DIRECTIONAL_PATTERNS = re.compile(
    r'(?:建议|推荐|操作建议|短线建议|最终建议)\s*[:：]\s*(?:买入|卖出|加仓|减仓|观望|持有)',
    re.IGNORECASE,
)
_VERDICT_LINE = re.compile(
    r'^\s*[-*]\s*(?:方向|结论|判断|建议|操作)\s*[:：]\s*.*(?:买入|卖出|看多|看空|加仓|减仓|观望)',
    re.IGNORECASE | re.MULTILINE,
)


def _strip_directional_bias(text: str) -> str:
    """从压缩摘要中移除方向性结论，防止污染下一轮分析。

    保留数据事实（价格、指标值），移除主观判断（买入/卖出/看多/看空）。
    """
    # 移除"建议: 买入/卖出"这类结论行
    text = _VERDICT_LINE.sub('', text)
    # 移除行内的方向性建议
    text = _DIRECTIONAL_PATTERNS.sub('', text)
    # 清理多余空行
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
6. 控制在 {max_len} 字以内
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
    max_len: int = 300,
) -> str:
    """LLM 降级压缩"""
    tool_text = ""
    if tool_calls:
        names = [tc.get("tool", "") for tc in tool_calls if tc.get("tool")]
        tool_text = ", ".join(names) if names else "（无）"
    else:
        tool_text = "（无）"

    prompt = _COMPRESS_PROMPT.format(
        output=output[:3000],
        tool_calls=tool_text,
        max_len=max_len,
    )

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
        logger.info("[Compress:LLM] 原始 %d 字 → 压缩 %d 字", len(output), len(summary))
        return summary

    except Exception as e:
        logger.warning("[Compress:LLM] 调用失败: %s", e)
        return ""


# ============================================================
# 主入口：规则引擎优先 + LLM 降级 + 结构化信息 + 老摘要衰减
# ============================================================

def compress_context(
    output: str,
    tool_calls: List[Dict] = None,
    model: str = None,
    domain: str = "",
    age_turns: int = 0,
) -> str:
    """压缩 agent 本轮输出为结构化 markdown 摘要。

    策略：
      1. output < SKIP_COMPRESS_THRESHOLD → 原文返回（跳过压缩）
      2. 提取结构化股票信息（代码/名称/方向/结论）
      3. 规则引擎压缩（按 age_turns 衰减目标长度）
      4. 质量足够 → 返回 结构化信息 + 压缩正文
      5. 质量不足 → LLM 降级
      6. LLM 也失败 → 截断

    Args:
        output: agent 本轮完整输出
        tool_calls: 工具调用日志
        model: LLM 模型名（LLM 降级时使用）
        domain: 当前领域
        age_turns: 已存储轮次数（0=本轮刚生成，1+=已存了N轮）。
                   值越大 → 压缩越激进。

    Returns:
        结构化 markdown 摘要字符串
    """
    if not output:
        return ""

    cleaned = _clean_output(output)

    # ── Step 0: 短内容跳过压缩（阈值随 age 衰减）────────────
    # 新摘要（age=0）：低于阈值直接返回
    # 老摘要（age>0）：阈值线性衰减，age 越大越倾向压缩
    skip_threshold = SKIP_COMPRESS_THRESHOLD
    if age_turns > 0:
        decay = min(age_turns, 5)
        skip_threshold = max(
            int(SKIP_COMPRESS_THRESHOLD * (1 - 0.15 * decay)),
            _AGGRESSIVE_MAX_LEN,  # 最低不低于激进压缩目标
        )
    if len(cleaned) <= skip_threshold:
        logger.info(
            "[Compress] 内容 %d 字 ≤ 阈值 %d，跳过压缩",
            len(cleaned), SKIP_COMPRESS_THRESHOLD,
        )
        # 即使不压缩，也提取结构化信息（方便后续检索）
        info = extract_structured_info(output, tool_calls, domain)
        struct_block = format_structured_info(info)
        if struct_block:
            return f"{struct_block}\n\n{cleaned}"
        return cleaned

    # ── Step 1: 提取结构化股票信息 ────────────────────────────
    info = extract_structured_info(output, tool_calls, domain)
    struct_block = format_structured_info(info)

    # ── Step 2: 根据 age_turns 计算压缩目标长度 ──────────────
    # 老摘要：age 越大 → 目标越短，线性衰减
    if age_turns >= _AGGRESSIVE_AFTER_TURNS:
        # 从 _RULE_MAX_LEN 线性衰减到 _AGGRESSIVE_MAX_LEN
        decay = min(age_turns - _AGGRESSIVE_AFTER_TURNS, 5)  # 最多衰减 5 档
        step = (_RULE_MAX_LEN - _AGGRESSIVE_MAX_LEN) / 5
        max_len = int(_RULE_MAX_LEN - step * decay)
        logger.info(
            "[Compress] 老摘要 (age=%d ≥ %d)，压缩目标 %d 字",
            age_turns, _AGGRESSIVE_AFTER_TURNS, max_len,
        )
    else:
        max_len = _RULE_MAX_LEN

    # 结构化区块占用的长度预算
    struct_len = len(struct_block) + 2 if struct_block else 0
    body_max_len = max_len - struct_len
    if body_max_len < 80:
        body_max_len = 80  # 保底

    # ── Step 3: 规则引擎压缩 ─────────────────────────────────
    rule_result, is_high_quality = compress_context_rule(
        output, tool_calls, max_len=body_max_len,
    )

    if is_high_quality:
        result = _combine_struct_and_body(struct_block, rule_result)
        # 移除方向性结论，防止跨轮次污染
        result = _strip_directional_bias(result)
        logger.info(
            "[Compress:Rule] 原始 %d 字 → 压缩 %d 字 (高质量, age=%d)",
            len(output), len(result), age_turns,
        )
        return result

    # ── Step 4: LLM 降级 ─────────────────────────────────────
    logger.info(
        "[Compress] 规则引擎质量不足 (len=%d), 降级到 LLM (target=%d)",
        len(rule_result), body_max_len,
    )
    llm_result = _compress_context_llm(
        output, tool_calls, model=model, max_len=body_max_len,
    )

    if llm_result and len(llm_result) >= 50:
        result = _combine_struct_and_body(struct_block, llm_result)
        result = _strip_directional_bias(result)
        logger.info(
            "[Compress:LLM] 原始 %d 字 → 压缩 %d 字 (age=%d)",
            len(output), len(result), age_turns,
        )
        return result

    # ── Step 5: 最终降级 ─────────────────────────────────────
    fallback = rule_result if rule_result else _smart_truncate(cleaned, body_max_len)
    result = _combine_struct_and_body(struct_block, fallback)
    result = _strip_directional_bias(result)
    logger.warning("[Compress] LLM 也失败, 降级截断: %d 字 (age=%d)", len(result), age_turns)
    return result


def _combine_struct_and_body(struct_block: str, body: str) -> str:
    """拼接结构化信息和压缩正文。"""
    if struct_block:
        return f"{struct_block}\n\n{body}"
    return body
