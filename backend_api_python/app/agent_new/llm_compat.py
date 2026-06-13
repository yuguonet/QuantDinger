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

from app.nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)

# 已知工具名（只解析这些，避免误匹配普通 JSON）
_KNOWN_TOOLS = frozenset({"call_skill", "final_answer", "search_stock_by_name"})

# 本地模型常见工具名变体（模糊匹配用）
_KNOWN_TOOL_ALIASES = {
    "call_skill": "call_skill",
    "call_skill_tool": "call_skill",
    "CallSkill": "call_skill",
    "final_answer": "final_answer",
    "FinalAnswer": "final_answer",
    "search_stock_by_name": "search_stock_by_name",
}


def _repair_json(s: str) -> str | None:
    """尝试修复本地模型常见的 JSON 格式错误。

    修复项:
    - 尾部逗号: {"a": 1, "b": 2,} → {"a": 1, "b": 2}
    - 单引号: {'name': 'x'} → {"name": "x"}
    - 未转义的换行符
    - 截断的 JSON（补右括号）
    """
    s = s.strip()
    if not s:
        return None

    # 1. 尾部逗号
    s = re.sub(r',\s*([}\]])', r'\1', s)

    # 2. 单引号 → 双引号（简单替换，不处理嵌套）
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')

    # 3. 未转义的换行符（在字符串值内部）
    s = s.replace('\n', '\\n').replace('\r', '\\r')

    # 4. 尝试直接解析
    try:
        json.loads(s)
        return s
    except (json.JSONDecodeError, TypeError):
        pass

    # 5. 截断修复：补缺失的右括号
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    if open_braces > 0 or open_brackets > 0:
        repaired = s + ']' * open_brackets + '}' * open_braces
        try:
            json.loads(repaired)
            return repaired
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _parse_tool_calls_from_content(content: str) -> list[ToolCallRequest]:
    """从 content 文本中解析 tool_call JSON。

    支持: ```json 块、裸 JSON、任意嵌套的 arguments。
    增强: 针对本地模型的 JSON 格式容错（尾部逗号、单引号、截断修复）。
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
        else:
            # 括号未闭合（JSON 截断），也作为候选（后续 _repair_json 会补全）
            if depth > 0:
                candidates.append(content[start:])

    logger.debug("[LLMCompat] 找到 %d 个候选 JSON", len(candidates))

    for candidate in candidates:
        # 先直接解析
        data = None
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            # 尝试修复后解析
            repaired = _repair_json(candidate)
            if repaired:
                try:
                    data = json.loads(repaired)
                    logger.info("[LLMCompat] 🔧 JSON 修复成功: %s...", repaired[:80])
                except (json.JSONDecodeError, TypeError):
                    continue
        if data is None:
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

        # 模糊匹配工具名（本地模型经常拼错大小写或加后缀）
        canonical_name = _KNOWN_TOOL_ALIASES.get(name, name)

        logger.debug("[LLMCompat] 候选: name=%s(canonical=%s), args=%s",
                     name, canonical_name, bool(args))
        if canonical_name in _KNOWN_TOOLS and isinstance(args, dict):
            logger.info("[LLMCompat] ✅ 匹配到已知工具: %s (原始: %s)", canonical_name, name)
            return [ToolCallRequest(
                id=str(uuid.uuid4())[:8],
                name=str(canonical_name),
                arguments=args,
            )]

    return []


def _patch_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 role:tool 消息转为 role:user（Llama 不认 tool 角色）。

    同时处理 assistant 消息中的 tool_calls：
    - 如果 content 只有 tool_call JSON → 替换为自然语言描述（保留上下文）
    - 如果 content 有 tool_call JSON + 其他文本 → 只保留其他文本

    ⚠️ 关键修复：不再跳过含 tool_call 的 assistant 消息。
    旧逻辑直接跳过导致 LLM 丢失"我调了什么工具"的上下文，
    多步 tool_call 链路断裂（如 technical_agent → indicator_agent → intelligence_agent）。
    """
    patched = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            # tool → user，包装成自然语言（参考 test_agent_flow.py）
            content = msg.get("content", "")
            name = msg.get("name", "tool")
            patched.append({
                "role": "user",
                "content": (
                    f"[工具 {name} 的返回结果]\n{content}\n\n"
                    f"请基于以上工具结果继续分析。\n"
                    f"- 如果还需要调用其他工具，请输出工具调用 JSON\n"
                    f"- 如果所有分析已完成，请直接输出包含 stock_code/action/score/direction/confidence/reasons/risks/skill_reports 的 JSON 结论"
                ),
            })
        elif role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content", "")
            # 提取 tool_call 信息用于上下文保留
            tool_call_descriptions = []
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                tc_name = fn.get("name", tc.get("name", "unknown"))
                tc_args = fn.get("arguments", tc.get("arguments", ""))
                if isinstance(tc_args, dict):
                    tc_args = json.dumps(tc_args, ensure_ascii=False)[:200]
                tool_call_descriptions.append(f"调用工具: {tc_name}({tc_args})")
            tool_summary = "; ".join(tool_call_descriptions)

            # 检查 content 是否只有 tool_call JSON（没有其他有用文本）
            non_json = _strip_tool_call_json(content)
            if not non_json or non_json.strip() in ("", "(调用工具中...)"):
                # content 只有 tool_call JSON → 替换为自然语言描述（保留上下文）
                patched.append({
                    "role": "assistant",
                    "content": f"[已执行工具调用] {tool_summary}",
                })
            else:
                # content 有 tool_call JSON + 其他文本，只保留其他文本
                patched.append({
                    "role": "assistant",
                    "content": non_json.strip(),
                })
        else:
            patched.append(msg)
    return patched


