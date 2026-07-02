# -*- coding: utf-8 -*-
"""
任务型 Agent — 基于 smolagents 的 ReAct 实现。

使用 smolagents CodeAgent 处理工具调用循环，
不依赖模型的 function calling 能力。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from agents.base import AgentBase, AgentResponse
from llm.base import ChatMessage, LLMBase
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _build_smolagent_tools(tool_registry: ToolRegistry, skill_adapter=None) -> List:
    """将 ToolRegistry 工具 + Skill 转换为 smolagents Tool 对象。"""
    from smolagents import tool as smolagents_tool

    tools = []

    # 注册普通工具
    for name in tool_registry.list_tools():
        tool_obj = tool_registry.get(name)
        if not tool_obj:
            continue
        fn = getattr(tool_obj, "_original_func", None)
        if fn is None:
            continue
        try:
            tools.append(smolagents_tool(fn))
        except Exception as e:
            logger.debug("[TaskAgent] 包装 %s 失败: %s", name, e)

    # 注册 Skill 工具
    if skill_adapter:
        _adapter = skill_adapter

        @smolagents_tool
        def get_skill_catalog() -> str:
            """获取可用技能列表。用户提到选股、筛选等需求时先调用此工具。"""
            catalog = _adapter.get_catalog_text()
            return catalog if catalog else "当前无可用技能。"

        @smolagents_tool
        def read_skill(skill_name: str) -> str:
            """加载指定技能的详细执行指令。

            Args:
                skill_name: 技能名称，如 market-screener
            """
            body = _adapter.get_body(skill_name)
            if body:
                return body
            available = ", ".join(s.name for s in _adapter.list_skills())
            return f"技能 '{skill_name}' 不存在。可用技能: {available}"

        tools.extend([get_skill_catalog, read_skill])

    return tools


class _SmolagentsLLMWrapper:
    """把 Agent Template 的 LLMBase 包装为 smolagents Model 接口。"""

    def __init__(self, llm: LLMBase):
        self._llm = llm

    def _call_llm(self, messages, **kwargs):
        """统一调用逻辑，返回带 token_usage 的响应。"""
        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role, content = getattr(m, "role", "user"), getattr(m, "content", "")
            chat_messages.append(ChatMessage(role=role, content=content))

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    resp = pool.submit(asyncio.run, self._llm.generate(chat_messages)).result()
            else:
                resp = asyncio.run(self._llm.generate(chat_messages))
        except Exception as e:
            logger.error("[TaskAgent] LLM 调用失败: %s", e)
            raise

        # 包装为 smolagents 期望的格式（token_usage 必须是对象属性，不能是 dict）
        class _TokenUsage:
            def __init__(self, r):
                self.input_tokens = r.prompt_tokens if r is not None else 0
                self.output_tokens = r.completion_tokens if r is not None else 0
                self.total_tokens = r.tokens_used if r is not None else 0

        class _Result:
            def __init__(self, r):
                self.content = r.content if r is not None else ""
                self.token_usage = _TokenUsage(r)

        return _Result(resp)

    def generate(self, messages, **kwargs):
        """smolagents 调用入口。"""
        return self._call_llm(messages, **kwargs)

    def __call__(self, messages, **kwargs):
        return self._call_llm(messages, **kwargs)


class TaskAgent(AgentBase):
    """任务型 Agent — smolagents ReAct 引擎。"""

    def __init__(
        self,
        max_tool_rounds: int = 10,
        skill_adapter=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.max_tool_rounds = max_tool_rounds
        self.skill_adapter = skill_adapter

    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        from smolagents import CodeAgent as SmolCodeAgent

        start_time = time.time()

        smol_tools = _build_smolagent_tools(self.tool_registry, self.skill_adapter) if self.tool_registry else []
        model = _SmolagentsLLMWrapper(self.llm)

        agent = SmolCodeAgent(
            tools=smol_tools,
            model=model,
            max_steps=self.max_tool_rounds,
        )

        logger.info("[TaskAgent] 执行: %s (工具: %d)", user_input[:60], len(smol_tools))
        try:
            result = agent.run(user_input)
        except Exception as e:
            logger.error("[TaskAgent] 执行失败: %s", e)
            return AgentResponse(
                content=f"执行异常: {e}",
                session_id=session_id,
                elapsed_seconds=round(time.time() - start_time, 2),
            )

        return AgentResponse(
            content=str(result),
            session_id=session_id,
            elapsed_seconds=round(time.time() - start_time, 2),
        )
