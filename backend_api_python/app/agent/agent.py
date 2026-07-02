# -*- coding: utf-8 -*-
"""
统一 Agent 入口

1. Planner: LLM 根据用户消息选择需要的工具
2. Executor: 只把选中的工具传给 TaskAgent (smolagents)

smolagents 是执行子模块，不负责工具选择。
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from llm import create_llm, QDSkillAdapter
from llm.base import ChatMessage
from tools.registry import ToolRegistry
from agents.chat_agent import ChatAgent
from agents.task_agent import TaskAgent
from agents.base import AgentResponse

logger = logging.getLogger(__name__)


async def _select_tools(llm, message: str, tool_names: List[str], skill_names: List[str]) -> dict:
    """Planner: 让 LLM 选择需要的工具和技能。"""
    names_str = ", ".join(tool_names)
    skills_str = ", ".join(skill_names) if skill_names else "无"

    prompt = (
        f"你是工具选择器。根据用户消息，选择需要的工具和技能。\n\n"
        f"可用工具: {names_str}\n"
        f"可用技能: {skills_str}\n\n"
        f"用户消息: {message}\n\n"
        f"只输出 JSON，不要其他内容:\n"
        f'{{"tools": ["工具名1", "工具名2"], "skills": ["技能名"]}}\n'
        f"如果不需要工具，tools 为空数组。最多选 5 个工具。"
    )

    resp = await llm.generate([ChatMessage(role="user", content=prompt)])

    try:
        # 从响应中提取 JSON
        text = resp.content.strip()
        if "```" in text:
            import re
            m = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL)
            if m:
                text = m.group(1).strip()
        result = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        result = {"tools": [], "skills": []}

    return result


class QDAgent:
    """统一 Agent — Planner 选工具 → TaskAgent 执行。"""

    def __init__(
        self,
        system_prompt: str = "你是 QuantDinger 量化分析 AI 助手。用中文回答。",
        max_tool_rounds: int = 10,
    ):
        self._llm = create_llm()
        self._skills = QDSkillAdapter()
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds

        # 全量注册
        self._registry = ToolRegistry()
        self._registry.discover()

        self._tool_count = len(self._registry)
        self._skill_count = len(self._skills)

        self._mode = "task" if self._tool_count > 0 else "chat"
        logger.info("[QDAgent] %s 模式: %d 工具, %d 技能", self._mode, self._tool_count, self._skill_count)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def tool_count(self) -> int:
        return self._tool_count

    @property
    def skill_count(self) -> int:
        return self._skill_count

    async def chat(self, message: str, session_id: str = "default") -> AgentResponse:
        if self._mode == "chat":
            agent = ChatAgent(llm=self._llm, system_prompt=self._system_prompt)
            return await agent.chat(user_input=message, session_id=session_id, use_rag=False)

        # ── Step 1: Planner 选工具 ──
        all_tool_names = self._registry.list_tools()
        all_skill_names = [s["name"] for s in self._skills.list_skills()]

        selection = await _select_tools(self._llm, message, all_tool_names, all_skill_names)
        selected_tools = selection.get("tools", [])
        selected_skills = selection.get("skills", [])

        logger.info("[QDAgent] Planner 选了 %d 工具: %s, %d 技能: %s",
                     len(selected_tools), selected_tools, len(selected_skills), selected_skills)

        # ── Step 2: 构建过滤后的 ToolRegistry ──
        filtered = ToolRegistry()
        for name in selected_tools:
            tool = self._registry.get(name)
            if tool:
                filtered.add(tool)

        # ── Step 3: TaskAgent 执行（只看到选中的工具）──
        agent = TaskAgent(
            llm=self._llm,
            tool_registry=filtered,
            system_prompt=self._system_prompt,
            max_tool_rounds=self._max_tool_rounds,
            skill_adapter=self._skills if selected_skills else None,
        )

        return await agent.chat(
            user_input=message,
            session_id=session_id,
            use_rag=False,
        )