def _strip_tool_call_json(content: str) -> str:
    """从 assistant content 中移除 tool_call JSON，保留非 JSON 文本。"""
    if not content:
        return content

    cleaned = content

    # 移除 ```json ... ``` 代码块中的 tool_call
    for m in re.finditer(r'```(?:json)?\s*\n?\{[^`]*"name"\s*:[^`]*\}\s*\n?```', cleaned, re.DOTALL):
        cleaned = cleaned.replace(m.group(0), "").strip()

    # 移除裸 JSON tool_call（用括号深度匹配）
    for m in re.finditer(r'\{', cleaned):
        depth = 0
        start = m.start()
        for i in range(start, len(cleaned)):
            if cleaned[i] == '{':
                depth += 1
            elif cleaned[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, dict) and data.get("name") in _KNOWN_TOOLS:
                            cleaned = cleaned.replace(candidate, "").strip()
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break

    # 关键修复：清理完后返回 cleaned（可能为空），不再 fallback 回原文
    # 空字符串表示"content 只有 tool_call JSON，没有其他文本"
    return cleaned.strip()


def _patch_response(response):
    """统一修补 LLM 响应，处理本地模型的 tool_call 兼容问题。

    两种情况：
    1. has_tool_calls=False → 从 content 中解析 tool_call JSON（本地模型不支持原生 tool_calls）
    2. has_tool_calls=True 但 should_execute_tools=False → finish_reason 非标准，
       修正为 "tool_calls" 使 runner 能正常执行（本地模型常见 "length"/"eos_token" 等）
    """
    # ── 情况 2：有 tool_calls 但 finish_reason 不对 → 修正 finish_reason ──
    if response.has_tool_calls and not response.should_execute_tools:
        logger.warning(
            "[LLMCompat] tool_calls 存在但 finish_reason='%s' 不标准，修正为 'tool_calls'",
            response.finish_reason,
        )
        return LLMResponse(
            content=response.content,
            tool_calls=response.tool_calls,
            finish_reason="tool_calls",
            usage=response.usage,
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
            error_status_code=response.error_status_code,
            error_kind=response.error_kind,
            error_type=response.error_type,
            error_code=response.error_code,
            error_retry_after_s=response.error_retry_after_s,
            error_should_retry=response.error_should_retry,
        )

    # ── 情况 1a：已有标准 tool_calls → 不需要解析 ──
    if response.has_tool_calls:
        logger.debug("[LLMCompat] 响应已有标准 tool_calls，跳过解析")
        return response

    # ── 情况 1b：没有 tool_calls → 尝试从 content 中解析 ──
    if not response.content:
        logger.debug("[LLMCompat] 响应 content 为空，跳过解析")
        return response

    logger.info("[LLMCompat] 检查 content 中的 tool_call (长度=%d): %s",
                len(response.content), response.content[:300])
    parsed = _parse_tool_calls_from_content(response.content)
    if not parsed:
        logger.info("[LLMCompat] content 中未解析到 tool_call")
        return response

    logger.info("[LLMCompat] ✅ 从 content 中解析到 tool_call: %s", [tc.name for tc in parsed])

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


def patch_runner_response(runner) -> None:
    """Monkey-patch runner._request_model，对每次 LLM 响应应用 _patch_response。

    比 wrap_provider 更可靠：不受 _refresh_provider_snapshot 覆盖影响。
    直接在 runner 的响应出口处兜底。
    """
    import functools
    from nanobot.providers.base import LLMResponse

    original_request_model = runner._request_model

    @functools.wraps(original_request_model)
    async def patched_request_model(spec, messages_for_model, hook, context):
        response = await original_request_model(spec, messages_for_model, hook, context)
        patched = _patch_response(response)
        if patched is not response:
            logger.info("[LLMCompat] ⚡ Runner 响应已修补: has_tool_calls=%s finish_reason=%s tool_calls=%s",
                        patched.has_tool_calls, patched.finish_reason,
                        [tc.name for tc in patched.tool_calls])
        return patched

    runner._request_model = patched_request_model
