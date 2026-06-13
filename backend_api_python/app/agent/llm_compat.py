# -*- coding: utf-8 -*-
"""
LLM 响应适配器 — 在 QuantDinger 层处理 LLM 响应格式兼容性。

解决问题：Llama 等本地模型不支持 OpenAI tool_calls 字段，
将 tool_call 以 JSON 文本形式放在 content 中返回。
本模块包装 LLMProvider，在响应层自动解析 content 中的 tool_call。

用法（在 nanobot_agent.py 中）：
    from app.agent.llm_compat import wrap_provider
    agent._agent_loop.provider = wrap_provider(agent._agent_loop.provider)
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)

# 已知工具名（只解析这些，避免误匹配普通 JSON）
_KNOWN_TOOLS = frozenset({"call_skill", "final_answer", "search_stock_by_name"})


def _parse_tool_calls_from_content(content: str) -> list[ToolCallRequest]:
    """从 content 文本中解析 tool_call JSON。

    支持: ```json 块、裸 JSON、任意嵌套的 arguments。
    """
    if not content or not content.strip():
        return []

    candidates: list[str] = []

    # 1. ```json ... ``` 代码块
    for m in re.finditer(r'```(?:json)?\s*\n?(.*?)\n?\s*```', content, re.DOTALL):
        candidates.append(m.group(1).strip())

    # 2. 裸 JSON — 括号深度匹配
    for m in re.finditer(r'\{', content):
        depth = 0
        start = m.start()
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    candidates.append(content[start:i + 1])
                    break

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        name = data.get("name", "")
        args = data.get("arguments", {})
        if not name and "function" in data:
            fn = data["function"]
            if isinstance(fn, dict):
                name = fn.get("name", "")
                args = fn.get("arguments", {})

        if name in _KNOWN_TOOLS and isinstance(args, dict):
            return [ToolCallRequest(
                id=str(uuid.uuid4())[:8],
                name=str(name),
                arguments=args,
            )]

    return []


def _patch_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 role:tool 消息转为 role:user（Llama 不认 tool 角色）。

    同时移除 assistant 消息中的 tool_calls 字段（Llama 不认）。
    """
    patched = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            # tool → user，包装成自然语言
            content = msg.get("content", "")
            name = msg.get("name", "tool")
            patched.append({
                "role": "user",
                "content": f"[工具 {name} 的返回结果]\n{content}",
            })
        elif role == "assistant" and msg.get("tool_calls"):
            # 移除 tool_calls 字段，只保留 content
            new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            if not new_msg.get("content"):
                new_msg["content"] = "(调用工具中...)"
            patched.append(new_msg)
        else:
            patched.append(msg)
    return patched
    """如果响应没有 tool_calls 但 content 中有，解析并修补。"""
    if response.has_tool_calls:
        return response
    if not response.content:
        return response

    parsed = _parse_tool_calls_from_content(response.content)
    if not parsed:
        return response

    logger.info("[LLMCompat] 从 content 中解析到 tool_call: %s", [tc.name for tc in parsed])

    # 清掉 content 中的 JSON，保留非 JSON 部分
    cleaned = response.content
    for tc in parsed:
        # 尝试从 content 中移除已解析的 JSON 块
        tc_json = json.dumps({"name": tc.name, "arguments": tc.arguments}, ensure_ascii=False)
        cleaned = cleaned.replace(tc_json, "").strip()
        # 也尝试移除 ```json 包裹的版本
        tc_json_block = f"```json\n{tc_json}\n```"
        cleaned = cleaned.replace(tc_json_block, "").strip()

    # 构造新的 LLMResponse（frozen dataclass，需要重新构造）
    return LLMResponse(
        content=cleaned or None,
        tool_calls=parsed,
        finish_reason="tool_calls",
        usage=response.usage,
        reasoning_content=response.reasoning_content,
        thinking_blocks=response.thinking_blocks,
    )


class CompatProvider(LLMProvider):
    """包装现有 LLMProvider，在响应层自动解析 content 中的 tool_call。

    对原始 provider 透明代理，只在 has_tool_calls=False 时介入。
    """

    def __init__(self, inner: LLMProvider):
        self._inner = inner
        # 代理所有属性
        self._generation = inner.generation
        self.supports_progress_deltas = getattr(inner, 'supports_progress_deltas', False)

    @property
    def generation(self):
        return self._inner.generation

    def get_default_model(self) -> str:
        return self._inner.get_default_model()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        messages = _patch_messages(messages)
        response = await self._inner.chat(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        return _patch_response(response)

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        if "messages" in kwargs:
            kwargs["messages"] = _patch_messages(kwargs["messages"])
        response = await self._inner.chat_with_retry(**kwargs)
        return _patch_response(response)

    async def chat_stream_with_retry(self, **kwargs) -> LLMResponse:
        if "messages" in kwargs:
            kwargs["messages"] = _patch_messages(kwargs["messages"])
        response = await self._inner.chat_stream_with_retry(**kwargs)
        return _patch_response(response)

    # 代理其他所有方法（如果框架调用了未覆盖的方法）
    def __getattr__(self, name):
        return getattr(self._inner, name)


def wrap_provider(provider: LLMProvider) -> LLMProvider:
    """包装 provider，自动处理 Llama 等模型的 content tool_call。

    如果已经是 CompatProvider，不重复包装。
    """
    if isinstance(provider, CompatProvider):
        return provider
    return CompatProvider(provider)
