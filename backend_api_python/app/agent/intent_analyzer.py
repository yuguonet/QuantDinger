# -*- coding: utf-8 -*-
"""
Intent Analyzer — 轻量级前置意图分析。

在 agent 执行前，用一次低成本 LLM 调用分析用户消息，
输出结构化的领域 + 意图 + 参数，供后续路由和工具过滤使用。

设计原则：
- 独立于 agent，不依赖工具目录，token 消耗极小
- 输出严格 JSON，便于程序化处理
- 失败时降级到默认流程（不阻塞主流程）
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.domain_registry import (
    build_intent_prompt_domains,
    build_intent_prompt_examples,
    get_domain,
    get_all_domains,
)

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """意图分析结果。"""
    domain: str = "chat"                       # 领域标识
    intent: str = ""                           # 具体意图
    params: Dict[str, Any] = field(default_factory=dict)  # 提取的参数
    confidence: float = 0.0                    # 置信度 0-1
    raw_response: str = ""                     # LLM 原始返回

    @property
    def domain_config(self):
        return get_domain(self.domain)

    @property
    def tool_filter(self) -> Optional[List[str]]:
        """该领域限定的工具列表，None 表示不限制。

        TODO: 当前未启用工具过滤（所有工具始终可用）。
        未来可根据 intent 结果过滤传给 agent 的工具集。
        """
        dc = self.domain_config
        return dc.tools if dc else None

    @property
    def domain_instructions(self) -> str:
        dc = self.domain_config
        return dc.instructions if dc else ""


# ═══════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════

_INTENT_PROMPT = """你是一个消息意图分析器。分析用户消息，输出 JSON。

## 可用领域
{domains}

{examples}

## 对话历史
{history}

## 输出格式
严格输出一个 JSON 对象（不要 markdown 包裹），包含以下字段：
- domain: 领域标识（必须是上面列出的之一）
- intent: 具体意图（简短描述，如 stock_analysis、code_modify、greeting）
- params: 提取的关键参数对象（可能包含 stock、stock_name、target、task、aspects、timeframe 等）
- confidence: 置信度 0-1

## 规则
1. 股票名称必须转为代码（如 贵州茅台→600519），不确定就留空
2. aspects 根据用户意图推断（如"最近怎么样"→["行情","技术面","资金流"]）
3. 涉及代码/编程/修改/重构/调试的归 coding
4. 无法判断时归 chat，confidence 给低值
5. 根据对话历史解析代词和引用（如"它"="贵州茅台"、"上一只"=之前提到的股票）
6. 只输出 JSON，不要任何其他文字

## 用户消息
{message}"""


# ═══════════════════════════════════════════════════════════════
# 分析器
# ═══════════════════════════════════════════════════════════════

def analyze_intent(
    message: str,
    model: str = None,
    provider: str = None,
    history: List[Dict[str, str]] = None,
) -> IntentResult:
    """分析用户消息的意图。

    Args:
        message: 当前用户消息
        model: 模型名（可选）
        provider: provider 名（可选）
        history: 最近对话历史 [{"role": "user/assistant", "content": "..."}, ...]

    失败时返回默认的 chat 领域（降级不阻塞）。
    """
    if not message or not message.strip():
        return IntentResult(domain="chat", intent="empty", confidence=1.0)

    # 格式化对话历史
    history_text = "（无）"
    if history:
        lines = []
        for msg in history[-6:]:  # 最近 3 轮
            role = "用户" if msg.get("role") == "user" else "助手"
            content = (msg.get("content") or "")[:200]
            lines.append(f"{role}: {content}")
        if lines:
            history_text = "\n".join(lines)

    # 构建 prompt
    domains_text = build_intent_prompt_domains()
    examples_text = build_intent_prompt_examples()
    prompt = _INTENT_PROMPT.format(
        domains=domains_text,
        examples=examples_text,
        history=history_text,
        message=message.strip(),
    )

    # 直接调 LLM API（不经过 smolagents）
    try:
        from app.services.llm import LLMService
        svc = LLMService(provider=provider)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        model_id = model or svc.get_default_model()

        import requests
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 清理可能的 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            # 去掉首尾的 ``` 行
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        result = json.loads(raw)
        domain = result.get("domain", "chat")
        # 校验领域是否存在
        if domain not in get_all_domains():
            logger.warning("[Intent] 未知领域 '%s'，降级到 chat", domain)
            domain = "chat"

        return IntentResult(
            domain=domain,
            intent=result.get("intent", ""),
            params=result.get("params", {}),
            confidence=float(result.get("confidence", 0.5)),
            raw_response=raw,
        )

    except json.JSONDecodeError as e:
        logger.warning("[Intent] JSON 解析失败: %s | raw: %s", e, raw[:200])
        return IntentResult(domain="chat", intent="parse_error", confidence=0.0, raw_response=raw)
    except Exception as e:
        logger.warning("[Intent] 分析失败，降级到默认: %s", e)
        return IntentResult(domain="chat", intent="error", confidence=0.0)


def format_intent_for_agent(intent: IntentResult, original_message: str) -> str:
    """将意图分析结果格式化为 agent 可用的上下文。

    返回空字符串表示不需要额外上下文（如简单闲聊）。
    """
    # 低置信度的 chat 不加额外上下文，直接走默认流程
    if intent.domain == "chat" and intent.confidence < 0.5:
        return ""

    # 高置信度的 chat（如打招呼）给一个简短提示
    if intent.domain == "chat":
        return f"[意图] {intent.intent}（直接回复即可，无需调用工具）"

    parts = [f"[意图分析] 领域={intent.domain}，意图={intent.intent}"]

    if intent.params:
        params_str = json.dumps(intent.params, ensure_ascii=False)
        parts.append(f"参数: {params_str}")

    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence})，请结合原始消息判断")

    return "\n".join(parts)
